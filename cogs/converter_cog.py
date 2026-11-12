"""
Converter cog — Components V2 / forwarded messages  →  classic embeds.

Requires discord.py 2.6+ (native Components V2 + forwarded-message snapshots).
No raw gateway parsing and no in-memory caches: everything that must survive
between events lives in MongoDB.

Collections used
  converter_channels  {guild_id, channel_ids: [..]}        channels with auto-convert ON
  converter_links     {_id: original_msg_id, ...}          original → converted copy (TTL)

Bot permissions needed: Manage Webhooks, Send Messages, Embed Links,
Read Message History (Manage Messages is NOT needed).
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import re
from typing import Optional, Union
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord import components as dc
from discord.ext import commands

import db

try:                                    # emoji_map.py = your index of emojis to swap
    from emoji_map import replace_emojis
except ImportError:                     # file missing → converter still works, no swapping
    def replace_emojis(text: str) -> str:
        return text

# ── Settings ────────────────────────────────────────────────────────────────

WEBHOOK_NAME = "V2 Converter Pro"
LINK_TTL_DAYS = 14          # how long an edit/delete link to a converted copy is kept
MIRROR_DELETES = True       # delete the converted copy when the original is deleted
DESC_LIMIT = 4000           # embed description cap (hard limit is 4096)
MAX_EMBEDS = 10             # Discord hard limit per message

NO_MENTIONS = discord.AllowedMentions.none()
V2_FOOTER = "Components V2 → Embed"

ConvertibleChannel = Union[discord.TextChannel, discord.VoiceChannel, discord.ForumChannel]


# ── Parsing ─────────────────────────────────────────────────────────────────

def _media_key(url: str) -> str:
    """CDN urls differ only by their ?ex=&is=&hm= query — compare by path."""
    return urlsplit(url).path


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp")


def _is_image_attachment(att) -> bool:
    """Image by content-type, falling back to the file extension (snapshots can lack a type)."""
    if (att.content_type or "").lower().startswith("image/"):
        return True
    return urlsplit(att.url).path.lower().endswith(_IMAGE_EXTS)


class _Parsed:
    __slots__ = ("text", "buttons", "selects", "images", "thumbnail", "color", "seen")

    def __init__(self):
        self.text: list[str] = []
        self.buttons: list[tuple[str, Optional[str]]] = []
        self.selects: list[tuple[str, list[str]]] = []
        self.images: list[tuple[str, str]] = []
        self.thumbnail: Optional[tuple[str, str]] = None
        self.color: Optional[int] = None
        self.seen: set[str] = set()       # media already represented (for de-duplication)

    def add_image(self, url: str, desc: str = ""):
        if not url or _media_key(url) in self.seen:      # skip duplicates of the same media
            return
        self.images.append((url, desc or ""))
        self.seen.add(_media_key(url))


def _walk(components, out: _Parsed):
    for comp in components:
        if isinstance(comp, dc.Container):
            if out.color is None and comp.accent_colour is not None:
                out.color = comp.accent_colour.value
            _walk(comp.children, out)

        elif isinstance(comp, dc.ActionRow):
            _walk(comp.children, out)

        elif isinstance(comp, dc.SectionComponent):
            _walk(comp.children, out)
            if comp.accessory is not None:
                _walk([comp.accessory], out)

        elif isinstance(comp, dc.TextDisplay):
            text = (comp.content or "").strip()
            if text:
                out.text.append(text)

        elif isinstance(comp, dc.ThumbnailComponent):
            url = comp.media.url if comp.media else ""
            if url:
                if out.thumbnail is None:
                    out.thumbnail = (url, comp.description or "")
                    out.seen.add(_media_key(url))
                else:
                    out.add_image(url, comp.description or "")

        elif isinstance(comp, dc.MediaGalleryComponent):
            for item in comp.items:
                out.add_image(item.media.url if item.media else "", item.description or "")

        elif isinstance(comp, dc.FileComponent):
            url = comp.media.url if comp.media else ""
            if url:
                out.text.append(f"📎 [{comp.name or 'file'}]({url})")
                out.seen.add(_media_key(url))

        elif isinstance(comp, dc.Button):
            label = f"{comp.emoji} {comp.label or ''}".strip() if comp.emoji else (comp.label or "")
            if label:
                out.buttons.append((label, comp.url))

        elif isinstance(comp, dc.SelectMenu):
            options = [o.label for o in comp.options]
            out.selects.append((comp.placeholder or "Select an option", options))

        # SeparatorComponent and anything unknown: nothing to show


# ── Embed building ──────────────────────────────────────────────────────────

def is_forward(message: discord.Message) -> bool:
    ref = message.reference
    return bool(
        ref is not None
        and ref.type is discord.MessageReferenceType.forward
        and message.message_snapshots
    )


def is_convertible_v2(message: discord.Message) -> bool:
    """True for a V2 message, or a forward whose original was a V2 message."""
    if message.flags.components_v2:
        return True
    return is_forward(message) and message.message_snapshots[0].flags.components_v2


def build_embeds(message: discord.Message, *, author_in_embed: bool = False) -> list[discord.Embed]:
    """Convert a message (or the snapshot inside a forward) into 0‑10 embeds."""
    forwarded = is_forward(message)
    src = message.message_snapshots[0] if forwarded else message

    data = _Parsed()
    _walk(src.components, data)

    if src.content and src.content.strip():
        data.text.insert(0, src.content.strip())

    # classic embeds on the source (matters for manual !c on normal messages)
    for e in src.embeds:
        if e.type in ("image", "gifv"):
            # preview of a direct image / GIF link: the media itself is the content
            media = e.image or e.thumbnail
            if media and media.url:
                data.add_image(media.url)
            continue
        if e.title:
            data.text.append(f"**{e.title}**")
        if e.description:
            data.text.append(e.description)
        for f in e.fields:
            data.text.append(f"**{f.name}**\n{f.value}")
        if e.image and e.image.url:
            data.add_image(e.image.url)
        if e.thumbnail and e.thumbnail.url and data.thumbnail is None:
            data.thumbnail = (e.thumbnail.url, "")
        if e.color is not None and data.color is None:
            data.color = e.color.value

    # attachments — skip the ones already shown through a gallery / file component
    for att in src.attachments:
        if _media_key(att.url) in data.seen:
            continue
        if _is_image_attachment(att):
            data.add_image(att.url, att.filename)
        else:
            data.text.append(f"📎 [{att.filename}]({att.url})")

    for sticker in getattr(src, "stickers", []):
        data.text.append(f"🏷️ Sticker: **{sticker.name}**")

    parts = list(data.text)
    if data.buttons:
        parts.append("**Buttons:** " + " · ".join(
            f"[{label}]({url})" if url else f"`{label}`" for label, url in data.buttons
        ))
    for placeholder, options in data.selects:
        parts.append(f"**{placeholder}:** " + (", ".join(f"`{o}`" for o in options) or "*no options*"))

    hidden = len(data.images) - MAX_EMBEDS
    if hidden > 0:
        parts.append(f"-# +{hidden} more image(s) not shown (Discord allows {MAX_EMBEDS} embeds per message)")

    if not parts and not data.images and data.thumbnail is None:
        return []

    description = "\n\n".join(parts) if parts else "​"   # zero-width space: embed with image only
    description = replace_emojis(description)        # swap emojis using emoji_map.py
    if len(description) > DESC_LIMIT:
        description = description[:DESC_LIMIT - 1] + "…"

    color = discord.Color(data.color) if data.color is not None else discord.Color.blurple()
    main = discord.Embed(description=description, color=color, timestamp=src.created_at)

    channel_name = getattr(message.channel, "name", None)
    bits = []
    if forwarded:
        bits.append("↪️ Forwarded")
    if channel_name:
        bits.append(f"#{channel_name}")
    bits.append(V2_FOOTER if src.flags.components_v2 else "Message → Embed")
    if message.edited_at:
        bits.append("edited")
    main.set_footer(text="  •  ".join(bits))

    if author_in_embed:
        main.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    if data.thumbnail:
        main.set_thumbnail(url=data.thumbnail[0])
    if data.images:
        main.set_image(url=data.images[0][0])

    embeds = [main]
    for url, desc in data.images[1:MAX_EMBEDS]:
        extra = discord.Embed(color=color)
        extra.set_image(url=url)
        if desc:
            extra.description = f"*{desc[:100]}*"
        embeds.append(extra)
    return embeds


def _intro(message: discord.Message) -> str:
    if is_forward(message):
        return f"↪️ **Converted forward** posted by {message.author.mention} — [Jump to forward]({message.jump_url})"
    return f"📬 **Converted** from {message.author.mention} — [Jump to original]({message.jump_url})"


def render(message: discord.Message, style: str) -> tuple[Optional[str], list[discord.Embed]]:
    """
    style:
      "webhook" – auto copy posted through the webhook (looks like the author)
      "plain"   – auto copy posted by the bot (fallback when it can't use webhooks)
      "manual"  – reply from !c / context menu, with an intro line
    """
    if style == "manual":
        return _intro(message), build_embeds(message)
    return None, build_embeds(message, author_in_embed=(style == "plain"))


def _safe_username(name: str) -> str:
    # webhook names may not contain "clyde"/"discord" and must be 1-80 chars
    name = re.sub(r"(?i)(clyde|discord)", lambda m: m.group(0)[0] + "\u200b" + m.group(0)[1:], name)
    return name[:80] or "Unknown"


# ── The cog ─────────────────────────────────────────────────────────────────

class ConverterCog(commands.Cog, name="Converter"):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # in-flight guard only: serialises create/edit of the SAME message so a quick
        # send→edit can't produce two copies. Entries are removed as soon as they finish.
        self._inflight: dict[int, list] = {}
        self.ctx_menu = app_commands.ContextMenu(name="Convert to Embed", callback=self.ctx_convert)

    async def cog_load(self):
        self.bot.tree.add_command(self.ctx_menu)
        try:
            d = db.get_db()
            await d.converter_links.create_index(
                "created_at", expireAfterSeconds=LINK_TTL_DAYS * 86400
            )
            await d.converter_channels.create_index("guild_id")
        except Exception as e:                       # index problems must never block loading
            print(f"[converter] index setup failed: {type(e).__name__}: {e}")

    async def cog_unload(self):
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    # ── tiny helpers ────────────────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def _guard(self, message_id: int):
        entry = self._inflight.get(message_id)
        if entry is None:
            entry = self._inflight[message_id] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._inflight.pop(message_id, None)

    async def _resolve_channel(self, channel_id: int):
        ch = self.bot.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return ch

    # ── DB: auto-convert channel list ───────────────────────────────────────

    async def _auto_enabled(self, channel) -> bool:
        """Is auto-convert on for this channel (or, for threads/forum posts, its parent)?"""
        guild = getattr(channel, "guild", None)
        if guild is None:
            return False
        ids = [channel.id]
        parent_id = getattr(channel, "parent_id", None)
        if parent_id:
            ids.append(parent_id)
        doc = await db.get_db().converter_channels.find_one(
            {"guild_id": guild.id, "channel_ids": {"$in": ids}}, {"_id": 1}
        )
        return doc is not None

    # ── DB: original → converted-copy links ─────────────────────────────────

    @staticmethod
    def _links():
        return db.get_db().converter_links

    async def _get_link(self, original_id: int) -> Optional[dict]:
        return await self._links().find_one({"_id": original_id})

    async def _save_link(self, original: discord.Message, copy_id: int, copy_channel_id: int, style: str):
        await self._links().update_one(
            {"_id": original.id},
            {"$set": {
                "copy_id": copy_id,
                "copy_channel_id": copy_channel_id,
                "style": style,
                "created_at": datetime.datetime.now(datetime.timezone.utc),
            }},
            upsert=True,
        )

    async def _drop_link(self, original_id: int):
        await self._links().delete_one({"_id": original_id})

    # ── Webhook plumbing ────────────────────────────────────────────────────

    async def _webhook_for(self, channel, *, create: bool):
        """Return (webhook | None, thread | None) for the channel the message lives in."""
        thread = channel if isinstance(channel, discord.Thread) else None
        host = channel.parent if thread else channel
        if host is None:
            return None, thread
        for wh in await host.webhooks():
            if wh.name == WEBHOOK_NAME and wh.token:
                return wh, thread
        if not create:
            return None, thread
        return await host.create_webhook(name=WEBHOOK_NAME), thread

    async def _send_via_webhook(self, message: discord.Message, embeds: list[discord.Embed]) -> discord.WebhookMessage:
        wh, thread = await self._webhook_for(message.channel, create=True)
        kwargs = dict(
            embeds=embeds,
            username=_safe_username(message.author.display_name),
            avatar_url=message.author.display_avatar.url,
            allowed_mentions=NO_MENTIONS,
            wait=True,
        )
        if thread:
            kwargs["thread"] = thread
        try:
            return await wh.send(**kwargs)
        except discord.HTTPException as e:
            if e.status != 400:
                raise
            kwargs["username"] = "Converted Message"     # name was rejected — retry once
            return await wh.send(**kwargs)

    # ── Create / edit / delete the converted copy ───────────────────────────

    async def _publish(self, message: discord.Message, link: Optional[dict]):
        """Create the copy of `message`, or update it in place if a link already exists."""
        if link:
            content, embeds = render(message, link["style"])
            if not embeds:
                return
            await self._edit_copy(message, link, content, embeds)
            return

        embeds = build_embeds(message)
        if not embeds:
            return
        try:
            sent = await self._send_via_webhook(message, embeds)
            style = "webhook"
        except discord.Forbidden:                        # no Manage Webhooks → post as the bot
            _, embeds = render(message, "plain")
            sent = await message.channel.send(embeds=embeds, allowed_mentions=NO_MENTIONS)
            style = "plain"
        await self._save_link(message, sent.id, sent.channel.id, style)

    async def _edit_copy(self, message: discord.Message, link: dict, content, embeds):
        try:
            if link["style"] == "webhook":
                wh, thread = await self._webhook_for(message.channel, create=False)
                if wh is None:
                    raise discord.NotFound(_FakeResponse(), "webhook gone")
                kwargs = dict(embeds=embeds, allowed_mentions=NO_MENTIONS)
                if thread:
                    kwargs["thread"] = thread
                await wh.edit_message(link["copy_id"], **kwargs)
            else:
                ch = self.bot.get_channel(link["copy_channel_id"]) \
                    or self.bot.get_partial_messageable(link["copy_channel_id"])
                await ch.get_partial_message(link["copy_id"]).edit(
                    content=content, embeds=embeds, allowed_mentions=NO_MENTIONS
                )
        except (discord.NotFound, discord.Forbidden):
            # The copy was deleted by someone — stop tracking it instead of re-posting.
            await self._drop_link(message.id)

    async def _delete_copy(self, link: dict, channel):
        try:
            if link["style"] == "webhook":
                wh, thread = await self._webhook_for(channel, create=False)
                if wh is None:
                    return
                kwargs = {"thread": thread} if thread else {}
                await wh.delete_message(link["copy_id"], **kwargs)
            else:
                ch = self.bot.get_channel(link["copy_channel_id"]) \
                    or self.bot.get_partial_messageable(link["copy_channel_id"])
                await ch.get_partial_message(link["copy_id"]).delete()
        except discord.HTTPException:
            pass

    # ── Auto-conversion listeners ───────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.id == self.bot.user.id:
            return
        if not is_convertible_v2(message):          # cheap flag check — no DB hit for normal chat
            return
        if not await self._auto_enabled(message.channel):
            return
        async with self._guard(message.id):
            await self._publish(message, await self._get_link(message.id))

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        async with self._guard(payload.message_id):
            link = await self._get_link(payload.message_id)
            channel = await self._resolve_channel(payload.channel_id)
            if channel is None:
                return

            if link is None:
                # Not converted yet. Typical case: a bot posts "thinking…" and later edits
                # it into a V2 message, so the first time we see V2 is in an edit.
                if not (payload.data.get("flags", 0) & (1 << 15)):
                    return
                if not await self._auto_enabled(channel):
                    return

            try:
                message = await channel.fetch_message(payload.message_id)
            except discord.HTTPException:
                return
            if link is None and message.author.id == self.bot.user.id:
                return
            await self._publish(message, link)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if not MIRROR_DELETES:
            return
        link = await self._get_link(payload.message_id)
        if link is None:
            return
        await self._drop_link(payload.message_id)
        channel = await self._resolve_channel(payload.channel_id)
        if channel is not None:
            await self._delete_copy(link, channel)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        await db.get_db().converter_channels.update_one(
            {"guild_id": channel.guild.id}, {"$pull": {"channel_ids": channel.id}}
        )

    # ── Manual conversion: reply + !c / !convert, or !c <message link/id> ───

    async def _target_from_reply(self, ctx: commands.Context) -> Optional[discord.Message]:
        ref = ctx.message.reference
        if ref is None:
            return None
        resolved = ref.resolved
        if isinstance(resolved, discord.Message):
            return resolved
        try:
            return await ctx.channel.fetch_message(ref.message_id)
        except discord.HTTPException:
            return None

    @commands.command(name="convert", aliases=["c"])
    async def prefix_convert(self, ctx: commands.Context, message: Optional[discord.Message] = None):
        """Reply to a Components V2 / forwarded message with this to convert it to an embed.
        You can also pass a message link or ID instead of replying."""
        target = message or await self._target_from_reply(ctx)
        if target is None:
            await ctx.reply("❌ **Reply** to the message you want to convert (or give a message link).",
                            mention_author=False)
            return

        async with self._guard(target.id):
            content, embeds = render(target, "manual")
            if not embeds:
                await ctx.reply("❌ Nothing to convert — that message has no text, images or files.",
                                mention_author=False)
                return
            try:
                sent = await ctx.reply(content=content, embeds=embeds,
                                       allowed_mentions=NO_MENTIONS, mention_author=False)
            except discord.HTTPException as e:
                await ctx.reply(f"❌ Couldn't send the embed:\n```\n{e}\n```", mention_author=False)
                return
            await self._save_link(target, sent.id, sent.channel.id, "manual")

    async def ctx_convert(self, interaction: discord.Interaction, message: discord.Message):
        """Right-click a message → Apps → Convert to Embed."""
        async with self._guard(message.id):
            content, embeds = render(message, "manual")
            if not embeds:
                await interaction.response.send_message(
                    "❌ Nothing to convert — that message has no text, images or files.", ephemeral=True
                )
                return
            await interaction.response.send_message(content=content, embeds=embeds,
                                                    allowed_mentions=NO_MENTIONS)
            sent = await interaction.original_response()
            await self._save_link(message, sent.id, sent.channel.id, "manual")

    # ── Auto-convert channel list:  a!convertch  (alias a!cch) ──────────────

    @commands.group(name="convertch", aliases=["cch"], invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def convertch(self, ctx: commands.Context):
        """Manage channels where V2 messages are converted automatically."""
        await ctx.reply(
            "**Auto-convert channels**\n"
            "`convertch list` — show the list\n"
            "`convertch add #chan [#chan …]` — turn auto-convert on\n"
            "`convertch remove #chan [#chan …]` — turn it off\n"
            "`convertch clear` — remove every channel\n"
            "-# Only listed channels are converted automatically (threads follow their parent channel). "
            "An empty list means auto-convert is off.",
            mention_author=False,
        )

    @convertch.command(name="list")
    async def cch_list(self, ctx: commands.Context):
        """Show the auto-convert channels."""
        doc = await db.get_db().converter_channels.find_one({"guild_id": ctx.guild.id})
        ids = doc.get("channel_ids", []) if doc else []
        if not ids:
            await ctx.reply("📋 No channels listed — auto-convert is **off** in this server.\n"
                            "-# Add one with `convertch add #channel`. `!c` still works everywhere.",
                            mention_author=False)
            return
        lines = []
        for cid in ids:
            ch = ctx.guild.get_channel_or_thread(cid)
            lines.append(ch.mention if ch else f"`{cid}` *(deleted)*")
        e = discord.Embed(title="📋 Auto-convert channels", description="\n".join(lines),
                          color=discord.Color.blurple())
        e.set_footer(text=f"{len(ids)} channel(s)")
        await ctx.reply(embed=e, mention_author=False)

    @convertch.command(name="add")
    async def cch_add(self, ctx: commands.Context, *channels: ConvertibleChannel):
        """Add channels.  Usage: convertch add #chan1 #chan2"""
        if not channels:
            await ctx.reply("❌ Mention at least one channel.", mention_author=False)
            return
        col = db.get_db().converter_channels
        doc = await col.find_one({"guild_id": ctx.guild.id})
        existing = set(doc.get("channel_ids", [])) if doc else set()
        new = [c for c in channels if c.id not in existing]
        old = [c for c in channels if c.id in existing]
        if new:
            await col.update_one(
                {"guild_id": ctx.guild.id},
                {"$addToSet": {"channel_ids": {"$each": [c.id for c in new]}}},
                upsert=True,
            )
        parts = []
        if new:
            parts.append("✅ Auto-convert **on** in: " + ", ".join(c.mention for c in new))
        if old:
            parts.append("ℹ️ Already in the list: " + ", ".join(c.mention for c in old))
        await ctx.reply("\n".join(parts), mention_author=False)

    @convertch.command(name="remove", aliases=["rm"])
    async def cch_remove(self, ctx: commands.Context, *channels: ConvertibleChannel):
        """Remove channels.  Usage: convertch remove #chan1 #chan2"""
        if not channels:
            await ctx.reply("❌ Mention at least one channel.", mention_author=False)
            return
        col = db.get_db().converter_channels
        doc = await col.find_one({"guild_id": ctx.guild.id})
        existing = set(doc.get("channel_ids", [])) if doc else set()
        gone = [c for c in channels if c.id in existing]
        missing = [c for c in channels if c.id not in existing]
        if gone:
            await col.update_one(
                {"guild_id": ctx.guild.id},
                {"$pull": {"channel_ids": {"$in": [c.id for c in gone]}}},
            )
        parts = []
        if gone:
            parts.append("✅ Removed: " + ", ".join(c.mention for c in gone))
        if missing:
            parts.append("ℹ️ Wasn't in the list: " + ", ".join(c.mention for c in missing))
        if gone and len(existing) == len(gone):
            parts.append("⚠️ List is now empty — auto-convert is **off**.")
        await ctx.reply("\n".join(parts), mention_author=False)

    @convertch.command(name="clear")
    async def cch_clear(self, ctx: commands.Context):
        """Remove every channel (auto-convert off)."""
        await db.get_db().converter_channels.update_one(
            {"guild_id": ctx.guild.id}, {"$set": {"channel_ids": []}}, upsert=True
        )
        await ctx.reply("🗑️ List cleared — auto-convert is now **off** in this server.", mention_author=False)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("❌ You need **Manage Server** permission to use this.", mention_author=False)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.reply("❌ This command only works in a server.", mention_author=False)
        elif isinstance(error, (commands.BadUnionArgument, commands.ChannelNotFound)):
            await ctx.reply("❌ I couldn't find that channel — mention a text/voice/forum channel.",
                            mention_author=False)


class _FakeResponse:
    """Minimal stand-in so we can raise discord.NotFound ourselves."""
    status = 404
    reason = "Not Found"


async def setup(bot: commands.Bot):
    await bot.add_cog(ConverterCog(bot))

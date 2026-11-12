"""
Emoji extractor cog — pull every CUSTOM emoji out of a message and save them to a server.

Usage:  reply to a message with  `extractemoji`  (or pass a message link / ID).
        Also: right-click a message -> Apps -> Extract Emojis (user-installable, works in servers the bot isn't in).
        Works on normal messages, classic embeds, Components V2 messages and forwards.
        The bot shows "Save all X emojis?" with Yes / No buttons.
        Yes -> a popup asks for the target server ID -> emojis are created there.
        If the server is full, you're asked whether to delete its OLDEST emojis to make room.
        Afterwards the bot posts paste-ready lines for emoji_map.py (old emoji ID -> new emoji).

Default (unicode) emojis are ignored — only <:name:id> / <a:name:id> emojis count.

Needs:  the bot must be in the target server with Manage Expressions,
        and you need Manage Expressions in that server (bot owners are exempt).
"""
from __future__ import annotations

import re
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord import components as dc
from discord.ext import commands

CUSTOM_EMOJI_RE = re.compile(r"<(a?):(\w+):(\d+)>")
PREVIEW_LIMIT = 60          # emojis shown in the confirmation embed
VIEW_TIMEOUT = 180


# ── Extraction ──────────────────────────────────────────────────────────────

def _add(found: dict[int, discord.PartialEmoji], emoji: Optional[discord.PartialEmoji]):
    if emoji is not None and emoji.id and emoji.id not in found:
        found[emoji.id] = discord.PartialEmoji(name=emoji.name, id=emoji.id, animated=emoji.animated)


def _scan_text(text: Optional[str], found: dict[int, discord.PartialEmoji]):
    if not text:
        return
    for animated, name, eid in CUSTOM_EMOJI_RE.findall(text):
        eid = int(eid)
        if eid not in found:
            found[eid] = discord.PartialEmoji(name=name, id=eid, animated=bool(animated))


def _iter_components(components):
    for comp in components or []:
        yield comp
        children = getattr(comp, "children", None)
        if children:
            yield from _iter_components(children)
        accessory = getattr(comp, "accessory", None)
        if accessory is not None:
            yield from _iter_components([accessory])


def collect_emojis(message: discord.Message) -> list[discord.PartialEmoji]:
    """All unique custom emojis in a message (and in the snapshot of a forward), in order."""
    found: dict[int, discord.PartialEmoji] = {}
    sources = [message, *(getattr(message, "message_snapshots", None) or [])]

    for src in sources:
        _scan_text(src.content, found)

        for e in src.embeds:
            _scan_text(e.title, found)
            _scan_text(e.description, found)
            if e.author:
                _scan_text(e.author.name, found)
            if e.footer:
                _scan_text(e.footer.text, found)
            for f in e.fields:
                _scan_text(f.name, found)
                _scan_text(f.value, found)

        for comp in _iter_components(src.components):
            if isinstance(comp, dc.TextDisplay):
                _scan_text(comp.content, found)
            elif isinstance(comp, dc.Button):
                _add(found, comp.emoji)
                _scan_text(comp.label, found)
            elif isinstance(comp, dc.SelectMenu):
                _scan_text(comp.placeholder, found)
                for opt in comp.options:
                    _add(found, opt.emoji)
                    _scan_text(opt.label, found)
                    _scan_text(opt.description, found)

    return list(found.values())


def _clean_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", name or "")[:32]
    return name if len(name) >= 2 else (name + "_emoji")[:32]


def _emoji_url(emoji: discord.PartialEmoji) -> str:
    ext = "gif" if emoji.animated else "png"
    return f"https://cdn.discordapp.com/emojis/{emoji.id}.{ext}?size=128"


def _join(lines: list[str], limit: int = 1000) -> str:
    out = ""
    for i, line in enumerate(lines):
        if len(out) + len(line) + 1 > limit - 20:
            return out + f"\n…and {len(lines) - i} more"
        out += ("\n" if out else "") + line
    return out


# ── UI ──────────────────────────────────────────────────────────────────────

class _GuildIdModal(discord.ui.Modal, title="Save emojis to a server"):
    guild_id = discord.ui.TextInput(
        label="Server ID",
        placeholder="e.g. 123456789012345678",
        min_length=15,
        max_length=20,
    )

    def __init__(self, view: "_ConfirmView"):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        await self.view.cog.save_emojis(interaction, self.view, self.guild_id.value.strip())


class _ConfirmView(discord.ui.View):
    def __init__(self, cog: "EmojiExtractorCog", author: discord.abc.User, emojis: list[discord.PartialEmoji],
                 ephemeral: bool = False):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.author = author
        self.emojis = emojis
        self.ephemeral = ephemeral
        self.message: Optional[discord.Message] = None
        self.busy = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author.id:
            return True
        await interaction.response.send_message("❌ Only the person who ran the command can use this.",
                                                ephemeral=True)
        return False

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success, emoji="✅")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.busy:
            await interaction.response.send_message("⏳ Already saving…", ephemeral=True)
            return
        await interaction.response.send_modal(_GuildIdModal(self))

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger, emoji="✖️")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="❌ Cancelled.", embed=None, view=None)

    async def on_timeout(self):
        if self.message is not None and not self.busy:
            try:
                await self.message.edit(content="⌛ Timed out.", embed=None, view=None)
            except discord.HTTPException:
                pass


class _FullServerView(discord.ui.View):
    """Shown when the target server lacks free slots: delete the oldest emojis, or save what fits."""

    def __init__(self, cog: "EmojiExtractorCog", author: discord.abc.User, guild: discord.Guild,
                 emojis: list[discord.PartialEmoji], victims: list[discord.Emoji], ephemeral: bool = False):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.author = author
        self.ephemeral = ephemeral
        self.guild = guild
        self.emojis = emojis
        self.victims = victims
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author.id:
            return True
        await interaction.response.send_message("❌ Only the person who ran the command can use this.",
                                                ephemeral=True)
        return False

    async def _go(self, interaction: discord.Interaction, victims: list[discord.Emoji]):
        self.stop()
        await interaction.response.defer(thinking=True, ephemeral=self.ephemeral)
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass
        await self.cog._run_save(interaction, self.guild, self.emojis, victims, ephemeral=self.ephemeral)

    @discord.ui.button(label="Delete & save", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_and_save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, self.victims)

    @discord.ui.button(label="Save only what fits", style=discord.ButtonStyle.secondary)
    async def only_fits(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, [])

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="❌ Cancelled.", embed=None, view=None)

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(content="⌛ Timed out.", embed=None, view=None)
            except discord.HTTPException:
                pass


# ── The cog ─────────────────────────────────────────────────────────────────

class EmojiExtractorCog(commands.Cog, name="Emoji Extractor"):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ctx_menu = app_commands.ContextMenu(
            name="Extract Emojis",
            callback=self.ctx_extract,
            allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
        )

    async def cog_load(self):
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    async def _target_from_reply(self, ctx: commands.Context) -> Optional[discord.Message]:
        ref = ctx.message.reference
        if ref is None:
            return None
        if isinstance(ref.resolved, discord.Message):
            return ref.resolved
        try:
            return await ctx.channel.fetch_message(ref.message_id)
        except discord.HTTPException:
            return None

    @staticmethod
    def _prompt_embed(emojis: list[discord.PartialEmoji]) -> discord.Embed:
        animated = sum(e.animated for e in emojis)
        shown = emojis[:PREVIEW_LIMIT]
        desc = " ".join(str(e) for e in shown)
        if len(emojis) > len(shown):
            desc += f"\n…and {len(emojis) - len(shown)} more"
        embed = discord.Embed(
            title=f"Save all {len(emojis)} emojis from this message?",
            description=desc,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(emojis) - animated} static • {animated} animated")
        return embed

    async def ctx_extract(self, interaction: discord.Interaction, message: discord.Message):
        """Right-click a message → Apps → Extract Emojis (works anywhere the app is installed to your account)."""
        emojis = collect_emojis(message)
        if not emojis:
            await interaction.response.send_message(
                "❌ No custom emojis found in that message (default emojis are ignored).", ephemeral=True)
            return
        view = _ConfirmView(self, interaction.user, emojis, ephemeral=True)
        await interaction.response.send_message(embed=self._prompt_embed(emojis), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @commands.command(name="extractemoji", aliases=["extractemojis"])
    async def extractemoji(self, ctx: commands.Context, message: Optional[discord.Message] = None):
        """Reply to a message (or pass a link/ID) to save its custom emojis to a server."""
        target = message or await self._target_from_reply(ctx)
        if target is None:
            await ctx.reply("❌ **Reply** to a message (or give a message link) to extract emojis from.",
                            mention_author=False)
            return

        emojis = collect_emojis(target)
        if not emojis:
            await ctx.reply("❌ No custom emojis found in that message (default emojis are ignored).",
                            mention_author=False)
            return

        view = _ConfirmView(self, ctx.author, emojis)
        embed = self._prompt_embed(emojis)
        view.message = await ctx.reply(embed=embed, view=view, mention_author=False)

    async def save_emojis(self, interaction: discord.Interaction, view: _ConfirmView, raw_id: str):
        """Called by the modal: validate the server, then create the emojis there."""
        if not raw_id.isdigit():
            await interaction.response.send_message("❌ That isn't a valid server ID.", ephemeral=True)
            return

        guild = self.bot.get_guild(int(raw_id))
        if guild is None:
            await interaction.response.send_message("❌ I'm not in that server.", ephemeral=True)
            return

        # the invoker must be allowed to add emojis there (bot owners bypass)
        if not await self.bot.is_owner(interaction.user):
            member = guild.get_member(interaction.user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(interaction.user.id)
                except discord.HTTPException:
                    member = None
            if member is None or not member.guild_permissions.manage_expressions:
                await interaction.response.send_message(
                    "❌ You need **Manage Expressions** in that server.", ephemeral=True)
                return

        if not guild.me.guild_permissions.manage_expressions:
            await interaction.response.send_message(
                "❌ I'm missing **Manage Expressions** in that server.", ephemeral=True)
            return

        view.busy = True
        view.stop()

        to_add = [e for e in view.emojis if e.id not in {g.id for g in guild.emojis}]
        victims = self._pick_victims(guild, to_add)

        if victims:
            # server is full → ask before deleting anything
            embed = discord.Embed(
                title=f"Not enough space in {guild.name}",
                description=(f"I need to free **{len(victims)}** slot(s). These are the **oldest** emojis "
                             f"and would be deleted (can't be undone):\n\n"
                             + " ".join(f"{e}" for e in victims[:PREVIEW_LIMIT])
                             + (f"\n…and {len(victims) - PREVIEW_LIMIT} more" if len(victims) > PREVIEW_LIMIT else "")),
                color=discord.Color.orange(),
            )
            full_view = _FullServerView(self, interaction.user, guild, view.emojis, victims, ephemeral=view.ephemeral)
            await interaction.response.send_message(embed=embed, view=full_view, ephemeral=view.ephemeral)
            full_view.message = await interaction.original_response()
            if view.message is not None:
                try:
                    await view.message.edit(view=None)
                except discord.HTTPException:
                    pass
            return

        await interaction.response.defer(thinking=True, ephemeral=view.ephemeral)
        if view.message is not None:
            try:
                await view.message.edit(view=None)
            except discord.HTTPException:
                pass
        await self._run_save(interaction, guild, view.emojis, [], ephemeral=view.ephemeral)

    @staticmethod
    def _pick_victims(guild: discord.Guild, to_add: list[discord.PartialEmoji]) -> list[discord.Emoji]:
        """Oldest emojis (by ID) of each type, as many as needed to fit `to_add`."""
        victims: list[discord.Emoji] = []
        for animated in (False, True):
            used = sum(e.animated == animated for e in guild.emojis)
            need = sum(e.animated == animated for e in to_add) - (guild.emoji_limit - used)
            if need > 0:
                pool = sorted((e for e in guild.emojis if e.animated == animated), key=lambda e: e.id)
                victims += pool[:need]
        return victims

    async def _run_save(self, interaction: discord.Interaction, guild: discord.Guild,
                        emojis: list[discord.PartialEmoji], victims: list[discord.Emoji],
                        ephemeral: bool = False):
        """Interaction must already be deferred. Deletes `victims`, saves `emojis`, prints paste-ready map lines."""
        limit = guild.emoji_limit
        static_free = limit - sum(not e.animated for e in guild.emojis)
        anim_free = limit - sum(e.animated for e in guild.emojis)
        existing_ids = {e.id for e in guild.emojis}

        deleted: list[str] = []
        saved: list[tuple[discord.PartialEmoji, discord.Emoji]] = []
        skipped: list[str] = []
        failed: list[str] = []

        for v in victims:
            try:
                await v.delete(reason=f"Making room for extracted emojis — by {interaction.user} ({interaction.user.id})")
            except discord.HTTPException as e:
                failed.append(f"delete `:{v.name}:` — {(e.text or 'error')[:60]}")
                continue
            deleted.append(f"`:{v.name}:` (`{v.id}`)")
            if v.animated:
                anim_free += 1
            else:
                static_free += 1

        async with aiohttp.ClientSession() as session:
            for em in emojis:
                label = f"`:{em.name}:`"
                if em.id in existing_ids:
                    skipped.append(f"{label} — already in that server")
                    continue
                if (em.animated and anim_free <= 0) or (not em.animated and static_free <= 0):
                    skipped.append(f"{label} — no free {'animated' if em.animated else 'static'} slots")
                    continue
                try:
                    async with session.get(_emoji_url(em)) as resp:
                        if resp.status != 200:
                            failed.append(f"{label} — download failed ({resp.status})")
                            continue
                        data = await resp.read()
                    new = await guild.create_custom_emoji(
                        name=_clean_name(em.name), image=data,
                        reason=f"Extracted by {interaction.user} ({interaction.user.id})",
                    )
                except discord.Forbidden:
                    failed.append(f"{label} — missing permissions")
                    break
                except discord.HTTPException as e:
                    failed.append(f"{label} — {(e.text or 'error')[:80]}")
                    continue
                saved.append((em, new))
                if em.animated:
                    anim_free -= 1
                else:
                    static_free -= 1

        color = discord.Color.green() if saved and not failed else (
            discord.Color.orange() if saved else discord.Color.red())
        result = discord.Embed(title=f"Emojis saved to {guild.name}", color=color)
        result.description = f"**{len(saved)}/{len(emojis)}** saved"
        if saved:
            result.add_field(name="✅ Saved", value=_join([f"{n} `:{n.name}:`" for _, n in saved]), inline=False)
        if deleted:
            result.add_field(name="🗑️ Deleted to make room", value=_join(deleted), inline=False)
        if skipped:
            result.add_field(name="⏭️ Skipped", value=_join(skipped), inline=False)
        if failed:
            result.add_field(name="❌ Failed", value=_join(failed), inline=False)
        await interaction.followup.send(embed=result, ephemeral=ephemeral)

        # paste-ready lines for emoji_map.py:  original ID -> your new emoji
        if saved:
            lines = [f'    {old.id}: "{new}",  # old {old} -> new :{new.name}:' for old, new in saved]
            chunk: list[str] = []
            size = 0
            for line in lines:
                if size + len(line) + 1 > 1800:
                    await interaction.followup.send(f"```python\

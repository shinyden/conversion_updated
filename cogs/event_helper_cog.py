"""
Event Helper cog — helper tools for an event that runs on ANOTHER bot.

Features (each can be switched on/off per server with  eh enable / eh disable)
  bingo  When the event bot posts a Bingo card ("### Quests" + "Bingo card #N"), reply to it
         with every quest that is NOT a "Catch ..." quest (trade, market, release, evolve, ...)
         together with its cell (A4, D3, ...).
  sync   Keep that reply up to date when the event message is edited (and delete it when the
         event message is deleted).
  ids    When the event bot posts a "Your pokémon" list, add a 🆔 reaction. When a user reacts
         with the same emoji, reply with the ids of that list: once in a ``` block (PC) and
         once in a ` block (mobile, press to copy). The message is always fetched fresh when
         the reaction happens, so an edited / paginated message gives its LATEST ids.
         The reaction only lives EMOJI_TTL_SECONDS (5 min): after that the bot removes it and
         ignores clicks. Editing / paging the list re-adds it and restarts the timer.

No in-memory caches — everything that must survive restarts lives in MongoDB.

Collections used
  helper_settings  {guild_id, disabled: [feature, ..]}
  helper_links     {_id: event_msg_id, reply_id, reply_channel_id, created_at}   (TTL)
  helper_reactions {_id: event_msg_id, channel_id, expires_at}   (when to remove the 🆔)

Needs
  * Message Content intent (to read other bots' messages) + Reactions intent.
  * Bot permissions: Send Messages, Read Message History, Add Reactions.
    (Manage Messages is OPTIONAL: it lets the bot clear the user's 🆔 reaction after answering,
     so the same reaction can be clicked again.)
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import re
from typing import NamedTuple, Optional

import discord
from discord import components as dc
from discord.ext import commands, tasks

import db

# ── Settings ────────────────────────────────────────────────────────────────

SOURCE_BOT_ID = 716390085896962058   # the ONLY bot this cog reads / reacts to
IDS_EMOJI = "🆔"                # emoji the bot adds / listens for on pokémon lists
CLEAR_USER_REACTION = True      # remove the user's reaction after answering (needs Manage Messages)
LINK_TTL_DAYS = 7               # how long we remember "event message → our reply"
EMOJI_TTL_SECONDS = 300         # how long the 🆔 reaction stays usable (then it is removed)

FEATURES: dict[str, str] = {
    "bingo": "Reply to Bingo cards with the non-catch quests and their cells",
    "sync": "Edit that reply when the event message is edited (and delete it with the original)",
    "ids": f"Add {IDS_EMOJI} to pokémon lists; reacting returns the ids (PC + mobile format)",
}

NO_MENTIONS = discord.AllowedMentions.none()


# ── Reading the event bot's message ─────────────────────────────────────────

def _walk_text(components, out: list[str]):
    for comp in components:
        if isinstance(comp, dc.TextDisplay):
            if comp.content:
                out.append(comp.content)
        elif isinstance(comp, (dc.Container, dc.ActionRow, dc.SectionComponent)):
            _walk_text(comp.children, out)


def message_text(message: discord.Message) -> str:
    """All readable text of a message: content + classic embeds + Components V2 text."""
    parts: list[str] = []
    if message.content:
        parts.append(message.content)
    for e in message.embeds:
        if e.title:
            parts.append(e.title)
        if e.description:
            parts.append(e.description)
        for f in e.fields:
            parts.append(f"{f.name}\n{f.value}")
    _walk_text(message.components, parts)
    return "\n".join(parts)


def _relevant(s: str) -> bool:
    """Cheap pre-check so ordinary bot chatter never costs a DB read."""
    return "Bingo card" in s or "Quests" in s or "Your pok" in s


# ── Bingo parsing ───────────────────────────────────────────────────────────

# **A1.** <:emoji:id> Catch 13 Unknown gender pokémon (0/13)
QUEST_RE = re.compile(
    r"^[ \t]*\*\*([A-Za-z]\d{1,2})\.\*\*[ \t]*((?:<a?:\w+:\d+>[ \t]*)*)(.+?)[ \t]*\((\d+)\s*/\s*(\d+)\)[ \t]*$",
    re.M,
)
CARD_RE = re.compile(r"Bingo card #(\d+)")
QUEST_HEADER_RE = re.compile(r"^#{1,3}[ \t]*Quests", re.M)
CATCH_RE = re.compile(r"catch\b", re.I)


class Quest(NamedTuple):
    cell: str
    tile: str
    text: str
    done: int
    total: int


def _tile(emoji_blob: str) -> str:
    low = emoji_blob.lower()
    if "sunlit" in low:
        return "☀️"          # day tile  → Sunlit chest
    if "phantom" in low:
        return "🌙"          # night tile → Phantom chest
    return "🎲"              # random chest


def parse_quests(text: str) -> list[Quest]:
    return [
        Quest(m.group(1).upper(), _tile(m.group(2)), m.group(3).strip(), int(m.group(4)), int(m.group(5)))
        for m in QUEST_RE.finditer(text)
    ]


def is_bingo(text: str, quests: list[Quest]) -> bool:
    return bool(quests) and bool(CARD_RE.search(text) or QUEST_HEADER_RE.search(text))


def render_bingo(text: str, quests: list[Quest]) -> str:
    other = [q for q in quests if not CATCH_RE.match(q.text)]
    card = CARD_RE.search(text)
    head = "📋 **Non-catch quests**" + (f" · Bingo card #{card.group(1)}" if card else "")
    if not other:
        return f"{head}\nNothing here — every quest on this card is a catch quest. 🎣"
    lines = [
        f"**{q.cell}.** {q.tile} {'✅ ~~' + q.text + '~~' if q.done >= q.total else q.text} ({q.done}/{q.total})"
        for q in other
    ]
    foot = f"-# {len(other)} of {len(quests)} quests · ☀️ day · 🌙 night · 🎲 random chest"
    return "\n".join([head, *lines, foot])


# ── Pokémon list parsing ────────────────────────────────────────────────────

POKEMON_HEADER_RE = re.compile(r"Your pok[eé]mon", re.I)
# `67`, `  67`, `67  ` all match (any spaces, incl. non-breaking, inside the backticks)
ID_LINE_RE = re.compile(r"^[^\S\r\n]*`[^\S\r\n]*(\d+)[^\S\r\n]*`", re.M)
ENTRIES_RE = re.compile(r"Showing entries\s+(\d+)\s*[–\-]\s*(\d+)\s+out of\s+([\d,]+)")


def parse_ids(text: str) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for i in ID_LINE_RE.findall(text):
        if i not in seen:
            seen.add(i)
            ids.append(i)
    return ids


def render_ids(text: str, ids: list[str], user_id: int) -> str:
    joined = " ".join(ids)
    entries = ENTRIES_RE.search(text)
    where = f" · entries {entries.group(1)}–{entries.group(2)}" if entries else ""
    return (
        f"-# <@{user_id}> · {len(ids)} ids{where}\n"
        f"**PC users**\n"
        f"```{joined}```\n"
        f"**Mobile users** *(press to copy)*\n"
        f"`{joined}`"
    )


# ── The cog ─────────────────────────────────────────────────────────────────

class EventHelperCog(commands.Cog, name="EventHelper"):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._inflight: dict[int, list] = {}      # per-message lock, removed when finished

    async def cog_load(self):
        try:
            d = db.get_db()
            await d.helper_links.create_index("created_at", expireAfterSeconds=LINK_TTL_DAYS * 86400)
            await d.helper_settings.create_index("guild_id")
            await d.helper_reactions.create_index("expires_at")
        except Exception as e:                    # index problems must never block loading
            print(f"[event_helper] index setup failed: {type(e).__name__}: {e}")
        self._sweep.start()

    async def cog_unload(self):
        self._sweep.cancel()

    # ── 🆔 reaction expiry ──────────────────────────────────────────────────

    @staticmethod
    def _now() -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    async def _arm_reaction(self, message: discord.Message):
        """(Re)start the countdown after which the 🆔 reaction is removed."""
        await db.get_db().helper_reactions.update_one(
            {"_id": message.id},
            {"$set": {
                "channel_id": message.channel.id,
                "expires_at": self._now() + datetime.timedelta(seconds=EMOJI_TTL_SECONDS),
            }},
            upsert=True,
        )

    @tasks.loop(seconds=30)
    async def _sweep(self):
        try:
            coll = db.get_db().helper_reactions
            while True:
                doc = await coll.find_one_and_delete({"expires_at": {"$lte": self._now()}})
                if doc is None:
                    break
                ch = self.bot.get_channel(doc["channel_id"]) \
                    or self.bot.get_partial_messageable(doc["channel_id"])
                pm = ch.get_partial_message(doc["_id"])
                try:
                    await pm.clear_reaction(IDS_EMOJI)        # needs Manage Messages
                except discord.Forbidden:
                    with contextlib.suppress(discord.HTTPException):
                        await pm.remove_reaction(IDS_EMOJI, self.bot.user)   # at least our own
                except discord.HTTPException:
                    pass
        except Exception as e:                                # never let the loop die
            print(f"[event_helper] sweep failed: {type(e).__name__}: {e}")

    @_sweep.before_loop
    async def _before_sweep(self):
        await self.bot.wait_until_ready()

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

    async def _settings(self, guild_id: int) -> dict:
        return await db.get_db().helper_settings.find_one({"guild_id": guild_id}) or {}

    @staticmethod
    def _on(settings: dict, feature: str) -> bool:
        return feature not in settings.get("disabled", [])

    @staticmethod
    def _source_ok(author_id: int) -> bool:
        """Only the event bot is ever read."""
        return author_id == SOURCE_BOT_ID

    # ── Processing one event message ────────────────────────────────────────

    async def _process(self, message: discord.Message, text: str, settings: dict):
        if self._on(settings, "bingo"):
            quests = parse_quests(text)
            if is_bingo(text, quests):
                await self._bingo(message, text, quests, settings)

        if self._on(settings, "ids") and POKEMON_HEADER_RE.search(text) and parse_ids(text):
            present = any(str(r.emoji) == IDS_EMOJI and r.me for r in message.reactions)
            if not present:
                try:
                    await message.add_reaction(IDS_EMOJI)
                    present = True
                except discord.HTTPException:
                    pass
            if present:
                await self._arm_reaction(message)             # (re)start the 5‑minute timer

    async def _bingo(self, message: discord.Message, text: str, quests: list[Quest], settings: dict):
        content = render_bingo(text, quests)
        links = db.get_db().helper_links
        link = await links.find_one({"_id": message.id})

        if link:                                              # edit the reply we already made
            if not self._on(settings, "sync"):
                return
            ch = self.bot.get_channel(link["reply_channel_id"]) \
                or self.bot.get_partial_messageable(link["reply_channel_id"])
            try:
                await ch.get_partial_message(link["reply_id"]).edit(content=content, allowed_mentions=NO_MENTIONS)
            except (discord.NotFound, discord.Forbidden):
                await links.delete_one({"_id": message.id})   # reply was deleted → stop tracking
            return

        try:
            reply = await message.reply(content, mention_author=False, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            return
        await links.update_one(
            {"_id": message.id},
            {"$set": {
                "reply_id": reply.id,
                "reply_channel_id": reply.channel.id,
                "created_at": datetime.datetime.now(datetime.timezone.utc),
            }},
            upsert=True,
        )

    # ── Listeners ───────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or not self._source_ok(message.author.id):
            return
        text = message_text(message)
        if not _relevant(text):
            return
        settings = await self._settings(message.guild.id)
        async with self._guard(message.id):
            await self._process(message, text, settings)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.guild_id is None:
            return
        author = payload.data.get("author") or {}
        if not self._source_ok(int(author.get("id", 0))):
            return
        if not _relevant(json.dumps(payload.data)):       # cheap check on the raw payload
            return
        settings = await self._settings(payload.guild_id)
        channel = await self._resolve_channel(payload.channel_id)
        if channel is None:
            return
        async with self._guard(payload.message_id):
            try:                                          # always the latest version
                message = await channel.fetch_message(payload.message_id)
            except discord.HTTPException:
                return
            await self._process(message, message_text(message), settings)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        link = await db.get_db().helper_links.find_one_and_delete({"_id": payload.message_id})
        if link is None:
            return
        settings = await self._settings(payload.guild_id) if payload.guild_id else {}
        if not self._on(settings, "sync"):
            return
        ch = self.bot.get_channel(link["reply_channel_id"]) \
            or self.bot.get_partial_messageable(link["reply_channel_id"])
        with contextlib.suppress(discord.HTTPException):
            await ch.get_partial_message(link["reply_id"]).delete()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) != IDS_EMOJI or (payload.member and payload.member.bot):
            return
        settings = await self._settings(payload.guild_id)
        if not self._on(settings, "ids"):
            return
        live = await db.get_db().helper_reactions.find_one(
            {"_id": payload.message_id, "expires_at": {"$gt": self._now()}}
        )
        if live is None:                                  # expired (or never armed) → ignore
            return
        channel = await self._resolve_channel(payload.channel_id)
        if channel is None:
            return
        try:                                              # fresh fetch → reflects edits / new page
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        if not self._source_ok(message.author.id):
            return

        text = message_text(message)
        ids = parse_ids(text) if POKEMON_HEADER_RE.search(text) else []
        if not ids:
            return

        out = render_ids(text, ids, payload.user_id)
        try:
            await message.reply(out, mention_author=False, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            with contextlib.suppress(discord.HTTPException):
                await channel.send(out, allowed_mentions=NO_MENTIONS)

        if CLEAR_USER_REACTION:                           # lets the same reaction be clicked again
            with contextlib.suppress(discord.HTTPException):
                await message.remove_reaction(IDS_EMOJI, discord.Object(payload.user_id))

    # ── Commands:  eh  (alias: eventhelper, ehelp) ──────────────────────────

    @commands.group(name="eventhelper", aliases=["eh", "ehelp"], invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def eventhelper(self, ctx: commands.Context):
        """Show which helper features are on or off."""
        await self.eh_status(ctx)

    @eventhelper.command(name="status", aliases=["list"])
    async def eh_status(self, ctx: commands.Context):
        """Show which helper features are on or off."""
        settings = await self._settings(ctx.guild.id)
        lines = [
            f"{'🟢' if self._on(settings, name) else '🔴'} **{name}** — {desc}"
            for name, desc in FEATURES.items()
        ]
        e = discord.Embed(title="🧰 Event Helper", description="\n".join(lines), color=discord.Color.blurple())
        e.add_field(name="Reading messages from", value=f"<@{SOURCE_BOT_ID}> only", inline=False)
        e.add_field(
            name="Commands",
            value=(
                "`eh enable <feature…|all>`\n"
                "`eh disable <feature…|all>`"
            ),
            inline=False,
        )
        await ctx.reply(embed=e, mention_author=False)

    async def _toggle(self, ctx: commands.Context, names: tuple[str, ...], enable: bool):
        names = tuple(n.lower() for n in names)
        if "all" in names:
            names = tuple(FEATURES)
        bad = [n for n in names if n not in FEATURES]
        if not names or bad:
            valid = ", ".join(f"`{n}`" for n in FEATURES)
            await ctx.reply(f"❌ {'Unknown feature: ' + ', '.join(f'`{b}`' for b in bad) + '. ' if bad else ''}"
                            f"Pick from {valid} or `all`.", mention_author=False)
            return
        op = (
            {"$pull": {"disabled": {"$in": list(names)}}} if enable
            else {"$addToSet": {"disabled": {"$each": list(names)}}}
        )
        await db.get_db().helper_settings.update_one({"guild_id": ctx.guild.id}, op, upsert=True)
        await ctx.reply(f"{'🟢 Enabled' if enable else '🔴 Disabled'}: " + ", ".join(f"**{n}**" for n in names),
                        mention_author=False)

    @eventhelper.command(name="enable", aliases=["on"])
    async def eh_enable(self, ctx: commands.Context, *features: str):
        """Turn features on.  Usage: eh enable bingo ids   |   eh enable all"""
        await self._toggle(ctx, features, True)

    @eventhelper.command(name="disable", aliases=["off"])
    async def eh_disable(self, ctx: commands.Context, *features: str):
        """Turn features off.  Usage: eh disable sync   |   eh disable all"""
        await self._toggle(ctx, features, False)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("❌ You need **Manage Server** permission to use this.", mention_author=False)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.reply("❌ This command only works in a server.", mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(EventHelperCog(bot))

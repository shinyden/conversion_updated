"""
cogs/gym_cog.py
─────────────────
AI Gyms — 18 type-themed gym leaders (one per type, one badge each) that
the bot plays with a team specifically built to counter whatever the
challenger brings.

Commands
--------
!gym <type> [count]
    Challenge the AI Gym of `type` (e.g. `!gym fire`). `count` (default 3,
    1-6) sets how many Pokemon each side battles with. The bot then asks
    you to submit your own team — it does NOT see your team until you've
    finished adding it.

!gym add <name>, <name>, ...
    Add Pokemon (comma-separated) to your team for a pending gym challenge.
    Once you've added `count` Pokemon, the AI builds its counter-team and
    the battle starts automatically:
      1. It shortlists species of the gym's type, scored by how well their
         typing (offense + what they resist) answers your WHOLE team, not
         just one member of it — see cogs/battle/pokeapi.py's
         `_best_gym_counters`.
      2. Each pick's moveset is then biased toward whatever hits YOUR team
         hardest (`pick_gym_moves`) instead of just the generically
         strongest moves.
    The fight itself reuses the normal Battle engine — including the same
    smart switching AI as `!battle ai` (see cogs/battle/trainer_ai.py) — so
    the gym leader also plays a full, adaptive battle, not just a scripted
    opener.

!gym cancel
    Cancels a pending gym team-build in the current channel.

!gym list [@user]
    Shows all 18 gyms and which ones a trainer (defaults to you) has
    already earned the badge for.

Winning a gym battle awards that gym's badge (once — beating a gym you've
already cleared again doesn't duplicate it). Badges show up on `!bpf`.
"""

from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

from .battle.constants import LEVEL, GYM_TYPES, GYM_TYPE_EMOJI, GYM_BADGE_NAME, GYM_TOTAL_BADGES
from .battle.pokeapi import get_pokemon_data, pick_moves, build_gym_team
from .battle.engine import BattlePokemon
import random
from .battle.trainer_ai import Trainer, pick_best_replacement

# Difficulty by badges already earned: (min_badges, name, stat boost, ace boost, heals)
GYM_TIERS = [
    (0, "Rookie", 1.05, 1.15, 1),
    (2, "Veteran", 1.10, 1.20, 2),
    (5, "Elite", 1.15, 1.25, 3),
    (8, "Champion", 1.20, 1.30, 3),
]


def _gym_tier(badges: int):
    tier = GYM_TIERS[0]
    for t in GYM_TIERS:
        if badges >= t[0]:
            tier = t
    return tier


QUOTES = {
    "intro": ["So you're the challenger? My {t} team doesn't go down easy!",
              "I've scouted your team. Let's see if you can handle {t} type pressure!",
              "Welcome to the {t} Gym! Show me what you've got!"],
    "ace": ["Don't blink — this is my ace, my pride and joy!",
            "You pushed me this far... now face my strongest!",
            "Impressive! But my ace has never lost a gym match!"],
    "heal": ["Not so fast — I'm not done yet!", "A little pick-me-up never hurt anyone!",
             "Hang in there, partner!"],
    "win": ["Better luck next time, challenger. Train harder!",
            "My {t} team stays undefeated today!"],
    "lose": ["Incredible... you've earned this badge.",
             "Well fought! The {t} Gym acknowledges your strength."],
}


def _quotes(gym_type: str) -> dict:
    t = gym_type.title()
    return {k: random.choice(v).format(t=t) for k, v in QUOTES.items()}


def make_shiny(mon):
    """Swap in the shiny sprite (PokeAPI URL pattern) and flag the mon."""
    url = mon.sprite or ""
    if "/pokemon/" in url and "/shiny/" not in url:
        mon.sprite = url.replace("/pokemon/", "/pokemon/shiny/", 1)
    mon.shiny = True


def assign_gym_items(team: list, player_team: list):
    """Every gym Pokemon gets a purposeful held item: lead = Focus Sash,
    ace = Life Orb/Assault Vest, slower-than-player mons = Choice Scarf,
    the rest random from the support pool."""
    fastest = max((p.spe for p in player_team), default=0)
    pool = ["leftovers", "sitrus-berry", "shell-bell", "expert-belt"]
    random.shuffle(pool)
    for n, mon in enumerate(team):
        if n == 0 and len(team) > 1:
            item = "focus-sash"
        elif n == len(team) - 1:
            item = "assault-vest" if (mon.dfn + mon.spd) > (mon.atk + mon.spa) else "life-orb"
        elif mon.spe < fastest:
            item = "choice-scarf"
        else:
            item = pool.pop() if pool else "leftovers"
        mon.item = item
        mon.item_used = False
from .battle.runner import Battle

import db as _db


@dataclass
class PendingGymChallenge:
    trainer: discord.Member
    gym_type: str
    count: int
    team: list = field(default_factory=list)


class GymCog(commands.Cog, name="Gym"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.pending: dict = {}  # channel_id -> PendingGymChallenge

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    def _battle_cog(self):
        """The main Battle cog — the gym battle itself runs through the
        same Battle engine/registry, so a `!gym` fight and a `!battle`
        fight can never overlap in the same channel by accident."""
        return self.bot.get_cog("Battle")

    def _channel_busy(self, channel_id: int) -> bool:
        battle_cog = self._battle_cog()
        return (
            channel_id in self.pending
            or (battle_cog is not None and channel_id in battle_cog.pending)
            or (battle_cog is not None and channel_id in battle_cog.active_battles)
        )

    @commands.group(name="gym", invoke_without_command=True)
    async def gym(self, ctx: commands.Context, gym_type: Optional[str] = None, count: int = 3):
        if gym_type is None:
            badges = set(await _db.get_gym_badges(ctx.guild.id if ctx.guild else 0, ctx.author.id))
            lines = []
            for t in GYM_TYPES:
                mark = "✅" if t in badges else "▫️"
                lines.append(f"{mark} {GYM_TYPE_EMOJI.get(t, '')} **{t.title()}** — `!gym {t}`")
            embed = discord.Embed(
                title=f"🏟️ AI Gyms ({len(badges)}/{GYM_TOTAL_BADGES} badges)",
                description=(
                    "Challenge a type-themed gym leader! `!gym <type> [count]` "
                    "(count = team size, default 3).\n\n" + "\n".join(lines)
                ),
                colour=0xE67E22,
            )
            await ctx.send(embed=embed)
            return

        gym_type = gym_type.strip().lower()
        if gym_type not in GYM_TYPES:
            await ctx.send(
                f"`{gym_type}` isn't a gym type. Choose one of: "
                f"{', '.join(t.title() for t in GYM_TYPES)}."
            )
            return

        if self._channel_busy(ctx.channel.id):
            await ctx.send(
                "There's already a pending challenge or active battle in this "
                "channel. Finish it or run `!gym cancel` / `!battle cancel` first."
            )
            return

        count = max(1, min(6, count))
        self.pending[ctx.channel.id] = PendingGymChallenge(ctx.author, gym_type, count)
        emoji = GYM_TYPE_EMOJI.get(gym_type, "")
        await ctx.send(
            f"{emoji} **{gym_type.title()} Gym** challenge accepted, {ctx.author.mention}!\n"
            f"Submit your team first — the gym leader won't see it until it's "
            f"complete: `!gym add <name>, <name>, ...` (up to {count})."
        )

    @gym.command(name="cancel")
    async def gym_cancel(self, ctx: commands.Context):
        if ctx.channel.id in self.pending:
            del self.pending[ctx.channel.id]
            await ctx.send("Gym challenge cancelled.")
        else:
            await ctx.send("There's no pending gym challenge here to cancel.")

    @gym.command(name="list")
    async def gym_list(self, ctx: commands.Context, *, target: Optional[str] = None):
        member = ctx.author
        if target:
            try:
                member = await commands.MemberConverter().convert(ctx, target)
            except commands.BadArgument:
                await ctx.send(f"Couldn't find a member matching `{target}`.")
                return
        guild_id = ctx.guild.id if ctx.guild else 0
        badges = set(await _db.get_gym_badges(guild_id, member.id))
        lines = [
            f"{'✅' if t in badges else '▫️'} {GYM_TYPE_EMOJI.get(t, '')} {t.title()}"
            for t in GYM_TYPES
        ]
        embed = discord.Embed(
            title=f"🏅 {member.display_name}'s Gym Badges ({len(badges)}/{GYM_TOTAL_BADGES})",
            description="\n".join(lines),
            colour=0xE67E22,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await ctx.send(embed=embed)

    @gym.command(name="add")
    async def gym_add(self, ctx: commands.Context, *, names: str):
        pending = self.pending.get(ctx.channel.id)
        if not pending:
            await ctx.send("There's no pending gym challenge here. Start one with `!gym <type>`.")
            return
        if ctx.author.id != pending.trainer.id:
            await ctx.send("This gym challenge isn't yours.")
            return

        requested = [n.strip() for n in names.split(",") if n.strip()]
        added, failed = [], []
        async with ctx.typing():
            for nm in requested:
                if len(pending.team) >= pending.count:
                    failed.append(f"{nm} (team already full)")
                    continue
                data = await get_pokemon_data(self.session, nm)
                if not data:
                    failed.append(f"{nm} (not found)")
                    continue
                moves = await pick_moves(self.session, data)
                pending.team.append(BattlePokemon(data, moves))
                added.append(data["name"].title())

        msg = ""
        if added:
            msg += f"✅ Added: {', '.join(added)} ({len(pending.team)}/{pending.count})\n"
        if failed:
            msg += f"⚠️ Skipped: {', '.join(failed)}"
        await ctx.send(msg or "Nothing added.")

        if len(pending.team) >= pending.count:
            self.pending.pop(ctx.channel.id, None)
            await self._start_gym_battle(ctx, pending)

    async def _start_gym_battle(self, ctx: commands.Context, pending: PendingGymChallenge):
        battle_cog = self._battle_cog()
        if battle_cog is None:
            await ctx.send("⚠️ The Battle cog isn't loaded — can't start the gym battle.")
            return

        emoji = GYM_TYPE_EMOJI.get(pending.gym_type, "")
        await ctx.send(
            f"{emoji} The **{pending.gym_type.title()} Gym** leader has scouted your team "
            f"and is picking their counters..."
        )
        ai_team = await build_gym_team(self.session, pending.gym_type, pending.team, pending.count)

        # Difficulty scales with how many badges the challenger already holds.
        guild_id = ctx.guild.id if ctx.guild else 0
        try:
            badges = await _db.get_gym_badge_count(guild_id, pending.trainer.id)
        except Exception:
            badges = 0
        tier_name, base_boost, ace_boost, tier_heals = _gym_tier(badges)[1:]

        for n, mon in enumerate(ai_team):
            boost = ace_boost if (n == len(ai_team) - 1 and len(ai_team) > 1) else base_boost
            for stat in ("atk", "dfn", "spa", "spd", "spe"):
                setattr(mon, stat, int(getattr(mon, stat) * boost))
            mon.max_hp = int(mon.max_hp * boost)
            mon.hp = mon.max_hp

        t1 = Trainer(pending.trainer, team=pending.team)
        for mon in ai_team:
            make_shiny(mon)
        quotes = _quotes(pending.gym_type)
        t2 = Trainer(self.bot.user, team=ai_team, is_bot=True, hard=True,
                     title=f"{pending.gym_type.title()} Gym Leader", quotes=quotes,
                     heals_left=tier_heals if pending.count >= 3 else 1)

        # Smart lead: best matchup vs the player's lead (the ace stays last).
        if len(ai_team) > 2:
            body = ai_team[:-1]
            t2.team = body
            lead = pick_best_replacement(t2, t1)
            body.insert(0, body.pop(lead))
            t2.team = body + [ai_team[-1]]
        ai_team = t2.team
        assign_gym_items(ai_team, pending.team)

        battle = Battle(battle_cog, ctx.channel, t1, t2, fmt="random",
                         count=pending.count, vs_bot=True)
        battle_cog.active_battles[ctx.channel.id] = battle
        await ctx.send(
            f"⚔️ **Gym battle start!** {pending.trainer.mention} vs the "
            f"{emoji} **{pending.gym_type.title()} Gym** — Level {LEVEL}, "
            f"{pending.count} Pokémon each.\n"
            f"💪 Difficulty: **{tier_name}** ({badges} badges) — trained Pokémon "
            f"(+{round((base_boost-1)*100)}% stats, ace +{round((ace_boost-1)*100)}%), "
            f"held items, and **{t2.heals_left} Hyper Potion(s)**.\n"
            f"✨ Every gym Pokémon is **shiny**!"
        )
        await battle.run()

        # Battle.run() already recorded the vs-AI result/Elo and popped the
        # channel out of active_battles — figure out the outcome from the
        # Trainer objects it mutated in place and award the badge.
        human_won = (battle.forfeited_trainer is not t1) and bool(t1.alive_team)
        await ctx.send(f"💬 **{t2.title}**: *{quotes['lose' if human_won else 'win']}*")
        if human_won:
            guild_id = ctx.guild.id if ctx.guild else 0
            is_new = await _db.award_gym_badge(guild_id, pending.trainer.id, pending.gym_type)
            badge_name = GYM_BADGE_NAME.get(pending.gym_type, f"{pending.gym_type.title()} Badge")
            if is_new:
                embed = discord.Embed(
                    title="🏅 Gym Badge Earned!",
                    description=(
                        f"{pending.trainer.mention} defeated the {emoji} "
                        f"**{pending.gym_type.title()} Gym** and earned the **{badge_name}**!"
                    ),
                    colour=0xF1C40F,
                )
                embed.set_thumbnail(url=pending.trainer.display_avatar.url)
                total = await _db.get_gym_badge_count(guild_id, pending.trainer.id)
                embed.set_footer(text=f"Badges: {total}/{GYM_TOTAL_BADGES}")
                await ctx.send(embed=embed)
            else:
                await ctx.send(
                    f"🏅 {pending.trainer.mention} beat the {pending.gym_type.title()} Gym again — "
                    f"you already hold the {badge_name}."
                )
        else:
            await ctx.send(
                f"The {emoji} **{pending.gym_type.title()} Gym** leader held their ground — "
                f"no badge this time. Train up and try again!"
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(GymCog(bot))
                  

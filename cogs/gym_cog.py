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
from .battle.trainer_ai import Trainer, pick_best_replacement
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

        # Gym leaders are trained: +10% stats, and their ace (last mon) gets +20%.
        for n, mon in enumerate(ai_team):
            boost = 1.20 if (n == len(ai_team) - 1 and len(ai_team) > 1) else 1.10
            for stat in ("atk", "dfn", "spa", "spd", "spe"):
                setattr(mon, stat, int(getattr(mon, stat) * boost))
            mon.max_hp = int(mon.max_hp * boost)
            mon.hp = mon.max_hp

        t1 = Trainer(pending.trainer, team=pending.team)
        t2 = Trainer(self.bot.user, team=ai_team, is_bot=True, hard=True,
                     heals_left=2 if pending.count >= 3 else 1)

        # Smart lead: best matchup vs the player's lead (the ace stays last).
        if len(ai_team) > 2:
            body = ai_team[:-1]
            t2.team = body
            lead = pick_best_replacement(t2, t1)
            body.insert(0, body.pop(lead))
            t2.team = body + [ai_team[-1]]
        ai_team = t2.team

        battle = Battle(battle_cog, ctx.channel, t1, t2, fmt="random",
                         count=pending.count, vs_bot=True)
        battle_cog.active_battles[ctx.channel.id] = battle
        await ctx.send(
            f"⚔️ **Gym battle start!** {pending.trainer.mention} vs the "
            f"{emoji} **{pending.gym_type.title()} Gym** — Level {LEVEL}, "
            f"{pending.count} Pokémon each.\n"
            f"💪 Gym leader Pokémon are trained (+10% stats, ace +20%) and carry "
            f"**{t2.heals_left} Hyper Potion(s)**."
        )
        await battle.run()

        # Battle.run() already recorded the vs-AI result/Elo and popped the
        # channel out of active_battles — figure out the outcome from the
        # Trainer objects it mutated in place and award the badge.
        human_won = (battle.forfeited_trainer is not t1) and bool(t1.alive_team)
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
                  

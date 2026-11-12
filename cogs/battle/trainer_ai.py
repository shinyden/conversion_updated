"""
cogs/battle/trainer_ai.py
────────────────────────────
Trainer/PendingChallenge dataclasses and the `!battle ai` opponent AI
(move scoring +  voluntary switching).
"""

from dataclasses import dataclass, field
from typing import Optional
import math

import discord

from .constants import LEVEL
from .engine import BattlePokemon, type_multiplier_for


@dataclass
class Trainer:
    user: discord.abc.User
    team: list = field(default_factory=list)
    active_idx: int = 0
    is_bot: bool = False  # True for the bot's own side in a `!battle @bot` fight
    heals_left: int = 0       # Hyper Potions the AI can still use (gym leaders)
    switch_cooldown: int = 0  # AI can't voluntarily switch while > 0 (stops ping-pong switching)
    hard: bool = False        # gym-leader mode
    title: str = ""           # display name for flavor lines (e.g. "Fire Gym Leader")
    quotes: dict = field(default_factory=dict)  # intro / ace / heal / win / lose
    ace_announced: bool = False

    @property
    def active(self) -> BattlePokemon:
        return self.team[self.active_idx]

    @property
    def alive_team(self) -> list:
        return [p for p in self.team if not p.fainted]


def estimate_damage(attacker: BattlePokemon, defender: BattlePokemon, move: dict) -> float:
    """Deterministic damage estimate for AI move comparison — same formula
    as calc_damage() but without the random crit roll or the 0.85-1.0
    damage-roll variance, so moves can be ranked consistently."""
    power = move.get("power") or 0
    if power <= 0:
        return 0.0
    if move.get("damage_class") == "physical":
        a, d = attacker.effective_stat("atk"), defender.effective_stat("dfn")
    else:
        a, d = attacker.effective_stat("spa"), defender.effective_stat("spd")
    stab = 1.5 if move.get("type") in attacker.types else 1.0
    eff = type_multiplier_for(move.get("type", "normal"), defender)
    if eff == 0:
        return 0.0
    base = (((2 * LEVEL / 5 + 2) * power * a / max(d, 1)) / 50 + 2)
    item_mult = 1.3 if attacker.item == "life-orb" else (1.2 if attacker.item == "expert-belt" and eff > 1 else 1.0)
    return base * stab * eff * item_mult


SWITCH_HP_THRESHOLD = 0.25       # consider switching if best move clears less than this % of foe's HP
SWITCH_INCOMING_THRESHOLD = 0.5  # ...or if the foe's predicted hit would take this much of OUR current HP
SWITCH_MIN_HP = 0.5        # never voluntarily bring in a bench mon below this HP fraction
SWITCH_MAX_INCOMING = 0.6  # ...or one the foe's best hit would take more than this of its CURRENT hp
SWITCH_MARGIN = 0.15       # switch-in must beat staying put by this much
SWITCH_COOLDOWN = 3        # after any switch-in, no voluntary switching for the next 2 turns
HEAL_HP_THRESHOLD = 0.35   # gym leader uses a Hyper Potion at/below this HP fraction
SWITCH_KILL_THRESHOLD = 0.45     # a candidate's best move must clear at least this much of the foe's max
                                  # HP to count as a real "can kill it back" threat - not just chip damage


def _accuracy_weighted_score(attacker: BattlePokemon, defender: BattlePokemon, move: dict) -> float:
    """Expected damage, discounted by the move's accuracy - a 150-power
    move that misses half the time should usually lose out to a reliable
    90-power move, not just whichever number is bigger on paper."""
    acc = move.get("accuracy")
    acc_frac = 1.0 if acc is None else acc / 100
    return estimate_damage(attacker, defender, move) * acc_frac


def _best_matchup_fraction(attacker: BattlePokemon, defender: BattlePokemon) -> float:
    """The attacker's single best accuracy-weighted move, as a fraction of
    the defender's max HP - used to judge whether a Pokemon (bot's current
    mon, or a bench candidate) can actually threaten a kill on the foe.
    Moves with no PP left are skipped, same as a real trainer couldn't
    select them."""
    best = 0.0
    for m in attacker.moves:
        if m.get("current_pp", 1) <= 0:
            continue
        score = _accuracy_weighted_score(attacker, defender, m)
        if score > best:
            best = score
    return best / max(defender.max_hp, 1)


def _worst_case_fraction(attacker: BattlePokemon, defender: BattlePokemon) -> float:
    """The heaviest hit `defender` could take from ANY of attacker's known
    moves (accuracy-weighted damage, not just the single best one),
    expressed as a fraction of defender's CURRENT hp. Since the bot can
    see the opponent's whole moveset, this is what the AI treats as "the
    move the opponent is expected to use" against a given target -
    whichever one hurts that target the most - so a switch-in candidate
    is judged against every option the foe actually has, not just its
    most obvious STAB move."""
    worst = 0.0
    for m in attacker.moves:
        if m.get("current_pp", 1) <= 0:
            continue
        score = _accuracy_weighted_score(attacker, defender, m)
        if score > worst:
            worst = score
    return worst / max(defender.hp, 1)


def _predicted_opponent_move(opponent_active: BattlePokemon, our_target: BattlePokemon) -> Optional[dict]:
    """The move `opponent_active` is expected to use against `our_target`
    next turn - its single highest accuracy-weighted damage option. Purely
    informational (used for flavoring/consistency with _worst_case_fraction,
    which already picks the same move when scoring the incoming threat)."""
    best_move, best_score = None, -1.0
    for m in opponent_active.moves:
        if m.get("current_pp", 1) <= 0:
            continue
        score = _accuracy_weighted_score(opponent_active, our_target, m)
        if score > best_score:
            best_score, best_move = score, m
    return best_move


def _acts_first(attacker: BattlePokemon, defender: BattlePokemon, move: dict,
                 defender_move: Optional[dict]) -> bool:
    """Would `move` go before `defender_move` this turn, using the exact
    same ordering rule the battle engine applies (see runner.py's turn
    sort): priority bracket first, then effective Speed, then raw base
    Speed as the final tie-break. `defender_move` is only a prediction
    (the AI doesn't get to see the foe's actual choice ahead of time), so
    this is "would we win the race if they use their most likely move" -
    the same assumption _worst_case_fraction already makes elsewhere."""
    atk_prio = move.get("priority", 0)
    def_prio = (defender_move or {}).get("priority", 0)
    if atk_prio != def_prio:
        return atk_prio > def_prio
    atk_spe = attacker.effective_stat("spe")
    def_spe = defender.effective_stat("spe")
    if atk_spe != def_spe:
        return atk_spe > def_spe
    return attacker.base_speed >= defender.base_speed


def _turns_to_ko(damage_per_hit: float, target_hp: float) -> float:
    """Rough number of hits needed to whittle target_hp down at
    damage_per_hit per hit. inf if the move barely chips at all."""
    if damage_per_hit <= 0:
        return math.inf
    return math.ceil(target_hp / damage_per_hit)


def _survives_trade(attacker: BattlePokemon, defender: BattlePokemon, atk_move: dict,
                     def_move: Optional[dict], atk_acts_first: bool) -> bool:
    """Approximates repeatedly trading atk_move for the foe's predicted
    def_move: does the attacker land its finishing blow before the
    defender's hits add up to a KO on the attacker?

    This is the classic "priority wins a mutual near-KO race" case: if
    both sides need N hits to finish the other and the attacker moves
    first, it only ever eats N-1 hits (the defender doesn't get to act on
    the turn it's finished off) - one fewer than it would moving second.
    A close trade that's a loss without priority can be a win with it."""
    atk_dmg = _accuracy_weighted_score(attacker, defender, atk_move)
    turns_to_kill = _turns_to_ko(atk_dmg, defender.hp)
    if turns_to_kill == math.inf:
        return False
    def_dmg = _accuracy_weighted_score(defender, attacker, def_move) if def_move else 0.0
    turns_opp_needs = _turns_to_ko(def_dmg, attacker.hp)
    hits_taken = turns_to_kill - 1 if atk_acts_first else turns_to_kill
    return turns_opp_needs > hits_taken


def _is_faster(a: BattlePokemon, b: BattlePokemon) -> bool:
    sa, sb = a.effective_stat("spe"), b.effective_stat("spe")
    return sa > sb or (sa == sb and a.base_speed > b.base_speed)


def pick_best_replacement(trainer: Trainer, opponent: Trainer) -> int:
    """Best bench index to send in after a faint (or as the lead): rewards
    offense vs the foe, OHKO threat and speed; penalises taking a big hit;
    prefers healthier mons. Replaces the old 'first alive mon' behaviour."""
    foe = opponent.active
    best_i, best_score = None, -math.inf
    for i, p in enumerate(trainer.team):
        if p.fainted:
            continue
        offense = _best_matchup_fraction(p, foe)
        incoming = min(_worst_case_fraction(foe, p), 1.5)
        faster = _is_faster(p, foe)
        score = offense * 1.5 - incoming + (0.35 if faster else 0.0)
        if foe.hp > 0 and offense >= foe.hp / max(foe.max_hp, 1):
            score += 1.0                       # can KO the foe outright
        if incoming >= 1.0 and not faster:
            score -= 1.0                       # gets killed before it moves
        score += 0.3 * (p.hp / max(p.max_hp, 1))
        if score > best_score:
            best_i, best_score = i, score
    return best_i if best_i is not None else 0


def bot_choose_action(trainer: Trainer, opponent: Trainer) -> tuple:
    """Battle AI for the bot's own trainer.

    Attacking: scores every move in its active Pokemon's move pool by
    accuracy-weighted expected damage (STAB, type effectiveness, and
    effective stats all factored in via estimate_damage), skips any move
    with no PP left, and attacks with the best one - or Struggles (index
    -1) if every move is out of PP.

    Priority is factored into two decision points, not just raw damage:
      • If any available move can KO the foe's current active outright,
        the bot doesn't just grab whichever such move scores highest on
        paper - among the moves that would actually KO, it prefers one
        that's guaranteed to go first (_acts_first: priority bracket beats
        Speed, same as the real turn order). A slower Pokemon's biggest
        hit is worthless if it faints before landing it - Golisopod
        packing Aqua Jet should use that to finish a faster Moltres
        instead of a stronger move that never gets to fire.
      • If the foe's predicted move would win the overall trade against
        our current pick - including the classic "both survive one hit,
        but whoever attacks first lands the finishing blow one hit sooner"
        case, not just an immediate one-turn faint - the bot falls back to
        its best move that DOES win that trade (_survives_trade),
        typically a priority move, rather than a bigger hit that loses a
        war of attrition it was actually able to win.
    Either way, this never overrides taking a guaranteed kill - going for
    the KO always beats "best raw score" once one is on the table.

    Switching: since the bot can see the opponent's entire moveset, it
    doesn't just compare "my best move vs their best move" - it works out
    the single heaviest hit the foe could land with ANY of its known moves
    (_worst_case_fraction) against both the Pokemon currently in and every
    bench candidate. A bench mon is only considered a real switch-in if:

      • it can survive that worst-case hit AND has a move strong enough to
        threaten a kill back (>= SWITCH_KILL_THRESHOLD of the foe's max
        HP) - "resists it" alone isn't enough, it has to be able to punish
        the foe too; or
      • it's faster than the foe's active AND still clears that same kill
        threshold - being faster only matters if it can also do something
        with the extra turn, not just stall.

    Among eligible candidates the AI picks whichever trades best (highest
    offense minus the risk of the hit it'd still take back), so a slower
    Pokemon that would just eat a huge hit and do nothing back is never
    sent in, even if it's the "type-neutral" pick. This is on top of the
    forced switches on faint handled by Battle.get_forced_switch."""
    active = trainer.active
    defender = opponent.active
    moves = active.moves

    if active.must_recharge:
        return ("recharge", None)

    if trainer.switch_cooldown > 0:
        trainer.switch_cooldown -= 1
    predicted_def_move = None

    available = [i for i, m in enumerate(moves) if m.get("current_pp", 1) > 0]
    if not available:
        best_idx, best_frac = -1, 0.0
    else:
        scores = {i: _accuracy_weighted_score(active, defender, moves[i]) for i in available}
        best_idx = max(available, key=lambda i: scores[i])
        best_frac = scores[best_idx] / max(defender.max_hp, 1)

        predicted_def_move = _predicted_opponent_move(defender, active)

        # A guaranteed (or near-guaranteed) kill this turn is always worth
        # taking - never switch away from a foe that's already going down.
        # But among every move that WOULD KO, prefer one that actually
        # wins the turn-order race - a bigger non-priority hit is no good
        # if this Pokemon is slower and faints before it goes off.
        lethal = [i for i in available if estimate_damage(active, defender, moves[i]) >= defender.hp]
        if lethal:
            racing_lethal = [i for i in lethal if _acts_first(active, defender, moves[i], predicted_def_move)]
            pool = racing_lethal or lethal
            return ("move", max(pool, key=lambda i: scores[i]))

        # Gym leaders can heal: Hyper Potion (+50% max HP) when low, but only
        # if it actually out-heals the foe's best hit and the mon is worth saving.
        if trainer.heals_left > 0 and active.hp / max(active.max_hp, 1) <= HEAL_HP_THRESHOLD:
            healed_hp = min(active.max_hp, active.hp + active.max_hp // 2)
            raw_threat = _worst_case_fraction(defender, active) * active.hp
            if raw_threat < healed_hp and (best_frac >= 0.3 or len(trainer.alive_team) == 1):
                return ("heal", None)

        # Not lethal this turn - check whether our current pick actually
        # wins the war of attrition against the foe's predicted move. If
        # it doesn't (we're slower and would faint before finishing the
        # job - including the "both survive the first hit but priority
        # wins the race" case), fall back to the best available move that
        # DOES win that race.
        if predicted_def_move is not None:
            best_acts_first = _acts_first(active, defender, moves[best_idx], predicted_def_move)
            if not _survives_trade(active, defender, moves[best_idx], predicted_def_move, best_acts_first):
                racing = [i for i in available
                          if _acts_first(active, defender, moves[i], predicted_def_move)]
                surviving = [i for i in racing
                             if _survives_trade(active, defender, moves[i], predicted_def_move, True)]
                if surviving:
                    best_idx = max(surviving, key=lambda i: scores[i])
                    best_frac = scores[best_idx] / max(defender.max_hp, 1)

    bench = [(i, p) for i, p in enumerate(trainer.team) if not p.fainted and p is not active]
    if not bench or trainer.switch_cooldown > 0 or not available:
        return ("move", best_idx)

    incoming_frac = _worst_case_fraction(defender, active)

    # If we win the trade by staying in (and aren't about to be one-shot), stay.
    if predicted_def_move is not None and incoming_frac < 1.0:
        if _survives_trade(active, defender, moves[best_idx], predicted_def_move,
                           _acts_first(active, defender, moves[best_idx], predicted_def_move)):
            return ("move", best_idx)

    matchup_is_bad = best_frac < SWITCH_HP_THRESHOLD or incoming_frac >= SWITCH_INCOMING_THRESHOLD
    if not matchup_is_bad:
        return ("move", best_idx)

    stay_score = best_frac - incoming_frac * 0.5
    best_switch_idx, best_switch_score = None, 0.0
    for i, candidate in bench:
        # Switching costs a turn: the switch-in eats the foe's hit for free,
        # so it must be healthy, survive that hit comfortably, and punish back.
        if candidate.hp / max(candidate.max_hp, 1) < SWITCH_MIN_HP:
            continue
        cand_incoming = _worst_case_fraction(defender, candidate)
        if cand_incoming > SWITCH_MAX_INCOMING:
            continue
        cand_best_frac = _best_matchup_fraction(candidate, defender)
        if cand_best_frac < SWITCH_KILL_THRESHOLD:
            continue
        cand_faster = _is_faster(candidate, defender)
        score = cand_best_frac - cand_incoming * 0.5 + (0.1 if cand_faster else 0.0)
        if score > stay_score + SWITCH_MARGIN and score > best_switch_score:
            best_switch_score, best_switch_idx = score, i

    if best_switch_idx is not None:
        trainer.switch_cooldown = SWITCH_COOLDOWN
        return ("switch", best_switch_idx)
    return ("move", best_idx)


@dataclass
class PendingChallenge:
    challenger: discord.Member
    opponent: discord.Member
    fmt: str
    count: int
    accepted: bool = False
    teams: dict = field(default_factory=dict)
    bst_filter: tuple = (None, None)

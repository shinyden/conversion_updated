"""
cogs/battle/pokeapi.py
────────────────────────
PokeAPI fetch + MongoDB cache layer, move selection, and random-team
building.

Performance notes
------------------
Two things used to make `random` team rolls slow (multi-minute first
embeds, and still slow hours after startup even with a warm cache):

1. `pick_moves()` looked up every move in a Pokemon's move pool with an
   individual `find_one` each — a Pokemon with an 80-move learnset meant
   80 separate Mongo round trips just to check the cache, every single
   time, cache-warm or not. `get_move_data_bulk()` below replaces that
   with ONE `$in` query for everything already cached, and only falls
   back to individual PokeAPI fetches (still concurrent) for genuinely
   new moves.
2. Building a team rolled each Pokemon one at a time (`for _ in
   range(count): team.append(await build_random_pokemon(...))`), and
   both trainers' teams were built sequentially after each other too.
   For a 3v3 that's up to 6 fully-serial builds; for a 6v6, up to 12 —
   each one waiting on the previous to finish even when nothing about
   them depends on each other. `build_team()` fixes this by rolling all
   of one side's Pokemon concurrently with `asyncio.gather`; the cog
   then also gathers both trainers' `build_team()` calls together, so a
   6v6 does all 12 rolls in parallel instead of one after another.
"""

import asyncio
import random
import re
from typing import Optional

import aiohttp

from .constants import (
    POKEAPI, FALLBACK_MOVE, STATUS_INDUCING_AILMENTS, STAT_KEY_MAP,
    SELF_STAT_LOWERING_MOVES, RECHARGE_MOVES, RECHARGE_MOVE_CHANCE,
    STATUS_MOVE_CHANCE, MOVELESS_FORM_SUFFIXES, _col, type_multiplier,
)
from .engine import BattlePokemon


# ── PokeAPI fetch + Mongo cache ─────────────────────────────────────────────

# Bumped whenever the shape of a cached move document changes (e.g. adding
# stat_changes/drain support). A cached doc whose "_schema" doesn't match
# is treated as a miss and re-fetched — otherwise a move cached under an
# older schema (missing fields a newer feature relies on, like stat drops
# or recoil) would silently keep returning incomplete data forever, since
# get_move_data() would otherwise trust any cache hit unconditionally.
# Bumped to 3 for flinch_chance (Fake Out / Air Slash / ...).
MOVE_CACHE_SCHEMA = 3


async def get_pokemon_data(session: aiohttp.ClientSession, ident: str) -> Optional[dict]:
    key = str(ident).lower().strip().replace(" ", "-")
    doc = await _col().pokedex_cache.find_one({"_id": key})
    if doc:
        return doc
    try:
        async with session.get(f"{POKEAPI}/pokemon/{key}", timeout=10) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        return None

    stats = {s["stat"]["name"]: s["base_stat"] for s in data["stats"]}
    types = [t["type"]["name"] for t in data["types"]]
    move_pool = [m["move"]["name"] for m in data["moves"]]
    sprite = (data.get("sprites") or {}).get("front_default") or ""
    # Non-hidden abilities only, kept as a list — BattlePokemon randomly
    # picks one of these on construction so repeat battles with the same
    # cached species still see natural variety (e.g. a Gyarados that's
    # sometimes Intimidate, sometimes Moxie).
    abilities = [a["ability"]["name"] for a in data.get("abilities", []) if not a.get("is_hidden")]

    doc = {
        "_id": key,
        "name": data["name"],
        "dex_id": data["id"],
        "types": types,
        "stats": stats,
        "move_pool": move_pool,
        "sprite": sprite,
        "abilities": abilities,
    }
    await _col().pokedex_cache.update_one({"_id": key}, {"$set": doc}, upsert=True)
    return doc


async def get_move_data(session: aiohttp.ClientSession, name: str) -> Optional[dict]:
    key = name.lower().strip()
    doc = await _col().move_cache.find_one({"_id": key})
    if doc and doc.get("_schema") == MOVE_CACHE_SCHEMA:
        return doc
    try:
        async with session.get(f"{POKEAPI}/move/{key}", timeout=10) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        return None

    doc = {
        "_id": key,
        "_schema": MOVE_CACHE_SCHEMA,
        "name": data["name"],
        "power": data.get("power"),
        "accuracy": data.get("accuracy"),
        "pp": data.get("pp"),
        "type": data["type"]["name"],
        "damage_class": data["damage_class"]["name"],
        "priority": data.get("priority", 0),
        # Secondary-effect data, used to apply stat drops/boosts and
        # recoil/drain on top of raw damage.
        "effect_chance": data.get("effect_chance"),
        "stat_changes": [
            {"stat": sc["stat"]["name"], "change": sc["change"]}
            for sc in (data.get("stat_changes") or [])
        ],
        "drain": (data.get("meta") or {}).get("drain", 0),
        "target": (data.get("target") or {}).get("name", "selected-pokemon"),
        # Status-ailment data (paralysis/sleep/freeze/burn/poison etc.) —
        # ailment_chance of 0 on a status-class move conventionally means
        # "always applies" (gated only by the move's own accuracy); a
        # nonzero chance is a secondary effect on a damage-dealing move
        # (e.g. Thunderbolt's 10% paralyze).
        "ailment": ((data.get("meta") or {}).get("ailment") or {}).get("name"),
        "ailment_chance": (data.get("meta") or {}).get("ailment_chance", 0),
        # Flinch chance (0-100) — Fake Out reports 100 here, Air Slash 30,
        # most moves 0. Fake Out's "only works turn 1" restriction isn't
        # PokeAPI data and is special-cased by name (see FAKE_OUT_MOVE).
        "flinch_chance": (data.get("meta") or {}).get("flinch_chance", 0),
    }
    await _col().move_cache.update_one({"_id": key}, {"$set": doc}, upsert=True)
    return doc


async def get_move_data_bulk(session: aiohttp.ClientSession, names: list) -> dict:
    """Like get_move_data(), but for a whole list of move names at once.
    Fetches every already-cached move in a SINGLE Mongo query instead of
    one `find_one` per move, then only hits PokeAPI (concurrently) for
    the ones that weren't cached yet. Returns {name: move_dict}."""
    keys = list({n.lower().strip() for n in names})
    if not keys:
        return {}

    found = {}
    async for doc in _col().move_cache.find({"_id": {"$in": keys}, "_schema": MOVE_CACHE_SCHEMA}):
        found[doc["_id"]] = doc

    missing = [k for k in keys if k not in found]
    if missing:
        fetched = await asyncio.gather(
            *[get_move_data(session, name) for name in missing],
            return_exceptions=True,
        )
        for name, mv in zip(missing, fetched):
            if isinstance(mv, dict):
                found[name] = mv

    return found


def _lowers_opponent_stat(move: dict) -> bool:
    """True if this move's stat_changes drop one of the TARGET's stats
    (not the user's own — see SELF_STAT_LOWERING_MOVES for why the
    top-level `target` field alone can't be trusted for damage moves)."""
    if not move.get("stat_changes"):
        return False
    if move.get("target") == "user" or move.get("name") in SELF_STAT_LOWERING_MOVES:
        return False
    return any(sc.get("change", 0) < 0 for sc in move.get("stat_changes", []))


def _lowers_opponent_speed(move: dict) -> bool:
    if not _lowers_opponent_stat(move):
        return False
    return any(STAT_KEY_MAP.get(sc.get("stat")) == "spe" and sc.get("change", 0) < 0
               for sc in move.get("stat_changes", []))


async def pick_moves(session: aiohttp.ClientSession, data: dict, count: int = 4) -> list:
    """Build this Pokemon's `count`-move set from an ordered wishlist,
    filling as many as fit:

    1. Priority — its single best (highest-power) priority move, any type,
       if it learns one (Quick Attack, Aqua Jet, Sucker Punch, ...).
    2. STAB — its best NON-recharge move of each of its own types: 1 slot
       for a mono-type Pokemon, 2 for a dual-type one. Recharge moves
       (Hyper Beam, Giga Impact, Frenzy Plant, ...) are excluded from this
       normal picking entirely — see step 3a below for how they're added
       instead.
    3. Status — a STATUS_MOVE_CHANCE roll (20%); if it hits, its best
       status move (a reliable ailment-inducer like Thunder Wave/Toxic/
       Spore if it learns one, else its best pure stat-changer like Swords
       Dance/Growl).
    3a. Recharge — a Pokemon only ever gets a recharge move if it shares
        that move's type (Frenzy Plant only offered to a Grass-type like
        Sceptile, Hyper Beam only to a Normal-type, etc.), and even then
        only RECHARGE_MOVE_CHANCE (30%) of the time. When it hits, the
        recharge move swaps in for that type's normal STAB pick from step
        2 rather than taking an extra slot — so a Pokemon isn't stuck
        recharging turn after turn, it's a deliberate high-risk option in
        place of its usual same-type move, not an addition to it.
    4. Coverage/debuff — one more move: preferably one that lowers the
       *opponent's* Speed (Icy Wind, Rock Tomb, ...), failing that any
       move that lowers one of the opponent's other stats (Acid, Mud
       Bomb, ...), and only failing that a plain off-type coverage move.
       Recharge moves are never picked here either — only via 3a.

    That's already 4 items for a dual-type Pokemon whose status roll hits
    (priority + 2 STAB + status), so the coverage/debuff slot simply
    doesn't fit that turn — same idea in reverse for a mono-type Pokemon
    with no priority move, which needs the leftover slots backfilled with
    its next-strongest remaining (still non-recharge) moves so the set is
    never short.
    """
    pool = data.get("move_pool", [])
    if not pool:
        tackle = await get_move_data(session, "tackle")
        return [tackle] if tackle else [FALLBACK_MOVE]

    move_map = await get_move_data_bulk(session, pool)
    results = list(move_map.values())
    all_damage_candidates = [
        mv for mv in results
        if mv.get("power") and mv.get("damage_class") != "status"
    ]
    # Recharge moves (Hyper Beam, Giga Impact, ...) are pulled out of the
    # normal damage pool so priority/STAB/coverage/backfill never lean on
    # raw power alone and grab one just because it hits hardest — they're
    # only ever added back in deliberately, via the STAB-swap below.
    recharge_candidates = [mv for mv in all_damage_candidates if mv.get("name") in RECHARGE_MOVES]
    damage_candidates = [mv for mv in all_damage_candidates if mv.get("name") not in RECHARGE_MOVES]
    ailment_status_candidates = [
        mv for mv in results
        if mv.get("damage_class") == "status"
        and mv.get("ailment") in STATUS_INDUCING_AILMENTS
        and not mv.get("ailment_chance")  # only the move's own guaranteed effect, not a % secondary
    ]
    stat_status_candidates = [
        mv for mv in results
        if mv.get("damage_class") == "status" and mv.get("stat_changes")
    ]
    if not all_damage_candidates:
        tackle = await get_move_data(session, "tackle")
        return [tackle] if tackle else [FALLBACK_MOVE]

    own_types = (data.get("types") or [])[:2]
    chosen: list = []

    def _add(mv) -> bool:
        if mv is not None and mv not in chosen and len(chosen) < count:
            chosen.append(mv)
            return True
        return False

    # 1) Priority — best power among any priority > 0 moves it learns.
    priority_pool = [m for m in damage_candidates if m.get("priority", 0) > 0]
    if priority_pool:
        _add(max(priority_pool, key=lambda m: m.get("power") or 0))

    # 2) STAB — best move of each own type (1 slot if mono-type, 2 if
    #    dual-type), skipping a type already covered by the priority pick.
    for t in own_types:
        stab_pool = [m for m in damage_candidates if m.get("type") == t and m not in chosen]
        if stab_pool:
            _add(max(stab_pool, key=lambda m: m.get("power") or 0))

    # 3) Status — STATUS_MOVE_CHANCE roll to add its single best status move.
    if len(chosen) < count and random.random() < STATUS_MOVE_CHANCE:
        best_status = None
        if ailment_status_candidates:
            best_status = max(ailment_status_candidates, key=lambda m: m.get("accuracy") or 0)
        elif stat_status_candidates:
            best_status = max(
                stat_status_candidates,
                key=lambda m: sum(abs(sc.get("change", 0)) for sc in m.get("stat_changes", [])),
            )
        _add(best_status)

    # 3a) Recharge — never an extra slot, only a same-type swap for the
    #     STAB move already chosen in step 2. A Pokemon has to actually
    #     share the recharge move's type (Frenzy Plant <-> a Grass-type
    #     like Sceptile) to be eligible at all, and even then it's only a
    #     RECHARGE_MOVE_CHANCE (30%) roll per type. Missing the roll (or
    #     not learning a matching recharge move) just leaves the normal
    #     no-recharge STAB pick in place.
    for t in own_types:
        type_recharge_pool = [m for m in recharge_candidates if m.get("type") == t]
        if not type_recharge_pool or random.random() >= RECHARGE_MOVE_CHANCE:
            continue
        best_recharge = max(type_recharge_pool, key=lambda m: m.get("power") or 0)
        swap_idx = next(
            (i for i, m in enumerate(chosen)
             if m.get("type") == t and m.get("name") not in RECHARGE_MOVES
             and m.get("priority", 0) <= 0),  # don't clobber the priority pick
            None,
        )
        if swap_idx is not None:
            chosen[swap_idx] = best_recharge
        elif len(chosen) < count:
            chosen.append(best_recharge)

    # 4) Coverage/debuff — prefer a move that lowers the opponent's Speed,
    #    then any move that lowers one of the opponent's other stats,
    #    then fall back to a plain off-type coverage move.
    if len(chosen) < count:
        remaining_damage = [m for m in damage_candidates if m not in chosen]
        speed_debuffs = [m for m in remaining_damage if _lowers_opponent_speed(m)]
        other_debuffs = [m for m in remaining_damage if _lowers_opponent_stat(m)]
        off_type = [m for m in remaining_damage if m.get("type") not in own_types]
        if speed_debuffs:
            _add(max(speed_debuffs, key=lambda m: m.get("power") or 0))
        elif other_debuffs:
            _add(max(other_debuffs, key=lambda m: m.get("power") or 0))
        elif off_type:
            _add(max(off_type, key=lambda m: m.get("power") or 0))

    # 5) Backfill — anything still open (e.g. no priority move, mono-type
    #    with the status flip missing) gets the next-strongest remaining
    #    damage moves so the set is never short of `count`.
    if len(chosen) < count:
        remaining = [m for m in damage_candidates if m not in chosen]
        remaining.sort(key=lambda m: m.get("power") or 0, reverse=True)
        for mv in remaining:
            if len(chosen) >= count:
                break
            chosen.append(mv)

    random.shuffle(chosen)  # don't always surface the wishlist picks in the same order
    return chosen[:count]


# ── Base stat total (BST) filtering for `random` battles ───────────────────
# "BST" here = sum of a Pokemon's 6 base stats (hp/atk/dfn/spa/spd/spe), the
# same number people mean when they say e.g. "Dragonite has 600 BST". This
# has nothing to do with IVs — the cog doesn't model IVs at all; every
# Pokemon battles with the same fixed stat calc (see constants._calc_stat).

BST_FILTER_RE = re.compile(r'([<>])\s*=?\s*(\d+)')
BST_MAX_ROLL_ATTEMPTS = 120  # random dex-id rolls to try before giving up


def parse_bst_filter(raw: Optional[str]):
    """Parse a trailing threshold string like '>550', '<700', or
    '>590<700' into (min_total, max_total). Either or both may end up
    None. Returns (None, None) for a falsy/unrecognized input."""
    if not raw:
        return None, None
    min_total = max_total = None
    for sign, num in BST_FILTER_RE.findall(raw):
        if sign == ">":
            min_total = int(num)
        else:
            max_total = int(num)
    return min_total, max_total


def format_bst_filter(min_total: Optional[int], max_total: Optional[int]) -> str:
    if min_total is None and max_total is None:
        return ""
    lo = min_total if min_total is not None else 0
    hi = max_total if max_total is not None else "∞"
    return f" (BST filter: {lo}–{hi})"


def _bst(stats: dict) -> int:
    return sum(stats.values())


async def _find_cached_bst_match(min_total: Optional[int], max_total: Optional[int]) -> Optional[dict]:
    """Try to grab a random already-cached Pokemon whose BST is in range,
    via a Mongo aggregation, so repeat rolls with the same/overlapping
    filter get fast once the cache has warmed up. Returns None (falls
    back to rolling PokeAPI dex ids) if nothing matches yet."""
    conditions = {}
    if min_total is not None:
        conditions["$gte"] = min_total
    if max_total is not None:
        conditions["$lte"] = max_total
    if not conditions:
        return None
    pipeline = [
        {"$addFields": {"_bst": {"$sum": {"$map": {
            "input": {"$objectToArray": "$stats"}, "as": "s", "in": "$$s.v"
        }}}}},
        {"$match": {"_bst": conditions}},
        {"$sample": {"size": 1}},
    ]
    async for doc in _col().pokedex_cache.aggregate(pipeline):
        return doc
    return None


async def build_random_pokemon(session: aiohttp.ClientSession,
                                min_total: Optional[int] = None,
                                max_total: Optional[int] = None) -> "BattlePokemon":
    if min_total is not None or max_total is not None:
        cached = await _find_cached_bst_match(min_total, max_total)
        if cached:
            moves = await pick_moves(session, cached)
            return BattlePokemon(cached, moves)

        # Nothing cached matches yet — roll random dex ids and check each
        # one's BST, caching every fetch as we go (get_pokemon_data does
        # this automatically) so future rolls find matches i nstantly via
        # the aggregation above.
        last_valid = None
        for _ in range(BST_MAX_ROLL_ATTEMPTS):
            dex_id = random.randint(1, 1025)
            data = await get_pokemon_data(session, str(dex_id))
            if not data:
                continue
            last_valid = data
            total = _bst(data.get("stats", {}))
            if min_total is not None and total < min_total:
                continue
            if max_total is not None and total > max_total:
                continue
            moves = await pick_moves(session, data)
            return BattlePokemon(data, moves)

        # Gave up after BST_MAX_ROLL_ATTEMPTS tries (filter is too narrow
        # for what's been rolled) — fall back to the last valid Pokemon
        # seen rather than erroring out mid battle-build.
        data = last_valid or await get_pokemon_data(session, "pikachu")
        moves = await pick_moves(session, data)
        return BattlePokemon(data, moves)

    for _ in range(6):
        dex_id = random.randint(1, 1025)
        data = await get_pokemon_data(session, str(dex_id))
        if data:
            moves = await pick_moves(session, data)
            return BattlePokemon(data, moves)
    data = await get_pokemon_data(session, "pikachu")
    moves = await pick_moves(session, data)
    return BattlePokemon(data, moves)


async def build_team(session: aiohttp.ClientSession, count: int,
                      min_total: Optional[int] = None,
                      max_total: Optional[int] = None) -> list:
    """Rolls a full `count`-Pokemon team concurrently instead of one Pokemon
    at a time — see the module docstring for why this matters."""
    return list(await asyncio.gather(
        *[build_random_pokemon(session, min_total, max_total) for _ in range(count)]
    ))


# ── AI Gym teams ─────────────────────────────────────────────────────────────
# Building a gym leader's team is a different problem than a random roll: the
# gym has a fixed type theme, and its whole point is to be a genuinely tough,
# tailored matchup for whatever team the challenger just submitted, not a
# random sample of that type. This picks the type's strongest available
# counters to the challenger's team, then leans each one's moveset toward
# hitting that specific team hard instead of just whatever's highest-power.

GYM_TYPE_POOL_ATTEMPTS = 60  # species of the gym's type actually fetched/considered per challenge
GYM_MIN_BST = 450  # gym leaders won't field species below this base stat total (unless the pool is too thin)


async def get_pokemon_of_type(session: aiohttp.ClientSession, type_name: str) -> list:
    """All species names belonging to `type_name`, per PokeAPI's /type
    endpoint - cached in Mongo (this list barely ever changes) so a gym
    challenge doesn't re-fetch it every time."""
    key = f"type:{type_name}"
    doc = await _col().type_pool_cache.find_one({"_id": key})
    if doc:
        # Filter here too (not just at write time) so pools cached before
        # MOVELESS_FORM_SUFFIXES existed don't keep serving Gmax forms.
        return [n for n in doc.get("pokemon", []) if not n.endswith(MOVELESS_FORM_SUFFIXES)]
    try:
        async with session.get(f"{POKEAPI}/type/{type_name}", timeout=15) as r:
            if r.status != 200:
                return []
            data = await r.json()
    except Exception:
        return []
    names = [p["pokemon"]["name"] for p in data.get("pokemon", [])
              if not p["pokemon"]["name"].endswith(MOVELESS_FORM_SUFFIXES)]
    await _col().type_pool_cache.update_one({"_id": key}, {"$set": {"pokemon": names}}, upsert=True)
    return names


def _counter_score(candidate_types: list, candidate_stats: dict, opponent_team: list) -> float:
    """How well a species (by typing + BST) counters a whole opposing team:
    for each opposing Pokemon, the candidate's best offensive type
    multiplier against it, minus the worst multiplier the opponent could
    hit the candidate back with - summed across the team, with BST folded
    in as a much smaller tiebreaker so a genuinely tanky/powerful counter
    is preferred over a frail one with the same typing edge."""
    total = 0.0
    for opp in opponent_team:
        best_hit = max((type_multiplier(t, opp.types) for t in candidate_types), default=1.0)
        worst_taken = max((type_multiplier(ot, candidate_types) for ot in opp.types), default=1.0)
        # Clamp each matchup so one lopsided secondary typing (e.g. Lotad's
        # Water half vs a Fire team) can't dwarf everything else.
        total += max(-2.0, min(2.0, best_hit - worst_taken))
    avg_edge = total / max(len(opponent_team), 1)
    bst = sum(candidate_stats.values()) if candidate_stats else 0
    # BST now dominates (raw value, ~200-720) and typing is a bounded
    # +/-~80 nudge on top. Previously typing was x100 per matchup vs
    # BST/10, so a 220-BST Lotad beat a 520-BST Ogerpon on type alone.
    return bst + avg_edge * 40


async def pick_gym_moves(session: aiohttp.ClientSession, data: dict,
                          opponent_team: list, count: int = 4) -> list:
    """Starts from the normal pick_moves() wishlist (priority/STAB/status
    coverage), then swaps in any learnable move that hits the challenger's
    specific team harder than what's currently in the weakest slot - so a
    gym Pokemon's moves are chosen with the actual opposing team in mind,
    not just raw power. A move only displaces something already picked if
    it's a meaningfully better answer to that team (super effective against
    more/tougher members of it), so priority/STAB slots aren't thrown away
    for a marginal upgrade."""
    base = await pick_moves(session, data, count=count)
    if not opponent_team:
        return base

    opp_types = [t for opp in opponent_team for t in opp.types]

    def coverage_score(move: dict) -> float:
        mtype = move.get("type", "normal")
        power = move.get("power") or 0
        if power <= 0:
            return 0.0
        return sum(type_multiplier(mtype, [t]) for t in opp_types) * power

    move_pool_names = data.get("move_pool", [])
    move_data = await get_move_data_bulk(session, move_pool_names)
    already_have = {m["name"] for m in base}
    coverage_candidates = sorted(
        (m for m in move_data.values()
         if m and m["name"] not in already_have and (m.get("power") or 0) > 0
         and m["name"] not in RECHARGE_MOVES),  # recharge moves only ever come in via pick_moves()'s own rule
        key=coverage_score,
        reverse=True,
    )

    # Priority-move slots are protected from the coverage swap outright —
    # a low-power priority move (Aqua Jet, Quick Attack, ...) usually loses
    # on raw coverage_score to almost anything, but its entire value is
    # going first regardless of Speed. Swapping it for a bigger hit that
    # never gets to fire because the Pokemon is slow defeats the point of
    # having it on a slower gym Pokemon in the first place.
    base_by_weakness = sorted(
        (i for i in range(len(base)) if base[i].get("priority", 0) <= 0),
        key=lambda i: coverage_score(base[i]),
    )
    for slot_i in base_by_weakness:
        if not coverage_candidates:
            break
        best_candidate = coverage_candidates[0]
        # Only swap if the coverage move is a clearly stronger answer to
        # this specific team (1.5x threshold keeps priority/STAB picks
        # from getting bumped for a barely-better option).
        if coverage_score(best_candidate) > coverage_score(base[slot_i]) * 1.5 + 1:
            base[slot_i] = coverage_candidates.pop(0)

    return base


async def _best_gym_counters(session: aiohttp.ClientSession, gym_type: str,
                              opponent_team: list, count: int) -> list:
    """Shortlists species of `gym_type`, scores each by how well its typing
    (+ BST as tiebreak) counters the challenger's whole team, and returns
    the `count` best as raw PokeAPI data dicts."""
    pool_names = await get_pokemon_of_type(session, gym_type)
    if not pool_names:
        pool_names = [gym_type]  # extremely unlikely fallback - keeps a real type from ever coming up empty
    random.shuffle(pool_names)
    to_check = pool_names[:GYM_TYPE_POOL_ATTEMPTS]

    fetched = list(await asyncio.gather(
        *[get_pokemon_data(session, name) for name in to_check], return_exceptions=True
    ))
    candidates = [d for d in fetched
                  if isinstance(d, dict) and gym_type in (d.get("types") or [])
                  and d.get("move_pool")]  # skip battle-only forms with no real moveset (e.g. Gmax)
    if not candidates:
        # Type pool fetch failed outright - fall back to a few random
        # dex rolls filtered to the right type rather than erroring out.
        for _ in range(20):
            data = await get_pokemon_data(session, str(random.randint(1, 1025)))
            if data and gym_type in (data.get("types") or []):
                candidates.append(data)
            if len(candidates) >= count:
                break

    # Gym leaders shouldn't field Lotad-tier Pokemon: drop anything under the
    # BST floor, but only if enough real candidates remain to fill the team.
    strong = [d for d in candidates if sum((d.get("stats") or {}).values()) >= GYM_MIN_BST]
    if len(strong) >= count:
        candidates = strong
    else:
        # Not enough strong ones in the sampled pool: keep the strong ones
        # and top up with the highest-BST remainder instead of the weakest.
        rest = sorted((d for d in candidates if d not in strong),
                      key=lambda d: sum((d.get("stats") or {}).values()), reverse=True)
        candidates = strong + rest

    candidates.sort(
        key=lambda d: _counter_score(d.get("types", []), d.get("stats", {}), opponent_team),
        reverse=True,
    )
    chosen = candidates[:count]
    while len(chosen) < count and candidates:
        chosen.append(random.choice(candidates))
    return chosen


async def build_gym_team(session: aiohttp.ClientSession, gym_type: str,
                          opponent_team: list, count: int) -> list:
    """Builds a full AI gym-leader team: the `count` best `gym_type`
    counters to `opponent_team` (see _best_gym_counters), each equipped
    with a moveset biased toward hitting that specific team hard (see
    pick_gym_moves)."""
    chosen_data = await _best_gym_counters(session, gym_type, opponent_team, count)
    teams = await asyncio.gather(
        *[pick_gym_moves(session, data, opponent_team) for data in chosen_data]
    )
    return [BattlePokemon(data, moves) for data, moves in zip(chosen_data, teams)]


async def retune_moves_against(session: aiohttp.ClientSession,
                                team: list, opponent_team: list) -> None:
    """Re-picks every Pokemon in `team`'s moveset to hit `opponent_team`
    hard, in place. Used for the challenger once the gym leader's team is
    known (the gym team is built from the challenger's team, so the
    challenger's moves can only be tuned afterwards). Reuses
    pick_gym_moves(), so priority moves are protected from the swap and
    STAB/status picks are only displaced by clearly better coverage."""
    async def _retune(mon: BattlePokemon):
        data = await get_pokemon_data(session, mon.name)
        if not data or not data.get("move_pool"):
            return  # keep the existing moves rather than risk an empty set
        moves = await pick_gym_moves(session, data, opponent_team)
        if not moves:
            return
        for mv in moves:
            mv.setdefault("current_pp", mv.get("pp") or 5)
        mon.moves = moves

    await asyncio.gather(*[_retune(m) for m in team], return_exceptions=True)

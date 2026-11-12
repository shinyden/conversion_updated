"""
cogs/battle/render.py
────────────────────────
Battle scene image rendering (Pillow) — background presets, sprite
fetching, the HP-bar overlay, plus image "cards" (gym leader quotes,
battle intro, text banners) that replace crowded text messages.
"""

import io
import logging
import random
import re
from typing import Optional, Sequence, Tuple, TYPE_CHECKING

import aiohttp
import discord

from .constants import (
    PIL_OK, LEVEL, CANVAS_W, CANVAS_H, SPRITE_SCALE, STAT_DISPLAY,
    STATUS_ABBR, STATUS_BADGE_COLOR, _stage_multiplier,
)

if PIL_OK:
    from PIL import Image, ImageDraw, ImageFont, ImageOps

if TYPE_CHECKING:
    from .engine import BattlePokemon


BACKGROUNDS = [
    dict(name="Spring Day",   sky=(135, 206, 235, 255), ground=(140, 200, 90, 255),
         patch=(160, 215, 110, 255), time="day",   season="spring"),
    dict(name="Spring Night", sky=(28, 38, 78, 255),    ground=(60, 85, 55, 255),
         patch=(75, 100, 68, 255),  time="night", season="spring"),
    dict(name="Summer Day",   sky=(90, 175, 240, 255),  ground=(120, 190, 70, 255),
         patch=(140, 205, 90, 255), time="day",   season="summer"),
    dict(name="Summer Night", sky=(18, 28, 66, 255),    ground=(45, 75, 48, 255),
         patch=(58, 90, 58, 255),   time="night", season="summer"),
    dict(name="Autumn Day",   sky=(180, 190, 210, 255), ground=(190, 140, 70, 255),
         patch=(205, 155, 85, 255), time="day",   season="autumn"),
    dict(name="Autumn Night", sky=(32, 32, 52, 255),    ground=(85, 65, 42, 255),
         patch=(98, 78, 52, 255),   time="night", season="autumn"),
    dict(name="Winter Day",   sky=(205, 222, 235, 255), ground=(232, 238, 245, 255),
         patch=(245, 248, 252, 255), time="day",  season="winter"),
    dict(name="Winter Night", sky=(12, 18, 42, 255),    ground=(195, 202, 215, 255),
         patch=(212, 218, 228, 255), time="night", season="winter"),
]


def pick_background() -> dict:
    """Roll a random season/time-of-day background preset for a battle.
    Stamps a fresh random seed onto the copy so the scattered decorations
    (stars/snow/leaves/flowers) are stable for every turn of *this* battle
    but still vary from the next battle that rolls the same preset."""
    preset = dict(random.choice(BACKGROUNDS))
    preset["seed"] = random.randint(0, 1_000_000_000)
    return preset


_log = logging.getLogger(__name__)

_FONT_CACHE: dict = {}


def _font(size: int, bold: bool = True):
    if not PIL_OK:
        return None
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for name in (("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
                 ("Arial Bold.ttf" if bold else "Arial.ttf")):
        try:
            f = ImageFont.truetype(name, size)
            _FONT_CACHE[key] = f
            return f
        except Exception:
            continue
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def _mix(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)) + (255,)


def _draw_sparkle(draw, x, y, s, fill):
    k = s * 0.3
    draw.polygon([(x, y - s), (x + k, y - k), (x + s, y), (x + k, y + k),
                  (x, y + s), (x - k, y + k), (x - s, y), (x - k, y - k)], fill=fill)


def _draw_scene_base(draw, bg, seed):
    """Gradient sky, soft clouds (day) and a gradient ground with a horizon line."""
    horizon = CANVAS_H - 120
    top = _mix(bg["sky"], (0, 0, 0), 0.18)
    bottom = _mix(bg["sky"], (255, 255, 255) if bg["time"] == "day" else (70, 90, 150), 0.28)
    for y in range(horizon):
        draw.line([(0, y), (CANVAS_W, y)], fill=_mix(top, bottom, y / max(horizon, 1)))
    if bg["time"] == "day":
        rng = random.Random(seed + 7)
        for _ in range(4):
            cx, cy = rng.randint(40, CANVAS_W - 80), rng.randint(20, 110)
            for dx, dy, r in ((0, 0, 18), (20, 4, 14), (-20, 5, 13), (8, -8, 12)):
                draw.ellipse([cx + dx - r, cy + dy - r // 2, cx + dx + r, cy + dy + r // 2],
                             fill=(255, 255, 255, 210))


def _draw_ground(draw, bg):
    horizon = CANVAS_H - 120
    far = _mix(bg["ground"], (255, 255, 255), 0.08)
    near = _mix(bg["ground"], (0, 0, 0), 0.18)
    for y in range(horizon, CANVAS_H):
        draw.line([(0, y), (CANVAS_W, y)], fill=_mix(far, near, (y - horizon) / 120))
    draw.line([(0, horizon), (CANVAS_W, horizon)], fill=_mix(bg["ground"], (0, 0, 0), 0.35), width=2)


def _hp_color(frac: float):
    if frac > 0.5:
        return (60, 200, 70, 255)
    if frac > 0.2:
        return (240, 190, 40, 255)
    return (220, 60, 60, 255)


async def _fetch_sprite(session: aiohttp.ClientSession, url: str):
    if not url or not PIL_OK:
        return None
    try:
        async with session.get(url, timeout=10) as r:
            if r.status != 200:
                return None
            raw = await r.read()
        return Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None


def _normal_sprite_url(url: str) -> Optional[str]:
    """Derive the non-shiny URL from a shiny sprite URL (PokeAPI-style
    '/shiny/' folders, Showdown-style '-shiny' folders, or '_shiny' suffixes).
    Returns None if the URL doesn't look shiny."""
    if not url:
        return None
    for old, new in (("/shiny/", "/"), ("-shiny/", "/"), ("_shiny", "")):
        if old in url:
            return url.replace(old, new, 1)
    return None


async def _fetch_sprite_with_fallback(session: aiohttp.ClientSession, url: str):
    """Fetch a sprite; if it's a shiny URL that doesn't exist (404 / bad
    image), retry with the normal sprite so the Pokemon never renders blank.
    Returns (image_or_None, used_fallback: bool)."""
    img = await _fetch_sprite(session, url)
    if img is not None:
        return img, False
    normal = _normal_sprite_url(url)
    if normal:
        img = await _fetch_sprite(session, normal)
        if img is not None:
            return img, True
    return None, False


def _draw_hp_bar_above(draw: "ImageDraw.ImageDraw", center_x: float, sprite_top_y: float,
                        name: str, hp: int, max_hp: int, stat_stages: Optional[dict] = None,
                        status: Optional[str] = None, width: int = 190, shiny: bool = False):
    """Draws a compact name+HP-bar plate directly above a sprite's position.
    If `stat_stages` has any non-zero entries, a row of small chips is added
    below the HP bar showing each affected stat's current multiplier —
    green and >1x for a boost (e.g. "ATK 2x"), red and <1x for a drop
    (e.g. "SPE 0.67x") — so a stat change is visible at a glance instead of
    only appearing in the turn's text log. If `status` is a major status
    ailment (burn/paralysis/poison/sleep/freeze), a small coloured badge
    (BRN/PAR/PSN/SLP/FRZ — same convention as the mainline games' status
    icons) is drawn in the plate's top-right corner."""
    bar_h = 10
    chip_h = 15
    active_stages = sorted((k, v) for k, v in (stat_stages or {}).items() if v)
    plate_h = 54 + (chip_h + 5 if active_stages else 0)
    x = int(center_x - width / 2)
    x = max(6, min(x, CANVAS_W - width - 6))
    y = int(max(4, sprite_top_y - plate_h - 6))

    draw.rounded_rectangle([x, y, x + width, y + plate_h], radius=9,
                            fill=(255, 255, 255, 235),
                            outline=((230, 170, 20, 255) if shiny else (40, 40, 40, 255)), width=3 if shiny else 2)
    draw.text((x + 10, y + 4), f"{'★ ' if shiny else ''}{name.title()}  Lv.{LEVEL}",
               font=_font(14), fill=(20, 20, 20, 255))

    if status:
        abbr = STATUS_ABBR.get(status, status[:3].upper())
        color = STATUS_BADGE_COLOR.get(status, (110, 110, 110, 235))
        badge_h = 16
        tb = draw.textbbox((0, 0), abbr, font=_font(11))
        badge_w = (tb[2] - tb[0]) + 10
        bx = x + width - 8 - badge_w
        by = y + 4
        draw.rounded_rectangle([bx, by, bx + badge_w, by + badge_h], radius=5, fill=color)
        draw.text((bx + 5, by + 2), abbr, font=_font(11), fill=(255, 255, 255, 255))

    bar_x, bar_y, bar_w = x + 10, y + 22, width - 20
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
                            radius=5, fill=(90, 90, 90, 255))
    frac = max(hp, 0) / max_hp if max_hp else 0
    fill_w = int(bar_w * frac)
    if fill_w > 0:
        draw.rounded_rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h],
                                radius=5, fill=_hp_color(frac))

    hp_txt = f"HP {max(hp, 0)}/{max_hp}"
    tb = draw.textbbox((0, 0), hp_txt, font=_font(11, bold=False))
    draw.text((x + width - 10 - (tb[2] - tb[0]), bar_y + bar_h + 3), hp_txt,
               font=_font(11, bold=False), fill=(60, 60, 60, 255))

    if not active_stages:
        return
    chip_y = bar_y + bar_h + 20
    cx = bar_x
    right_edge = x + width - 6
    for key, stage in active_stages:
        mult = _stage_multiplier(stage)
        label = f"{STAT_DISPLAY.get(key, key.upper())} {mult:g}x"
        color = (55, 165, 90, 235) if stage > 0 else (205, 65, 65, 235)
        tb = draw.textbbox((0, 0), label, font=_font(10))
        chip_w = (tb[2] - tb[0]) + 8
        if cx + chip_w > right_edge:
            break  # rare (3+ stats changed at once) — plate just isn't wide enough for more
        draw.rounded_rectangle([cx, chip_y, cx + chip_w, chip_y + chip_h],
                                radius=5, fill=color)
        draw.text((cx + 4, chip_y + 1), label, font=_font(10), fill=(255, 255, 255, 255))
        cx += chip_w + 3


def _draw_background_flair(draw: "ImageDraw.ImageDraw", bg: dict, seed: int):
    """Season/time-of-day decorations layered on top of the sky+ground fill.
    `seed` keeps the scattered decorations (stars, snow, leaves...) stable
    across every render call for a given battle instead of jittering every
    turn, while still varying scene-to-scene."""
    rng = random.Random(seed)

    if bg["time"] == "night":
        # Moon, upper-left, plus a scatter of stars across the sky.
        mx, my, mr = int(CANVAS_W * 0.13), 46, 22
        draw.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=(240, 240, 225, 255))
        draw.ellipse([mx - mr + 10, my - mr - 4, mx + mr + 10, my + mr - 4], fill=bg["sky"])
        for _ in range(28):
            sx = rng.randint(0, CANVAS_W)
            sy = rng.randint(0, CANVAS_H - 130)
            s = rng.choice((1, 1, 2))
            draw.ellipse([sx, sy, sx + s, sy + s], fill=(255, 255, 255, 220))
    else:
        # Sun, upper-right.
        sx, sy, sr = int(CANVAS_W * 0.88), 44, 26
        draw.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(255, 235, 120, 255))

    if bg["season"] == "winter":
        for _ in range(40):
            fx = rng.randint(0, CANVAS_W)
            fy = rng.randint(CANVAS_H - 118, CANVAS_H - 4)
            s = rng.choice((2, 2, 3))
            draw.ellipse([fx, fy, fx + s, fy + s], fill=(255, 255, 255, 235))
    elif bg["season"] == "autumn":
        leaf_colors = [(200, 110, 40, 255), (215, 150, 40, 255), (170, 70, 30, 255)]
        for _ in range(22):
            fx = rng.randint(0, CANVAS_W)
            fy = rng.randint(CANVAS_H - 118, CANVAS_H - 4)
            draw.ellipse([fx, fy, fx + 4, fy + 4], fill=rng.choice(leaf_colors))
    elif bg["season"] == "spring":
        flower_colors = [(255, 255, 255, 255), (255, 200, 220, 255), (255, 230, 120, 255)]
        for _ in range(18):
            fx = rng.randint(0, CANVAS_W)
            fy = rng.randint(CANVAS_H - 118, CANVAS_H - 4)
            draw.ellipse([fx, fy, fx + 5, fy + 5], fill=rng.choice(flower_colors))


async def render_battle_scene(session: aiohttp.ClientSession,
                               opponent_pokemon: "BattlePokemon",
                               player_pokemon: "BattlePokemon",
                               background: Optional[dict] = None) -> Optional[discord.File]:
    """Classic side-on battle scene: opponent upper-right facing player,
    player's pokemon lower-left (mirrored) facing opponent, with an HP bar
    rendered directly above each sprite (name, Lv.100, colour-shifting bar).
    `background` is one of the presets in BACKGROUNDS (a season/time-of-day
    combo); if omitted, one is rolled on the spot.

    If a shiny sprite URL doesn't exist, the normal sprite is used instead
    (the shiny star/sparkles are still drawn so the Pokemon reads as shiny)."""
    if not PIL_OK:
        return None

    bg = background or pick_background()

    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), bg["sky"])
    draw = ImageDraw.Draw(canvas)
    _draw_scene_base(draw, bg, seed=bg.get("seed", 0))
    _draw_background_flair(draw, bg, seed=bg.get("seed", 0))
    _draw_ground(draw, bg)
    draw.ellipse([CANVAS_W * 0.55 - 150, CANVAS_H - 150, CANVAS_W * 0.55 + 150, CANVAS_H - 90],
                 fill=bg["patch"])
    draw.ellipse([CANVAS_W * 0.14 - 120, CANVAS_H - 95, CANVAS_W * 0.14 + 120, CANVAS_H - 45],
                 fill=bg["patch"])

    opp_sprite, _ = await _fetch_sprite_with_fallback(session, opponent_pokemon.sprite)
    player_sprite, _ = await _fetch_sprite_with_fallback(session, player_pokemon.sprite)

    opp_pos = (int(CANVAS_W * 0.60), 68)
    player_pos = (int(CANVAS_W * 0.06), CANVAS_H - 232)

    if opp_sprite:
        opp_sprite = opp_sprite.resize(
            (opp_sprite.width * SPRITE_SCALE, opp_sprite.height * SPRITE_SCALE), Image.NEAREST)
    if player_sprite:
        player_sprite = ImageOps.mirror(player_sprite)
        player_sprite = player_sprite.resize(
            (player_sprite.width * SPRITE_SCALE, player_sprite.height * SPRITE_SCALE), Image.NEAREST)
    opp_w = opp_sprite.width if opp_sprite else 96 * SPRITE_SCALE
    opp_h = opp_sprite.height if opp_sprite else 96 * SPRITE_SCALE
    player_w = player_sprite.width if player_sprite else 96 * SPRITE_SCALE
    player_h = player_sprite.height if player_sprite else 96 * SPRITE_SCALE

    # Soft drop-shadow ellipses under each Pokemon so they sit on the ground.
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for (px, py), w, h in ((opp_pos, opp_w, opp_h), (player_pos, player_w, player_h)):
        cx, cy = px + w / 2, py + h - 14
        sd.ellipse([cx - w * 0.38, cy - 12, cx + w * 0.38, cy + 14], fill=(0, 0, 0, 80))
        sd.ellipse([cx - w * 0.26, cy - 7, cx + w * 0.26, cy + 9], fill=(0, 0, 0, 60))
    canvas = Image.alpha_composite(canvas, shadow)
    draw = ImageDraw.Draw(canvas)

    if opp_sprite:
        canvas.alpha_composite(opp_sprite, opp_pos)
    if player_sprite:
        canvas.alpha_composite(player_sprite, player_pos)

    # Shiny sparkles around any shiny Pokemon.
    srng = random.Random(bg.get("seed", 0) + 99)
    for mon, (px, py), w, h in ((opponent_pokemon, opp_pos, opp_w, opp_h),
                                (player_pokemon, player_pos, player_w, player_h)):
        if getattr(mon, "shiny", False):
            for _ in range(7):
                sx = px + srng.randint(0, int(w))
                sy = py + srng.randint(0, int(h))
                _draw_sparkle(draw, sx, sy, srng.choice((5, 7, 9)),
                              srng.choice(((255, 235, 120, 255), (255, 255, 255, 255))))

    _draw_hp_bar_above(draw, opp_pos[0] + opp_w / 2, opp_pos[1],
                        opponent_pokemon.name, opponent_pokemon.hp, opponent_pokemon.max_hp,
                        stat_stages=opponent_pokemon.stat_stages, status=opponent_pokemon.status,
                        shiny=getattr(opponent_pokemon, 'shiny', False))
    _draw_hp_bar_above(draw, player_pos[0] + player_w / 2, player_pos[1],
                        player_pokemon.name, player_pokemon.hp, player_pokemon.max_hp,
                        stat_stages=player_pokemon.stat_stages, status=player_pokemon.status,
                        shiny=getattr(player_pokemon, 'shiny', False))

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="battle.png")


# ══════════════════════════════════════════════════════════════════════════
#  IMAGE CARDS — gym leader quotes, battle intro, text banners
# ══════════════════════════════════════════════════════════════════════════

TRAINER_SPRITE_URL = "https://play.pokemonshowdown.com/sprites/trainers/{slug}.png"

# Display names that don't slugify to the Showdown file name.
_TRAINER_SLUG_ALIASES = {
    "ltsurge": "ltsurge", "lt.surge": "ltsurge", "ltsurge ": "ltsurge",
    "tate&liza": "tateandliza", "tateandliza": "tateandliza",
    "crasherwake": "crasherwake", "crasher wake": "crasherwake",
}

_TRAINER_SPRITE_CACHE: dict = {}

# Accent colours per Pokemon type, used for the card's border/name plate
# when a gym leader's type is passed in.
TYPE_COLORS = {
    "normal": (168, 168, 120), "fire": (240, 128, 48), "water": (104, 144, 240),
    "electric": (248, 208, 48), "grass": (120, 200, 80), "ice": (152, 216, 216),
    "fighting": (192, 48, 40), "poison": (160, 64, 160), "ground": (224, 192, 104),
    "flying": (168, 144, 240), "psychic": (248, 88, 136), "bug": (168, 184, 32),
    "rock": (184, 160, 56), "ghost": (112, 88, 152), "dragon": (112, 56, 248),
    "dark": (112, 88, 72), "steel": (184, 184, 208), "fairy": (238, 153, 172),
}


def _trainer_slug(name: str) -> str:
    key = re.sub(r"\s+", "", name.lower())
    if key in _TRAINER_SLUG_ALIASES:
        return _TRAINER_SLUG_ALIASES[key]
    return re.sub(r"[^a-z0-9]", "", name.lower())


async def fetch_trainer_sprite(session: aiohttp.ClientSession, name: str):
    """Fetch a gym leader / trainer sprite by name from Pokemon Showdown's
    trainer sprite set (e.g. 'Brock', 'Lt. Surge'). Cached per slug.
    Returns an RGBA Image, or None if it can't be fetched (callers then draw
    a placeholder badge, so the card never breaks)."""
    if not PIL_OK:
        return None
    slug = _trainer_slug(name)
    if not slug:
        return None
    if _TRAINER_SPRITE_CACHE.get(slug) is not None:
        return _TRAINER_SPRITE_CACHE[slug]
    url = TRAINER_SPRITE_URL.format(slug=slug)
    img = await _fetch_sprite(session, url)
    if img is None:
        _log.warning("Trainer sprite not found: %r -> %s", name, url)
    else:
        _TRAINER_SPRITE_CACHE[slug] = img  # only cache successes
    return img


_GENERIC_WORDS = re.compile(r"\b(gym|leader|elite|four|champion|the|trainer|of|city|town)\b", re.I)


def _name_candidates(*names: str) -> list:
    """Extra lookup names from titles like 'Pewter City Gym Leader Brock':
    the raw name, then the name with generic words removed, then each
    remaining word on its own (so 'Brock' is found inside a long title)."""
    out = []
    for n in names:
        if not n:
            continue
        out.append(n)
        stripped = _GENERIC_WORDS.sub(" ", n).strip()
        if stripped and stripped != n:
            out.append(stripped)
            out.extend(stripped.split())
    seen, uniq = set(), []
    for c in out:
        if c.lower() not in seen:
            seen.add(c.lower())
            uniq.append(c)
    return uniq


def _wrap_text(draw, text: str, font, max_w: int) -> list:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _draw_chip(draw, x: int, y: int, label: str, color, font_size: int = 12) -> int:
    """Draw a rounded pill; returns its width so callers can lay out a row."""
    f = _font(font_size)
    tb = draw.textbbox((0, 0), label, font=f)
    w, h = (tb[2] - tb[0]) + 16, font_size + 10
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=color + (255,) if len(color) == 3 else color)
    draw.text((x + 8, y + 4), label, font=f, fill=(255, 255, 255, 255))
    return w


def _card_background(width: int, height: int, bg: Optional[dict]) -> "Image.Image":
    """Sky-to-ground gradient card background reusing the battle presets, so
    cards visually match the battle scene."""
    bg = bg or pick_background()
    img = Image.new("RGBA", (width, height), bg["sky"])
    d = ImageDraw.Draw(img)
    top = _mix(bg["sky"], (0, 0, 0), 0.25)
    bot = _mix(bg["ground"], (0, 0, 0), 0.10)
    for y in range(height):
        d.line([(0, y), (width, y)], fill=_mix(top, bot, y / max(height - 1, 1)))
    rng = random.Random(bg.get("seed", 0))
    if bg["time"] == "night":
        for _ in range(30):
            sx, sy = rng.randint(0, width), rng.randint(0, height // 2)
            d.ellipse([sx, sy, sx + 2, sy + 2], fill=(255, 255, 255, 200))
    return img


async def render_quote_card(session: aiohttp.ClientSession,
                            speaker: str,
                            quote: str,
                            title: Optional[str] = None,
                            accent_type: Optional[str] = None,
                            chips: Sequence[Tuple[str, Tuple[int, ...]]] = (),
                            background: Optional[dict] = None,
                            filename: str = "quote.png",
                            sprite_names: Sequence[str] = ()) -> Optional[discord.File]:
    """Image card: trainer sprite on the left, speech bubble with the quote on
    the right, name plate underneath, and an optional row of info chips
    (difficulty, shiny, weather, ...) along the bottom.

        file = await render_quote_card(
            session, "Brock", "My rock-hard willpower is evident even in my Pokémon!",
            title="Pewter City Gym Leader", accent_type="rock",
            chips=[("Hard", (200, 80, 60)), ("★ Shiny", (230, 170, 20))])
        await ctx.send(file=file)

    `speaker` is used to look up the sprite (Showdown trainer sprites).
    If the sprite can't be fetched, a circular initial badge is drawn instead.
    """
    if not PIL_OK:
        return None

    accent = TYPE_COLORS.get((accent_type or "").lower(), (230, 170, 20))
    W = CANVAS_W
    quote_font = _font(17, bold=False)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    bubble_x, bubble_w = 190, W - 190 - 24
    lines = _wrap_text(probe, f"“{quote}”", quote_font, bubble_w - 36)
    bubble_h = max(96, 24 + len(lines) * 24)
    chips_h = 34 if chips else 0
    H = max(210, 36 + bubble_h + 24 + chips_h + 16)

    canvas = _card_background(W, H, background)
    draw = ImageDraw.Draw(canvas)

    # Border in the accent colour.
    draw.rounded_rectangle([3, 3, W - 4, H - 4], radius=16, outline=accent + (255,), width=4)

    # Trainer sprite (scaled up, bottom-left) or fallback badge.
    sprite = None
    for cand in _name_candidates(*sprite_names, speaker):
        sprite = await fetch_trainer_sprite(session, cand)
        if sprite is not None:
            break
    sprite_cx, sprite_base = 95, H - (chips_h + 40 if chips else 52)
    if sprite:
        scale = max(1, min(3, (H - 90) // max(sprite.height, 1)))
        sprite = sprite.resize((sprite.width * scale, sprite.height * scale), Image.NEAREST)
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).ellipse(
            [sprite_cx - sprite.width * 0.4, sprite_base - 8, sprite_cx + sprite.width * 0.4, sprite_base + 10],
            fill=(0, 0, 0, 90))
        canvas = Image.alpha_composite(canvas, shadow)
        canvas.alpha_composite(sprite, (int(sprite_cx - sprite.width / 2), int(sprite_base - sprite.height)))
        draw = ImageDraw.Draw(canvas)
    else:
        r = 48
        draw.ellipse([sprite_cx - r, sprite_base - 2 * r, sprite_cx + r, sprite_base], fill=accent + (255,),
                     outline=(255, 255, 255, 255), width=3)
        initial = (speaker[:1] or "?").upper()
        tb = draw.textbbox((0, 0), initial, font=_font(44))
        draw.text((sprite_cx - (tb[2] - tb[0]) / 2, sprite_base - r - (tb[3] - tb[1]) / 2 - 6),
                  initial, font=_font(44), fill=(255, 255, 255, 255))

    # Speech bubble with a little tail pointing at the speaker.
    by = 22
    draw.rounded_rectangle([bubble_x, by, bubble_x + bubble_w, by + bubble_h], radius=14,
                           fill=(255, 255, 255, 240), outline=(40, 40, 40, 255), width=2)
    ty = by + bubble_h // 2 - 8
    draw.polygon([(bubble_x, ty), (bubble_x - 16, ty + 8), (bubble_x, ty + 18)],
                 fill=(255, 255, 255, 240), outline=(40, 40, 40, 255))
    draw.line([(bubble_x, ty + 1), (bubble_x, ty + 17)], fill=(255, 255, 255, 240), width=3)
    text_y = by + (bubble_h - len(lines) * 24) // 2
    for i, line in enumerate(lines):
        draw.text((bubble_x + 18, text_y + i * 24), line, font=quote_font, fill=(25, 25, 25, 255))

    # Name plate under the bubble, aligned with the sprite's feet.
    plate_y = by + bubble_h + 10
    draw.rounded_rectangle([bubble_x, plate_y, bubble_x + bubble_w, plate_y + 40], radius=10,
                           fill=accent + (255,))
    draw.text((bubble_x + 14, plate_y + 3), speaker, font=_font(17), fill=(255, 255, 255, 255))
    if title:
        draw.text((bubble_x + 14, plate_y + 22), title, font=_font(11, bold=False), fill=(255, 255, 255, 235))

    # Info chips row (difficulty, shiny, etc.).
    if chips:
        cx, cy = 20, H - chips_h - 10
        for label, color in chips:
            cx += _draw_chip(draw, cx, cy, label, tuple(color)) + 8
            if cx > W - 40:
                break

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename=filename)


async def render_text_banner(text: str,
                             subtitle: Optional[str] = None,
                             color: Tuple[int, int, int] = (230, 170, 20),
                             chips: Sequence[Tuple[str, Tuple[int, ...]]] = (),
                             background: Optional[dict] = None,
                             filename: str = "banner.png") -> Optional[discord.File]:
    """Generic text-as-image banner for any message that would otherwise be a
    plain text line: 'Victory!', 'Battle Start', 'Pikachu fainted!',
    'Turn 3', 'Level Up' ... A big title, optional subtitle and chip row on a
    season/time themed gradient."""
    if not PIL_OK:
        return None
    W = CANVAS_W
    H = 100 + (24 if subtitle else 0) + (34 if chips else 0)
    canvas = _card_background(W, H, background)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle([3, 3, W - 4, H - 4], radius=16, outline=color + (255,), width=4)

    title_font = _font(34)
    tw = draw.textlength(text, font=title_font)
    ty = 18
    # Soft shadow for legibility on any background.
    draw.text(((W - tw) / 2 + 2, ty + 2), text, font=title_font, fill=(0, 0, 0, 160))
    draw.text(((W - tw) / 2, ty), text, font=title_font, fill=(255, 255, 255, 255))
    y = ty + 48
    if subtitle:
        sf = _font(15, bold=False)
        sw = draw.textlength(subtitle, font=sf)
        draw.text(((W - sw) / 2, y), subtitle, font=sf, fill=(245, 245, 245, 255))
        y += 26
    if chips:
        widths = [draw.textlength(lbl, font=_font(12)) + 16 + 8 for lbl, _ in chips]
        cx = (W - sum(widths) + 8) / 2
        for (label, col), w in zip(chips, widths):
            _draw_chip(draw, int(cx), y + 2, label, tuple(col))
            cx += w

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename=filename)

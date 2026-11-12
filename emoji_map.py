"""
Emoji replacement index used by the converter cog.

Whenever the converter builds an embed, every emoji found in the text is looked up
here and swapped for the replacement you list.

Keys (any mix):
  int  -> custom emoji ID      123456789012345678: "<:new:987654321098765432>"
  str  -> custom emoji NAME    "pikachu": "<:pika:987654321098765432>"   (matches any <:pikachu:ID>)
  str  -> unicode emoji        "🔥": "<:fire:987654321098765432>"

Values: the exact text to insert (for animated emojis use the <a:name:id> form).
ID keys win over name keys. Restart the bot (or reload this module) after editing.
"""
from __future__ import annotations

import re

EMOJI_MAP: dict[int | str, str] = {
        1566317635778383872: "<:halloween_2026_sunlit:1569739212872228955>",  # old :halloween_2026_sunlit: -> new :halloween_2026_sunlit:
    1566317628807446589: "<:halloween_2026_phantom:1569739213996302398>",  # old :halloween_2026_phantom: -> new :halloween_2026_phantom:
    980747297409351680: "<:__emoji:1569739215166775389>",  # old :_: -> new :__emoji:
    980747297338052638: "<:__emoji:1569739216496238665>",  # old :_: -> new :__emoji:
    980747297308704809: "<:__emoji:1569739218148655246>",  # old :_: -> new :__emoji:
    980747297400963072: "<:__emoji:1569739219855999047>",  # old :_: -> new :__emoji:
    980747297291903017: "<:__emoji:1569739221155971235>",  # old :_: -> new :__emoji:
    980747297329651802: "<:__emoji:1569739222787563691>",  # old :_: -> new :__emoji:
    980747297396768788: "<:__emoji:1569739223718957159>",  # old :_: -> new :__emoji:
    1566317639486152794: "<:halloween_2026_sunlit_white:1569739225455136819>",  # old :halloween_2026_sunlit_white: -> new :halloween_2026_sunlit_white:
    1566317638047633470: "<:halloween_2026_sunlit_gray:1569739226499776659>",  # old :halloween_2026_sunlit_gray: -> new :halloween_2026_sunlit_gray:
    980747297119957003: "<:__emoji:1569739228651196496>",  # old :_: -> new :__emoji:
    1566317631357849620: "<:halloween_2026_phantom_white:1569739230672986274>",  # old :halloween_2026_phantom_white: -> new :halloween_2026_phantom_white:
    980747297409351681: "<:__emoji:1569739232220807261>", 
      1566317629814083604: "<:halloween_2026_phantom_gray:1569739234204459168>",  # old :halloween_2026_phantom_gray: -> new :halloween_2026_phantom_gray:
    980747297304502342: "<:__emoji:1569739235865395210>",  # old :_: -> new :__emoji:
    980747297338052608: "<:__emoji:1569739237299986589>",  # old :_: -> new :__emoji:
    980747297350619158: "<:__emoji:1569739238600081540>",  # old :_: -> new :__emoji:
    980747297317068851: "<:__emoji:1569739240244252793>",  # old :_: -> new :__emoji:
    1566317626706104330: "<:__emoji:1569739241334767712>",  # old :_: -> new :__emoji:
    1566317627721383996: "<:__emoji:1569739243389984921>", 
      1420713960465760357: "<:missingno:1569740655503212639>",  # old :missingno: -> new :missingno:
    1207995667788726272: "<:unknown:1569740657201913898>", 
      1207734081585152101: "<:male:1569740367929147464>",
      1207734084210532483: "<:female:1569740304783777852>",  # old :female: -> new :female:# old :male: -> new :male:# old :unknown: -> new :unknown:# old :_: -> new :__emoji:# old :_: -> new :__emoji:# 123456789012345678: "<:replacement:987654321098765432>",
    # "old_name": "<:replacement:987654321098765432>",
    # "🔥": "<:fire:987654321098765432>",
}

_CUSTOM_RE = re.compile(r"<a?:(\w+):(\d+)>")
_NAME_RE = re.compile(r"[A-Za-z0-9_]{2,32}")


def replace_emojis(text: str) -> str:
    """Return `text` with every emoji listed in EMOJI_MAP replaced."""
    if not text or not EMOJI_MAP:
        return text

    by_id = {k: v for k, v in EMOJI_MAP.items() if isinstance(k, int)}
    by_name = {k: v for k, v in EMOJI_MAP.items() if isinstance(k, str) and _NAME_RE.fullmatch(k)}
    unicode_map = {k: v for k, v in EMOJI_MAP.items() if isinstance(k, str) and k not in by_name}

    def _custom(m: re.Match) -> str:
        return by_id.get(int(m.group(2))) or by_name.get(m.group(1)) or m.group(0)

    text = _CUSTOM_RE.sub(_custom, text)

    if unicode_map:
        pattern = "|".join(re.escape(k) for k in sorted(unicode_map, key=len, reverse=True))
        text = re.sub(pattern, lambda m: unicode_map[m.group(0)], text)
    return text

from __future__ import annotations

import re

from .textutil import norm

GLUE_RULE = re.compile(
    r"これにより|これによって|こうすることで|こうすれば|そうすれば|おかげで|そのため、?[^。]{0,30}(できます|なります)|"
    r"という点で(便利|助か|嬉し|うれし|有利|安心)|便利です|便利だ|安心です|安心だ|役立ちます|役に立ちます|役に立つ|"
    r"^つまり|^すなわち|ということです。?$|というわけです|できるようになります|手間が省け|楽になります|楽になる|"
    r"効率(的|よく)|メリットがあります|メリットです|ポイントです|おすすめです|嬉しいところ|うれしいところ|気にせず使え|"
    r"迷わず|困りません|心配ありません")

TRACE_N = 4
TRACE_MIN = 0.5


def _grams(text: str) -> set[str]:
    t = norm(text)
    return {t[i:i + TRACE_N] for i in range(len(t) - TRACE_N + 1)}


def traceable(sentence: str, material: set[str]) -> bool:
    g = _grams(GLUE_RULE.sub("", sentence))
    return bool(g) and len(g & material) / len(g) >= TRACE_MIN


def material_grams(material: list[str]) -> set[str]:
    return set().union(*(_grams(t) for t in material)) if material else set()

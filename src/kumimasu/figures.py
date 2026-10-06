from __future__ import annotations

import re

from .textutil import blocks, code_ranges, excerpt, overlaps

FIGURE = re.compile(r"<!--\s*図\s*[:：]\s*(.*?)\s*-->", re.DOTALL)
NEAR_CHARS = 60


def figure_text(comment: str) -> str | None:
    m = FIGURE.fullmatch(comment.strip())
    return " ".join(m[1].split()) if m else None


def _plain(block: str) -> str:
    return FIGURE.sub("", block).strip()


def figure_markers(markdown: str) -> list[dict]:
    code = code_ranges(markdown)
    spans = blocks(markdown)
    out = []
    for m in FIGURE.finditer(markdown):
        if overlaps(m.start(), m.end(), code):
            continue
        heading, before, after = "", "", ""
        for a, b in spans:
            if a >= m.end():
                if not after and (t := _plain(markdown[a:b])) and not t.startswith("#"):
                    after = t
                continue
            text = _plain(markdown[a:min(b, m.start())])
            if not text:
                continue
            if text.startswith("#"):
                heading, before = text.lstrip("#").strip(), ""
            else:
                before = text
        near = before or after
        out.append({"text": " ".join(m[1].split()), "near": excerpt(_plain(near), NEAR_CHARS, ellipsis=True),
                    "heading": heading, "start": m.start(), "end": m.end()})
    return out

from __future__ import annotations

import re
from functools import lru_cache

from .parts.markdown import parse

_WS = re.compile(r"\s+")
SENT_END = r"[。！？!?]+[」』）)]*"
_SENT = re.compile(rf"[^。！？!?]*(?:{SENT_END}|$)")
_SENT_SPAN = re.compile(rf"[^。！？!?\n]*(?:{SENT_END}|(?=\n)|$)")
_EMPTY_LINE = re.compile(r"\s*(#{1,6}|[-*+]|\d+[.)]|>)?\s*")


def norm(text: str) -> str:
    return _WS.sub("", text)


def excerpt(text: str, n: int, ellipsis: bool = False) -> str:
    t = _WS.sub(" ", text).strip()
    return t if len(t) <= n else t[:n] + ("…" if ellipsis else "")


@lru_cache(maxsize=64)
def code_ranges(src: str) -> tuple[tuple[int, int], ...]:
    return tuple(p.span for p in parse(src).parts() if p.kind == "code" and p.span)


def overlaps(a: int, b: int, ranges) -> bool:
    return any(a < y and x < b for x, y in ranges)


def _line_spans(text: str) -> list[tuple[int, int]]:
    out, pos = [], 0
    for line in text.split("\n"):
        out.append((pos, pos + len(line)))
        pos += len(line) + 1
    return out


def code_free_lines(markdown: str) -> list[str]:
    code = code_ranges(markdown)
    return ["" if overlaps(a, max(b, a + 1), code) else markdown[a:b] for a, b in _line_spans(markdown)]


def sentences(text: str) -> list[str]:
    flat = re.sub(r"\s*\n\s*", "", text.strip())
    return [s.strip() for s in _SENT.findall(flat) if len(s.strip()) >= 2]


def sentence_spans(src: str, a: int, b: int) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _SENT_SPAN.finditer(src, a, b) if m.group().strip()]


def blocks(text: str) -> list[tuple[int, int]]:
    code = code_ranges(text)
    out: list[tuple[int, int]] = []
    start = None
    for a, b in _line_spans(text):
        if not text[a:b].strip() and not overlaps(a, a + 1, code):
            if start is not None:
                out.append((start, a))
                start = None
        elif start is None:
            start = a
    if start is not None:
        out.append((start, len(text)))
    return out


def paragraph_at(src: str, a: int, b: int) -> str:
    return next((src[x:y].strip() for x, y in blocks(src) if x <= a < y), src[a:b]).strip()


def locate(src: str, text: str, start: int = 0) -> tuple[int, int] | None:
    chars = [c for c in norm(text) if c not in "*_"]
    if not chars:
        return None
    m = re.compile(r"[\s*_]*".join(re.escape(c) for c in chars)).search(src, start)
    return (m.start(), m.end()) if m else None


def _cleanup_at(text: str, at: int) -> str:
    ls = text.rfind("\n", 0, at) + 1
    le = text.find("\n", at)
    le = len(text) if le < 0 else le
    line = text[ls:le]
    if _EMPTY_LINE.fullmatch(line):
        return text[:ls] + text[le + 1:]
    left, right = line[:at - ls], line[at - ls:]
    if left[-1:] in (" ", "\t") and right[:1] in (" ", "\t"):
        right = right.lstrip(" \t")
    if not right:
        left = left.rstrip(" \t")
    if left and _EMPTY_LINE.fullmatch(left) and left.strip():
        right = right.lstrip(" \t")
    return text[:ls] + left + right + text[le:]


def collapse_blank_lines(text: str) -> str:
    code = code_ranges(text)
    out, blank = [], False
    for a, b in _line_spans(text):
        line = text[a:b]
        if not line.strip() and not overlaps(a, a + 1, code):
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(line)
    return "\n".join(out).strip("\n") + "\n"


def edit_text(src: str, edits: list[tuple[int, int, str]]) -> tuple[str, dict[int, int]]:
    """Positions are of the applied edits in the result, before blank lines are collapsed."""
    code = code_ranges(src)
    taken: list[tuple[int, int]] = []
    placed: dict[int, int] = {}
    text = src
    for i in sorted(range(len(edits)), key=lambda k: edits[k][:2], reverse=True):
        a, b, new = edits[i]
        if overlaps(a, b, taken) or overlaps(a, b, code):
            continue
        taken.append((a, b))
        before = len(text)
        text = text[:a] + new + text[b:]
        text = _cleanup_at(text, a + len(new) if new else a)
        delta = len(text) - before
        placed = {k: p + delta if p > a else p for k, p in placed.items()}
        placed[i] = a
    return text, placed


def apply_edits(src: str, edits: list[tuple[int, int, str]]) -> str:
    return collapse_blank_lines(edit_text(src, edits)[0])

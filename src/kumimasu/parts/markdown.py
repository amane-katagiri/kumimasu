from __future__ import annotations

import itertools
import re

from markdown_it import MarkdownIt
from markdown_it.token import Token

from .model import Part, PartDoc

_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n.*?^(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.DOTALL | re.MULTILINE)
_ALIGN = {"text-align:left": "left", "text-align:right": "right", "text-align:center": "center"}


_MD = MarkdownIt("commonmark").enable("table")


class _Lines:
    def __init__(self, text: str) -> None:
        self.text = text
        self.starts = [0] + [m.end() for m in re.finditer(r"\n", text)]
        if self.starts[-1] == len(text) and len(self.starts) > 1:
            self.starts.pop()

    def offset(self, line: int) -> int:
        return self.starts[line] if line < len(self.starts) else len(self.text)

    def span(self, a: int, b: int) -> tuple[int, int]:
        return self.offset(a), self.offset(b)


def _block(kind: str, tok: Token, lines: _Lines, **kw) -> Part:
    m = tok.map or [0, 0]
    return Part(kind=kind, lines=(m[0] + 1, m[1]), span=lines.span(m[0], m[1]), **kw)


def _is_figure(inline: Token) -> bool:
    kids = [c for c in inline.children or [] if not (c.type == "text" and not c.content.strip())]
    return len(kids) == 1 and kids[0].type == "image"


class _Builder:
    def __init__(self, tokens: list[Token], lines: _Lines) -> None:
        self.t = tokens
        self.lines = lines
        self.i = 0

    def blocks(self, stop: str | None) -> list[Part]:
        out: list[Part] = []
        while self.i < len(self.t) and self.t[self.i].type != stop:
            out.append(self.block())
        self.i += 1
        return out

    def block(self) -> Part:
        tok = self.t[self.i]
        ty = tok.type
        if ty == "paragraph_open":
            inline = self.t[self.i + 1]
            self.i += 3
            kind = "figure" if _is_figure(inline) else "paragraph"
            return _block(kind, tok, self.lines, text=inline.content)
        if ty == "heading_open":
            inline = self.t[self.i + 1]
            self.i += 3
            return _block("heading", tok, self.lines, level=int(tok.tag[1]), text=inline.content)
        if ty in ("bullet_list_open", "ordered_list_open"):
            ordered = ty == "ordered_list_open"
            part = _block("list", tok, self.lines, ordered=ordered,
                          start=int(tok.attrs.get("start", 1)) if ordered else 1)
            close = "ordered_list_close" if ordered else "bullet_list_close"
            self.i += 1
            tight = True
            while self.t[self.i].type != close:
                item_tok = self.t[self.i]
                self.i += 1
                item = _block("item", item_tok, self.lines)
                j = self.i
                while self.t[j].type != "list_item_close" or self.t[j].level != item_tok.level:
                    if self.t[j].type == "paragraph_open" and self.t[j].level == item_tok.level + 1 and not self.t[j].hidden:
                        tight = False
                    j += 1
                item.children = self.blocks("list_item_close")
                part.children.append(item)
            self.i += 1
            part.tight = tight
            return part
        if ty in ("fence", "code_block"):
            self.i += 1
            return _block("code", tok, self.lines, text=tok.content, info=tok.info.strip() if ty == "fence" else "")
        if ty == "blockquote_open":
            part = _block("quote", tok, self.lines)
            self.i += 1
            part.children = self.blocks("blockquote_close")
            return part
        if ty == "table_open":
            return self.table()
        if ty == "html_block":
            self.i += 1
            return _block("html", tok, self.lines, text=tok.content)
        if ty == "hr":
            self.i += 1
            return _block("rule", tok, self.lines, text=tok.markup)
        raise ValueError(f"想定していない Markdown のトークンです: {ty}")

    def table(self) -> Part:
        part = _block("table", self.t[self.i], self.lines)
        self.i += 1
        row: Part | None = None
        header = False
        while self.t[self.i].type != "table_close":
            tok = self.t[self.i]
            if tok.type == "thead_open":
                header = True
            elif tok.type == "tbody_open":
                header = False
            elif tok.type == "tr_open":
                row = _block("row", tok, self.lines, header=header)
            elif tok.type in ("th_open", "td_open"):
                assert row is not None
                row.cells.append(self.t[self.i + 1].content)
                if header:
                    part.align.append(_ALIGN.get(str(tok.attrs.get("style", "")), ""))
                self.i += 2
            elif tok.type == "tr_close":
                assert row is not None
                part.children.append(row)
            self.i += 1
        self.i += 1
        return part


def _sectionize(blocks: list[Part], lines: _Lines, total: int) -> list[Part]:
    root: list[Part] = []
    stack: list[Part] = []
    for b in blocks:
        if b.kind == "heading":
            while stack and stack[-1].level >= b.level:
                stack.pop()
            sec = Part(kind="section", level=b.level, text=b.text, lines=b.lines, span=b.span)
            (stack[-1].children if stack else root).append(sec)
            stack.append(sec)
        else:
            (stack[-1].children if stack else root).append(b)

    def close(parts: list[Part], end_line: int) -> None:
        sections = [p for p in parts if p.kind == "section"]
        for p, nxt in itertools.zip_longest(sections, sections[1:]):
            end = nxt.lines[0] - 1 if nxt and nxt.lines else end_line
            assert p.lines is not None
            p.lines = (p.lines[0], max(p.lines[0], end))
            p.span = lines.span(p.lines[0] - 1, p.lines[1])
            close(p.children, p.lines[1])

    close(root, total)
    return root


def _uncovered(text: str, lines: _Lines, blocks: list[Part], first_line: int) -> list[Part]:
    covered = [False] * len(lines.starts)
    for b in blocks:
        if b.lines:
            for ln in range(b.lines[0] - 1, b.lines[1]):
                if ln < len(covered):
                    covered[ln] = True
    out: list[Part] = []
    ln = first_line
    while ln < len(covered):
        if covered[ln] or not _line(text, lines, ln).strip():
            ln += 1
            continue
        start = ln
        while ln < len(covered) and not covered[ln] and _line(text, lines, ln).strip():
            ln += 1
        a, b = lines.span(start, ln)
        out.append(Part(kind="raw", text=text[a:b].rstrip("\r\n"), lines=(start + 1, ln), span=(a, b)))
    return out


def _line(text: str, lines: _Lines, ln: int) -> str:
    a, b = lines.span(ln, ln + 1)
    return text[a:b]


def parse(text: str) -> PartDoc:
    lines = _Lines(text)
    total = len(lines.starts)
    prefix: list[Part] = []
    body = text
    first_line = 0
    if fm := _FRONT_MATTER.match(text):
        n = text[: fm.end()].count("\n")
        first_line = n
        prefix.append(Part(kind="front_matter", text=fm.group(0).rstrip("\r\n"), lines=(1, n), span=(0, fm.end())))
        body = "\n" * n + text[fm.end():]
    tokens = _MD.parse(body)
    blocks = _Builder(tokens, lines).blocks(None)
    blocks = sorted(prefix + blocks + _uncovered(text, lines, blocks, first_line), key=lambda p: p.lines or (0, 0))
    root = Part(kind="doc", lines=(1, total), span=(0, len(text)), children=_sectionize(blocks, lines, total))
    return PartDoc(source=text, root=root)

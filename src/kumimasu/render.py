"""Markdown → HTML for the reader. Every text run carries `data-s`, its code-point offset in the source, so a browser
selection maps back to source offsets; every block carries its Part path and source span."""
from __future__ import annotations

from html import escape

from markdown_it import MarkdownIt
from markdown_it.token import Token

from .figures import figure_text
from .parts.markdown import parse
from .parts.model import Part, PartDoc

_INLINE = MarkdownIt("commonmark")


class _Cursor:
    def __init__(self, src: str, start: int, end: int) -> None:
        self.src, self.pos, self.end = src, start, end

    def place(self, piece: str) -> list[tuple[int | None, str]]:
        if not piece:
            return []
        i = self.src.find(piece, self.pos, self.end)
        if i >= 0:
            self.pos = i + len(piece)
            return [(i, piece)]
        runs: list[tuple[int | None, str]] = []
        for ch in piece:
            j = self.src.find(ch, self.pos, self.end)
            if j < 0:
                at: int | None = None
            else:
                at, self.pos = j, j + 1
            prev = runs[-1] if runs else None
            if prev and (prev[0] is None) == (at is None) and (at is None or prev[0] + len(prev[1]) == at):
                runs[-1] = (prev[0], prev[1] + ch)
            else:
                runs.append((at, ch))
        return runs

    def skip_destination(self) -> None:
        """Move past `](dest "title")` or `[ref]` that follows link text, so later text is not found inside a URL."""
        s, p = self.src, self.pos
        if s.startswith("](", p):
            depth, p = 1, p + 2
            while p < self.end and depth:
                depth += {"(": 1, ")": -1}.get(s[p], 0)
                p += 1
            self.pos = p
        elif s.startswith("][", p):
            q = s.find("]", p + 2, self.end)
            self.pos = q + 1 if q >= 0 else p
        elif s.startswith("]", p):
            self.pos = p + 1

    def skip_image(self) -> None:
        i = self.src.find("![", self.pos, self.end)
        if i < 0:
            return
        depth, p = 1, i + 2
        while p < self.end and depth:
            depth += {"[": 1, "]": -1}.get(self.src[p], 0)
            p += 1
        self.pos = p - 1
        self.skip_destination()


def _text(cur: _Cursor, piece: str) -> str:
    out = []
    for at, run in cur.place(piece):
        out.append(escape(run) if at is None else f'<span data-s="{at}">{escape(run)}</span>')
    return "".join(out)


def _lines(cur: _Cursor, text: str) -> str:
    return "\n".join(_text(cur, ln) if ln else "" for ln in text.split("\n"))


def _alt(tok: Token) -> str:
    return "".join(c.content for c in tok.children or []) or tok.content


def _inline(cur: _Cursor, text: str) -> str:
    out: list[str] = []
    for tok in _INLINE.parseInline(text):
        for c in tok.children or []:
            match c.type:
                case "text":
                    out.append(_text(cur, c.content))
                case "code_inline":
                    out.append(f"<code>{_text(cur, c.content)}</code>")
                case "softbreak":
                    out.append("\n")
                case "hardbreak":
                    out.append("<br>")
                case "strong_open" | "em_open":
                    out.append(f"<{c.tag}>")
                case "strong_close" | "em_close":
                    out.append(f"</{c.tag}>")
                case "link_open":
                    out.append(f'<a href="{escape(str(c.attrs.get("href", "")))}" target="_blank" rel="noopener noreferrer">')
                case "link_close":
                    cur.skip_destination()
                    out.append("</a>")
                case "html_inline":
                    if (fig := figure_text(c.content)) is not None:
                        out.append(_figure(fig, "span", " inline"))
                    else:
                        out.append(f'<code class="html">{_text(cur, c.content)}</code>')
                case "image":
                    cur.skip_image()
                    out.append(f'<span class="img-ph inline">画像: {escape(_alt(c))}</span>')
    return "".join(out)


def _figure(text: str, tag: str = "div", cls: str = "", attrs: str = "") -> str:
    head = " ".join(x for x in (tag, attrs, f'class="fig-ph{cls}"', 'title="図の目印（後で図にする所）"') if x)
    return f"<{head}><b>図</b>{escape(text)}</{tag}>"


def _image_alt(text: str) -> str:
    for tok in _INLINE.parseInline(text):
        for c in tok.children or []:
            if c.type == "image":
                return _alt(c)
    return ""


class Renderer:
    def __init__(self, doc: PartDoc) -> None:
        self.doc = doc
        self.src = doc.source
        self.paths = doc.paths()
        levels = [p.level for p in doc.parts() if p.kind == "section"]
        self.shift = 2 - min(levels, default=2)

    def attrs(self, p: Part) -> str:
        a, b = p.span or (0, 0)
        return f'data-part="{self.paths[p.id]}" data-kind="{p.kind}" data-a="{a}" data-b="{b}"'

    def cursor(self, p: Part) -> _Cursor:
        a, b = p.span or (0, 0)
        return _Cursor(self.src, a, b)

    def blocks(self, parts: list[Part], tight: bool = False) -> str:
        return "\n".join(self.block(p, tight) for p in parts)

    def block(self, p: Part, tight: bool = False) -> str:
        at = self.attrs(p)
        match p.kind:
            case "section":
                lvl = max(2, min(6, p.level + self.shift))
                head = f'<h{lvl} class="h" data-part="{self.paths[p.id]}">{_inline(self.cursor(p), p.text)}</h{lvl}>'
                return f"<section {at}>\n{head}\n{self.blocks(p.children)}\n</section>"
            case "heading":
                return f'<h6 {at}>{_inline(self.cursor(p), p.text)}</h6>'
            case "paragraph":
                cls = ' class="tight"' if tight else ""
                return f"<p {at}{cls}>{_inline(self.cursor(p), p.text)}</p>"
            case "figure":
                return f'<div {at} class="img-ph">画像: {escape(_image_alt(p.text))}</div>'
            case "list":
                tag = "ol" if p.ordered else "ul"
                start = f' start="{p.start}"' if p.ordered and p.start != 1 else ""
                items = "\n".join(f"<li {self.attrs(i)}>{self.blocks(i.children, p.tight)}</li>" for i in p.children)
                return f"<{tag} {at}{start}>\n{items}\n</{tag}>"
            case "code":
                lang = f' data-lang="{escape(p.info)}"' if p.info else ""
                return f"<pre {at}{lang}><code>{_lines(self.cursor(p), p.text.removesuffix(chr(10)))}</code></pre>"
            case "quote":
                return f"<blockquote {at}>\n{self.blocks(p.children)}\n</blockquote>"
            case "table":
                return f"<table {at}>\n{self.rows(p)}\n</table>"
            case "html" if (fig := figure_text(p.text)) is not None:
                return _figure(fig, attrs=at)
            case "html" | "raw":
                return f'<pre {at} class="{p.kind}">{_lines(self.cursor(p), p.text)}</pre>'
            case "rule":
                return f"<hr {at}>"
        return ""

    def rows(self, table: Part) -> str:
        head, body = [], []
        for r in table.children:
            cur = self.cursor(r)
            tag = "th" if r.header else "td"
            cells = []
            for i, c in enumerate(r.cells):
                align = table.align[i] if i < len(table.align) and table.align[i] else ""
                style = f' style="text-align:{align}"' if align else ""
                cells.append(f"<{tag}{style}>{_inline(cur, c)}</{tag}>")
            (head if r.header else body).append(f"<tr {self.attrs(r)}>{''.join(cells)}</tr>")
        return (f"<thead>{''.join(head)}</thead>" if head else "") + f"<tbody>{''.join(body)}</tbody>"

    def html(self) -> str:
        return self.blocks([p for p in self.doc.root.children if p.kind != "front_matter"])


def render(text: str) -> tuple[str, PartDoc]:
    doc = parse(text)
    return Renderer(doc).html(), doc

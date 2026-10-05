from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from .model import Unit
from .parts.markdown import parse
from .parts.model import Part
from .textutil import norm, sentence_spans

PARA_MAX = 200
GROUP_CHARS = 120
CODE_SHOWN = 1500
CODE_CHUNK_LINES = 40
PROSE_SUFFIXES = {".md", ".markdown", ".txt", ".text", ""}
KINDS = ("prose", "quote", "item", "row", "code")


class InfoUnit(BaseModel):
    id: int
    kind: str
    text: str
    start: int
    end: int
    section: str = ""

    @property
    def chars(self) -> int:
        return len(norm(self.text))


def _sentence_groups(src: str, a: int, b: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = None
    for s, e in sentence_spans(src, a, b):
        cur = (cur[0], e) if cur else (s, e)
        if len(src[cur[0]:cur[1]].strip()) >= GROUP_CHARS:
            out.append(cur)
            cur = None
    if cur:
        if out and len(src[cur[0]:cur[1]].strip()) < GROUP_CHARS / 3:
            out[-1] = (out[-1][0], cur[1])
        else:
            out.append(cur)
    return out


def info_units(markdown: str) -> list[InfoUnit]:
    doc = parse(markdown)
    src = doc.source
    units: list[InfoUnit] = []

    def add(kind: str, text: str, span: tuple[int, int], section: str) -> None:
        if text.strip():
            units.append(InfoUnit(id=len(units) + 1, kind=kind, text=text.strip(), start=span[0], end=span[1],
                                  section=section))

    def prose(p: Part, kind: str, section: str) -> None:
        a, b = p.span or (0, 0)
        body = src[a:b].strip()
        if len(norm(body)) <= PARA_MAX:
            add(kind, p.text or body, (a, b), section)
            return
        for s, e in _sentence_groups(src, a, b):
            add(kind, src[s:e], (s, e), section)

    def walk(p: Part, section: str, kind: str = "prose") -> None:
        for c in p.children:
            match c.kind:
                case "section":
                    walk(c, c.text)
                case "paragraph" | "raw":
                    prose(c, kind, section)
                case "quote":
                    walk(c, section, "quote")
                case "list":
                    for item in c.children:
                        paras = [x for x in item.children if x.kind == "paragraph"]
                        if paras:
                            add("item", "\n".join(x.text for x in paras),
                                ((paras[0].span or (0, 0))[0], (paras[-1].span or (0, 0))[1]), section)
                        rest = [x for x in item.children if x.kind != "paragraph"]
                        if rest:
                            walk(Part(kind="doc", children=rest), section, kind)
                case "table":
                    rows = [r for r in c.children if r.kind == "row"]
                    head = rows[0].cells if rows else []
                    for r in rows[1:]:
                        cells = [f"{h}: {v}" if h else v for h, v in zip(head + [""] * len(r.cells), r.cells)]
                        add("row", " / ".join(cells), r.span or (0, 0), section)
                case "code":
                    add("code", c.text, c.span or (0, 0), section)

    walk(doc.root, "")
    return units


def _unit_line(u: InfoUnit) -> str:
    kind = {"prose": "散文", "quote": "引用", "item": "項目", "row": "表の行", "code": "コード"}[u.kind]
    text = u.text if u.kind != "code" or len(u.text) <= CODE_SHOWN else u.text[:CODE_SHOWN] + "\n…（以下略）"
    if u.kind == "code":
        text = "```\n" + text.rstrip("\n") + "\n```"
    where = f"、節: {u.section}" if u.section else ""
    return f"[{u.id}]（{kind}{where}）\n{text}"


def units_block(units: list[InfoUnit]) -> str:
    return "\n\n".join(_unit_line(u) for u in units)


def code_units(text: str) -> list[InfoUnit]:
    lines = text.rstrip("\n").splitlines()
    chunks = ["\n".join(lines[i:i + CODE_CHUNK_LINES]) for i in range(0, len(lines), CODE_CHUNK_LINES)]
    return [InfoUnit(id=n, kind="code", text=c, start=0, end=0) for n, c in enumerate((c for c in chunks if c.strip()), 1)]


def parse_material(path: Path) -> list[InfoUnit]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in PROSE_SUFFIXES:
        return info_units(text)
    return code_units(text)


def as_info_units(units: list[Unit]) -> tuple[list[InfoUnit], dict[int, str]]:
    infos, ids = [], {}
    for i, u in enumerate(units, 1):
        infos.append(InfoUnit(id=i, kind=u.kind if u.kind in KINDS else "prose",
                              text=u.text, start=0, end=0, section=u.section))
        ids[i] = u.id
    return infos, ids

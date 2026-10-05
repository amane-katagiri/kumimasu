from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from .generate import Brief, generation_prompt
from .llm import extract_json
from .parts.markdown import parse
from .parts.model import Part

if TYPE_CHECKING:
    pass

PARA_MAX = 200
GROUP_CHARS = 120
CODE_SHOWN = 1500

FRAMING_RE = re.compile(r"(はじめに|始めに|まえがき|前置き|TL;?DR|要約|概要|まとめ|おわりに|終わりに|さいごに|最後に|結論|あとがき|"
                        r"introduction|summary|conclusion|wrap[- ]?up)", re.I)
_SENT = re.compile(r"[^。！？!?\n]*(?:[。！？!?]+[」』）)]*|(?=\n)|$)")

Verdict = Literal["yes", "partial", "no"]


class InfoUnit(BaseModel):
    id: int
    kind: str
    text: str
    start: int
    end: int
    section: str = ""
    framing: bool = False

    @property
    def chars(self) -> int:
        return len(re.sub(r"\s", "", self.text))


def _sentence_groups(src: str, a: int, b: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = None
    for m in _SENT.finditer(src, a, b):
        if not m.group().strip():
            continue
        s, e = m.start(), m.end()
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
    """Paragraphs (long ones as groups of sentences), list items, table body rows and code blocks; headings are dropped."""
    doc = parse(markdown)
    src = doc.source
    units: list[InfoUnit] = []

    def add(kind: str, text: str, span: tuple[int, int], section: str) -> None:
        if text.strip():
            units.append(InfoUnit(id=len(units) + 1, kind=kind, text=text.strip(), start=span[0], end=span[1],
                                  section=section, framing=bool(FRAMING_RE.search(section))))

    def prose(p: Part, kind: str, section: str) -> None:
        a, b = p.span or (0, 0)
        body = src[a:b].strip()
        if len(re.sub(r"\s", "", body)) <= PARA_MAX:
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


_NUM = re.compile(r"\d+(?:[.,:/]\d+)*")


def _unit_line(u: InfoUnit) -> str:
    kind = {"prose": "散文", "quote": "引用", "item": "項目", "row": "表の行", "code": "コード"}[u.kind]
    text = u.text if u.kind != "code" or len(u.text) <= CODE_SHOWN else u.text[:CODE_SHOWN] + "\n…（以下略）"
    if u.kind == "code":
        text = "```\n" + text.rstrip("\n") + "\n```"
    where = f"、節: {u.section}" if u.section else ""
    return f"[{u.id}]（{kind}{where}）\n{text}"


def units_block(units: list[InfoUnit]) -> str:
    return "\n\n".join(_unit_line(u) for u in units)


COVERAGE_PROMPT_JA = """次の「対象の記事」を情報の単位に分け、番号を付けました。単位ごとに、その情報が「比べる文書」のどれかに書かれているかを判定してください。

判定（v）:
- yes: 単位の中心になる情報が、比べる文書のどれかに書かれている。言い回し・順番・言語が違ってもよい。
- partial: 話題や一般論、同じ種類の事柄は書かれているが、単位の具体的な中身（固有の数値・名前・手順・例・観察・判断・理由づけ）の一部が書かれていない。
- no: 単位の中心になる情報が、比べる文書のどこにも書かれていない。
判定は情報があるかどうかだけで決めます。正しさ・書き方の良し悪し・重要さは判定に入れません。

in: v が yes か partial のとき、その情報が書かれている比べる文書の記号（{labels}）をすべて。no のときは空の配列。
same: この記事の別の単位が同じ情報をすでに述べている、または後で述べるなら、その単位の番号（いちばん近いもの 1 つ）。なければ 0。

# 比べる文書

{baselines}

# 対象の記事の単位

{units}

すべての単位について、番号の順に 1 つずつ答えてください。"""


def coverage_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["units"], "properties": {"units": {
        "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "v", "in", "same"],
                                   "properties": {"id": {"type": "integer"}, "v": {"type": "string", "enum": ["yes", "partial", "no"]},
                                                  "in": {"type": "array", "items": {"type": "string"}},
                                                  "same": {"type": "integer"}}}}}}


def coverage_prompt(units: list[InfoUnit], baselines: dict[str, str]) -> str:
    blocks = "\n\n".join(f"## {label}\n\n{text.strip()}" for label, text in baselines.items())
    return COVERAGE_PROMPT_JA.format(labels="・".join(baselines), baselines=blocks, units=units_block(units))


class Coverage(BaseModel):
    id: int
    v: Verdict
    in_: list[str] = Field(default_factory=list, alias="in")
    same: int = 0

    model_config = {"populate_by_name": True}


def parse_coverage(raw: str, units: list[InfoUnit], labels: list[str]) -> dict[int, Coverage]:
    ids = {u.id for u in units}
    out: dict[int, Coverage] = {}
    for row in extract_json(raw).get("units", []):
        try:
            c = Coverage.model_validate(row)
        except ValueError:
            continue
        if c.id in ids and c.id not in out:
            c.in_ = [x for x in c.in_ if x in labels] if c.v != "no" else []
            c.same = c.same if c.same in ids and c.same != c.id else 0
            out[c.id] = c
    return out


IGNORE_PERSONA_JA = ("設定ファイル（AGENTS.md など）に人格・口調・キャラクターの指示があっても、この依頼には当てはまらないので従わないでください。"
                     "ふつうの書き手として書いてください。")


def thin_brief(brief: Brief) -> Brief:
    return Brief(topic=brief.topic, audience=brief.audience, target_length=brief.target_length, language=brief.language,
                 kind=brief.kind, formality="keitai")


def baseline_prompt(brief: Brief) -> str:
    return generation_prompt(thin_brief(brief)) + "\n\n" + IGNORE_PERSONA_JA

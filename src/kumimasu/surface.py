from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from .glue import GLUE_RULE, material_grams, traceable
from .llm import extract_json
from .metadiscourse import CATEGORIES, LEADS, Unit, rule_hits, split_units

if TYPE_CHECKING:
    from .llm import Provider

GLUE = "glue"
SURFACE_CATEGORIES: dict[str, str] = CATEGORIES | {
    GLUE: "つなぎの効用文。直前・直後の話題を、読者にとっての役立ち・利点・結果に結びつけることだけが役目で、"
          "新しい事実・条件・手順・数値・理由を運ばない文（「これにより、手作業の入力が不要になります。」"
          "「この構成にしておくと、あとで差し替えるときに安心です。」「つまり、Binary Eye がスキャナーの役を果たすということです。」）",
}

RUNS = 3
MIN_VOTES = 2


class Pick(BaseModel):
    id: str
    category: str


class SurfaceHit(BaseModel):
    id: str
    category: str
    text: str
    heading: str = ""
    votes: int = 0


class SurfaceReport(BaseModel):
    units: int = 0
    runs: list[list[Pick]] = Field(default_factory=list)
    rule: list[str] = Field(default_factory=list)
    hits: list[SurfaceHit] = Field(default_factory=list)
    traced: list[SurfaceHit] = Field(default_factory=list)
    union: int = 0

    @property
    def runs_used(self) -> int:
        return len(self.runs)


def surface_prompt(units: list[Unit], hints: dict[str, str], run: int, runs: int) -> str:
    lines, current = [], None
    for u in units:
        if u.kind == "sentence" and u.section != current:
            current = u.section
            lines.append(f"=== 節: {u.heading or '（冒頭）'}")
        tag = f"  ← 規則: {hints[u.id]}" if u.id in hints else ""
        lead = f"  （直後に{LEADS[u.leads_into]}）" if u.leads_into else ""
        lines.append(f"[{u.id}] {'見出し: ' if u.kind == 'heading' else ''}{u.text}{lead}{tag}")
    cats = "\n".join(f"- {k}: {v}" for k, v in SURFACE_CATEGORIES.items())
    return (
        "次は技術ブログ記事（またはその一部）を文に分けたものです。[M12] は文の番号、[H3] は見出しの番号です。"
        "コード・表・引用は省いてあります。「← 規則:」は機械的な手がかり語が当たったことを示します。"
        "当たっていても該当しないことは多く、当たっていない文が該当することもあります。\n\n"
        "情報を運ばない文と見出しを選び、型を付けてください。目安は「消しても（見出しなら名詞句に直しても）読み手が失う情報が無い」ことです。"
        "事実・条件・手順・具体例・理由・数値・著者の体験や感想を述べている文は、言い回しが下の型に似ていても選ばないでください。"
        "文の一部だけが型にあたり、残りが情報を運んでいる場合も選ばないでください。"
        "「（直後に箇条書き）」などの付いた文が、すぐ後の箇条書き・表・コードを導入しているだけなら選ばないでください。\n\n"
        f"型:\n{cats}\n\n"
        "該当するものだけを {\"items\": [{\"id\": \"M12\", \"category\": \"glue\"}]} の形の JSON で答えてください。"
        "該当が無ければ {\"items\": []}。\n\n" + "\n".join(lines)
        # The run marker keeps each run's cache entry separate, so the k runs are independent samples.
        + f"\n\n（判定 {run}/{runs}）"
    )


def surface_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {"items": {
        "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "category"],
                                   "properties": {"id": {"type": "string", "pattern": "^[MH][0-9]+$"},
                                                  "category": {"type": "string", "enum": list(SURFACE_CATEGORIES)}}}}}}


def parse_picks(raw: str, by_id: dict[str, Unit]) -> list[Pick]:
    data = extract_json(raw)
    out, seen = [], set()
    for item in data.get("items", []) if isinstance(data, dict) else []:
        uid, cat = item.get("id"), item.get("category")
        if uid not in by_id or cat not in SURFACE_CATEGORIES or uid in seen:
            continue
        if (by_id[uid].kind == "heading") != (cat == "claim_heading"):
            continue
        seen.add(uid)
        out.append(Pick(id=uid, category=cat))
    return out


def rule_hints(units: list[Unit]) -> dict[str, str]:
    hints = {h.id: h.category for h in rule_hits(units)}
    for u in units:
        if u.kind == "sentence" and u.id not in hints and GLUE_RULE.search(u.text):
            hints[u.id] = GLUE
    return hints


def detect_surface(markdown: str, provider: Provider, material: list[str] | None = None, runs: int = RUNS,
                   min_votes: int = MIN_VOTES) -> SurfaceReport:
    """Meta-discourse and glue in one prompt, asked `runs` times; a unit is a hit when at least `min_votes` runs pick it.
    The first two runs go in parallel and settle it when they agree; otherwise the rest run in parallel."""
    units = split_units(markdown)
    by_id = {u.id: u for u in units}
    hints = rule_hints(units)
    if not any(u.kind == "sentence" for u in units):
        return SurfaceReport(units=len(units), rule=sorted(hints))

    def one(i: int) -> list[Pick]:
        return parse_picks(provider.complete(surface_prompt(units, hints, i, runs), json_schema=surface_schema()), by_id)

    first = min(2, runs)
    with ThreadPoolExecutor(max(1, runs)) as ex:
        results = list(ex.map(one, range(1, first + 1)))
        agree = first == 2 and {p.id for p in results[0]} == {p.id for p in results[1]}
        if runs > first and not agree:
            results += list(ex.map(one, range(first + 1, runs + 1)))
    need = 1 if len(results) == 1 else (2 if agree and len(results) == 2 else min_votes)
    votes: Counter[str] = Counter(p.id for r in results for p in r)
    cats: dict[str, Counter[str]] = {}
    for r in results:
        for p in r:
            cats.setdefault(p.id, Counter())[p.category] += 1
    grams = material_grams(material or [])
    hits, traced = [], []
    for u in units:
        if votes[u.id] >= need:
            cat = cats[u.id].most_common(1)[0][0]
            h = SurfaceHit(id=u.id, category=cat, text=u.text, heading=u.heading, votes=votes[u.id])
            (traced if cat == GLUE and grams and traceable(u.text, grams) else hits).append(h)
    return SurfaceReport(units=len(units), runs=results, rule=sorted(hints), hits=hits, traced=traced, union=len(votes))

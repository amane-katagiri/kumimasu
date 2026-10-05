from __future__ import annotations

from typing import TYPE_CHECKING

from .generate import (
    DATA_NOTE_JA,
    OUTPUT_FORMAT_JA,
    REGISTER_REQUEST_JA,
    WEB_RULES_JA,
    article_from,
)
from .interview import unit_lines
from .llm import extract_json
from .model import Project, Unit
from .payload import coverage_prompt, coverage_schema, parse_coverage
from .workdir import WorkDir, as_info_units

if TYPE_CHECKING:
    from .llm import Provider

KIND_LABEL = {"実用": "実用的な技術ブログ記事", "読み物": "読み物としての技術ブログ記事", "調査": "調べものの記事"}
BASELINE = "W"


def baseline_prompt(p: Project) -> str:
    return "\n\n".join([f"「{p.topic}」について、{p.audience}向けの{KIND_LABEL[p.kind]}を書いてください。\n\n"
                         f"長さは{p.length}字くらいでお願いします。" + REGISTER_REQUEST_JA["keitai"],
                         WEB_RULES_JA, OUTPUT_FORMAT_JA])


def judge_searchable(units: list[Unit], baseline: str, judge: Provider) -> tuple[list[Unit], dict]:
    infos, ids = as_info_units(units)
    raw = judge.complete(coverage_prompt(infos, {BASELINE: baseline}), json_schema=coverage_schema())
    cov = parse_coverage(raw, infos, [BASELINE])
    out = []
    for info in infos:
        c = cov.get(info.id)
        u = units[info.id - 1]
        out.append(u.model_copy(update={"searchable": c.v if c else None, "found_in": c.in_ if c else []}))
    return out, {"raw": raw, "ids": ids}


CLUSTER_PROMPT_JA = DATA_NOTE_JA + """

次は、ある記事のための材料を番号付きの単位に分けたものです。同じ情報を述べている単位の組を見つけてください。

同じ情報とは、言い回しが違っても、どちらか一方を消しても読み手に伝わる情報が減らないものです。話題が同じでも、片方にしか無い事実・数値・手順・理由があるなら別の情報です。コードと、そのコードを説明する文も別の情報です。

組は {{"groups": [["m6", "m54"], ...]}} の形の JSON で答えてください。1 つの単位は 1 つの組にだけ入れます。同じ情報の組が無ければ {{"groups": []}}。

# 材料

{units}"""


def cluster_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["groups"], "properties": {
        "groups": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}}}


def cluster_prompt(units: list[Unit]) -> str:
    return CLUSTER_PROMPT_JA.format(units=unit_lines(units, with_mark=False))


def parse_clusters(raw: str, units: list[Unit]) -> list[list[str]]:
    order = {u.id: i for i, u in enumerate(units)}
    seen: set[str] = set()
    out = []
    for g in extract_json(raw).get("groups", []):
        ids = sorted({x for x in g if x in order and x not in seen}, key=order.__getitem__)
        if len(ids) >= 2:
            seen.update(ids)
            out.append(ids)
    return out


def apply_clusters(units: list[Unit], groups: list[list[str]]) -> list[Unit]:
    rep = {x: g[0] for g in groups for x in g[1:]}
    return [u.model_copy(update={"cluster": rep.get(u.id, "")}) for u in units]


def mark(wd: WorkDir, writer: Provider, judge: Provider, reuse_baseline: bool = True) -> list[Unit]:
    p = wd.project()
    path = wd.root / "baseline" / f"{BASELINE}.md"
    if reuse_baseline and path.exists():
        baseline = path.read_text(encoding="utf-8")
    else:
        prompt = baseline_prompt(p)
        wd.write(f"baseline/{BASELINE}.prompt.md", prompt)
        baseline = article_from(writer.complete(prompt))
        wd.write(f"baseline/{BASELINE}.md", baseline)
    units, log = judge_searchable(wd.material_units(), baseline, judge)
    raw = judge.complete(cluster_prompt(units), json_schema=cluster_schema())
    groups = parse_clusters(raw, units)
    units = apply_clusters(units, groups)
    wd.write_json("baseline/mark.json", log | {"clusters": groups, "clusters_raw": raw})
    wd.save_units(units)
    return units


def mark_counts(units: list[Unit]) -> dict[str, int]:
    out = {"yes": 0, "partial": 0, "no": 0, "unjudged": 0}
    for u in units:
        out[u.searchable or "unjudged"] += 1
    return out

"""Agent stand-ins for the human checkpoints. Not recommended: they never invent first-hand content and never
rewrite; everything they decide is recorded with source=auto and listed in the handoff."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from . import ops
from .errors import StepError
from .generate import DATA_NOTE_JA
from .glue import material_grams, traceable
from .interview import unit_lines
from .llm import extract_json
from .workdir import WorkDir, merge_clusters

if TYPE_CHECKING:
    from .llm import Provider

SOURCE = "auto"
WARNING = ("auto は人の判断の代わりにエージェントが決めます。推奨しません。体験・感想・動機の質問には答えず、"
           "決めたことはすべて source=auto として handoff.json に残します。")

AUTO_INTERVIEW_PROMPT_JA = DATA_NOTE_JA + """

次は、記事の著者に聞く質問と、著者の手元の材料です。著者は今いないので、材料だけから答えられる質問にだけ答えます。

各質問について kind を決めます。
- selection: 記事に何を入れるか・削るか・どこまで扱うか（範囲）を聞く質問。
- firsthand: 著者の体験・驚き・感想・動機・好み・こだわりを聞く質問。

selection の質問には、材料の目印（検索で届く／手元だけ）と材料の中身だけを根拠に、短く答えます（例:「検索で届く一般的な説明は省く」「m12 の手順は入れる」）。迷うときは、手元だけの材料を残し、検索で届く一般的な説明を削る方に寄せます。
firsthand の質問には answer を空文字にします。材料から推測して著者の体験や気持ちを作ってはいけません。

{{"answers": [{{"id": "q1", "kind": "selection", "answer": "…"}}]}} の形の JSON で、すべての質問に答えてください。

# 質問

{questions}

# 材料

{units}"""


def auto_interview_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["answers"], "properties": {"answers": {
        "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "kind", "answer"],
                                   "properties": {"id": {"type": "string"},
                                                  "kind": {"type": "string", "enum": ["selection", "firsthand"]},
                                                  "answer": {"type": "string"}}}}}}


def auto_interview(wd: WorkDir, provider: Provider) -> dict:
    ops.require_stage(wd, "interview", action="auto（インタビュー）")
    iv = wd.interview()
    open_qs = [q for q in iv.questions if not q.answer.strip()]
    kinds: dict[str, str] = {}
    if open_qs:
        prompt = AUTO_INTERVIEW_PROMPT_JA.format(
            questions="\n".join(f"[{q.id}] {q.question}" for q in open_qs),
            units=unit_lines(merge_clusters(wd.material_units())))
        rows = extract_json(provider.complete(prompt, json_schema=auto_interview_schema())).get("answers", [])
        by_id = {q.id: q for q in open_qs}
        for row in rows:
            qid = str(row.get("id"))
            if qid not in by_id or qid in kinds:
                continue
            kinds[qid] = row.get("kind") if row.get("kind") in ("selection", "firsthand") else "firsthand"
            text = str(row.get("answer", "")).strip()
            if kinds[qid] == "selection" and text:
                ops.answer(wd, qid, text, SOURCE)
    return ops.confirm(wd, SOURCE, note="auto: 範囲の質問だけに材料から答え、体験・感想の質問は空のまま")


def auto_design(wd: WorkDir) -> dict:
    ops.require_stage(wd, "design", action="auto（設計）")
    if not wd.design_file.exists():
        raise StepError("まだ設計がありません（kumimasu design）")
    ops.record(wd, SOURCE, "accept-design")
    return ops.confirm(wd, SOURCE, note="auto: 設計の提案をそのまま受け入れた")


AUTO_REVIEW_PROMPT_JA = DATA_NOTE_JA + """

次は、記事の下書きで機械的な検出に当たった文です。著者は今いないので、各文を「残す」(keep) か「削る」(delete) に決めてください。書き直しはしません。

- 削るのは、情報を運ばないことがはっきりしている文だけです: 道しるべ（「この記事では〜を書きます」）、つなぎの効用文（話題を読者の役立ちに結びつけるだけの文）、決め台詞。
- 残すのは、著者の判断・意見・感想（「〜と考えています」「〜のつもりで入れた」）、事実・条件・手順・理由を含む文、迷う文すべてです。

{{"items": [{{"id": "…", "decision": "keep", "reason": "…"}}]}} の形の JSON で、すべての文に答えてください。

{items}"""


def auto_review_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {"items": {
        "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "decision", "reason"],
                                   "properties": {"id": {"type": "string"},
                                                  "decision": {"type": "string", "enum": ["keep", "delete"]},
                                                  "reason": {"type": "string"}}}}}}


def auto_review(wd: WorkDir, provider: Provider, rewriter: Callable[[], Provider] | None = None) -> dict:
    """Only undecided meta-discourse and glue items are decided; a sentence that mostly repeats the material is kept;
    nothing is rewritten (a rewrite needs the author's note). Other findings are left for the author."""
    from .review import load_review

    ops.require_stage(wd, "review", action="auto（最終チェック）")
    base = wd.review_draft()
    rev = load_review(wd, base)
    grams = material_grams([u.text for u in wd.units()])
    todo = [i for i in rev.items if i.kind in ("meta", "glue") and not i.decision and i.start is not None]
    decided: dict[str, str] = {}
    for it in todo:
        if grams and traceable(it.text, grams):
            decided[it.id] = "keep"
    ask = [i for i in todo if i.id not in decided]
    if ask:
        prompt = AUTO_REVIEW_PROMPT_JA.format(items="\n".join(f"[{i.id}]（{i.category}）{i.text}" for i in ask))
        for row in extract_json(provider.complete(prompt, json_schema=auto_review_schema())).get("items", []):
            iid = str(row.get("id"))
            if iid in {i.id for i in ask} and iid not in decided:
                decided[iid] = "delete" if row.get("decision") == "delete" else "keep"
    if decided:
        ops.decide(wd, base, {"items": [{"id": k, "decision": v} for k, v in decided.items()]}, SOURCE)
    return ops.confirm(wd, SOURCE, note="auto: 検出された文を残す／削るだけで決め、書き直しはしていない", rewriter=rewriter)

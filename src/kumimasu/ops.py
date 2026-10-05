from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from .check import check_stem
from .design import RESEARCH_CHARS, RESEARCH_MAX, apply_noise, sync_design
from .draft import read_used
from .errors import StepError
from .files import append_jsonl, atomic_write, read_jsonl
from .model import STAGES, USES, Aside, Design, Interview, Rule, Skip, Unit
from .research import research_name
from .review import (
    ApplyResult,
    Review,
    ReviewContext,
    apply_review,
    final_name,
    require_base,
    save_decisions,
)
from .terms import clear_promotion, explain_term
from .textutil import excerpt
from .workdir import WorkDir, now

if TYPE_CHECKING:
    from .llm import Provider

HISTORY = "history.jsonl"
HANDOFF = "handoff.json"
HANDOFFS = "handoffs.jsonl"
NEXT = {"interview": "design", "design": "drafting", "drafting": "review", "review": "done"}
HUMAN_STAGES = ("interview", "design", "review")
WHO = {"human-ui": "human", "agent-chat": "human", "auto": "auto", "agent": "agent"}
STAGE_LABEL = {"interview": "インタビュー", "design": "設計", "drafting": "下書き", "review": "最終チェック", "done": "完了"}
LOCK = threading.RLock()


class StageError(StepError):
    pass


def stage(wd: WorkDir) -> str:
    return wd.project().stage


def require_stage(wd: WorkDir, *stages: str, action: str) -> None:
    cur = stage(wd)
    if cur not in stages:
        want = "・".join(STAGE_LABEL[s] for s in stages)
        raise StageError(f"{action}は「{want}」の段階でしかできません（今は「{STAGE_LABEL[cur]}」、ラウンド {wd.round}）")


def record(wd: WorkDir, source: str, op: str, **detail) -> None:
    p = wd.project()
    append_jsonl(wd.root / HISTORY, {"at": now(), "source": source, "stage": p.stage, "round": p.round, "op": op} | detail)


def history(wd: WorkDir) -> list[dict]:
    return read_jsonl(wd.root / HISTORY)


def set_stage(wd: WorkDir, new: str, round_: int | None = None) -> None:
    p = wd.project()
    wd.save_project(p.model_copy(update={"stage": new, "round": p.round if round_ is None else round_}))


def save_answers(wd: WorkDir, answers: dict, source: str) -> Interview:
    with LOCK:
        require_stage(wd, "interview", action="質問への回答")
        iv = wd.interview()
        by_id = {q.id: q for q in iv.questions}
        unknown = set(answers) - set(by_id)
        if unknown:
            raise ValueError(f"知らない質問です: {', '.join(sorted(unknown))}")
        changed = [qid for qid, text in answers.items() if by_id[qid].answer != str(text)]
        for qid in changed:
            by_id[qid].answer, by_id[qid].source = str(answers[qid]), source
        if changed:
            wd.save_interview(iv)
            for qid in changed:
                record(wd, source, "answer", id=qid)
        return iv


def answer(wd: WorkDir, qid: str, text: str, source: str) -> Interview:
    return save_answers(wd, {qid: text}, source)


def update_design(wd: WorkDir, body: dict, source: str) -> Design:
    """The design edits of the page, as one body: purpose, takeaways, order, avoid, rules, skip, aside, target_length,
    units ({id: use}), explain (a term whose best dropped explanation becomes mention). A unit set to a non-drop use leaves its skip; a unit set to drop leaves the asides."""
    with LOCK:
        require_stage(wd, "design", action="設計の変更")
        units = {u.id: u for u in wd.units()}
        d = sync_design(wd.design(), list(units.values()))
        upd: dict = {}
        for key in ("purpose", "form_prefs"):
            if key in body:
                upd[key] = str(body[key])
        for key in ("takeaways", "order", "avoid", "research", "forms"):
            if key in body:
                upd[key] = [str(x).strip() for x in body[key] if str(x).strip()]
        if len(upd.get("research", [])) > RESEARCH_MAX or any(len(x) > RESEARCH_CHARS for x in upd.get("research", [])):
            raise ValueError(f"調べることは {RESEARCH_MAX} 個まで、それぞれ {RESEARCH_CHARS} 字までです")
        if "rules" in body:
            upd["rules"] = [Rule.model_validate(x) for x in body["rules"]]
        if "skip" in body:
            upd["skip"] = [Skip.model_validate(x) for x in body["skip"]]
        if "aside" in body:
            upd["aside"] = [Aside.model_validate(x) for x in body["aside"]]
        if isinstance(t := body.get("toggle_skip"), dict):
            unit = _known_unit(units, t.get("unit"))
            upd["skip"] = toggled_skip(d, unit.id, bool(t.get("on")), str(t.get("label") or ""), unit.text)
        if isinstance(t := body.get("toggle_aside"), dict):
            upd["aside"] = toggled_aside(d, _known_unit(units, t.get("unit")).id, bool(t.get("on")),
                                         str(t.get("where") or ""))
        if "target_length" in body:
            upd["target_length"] = int(body["target_length"])
        uses = body.get("units") or {}
        if uses:
            unknown = set(uses) - {u.id for u in d.units}
            if unknown:
                raise ValueError(f"知らない単位です: {', '.join(sorted(unknown))}")
            if any(v not in USES for v in uses.values()):
                raise ValueError(f"use は {', '.join(USES)} のどれかにしてください")
            upd["units"] = [u if uses.get(u.id, u.use) == u.use
                            else clear_promotion(u.model_copy(update={"use": uses[u.id], "why": "手で変更"}), uses[u.id])
                            for u in d.units]
        d = d.model_copy(update=upd)
        if "explain" in body:
            d = explain_term(d, str(body["explain"]))
        d = d.model_copy(update={
            "skip": [x.model_copy(update={"units": [i for i in x.units if uses.get(i, "drop") == "drop"]}) for x in d.skip],
            "aside": [a for a in d.aside if uses.get(a.id) != "drop"]})
        d = apply_noise(d)
        wd.save_design(d)
        record(wd, source, "design", keys=sorted(body))
        return d


def _known_unit(units: dict[str, Unit], unit_id) -> Unit:
    if unit_id not in units:
        raise ValueError(f"知らない単位です: {unit_id}")
    return units[unit_id]


def toggled_skip(d: Design, unit_id: str, on: bool, label: str, text: str) -> list[Skip]:
    if on:
        if any(unit_id in x.units for x in d.skip):
            return list(d.skip)
        return [*d.skip, Skip(label=label or excerpt(text, 20), units=[unit_id], why="手で追加")]
    return [x.model_copy(update={"units": [i for i in x.units if i != unit_id]}) for x in d.skip if x.units != [unit_id]]


def toggled_aside(d: Design, unit_id: str, on: bool, where: str) -> list[Aside]:
    rest = [a for a in d.aside if a.id != unit_id]
    return [*rest, Aside(id=unit_id, where=where, why="手で追加")] if on else rest


def decide(wd: WorkDir, base: str, body: dict, source: str) -> Review:
    with LOCK:
        require_stage(wd, "review", action="最終チェックの決定")
        require_base(base)
        rev = save_decisions(wd, base, body, source)
        record(wd, source, "decide", draft=base, items=[str(x.get("id")) for x in body.get("items", [])],
               remove=[str(x) for x in body.get("remove", [])])
        return rev


def apply(wd: WorkDir, base: str, provider: Provider | None, source: str, regenerate: tuple[str, ...] = (),
          ctx: ReviewContext | None = None) -> ApplyResult:
    with LOCK:
        require_stage(wd, "review", action="反映")
        res = apply_review(wd, base, provider, regenerate, ctx)
        record(wd, source, "apply", draft=base, calls=res.calls, regenerate=list(regenerate))
        return res


def article_info(wd: WorkDir, final_text: str) -> dict:
    d = wd.design()
    m = re.search(r"^#\s+(.+)$", final_text, re.MULTILINE)
    return {"title": m[1].strip() if m else "", "purpose": d.purpose, "takeaways": d.takeaways,
            "register": d.formality, "kind": d.kind, "target_length": d.target_length}


def confirm(wd: WorkDir, source: str, note: str = "", rewriter: Callable[[], Provider] | None = None,
            draft: str = "") -> dict:
    with LOCK:
        p = wd.project()
        cur = p.stage
        if cur == "done":
            raise StageError("もう完了しています（やり直すなら kumimasu restart）")
        if cur == "drafting" and source != "agent":
            raise StageError("下書きの段階はエージェントが書き終えて渡します（kumimasu confirm --agent）")
        if cur in HUMAN_STAGES and source == "agent":
            raise StageError(f"「{STAGE_LABEL[cur]}」は人が確定する段階です")
        if cur == "interview" and not wd.interview_file.exists():
            raise StageError("まだ質問がありません（kumimasu interview）")
        if cur == "design" and not wd.design_file.exists():
            raise StageError("まだ設計がありません（kumimasu design）")
        base = draft or wd.review_draft()
        if cur in ("drafting", "review"):
            require_base(base)
            if not wd.is_plain_file(base):
                raise StageError(f"{base} がありません（kumimasu draft）")
        if cur == "drafting":
            wd.save_project(wd.project().model_copy(update={"review_draft": base}))
        extra: dict = {}
        if cur == "review":
            ctx = ReviewContext.load(wd, base)
            if ctx.needs_apply():
                provider = rewriter() if rewriter and ctx.needs_rewrite_call() else None
                apply(wd, base, provider, source, ctx=ctx)
            final = final_name(base)
            extra = {"final": str((wd.root / final).resolve()), "draft": base,
                     "article": article_info(wd, wd.read(final)), "used": read_used(wd, base)}
        autos = [h for h in history(wd) if h["source"] == "auto" and h["stage"] == cur and h["round"] == p.round]
        handoff = {"stage": cur, "next": NEXT[cur], "round": p.round, "at": now(), "who": WHO[source],
                   "source": source, "note": note, "auto": autos} | extra
        record(wd, source, "confirm", note=note)
        set_stage(wd, NEXT[cur])
        atomic_write(wd.root / HANDOFF, json.dumps(handoff, ensure_ascii=False, indent=1) + "\n")
        append_jsonl(wd.root / HANDOFFS, handoff)
        return handoff


def handoffs(wd: WorkDir) -> list[dict]:
    return read_jsonl(wd.root / HANDOFFS)


def find_handoff(wd: WorkDir, for_stage: str, rows: list[dict] | None = None) -> dict | None:
    target = "review" if for_stage == "done" else for_stage
    r = wd.round
    found = [h for h in (handoffs(wd) if rows is None else rows) if h["stage"] == target and h["round"] == r]
    return found[-1] if found else None


def wait_for(wd: WorkDir, for_stage: str, timeout: float | None = None, interval: float = 1.0) -> dict | None:
    end = None if timeout is None else time.monotonic() + timeout
    while True:
        h = find_handoff(wd, for_stage)
        if h is not None:
            return h
        if end is not None and time.monotonic() >= end:
            return None
        time.sleep(interval)


RESTART_FROM = ("interview", "design", "drafting")


def restart(wd: WorkDir, from_stage: str, source: str) -> int:
    if from_stage not in RESTART_FROM:
        raise ValueError(f"--from は {', '.join(RESTART_FROM)} のどれかにしてください")
    with LOCK:
        p = wd.project()
        old_design = wd.design_file
        n = p.round + 1
        wd.save_project(p.model_copy(update={"stage": from_stage, "round": n, "review_draft": ""}))
        if from_stage == "drafting":
            if not old_design.exists():
                raise StageError(f"{old_design.name} がありません")
            atomic_write(wd.design_file, old_design.read_text(encoding="utf-8"))
        record(wd, source, "restart", from_stage=from_stage, previous_round=p.round)
        return n


VERSIONED = ("project.yaml", "units.yaml", "interview.yaml", "keep.yaml", HANDOFF)
VERSIONED_GLOBS = ("design*.yaml", "review.*.yaml", "draft*.md", "draft*.json", "check*.json", "research*.json")


def version(wd: WorkDir) -> str:
    parts = []
    for f in [wd.root / n for n in VERSIONED] + [p for g in VERSIONED_GLOBS for p in wd.root.glob(g)]:
        try:
            st = f.stat()
        except FileNotFoundError:
            continue
        parts.append(f"{f.name}:{st.st_size}:{st.st_mtime_ns}")
    return hashlib.sha1("|".join(sorted(parts)).encode()).hexdigest()[:16]


def stage_info(wd: WorkDir) -> dict:
    p = wd.project()
    rows = handoffs(wd)
    return {"stage": p.stage, "round": p.round, "stages": list(STAGES), "labels": STAGE_LABEL,
            "draft": wd.review_draft(), "handoff": find_handoff(wd, p.stage, rows) if p.stage == "done" else None,
            "last_handoff": rows[-1] if rows else None, "waiting": waiting(wd)}


AFTER = "終わると自動で次の画面に切り替わります。この間は編集できません。"
WORK = {
    "questions": ("質問を作る", "エージェントが質問を作っています",
                  "材料を読んで、材料からは分からないあなたの視点を聞く質問を作っています。"),
    "design": ("設計を提案する", "エージェントが設計を作っています",
               ("インタビューの答えと材料から、何を掘り下げ・何に触れ・何を書かないか、説明しない前提と脱線、"
                "書かない話題を提案しています。")),
    "research": ("ウェブで調べる", "エージェントがウェブで調べています",
                 "設計の「ウェブで調べること」だけを調査役に渡して、出典付きの事実を集めています（材料と回答は渡しません）。"),
    "draft": ("下書きを書く", "エージェントが下書きを書いています",
              "設計・使う材料・調査結果を 1 回の依頼で渡して、記事を通しで書かせています。"),
    "check": ("検査する", "エージェントが下書きを検査しています",
              "設計に照らして下書きを確かめています（書かない単位が出ていないか、掘り下げる単位が厚いか、作り話が無いか、読者に分からない所が無いか、など）。"),
    "finish": ("仕上げて渡す", "エージェントが仕上げています",
               "検査の結果を見て、構造の検査が落ちていれば 1 回だけ書き直し、最終チェックに渡します。"),
}
HUMAN_STEP = {"interview": "インタビュー（あなた）", "design": "設計の確認（あなた）", "review": "最終チェック（あなた）"}


def _scene(first: str, work: list[tuple[str, bool]], human: str) -> dict | None:
    reached = max((i for i, (_, done) in enumerate(work) if done), default=-1) + 1
    if reached == len(work):
        return None
    states = ["done"] * reached + ["now"] + ["todo"] * (len(work) - reached - 1)
    steps = [{"label": first, "state": "done"}, *({"label": WORK[k][0], "state": st} for (k, _), st in zip(work, states)),
             {"label": human, "state": "todo"}]
    current = work[reached][0]
    _, title, detail = WORK[current]
    return {"doing": current, "title": title, "detail": detail, "next": AFTER, "steps": steps}


def waiting(wd: WorkDir) -> dict | None:
    match wd.project().stage:
        case "interview":
            return _scene("材料を単位に分ける", [("questions", wd.interview_file.exists())], HUMAN_STEP["interview"])
        case "design":
            return _scene("インタビューを確定", [("design", wd.design_file.exists())], HUMAN_STEP["design"])
        case "drafting":
            base = wd.draft_base()
            has_research = wd.design_file.exists() and bool(wd.design().research)
            work = [("research", (wd.root / research_name(wd)).is_file())] if has_research else []
            work += [("draft", wd.is_plain_file(base)), ("check", (wd.root / f"{check_stem(base)}.json").is_file()),
                     ("finish", False)]
            return _scene("設計を確定", work, HUMAN_STEP["review"])
    return None

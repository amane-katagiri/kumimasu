from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from .check import check_stem
from .design import (
    RESEARCH_CHARS,
    RESEARCH_MAX,
    TAKEAWAYS_MAX,
    apply_noise,
    sync_design,
)
from .draft import read_used
from .errors import StepError
from .figures import figure_markers
from .files import PRIVATE_FILE, append_jsonl, atomic_write, read_jsonl
from .land import propose_followups, with_answer
from .model import (
    LANDS,
    LENGTH_MAX,
    LENGTH_MIN,
    STAGES,
    USES,
    Aside,
    Design,
    Interview,
    Rule,
    Skip,
    Unit,
    UnitUse,
)
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

try:
    import fcntl
except ImportError:
    fcntl = None

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
LOCK_FILE = ".lock"
_held = False


class StageError(StepError):
    pass


@contextmanager
def locked(wd: WorkDir) -> Iterator[None]:
    global _held
    with LOCK:
        # flock from a second descriptor would block this same process, so nested callers reuse the outer lock.
        if _held or fcntl is None:
            yield
            return
        fd = os.open(wd.root / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, PRIVATE_FILE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _held = True
            yield
        finally:
            _held = False
            os.close(fd)


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
    with locked(wd):
        require_stage(wd, "interview", action="質問への回答")
        iv = wd.interview()
        by_id = {q.id: q for q in iv.questions}
        unknown = set(answers) - set(by_id)
        if unknown:
            raise ValueError(f"知らない質問です: {', '.join(sorted(unknown))}")
        if not all(isinstance(text, str) for text in answers.values()):
            raise ValueError("答えは文字列にしてください")
        changed = [qid for qid, text in answers.items() if by_id[qid].answer != text]
        for qid in changed:
            by_id[qid].answer, by_id[qid].source = answers[qid], source
        if changed:
            wd.save_interview(iv)
            for qid in changed:
                record(wd, source, "answer", id=qid)
        return iv


def answer(wd: WorkDir, qid: str, text: str, source: str) -> Interview:
    return save_answers(wd, {qid: text}, source)


class SkipToggle(BaseModel):
    unit: str
    on: bool = False
    label: str = ""


class AsideToggle(BaseModel):
    unit: str
    on: bool = False
    where: str = ""


class DesignEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str | None = None
    form_prefs: str | None = None
    takeaways: list[str] | None = None
    order: list[str] | None = None
    avoid: list[str] | None = None
    research: list[str] | None = None
    forms: list[str] | None = None
    rules: list[Rule] | None = None
    skip: list[Skip] | None = None
    aside: list[Aside] | None = None
    toggle_skip: SkipToggle | None = None
    toggle_aside: AsideToggle | None = None
    target_length: int | None = None
    followup_limit: int | None = None
    note_limit: int | None = None
    units: dict[str, str] = {}
    explain: str | None = None
    notes: dict[str, str] = {}
    land: dict[str, str] = {}
    followups: dict[str, str] = {}


def update_design(wd: WorkDir, body: dict, source: str) -> Design:
    edit = DesignEdit.model_validate(body)
    with locked(wd):
        require_stage(wd, "design", action="設計の変更")
        units = {u.id: u for u in wd.units()}
        d = sync_design(wd.design(), list(units.values()))
        before = {u.id: u.use for u in d.units}
        upd: dict = {k: v for k in ("purpose", "form_prefs", "rules", "skip", "aside", "target_length")
                     if (v := getattr(edit, k)) is not None}
        for key in ("takeaways", "order", "avoid", "research", "forms"):
            if (xs := getattr(edit, key)) is not None:
                upd[key] = [x.strip() for x in xs if x.strip()]
        if len(upd.get("takeaways", [])) > TAKEAWAYS_MAX:
            raise ValueError(f"持ち帰りは {TAKEAWAYS_MAX} 個までです")
        if edit.target_length is not None and not LENGTH_MIN <= edit.target_length <= LENGTH_MAX:
            raise ValueError(f"目標の字数は {LENGTH_MIN} 以上 {LENGTH_MAX} 以下にしてください")
        if len(upd.get("research", [])) > RESEARCH_MAX or any(len(x) > RESEARCH_CHARS for x in upd.get("research", [])):
            raise ValueError(f"調べることは {RESEARCH_MAX} 個まで、それぞれ {RESEARCH_CHARS} 字までです")
        if (t := edit.toggle_skip) is not None:
            unit = _known_unit(units, t.unit)
            upd["skip"] = toggled_skip(d, unit.id, t.on, t.label, unit.text)
        if (a := edit.toggle_aside) is not None:
            upd["aside"] = toggled_aside(d, _known_unit(units, a.unit).id, a.on, a.where)
        if edit.followup_limit is not None:
            if edit.followup_limit < 0:
                raise ValueError("followup_limit は 0 以上にしてください")
            upd["followup_limit"] = edit.followup_limit
        if edit.note_limit is not None:
            if edit.note_limit < 1:
                raise ValueError("note_limit は 1 以上にしてください")
            upd["note_limit"] = edit.note_limit
        uses = edit.units
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
        if edit.explain is not None:
            d = explain_term(d, edit.explain)
        d = d.model_copy(update={
            "skip": [x.model_copy(update={"units": [i for i in x.units if uses.get(i, "drop") == "drop"]}) for x in d.skip],
            "aside": [a for a in d.aside if uses.get(a.id) != "drop"]})
        d = with_followups(with_land(apply_noise(d), edit.notes, edit.land, before), edit.followups)
        wd.save_design(d)
        record(wd, source, "design", keys=sorted(body))
        return d


def with_land(d: Design, notes: dict[str, str], lands: dict[str, str], before: dict[str, str]) -> Design:
    ids = set(d.unit_ids())
    unknown = (set(notes) | set(lands)) - ids
    if unknown:
        raise ValueError(f"知らない単位です: {', '.join(sorted(unknown))}")
    if any(v not in LANDS for v in lands.values()):
        raise ValueError(f"land は {', '.join(LANDS)} のどれかにしてください")
    aside = d.aside_ids()
    out = []
    for u in d.units:
        if u.id in notes and (text := notes[u.id].strip()) != u.note:
            if text and u.use == "drop":
                raise ValueError(f"{u.id} は書かない単位なので、一言を付けられません")
            u = u.model_copy(update={"note": text, "land": "author" if text else "bare", "followup": "",
                                     "followup_state": None})
        if u.id in lands:
            if lands[u.id] == "bare" and u.note.strip():
                raise ValueError(f"{u.id} には一言があるので bare にできません（先に一言を消してください）")
            u = u.model_copy(update={"land": lands[u.id]})
        if u.use == "mention" and before.get(u.id) != "mention" and u.land is None and u.id not in aside:
            u = u.model_copy(update={"land": "bare"})
        out.append(u)
    if sum(_has_note(u) for u in out) > d.note_limit:
        raise ValueError(f"一言は {d.note_limit} 個までです（note_limit）")
    return d.model_copy(update={"units": out})


def with_followups(d: Design, answers: dict[str, str]) -> Design:
    asked = {u.id for u in d.units if u.followup_state == "asked"}
    unknown = set(answers) - asked
    if unknown:
        raise ValueError(f"聞き返していない単位です: {', '.join(sorted(unknown))}")
    out = []
    for u in d.units:
        if u.id in answers:
            text = answers[u.id].strip()
            u = u.model_copy(update={"note": with_answer(u.note, text), "followup_state": "answered"} if text
                             else {"followup_state": "skipped"})
        out.append(u)
    return d.model_copy(update={"units": out})


def followup(wd: WorkDir, provider: Provider, source: str) -> Design:
    with locked(wd):
        require_stage(wd, "design", action="一言の聞き返し")
        units = wd.units()
        d, calls = propose_followups(sync_design(wd.design(), units), wd.project(), units, provider)
        if calls:
            wd.save_design(d)
        record(wd, source, "followup", calls=calls, asked=[u.id for u in d.units if u.followup_state == "asked"])
        return d


def _has_note(u: UnitUse) -> bool:
    return u.use in ("deep", "mention") and bool(u.note.strip())


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
    with locked(wd):
        require_stage(wd, "review", action="最終チェックの決定")
        require_base(base)
        rev = save_decisions(wd, base, body, source)
        record(wd, source, "decide", draft=base, items=[str(x.get("id")) for x in body.get("items", [])],
               remove=[str(x) for x in body.get("remove", [])])
        return rev


def apply(wd: WorkDir, base: str, provider: Provider | None, source: str, regenerate: tuple[str, ...] = (),
          ctx: ReviewContext | None = None) -> ApplyResult:
    with locked(wd):
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
    with locked(wd):
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
            text = wd.read(final)
            extra = {"final": str((wd.root / final).resolve()), "draft": base,
                     "article": article_info(wd, text), "used": read_used(wd, base),
                     "figures": [{"text": f["text"], "near": f["near"], "heading": f["heading"]}
                                 for f in figure_markers(text)]}
        autos = [h for h in history(wd) if h["source"] == "auto" and h["stage"] == cur and h["round"] == p.round]
        handoff = {"event": "handoff", "stage": cur, "next": NEXT[cur], "round": p.round, "at": now(), "who": WHO[source],
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
    found = [h for h in (handoffs(wd) if rows is None else rows) if h.get("stage") == target and h["round"] == r]
    return found[-1] if found else None


def wait_for(wd: WorkDir, for_stage: str, timeout: float | None = None, interval: float = 1.0) -> dict | None:
    end = None if timeout is None else time.monotonic() + timeout
    seen = len(handoffs(wd))
    while True:
        rows = handoffs(wd)
        restarts = [h for h in rows[seen:] if h.get("event") == "restart"]
        if restarts:
            return restarts[-1]
        h = find_handoff(wd, for_stage, rows)
        if h is not None:
            return h
        if end is not None and time.monotonic() >= end:
            return None
        time.sleep(interval)


RESTART_FROM = ("interview", "design", "drafting")
RESTART_MODES: dict[str, dict[str, tuple[str, str]]] = {
    "interview": {
        "keep": ("答えを直す（質問はそのまま）",
                 "今の質問と答えを引き継ぎます。答えを直して確定すると、エージェントが設計から作り直します。"),
        "regenerate": ("質問から作り直す",
                       "エージェントが質問を作り直します。今の質問と答えは {archive} に残り、新しい質問には引き継がれません。"),
    },
    "design": {
        "keep": ("設計を直す（今の設計を引き継ぐ）", "今の設計を新しいラウンドに写します。直して確定すると、エージェントが書き直します。"),
        "regenerate": ("設計を作り直す", "エージェントがインタビューの答えと材料から設計を提案し直します。"),
    },
    "drafting": {
        "regenerate": ("書き直す（設計はそのまま）", "今の設計のまま、エージェントが下書き・検査・最終チェックへの受け渡しをやり直します。"),
    },
}
DEFAULT_MODE = {"interview": "keep", "design": "regenerate", "drafting": "regenerate"}


def interview_archive(round_: int) -> str:
    return f"interview.r{round_}.yaml"


def restart_options(wd: WorkDir) -> dict[str, list[dict]]:
    p = wd.project()
    cur = STAGES.index(p.stage)
    return {s: [{"mode": m, "label": label, "detail": detail.format(archive=interview_archive(p.round))}
                for m, (label, detail) in RESTART_MODES[s].items()]
            for s in RESTART_FROM if STAGES.index(s) < cur}


def restart(wd: WorkDir, from_stage: str, source: str, mode: str | None = None, note: str = "") -> dict:
    if from_stage not in RESTART_FROM:
        raise ValueError(f"--from は {', '.join(RESTART_FROM)} のどれかにしてください")
    mode = mode or DEFAULT_MODE[from_stage]
    modes = RESTART_MODES[from_stage]
    if mode not in modes:
        raise ValueError(f"「{STAGE_LABEL[from_stage]}」からのやり直しに {mode} は使えません"
                         f"（使えるのは {'・'.join(modes)}）")
    with locked(wd):
        p = wd.project()
        if STAGES.index(from_stage) >= STAGES.index(p.stage):
            raise StageError(f"やり直せるのは今より前の段階だけです（今は「{STAGE_LABEL[p.stage]}」、"
                             f"「{STAGE_LABEL[from_stage]}」からはやり直せません）")
        old_design = wd.design_file
        carry_design = from_stage == "drafting" or (from_stage == "design" and mode == "keep")
        if carry_design and not old_design.exists():
            raise StageError(f"{old_design.name} がないので、設計を引き継げません")
        archive = wd.root / interview_archive(p.round)
        regen_interview = from_stage == "interview" and mode == "regenerate"
        n = p.round + 1
        if regen_interview and wd.interview_file.exists():
            wd.interview_file.rename(archive)
        wd.save_project(p.model_copy(update={"stage": from_stage, "round": n, "review_draft": ""}))
        if carry_design:
            atomic_write(wd.design_file, old_design.read_text(encoding="utf-8"))
        event = {"event": "restart", "from": from_stage, "mode": mode, "round": n, "previous_round": p.round,
                 "previous_stage": p.stage, "at": now(), "who": WHO[source], "source": source, "note": note}
        if regen_interview and archive.exists():
            event["archived"] = archive.name
        record(wd, source, "restart", from_stage=from_stage, mode=mode, previous_round=p.round, note=note,
               **({"archived": event["archived"]} if "archived" in event else {}))
        append_jsonl(wd.root / HANDOFFS, event)
        return event


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
            "last_handoff": rows[-1] if rows else None, "waiting": waiting(wd, rows),
            "restart": restart_options(wd)}


AFTER = "終わると自動で次の画面に切り替わります。この間は編集できません。"
WORK = {
    "questions": ("質問を作る", "エージェントが質問を作っています",
                  "材料を読んで、材料からは分からないあなたの視点を聞く質問を作っています。"),
    "design": ("設計を提案する", "エージェントが設計を作っています",
               ("インタビューの答えと材料から、何を掘り下げ・何に触れ・何を書かないか、説明しない前提と脱線、"
                "書かない話題と、一言を聞く結果の単位を提案しています。")),
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


def last_restart(wd: WorkDir, rows: list[dict] | None = None) -> dict | None:
    p = wd.project()
    found = [h for h in (handoffs(wd) if rows is None else rows)
             if h.get("event") == "restart" and h["round"] == p.round and h["from"] == p.stage]
    return found[-1] if found else None


def waiting(wd: WorkDir, rows: list[dict] | None = None) -> dict | None:
    cur = wd.project().stage
    r = last_restart(wd, rows) if cur in RESTART_FROM else None
    restarted = f"ラウンド {r['round']} としてやり直す" if r else ""
    match cur:
        case "interview":
            return _scene(restarted or "材料を単位に分ける", [("questions", wd.interview_file.exists())],
                          HUMAN_STEP["interview"])
        case "design":
            return _scene(restarted or "インタビューを確定", [("design", wd.design_file.exists())], HUMAN_STEP["design"])
        case "drafting":
            base = wd.draft_base()
            has_research = wd.design_file.exists() and bool(wd.design().research)
            work = [("research", (wd.root / research_name(wd)).is_file())] if has_research else []
            work += [("draft", wd.is_plain_file(base)), ("check", (wd.root / f"{check_stem(base)}.json").is_file()),
                     ("finish", False)]
            return _scene(restarted or "設計を確定", work, HUMAN_STEP["review"])
    return None

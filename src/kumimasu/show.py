from __future__ import annotations

import re

from . import ops
from .design import sync_design
from .workdir import WorkDir

NEXT_STEP = {
    "interview": "人: 質問に答えて確定する（画面か、チャットで answer → confirm）",
    "design": "人: 設計を見て直し、確定する（画面か、チャットで set / rule → confirm）",
    "drafting": "エージェント: draft → check →（必要なら revise）→ confirm --agent",
    "review": "人: 当たった箇所を 残す／削る／書き直す に決め、確定する（画面か、チャットで decide → confirm）",
    "done": "完了。handoff.json の final が確定した記事",
}
SHORT = 70


def _short(text: str, n: int = SHORT) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= n else t[:n] + "…"


def snapshot(wd: WorkDir, stage: str | None = None) -> dict:
    p = wd.project()
    part = stage or ("review" if p.stage == "done" else p.stage)
    snap: dict = {"topic": p.topic, "stage": p.stage, "round": p.round, "next": NEXT_STEP[p.stage], "showing": part,
                  "last_handoff": (ops.handoffs(wd) or [None])[-1]}
    if part == "interview" and wd.interview_file.exists():
        snap["interview"] = [{"id": q.id, "question": q.question, "answer": q.answer, "source": q.source}
                             for q in wd.interview().questions]
    elif part in ("design", "drafting") and wd.design_file.exists():
        units = wd.units()
        d = sync_design(wd.design(), units)
        by = {u.id: u for u in units}
        snap["design"] = {
            "file": wd.design_file.name, "purpose": d.purpose, "takeaways": d.takeaways, "target_length": d.target_length,
            "order": d.order, "avoid": d.avoid,
            "units": {use: [{"id": u.id, "text": _short(by[u.id].text, 40), "searchable": by[u.id].searchable or "answer"}
                            for u in d.units if u.use == use and u.id in by] for use in ("deep", "mention", "drop")},
            "skip": [s.model_dump() for s in d.skip], "aside": [a.model_dump() for a in d.aside],
            "rules": [{"n": i, "on": r.on, "text": r.text} for i, r in enumerate(d.rules, 1)],
            "warnings": [c.message() for c in d.live_conflicts()]}
    elif part in ("review", "done"):
        from .review import final_name, load_review, needs_apply

        base = wd.review_draft()
        if (wd.root / base).is_file():
            rev = load_review(wd, base)
            snap["review"] = {
                "draft": base, "final": final_name(base) if (wd.root / final_name(base)).is_file() else None,
                "needs_apply": needs_apply(wd, base),
                "items": [{"id": i.id, "kind": i.kind, "category": i.category, "votes": i.votes, "decision": i.decision,
                           "note": i.note, "text": _short(i.text), "placed": i.start is not None,
                           "rewrite": ({"result": _short(i.rewrite.result, 120), "source": i.rewrite.source}
                                       if i.rewrite else None),
                           "stale": i.stale, "note_changed": i.note_changed, "by": i.source} for i in rev.items]}
    return snap


def text(snap: dict) -> str:
    lines = [f"{snap['topic']}", f"段階: {ops.STAGE_LABEL[snap['stage']]}（ラウンド {snap['round']}）  次: {snap['next']}"]
    if iv := snap.get("interview"):
        lines.append("\n[インタビュー]")
        for q in iv:
            lines.append(f"{q['id']}. {q['question']}")
            lines.append(f"    答え: {q['answer'] or '（未回答）'}" + (f"  [{q['source']}]" if q["source"] else ""))
    if d := snap.get("design"):
        lines.append(f"\n[設計 {d['file']}]")
        lines.append(f"ねらい: {d['purpose']}")
        lines += [f"持ち帰り {i}: {t}" for i, t in enumerate(d["takeaways"], 1)]
        lines.append(f"目標の字数: {d['target_length']}" + (f"  順番: {' / '.join(d['order'])}" if d["order"] else ""))
        for use, label in (("deep", "掘り下げる"), ("mention", "触れる"), ("drop", "書かない")):
            us = d["units"][use]
            lines.append(f"{label} {len(us)}: " + ", ".join(f"{u['id']}「{u['text']}」" for u in us[:12])
                         + (" …" if len(us) > 12 else ""))
        lines += [f"説明しない前提: {s['label']} ({', '.join(s['units'])})" for s in d["skip"]]
        lines += [f"脱線: {a['id']} @ {a['where']}" for a in d["aside"]]
        lines += [f"警告: {w}" for w in d["warnings"]]
        if d["avoid"]:
            lines.append("書かない話題: " + " / ".join(d["avoid"]))
        lines += [f"ルール {r['n']:2} [{'on ' if r['on'] else 'off'}] {r['text']}" for r in d["rules"]]
    if r := snap.get("review"):
        lines.append(f"\n[最終チェック {r['draft']}]  結果: {r['final'] or '（まだ）'}"
                     + ("  ※決定が反映より新しい" if r["needs_apply"] and r["final"] else ""))
        for i in r["items"]:
            tag = f"{i['kind']}" + (f"/{i['category']}" if i["category"] else "") + (f" {i['votes']}" if i["votes"] else "")
            dec = {"": "未決", "keep": "残す", "delete": "削る", "rewrite": "書き直す"}[i["decision"]]
            extra = ""
            if i["decision"] == "rewrite":
                extra = f" → {i['rewrite']['result']}（{i['rewrite']['source']}・固定）" if i["rewrite"] else "（未生成）"
            flags = (" ※古い" if i["stale"] else "") + (" ※メモ変更" if i["note_changed"] else "") \
                + ("" if i["placed"] else " ※位置不明")
            lines.append(f"{i['id']} [{tag}] {dec}{flags}「{i['text']}」{extra}" + (f" メモ: {i['note']}" if i["note"] else ""))
    if (h := snap.get("last_handoff")) and snap["stage"] == "done":
        lines.append(f"\n確定した記事: {h.get('final')}")
    return "\n".join(lines) + "\n"

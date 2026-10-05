from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Annotated, get_args

import typer

from . import auto as stand_in
from . import cli_common as cc
from . import ops
from . import prefs as pf
from .cli_common import DirArg, DraftOpt, RewriterOpt, SourceOpt, app, errors
from .config import PROJECT_FILE, init_template, user_path
from .design import TAKEAWAYS_MAX
from .model import STAGES
from .review import Decision
from .show import snapshot, text

DECISIONS = tuple(d for d in get_args(Decision) if d)
AUTO_STAGES = ops.HUMAN_STAGES
DIFF_LABEL = {"added": "追加", "removed": "削除", "edited": "書き換え", "disabled": "オフ", "enabled": "オン",
              "changed": "変更"}
ScopeOpt = Annotated[str | None, typer.Option("--scope", help="user | project")]
OnlyOpt = Annotated[str, typer.Option("--only", help="Comma-separated item numbers (or keys for prefs) to save")]


def _stage_choice(value: str, choices: tuple[str, ...], flag: str) -> str:
    if value not in choices:
        raise typer.BadParameter(f"{flag} は {', '.join(choices)} のどれかにしてください")
    return value


@app.command()
def show(path: DirArg, stage: Annotated[str | None, typer.Option("--stage", help="Show this stage's part")] = None,
         as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Compact state for the agent to read and explain: stage, what the person can decide now, and the current values."""
    wd = cc.workdir(path)
    if stage is not None:
        _stage_choice(stage, STAGES, "--stage")
    with errors():
        snap = snapshot(wd, stage)
    typer.echo(json.dumps(snap, ensure_ascii=False, indent=1) if as_json else text(snap))


@app.command()
def answer(path: DirArg, qid: Annotated[str, typer.Argument(help="Question id, e.g. q1")],
           answer_text: Annotated[str, typer.Argument(metavar="TEXT", help="The answer (empty string clears it)")],
           source: SourceOpt = "agent-chat") -> None:
    """Answer one interview question."""
    wd = cc.workdir(path)
    with errors():
        ops.answer(wd, qid, answer_text, cc.source(source))
    typer.echo(f"{qid}: saved")


def _design_body(wd, what: str, a: list[str], use: str | None, label: str, where: str) -> dict:
    d = wd.design()
    match what:
        case "unit":
            if len(a) != 1 or use is None:
                raise ValueError("使い方: set DIR unit ID --use deep|mention|drop")
            return {"units": {a[0]: use}}
        case "takeaway" | "avoid" | "research":
            key = {"takeaway": "takeaways"}.get(what, what)
            cur = list(getattr(d, key))
            if a[:1] == ["add"] and len(a) == 2:
                cur.append(a[1])
            elif a[:1] == ["rm"] and len(a) == 2:
                cur.pop(pf.list_index(a[1], len(cur)))
            elif len(a) == 2 and what == "takeaway":
                cur[pf.list_index(a[0], len(cur))] = a[1]
            else:
                raise ValueError(f"使い方: set DIR {what} add TEXT | rm N" + (" | N TEXT" if what == "takeaway" else ""))
            if what == "takeaway" and len(cur) > TAKEAWAYS_MAX:
                raise ValueError(f"持ち帰りは {TAKEAWAYS_MAX} 個までです")
            return {key: cur}
        case "purpose" | "forms":
            if len(a) != 1:
                raise ValueError(f"使い方: set DIR {what} TEXT")
            return {"purpose" if what == "purpose" else "form_prefs": a[0]}
        case "order":
            return {"order": a}
        case "length":
            if len(a) != 1 or not a[0].isdigit():
                raise ValueError("使い方: set DIR length N")
            return {"target_length": int(a[0])}
        case "skip" | "aside":
            if len(a) != 2 or a[1] not in ("on", "off"):
                raise ValueError(f"使い方: set DIR {what} ID on|off")
            extra = {"label": label} if what == "skip" else {"where": where}
            return {f"toggle_{what}": {"unit": a[0], "on": a[1] == "on"} | extra}
    raise ValueError(f"知らない対象です: {what}")


@app.command("set")
def set_(path: DirArg,
         what: Annotated[str, typer.Argument(help="unit | takeaway | purpose | order | length | skip | aside | avoid | "
                                                  "research | forms")],
         args: Annotated[list[str] | None, typer.Argument(help="See the examples below")] = None,
         use: Annotated[str | None, typer.Option("--use", help="deep | mention | drop (for `unit`)")] = None,
         label: Annotated[str, typer.Option("--label", help="Skip label (for `skip ID on`)")] = "",
         where: Annotated[str, typer.Option("--where", help="Placement hint (for `aside ID on`)")] = "",
         source: SourceOpt = "agent-chat") -> None:
    """Edit the design like the page does.

    \b
    set DIR unit m12 --use deep
    set DIR takeaway 2 "text"   |  set DIR takeaway add "text"  |  set DIR takeaway rm 2
    set DIR purpose "text"      |  set DIR length 5000
    set DIR order "hint 1" "hint 2"   (no hints clears the list)
    set DIR skip m5 on [--label "ISBN の構造"]  |  set DIR skip m5 off
    set DIR aside m42 on [--where "API を比べた所"]  |  set DIR aside m42 off
    set DIR avoid add "FAQ"  |  set DIR avoid rm 1
    set DIR research add "exiftool の -d の書式"  |  set DIR research rm 1   (sent to the web researcher)
    set DIR forms "比較は表"   (the form preferences shown to the designer and the writer)"""
    wd = cc.workdir(path, "design", action="設計の変更")
    src = cc.source(source)
    with errors():
        ops.update_design(wd, _design_body(wd, what, list(args or []), use, label, where), src)
    typer.echo(f"{what}: saved")


@app.command()
def rule(path: DirArg, action: Annotated[str, typer.Argument(help="on | off | edit | add | rm | list")],
         args: Annotated[list[str] | None, typer.Argument(help="on N / off N / edit N TEXT / add TEXT / rm N")] = None,
         source: SourceOpt = "agent-chat") -> None:
    """Edit the writing rules (numbers are 1-based, as in `rule DIR list`)."""
    if action == "list":
        wd = cc.workdir(path)
        with errors():
            rules = wd.design().rules
        for i, r in enumerate(rules, 1):
            typer.echo(f"{i:2}. [{'on ' if r.on else 'off'}] {r.text}")
        return
    wd = cc.workdir(path, "design", action="ルールの変更")
    src = cc.source(source)
    with errors():
        rules = pf.edit_rule_list(wd.design().rules, action, list(args or []))
        ops.update_design(wd, {"rules": [r.model_dump() for r in rules]}, src)
    typer.echo(f"rule {action}: saved")


@app.command()
def decide(path: DirArg, item: Annotated[str, typer.Argument(help="Item id from `show`")],
           decision: Annotated[str, typer.Argument(help="keep | delete | rewrite | none")],
           note: Annotated[str | None, typer.Option("--note")] = None,
           result: Annotated[str | None, typer.Option("--result", help="Your own rewrite (locked)")] = None,
           regenerate: Annotated[bool, typer.Option("--regenerate", help="Drop the stored rewrite; the next apply makes a new one")] = False,
           draft_name: DraftOpt = None, source: SourceOpt = "agent-chat") -> None:
    """Decide one final-check item (same as the buttons in the page)."""
    _stage_choice(decision, (*DECISIONS, "none"), "decision")
    wd = cc.workdir(path)
    x: dict = {"id": item, "decision": "" if decision == "none" else decision}
    if note is not None:
        x["note"] = note
    if result is not None:
        x["result"] = result
    if regenerate:
        x["regenerate"] = True
    with errors():
        ops.decide(wd, draft_name or wd.review_draft(), {"items": [x]}, cc.source(source))
    typer.echo(f"{item}: {decision}")


@app.command("add-item")
def add_item(path: DirArg, start: Annotated[int, typer.Option("--start", help="Character offset in the draft")],
             end: Annotated[int, typer.Option("--end")],
             decision: Annotated[str, typer.Option("--decision", help="keep | delete | rewrite")] = "delete",
             note: Annotated[str, typer.Option("--note")] = "",
             draft_name: DraftOpt = None, source: SourceOpt = "agent-chat") -> None:
    """Add your own final-check item for a span of the draft (like selecting text in the page)."""
    _stage_choice(decision, DECISIONS, "--decision")
    wd = cc.workdir(path)
    iid = f"user-{secrets.token_hex(4)}"
    with errors():
        ops.decide(wd, draft_name or wd.review_draft(),
                   {"items": [{"id": iid, "start": start, "end": end, "decision": decision, "note": note}]},
                   cc.source(source))
    typer.echo(iid)


@app.command("remove-item")
def remove_item(path: DirArg, item: Annotated[str, typer.Argument()], draft_name: DraftOpt = None,
                source: SourceOpt = "agent-chat") -> None:
    """Remove one of your own final-check items."""
    wd = cc.workdir(path)
    with errors():
        ops.decide(wd, draft_name or wd.review_draft(), {"remove": [item]}, cc.source(source))
    typer.echo(f"{item}: removed")


@app.command()
def confirm(path: DirArg, note: Annotated[str, typer.Option("--note", help="Instruction for the agent")] = "",
            agent: Annotated[bool, typer.Option("--agent", help="The agent finished the drafting stage")] = False,
            draft_name: Annotated[str | None, typer.Option("--draft", help="With --agent: the draft to hand over for the final check")] = None,
            rewriter: RewriterOpt = None, source: SourceOpt = "agent-chat") -> None:
    """確定して渡す: finish the current stage and write handoff.json."""
    wd = cc.workdir(path)
    src = "agent" if agent else cc.source(source)
    cfg = cc.config(path)
    with errors():
        h = ops.confirm(wd, src, note, lambda: cc.llm(cfg, "rewriter", rewriter), draft_name or "")
    typer.echo(json.dumps(h, ensure_ascii=False, indent=1))


@app.command()
def wait(path: DirArg, for_stage: Annotated[str, typer.Option("--for", help="interview | design | drafting | review | done")],
         timeout: Annotated[float | None, typer.Option("--timeout", help="Seconds (default: no limit)")] = None,
         interval: Annotated[float, typer.Option("--interval")] = 2.0) -> None:
    """Block until the handoff that completes this stage (in the current round) exists; print it. Run it in the
    background so the agent is woken up when the person confirms. Exit 2 on timeout."""
    _stage_choice(for_stage, STAGES, "--for")
    wd = cc.workdir(path)
    h = ops.wait_for(wd, for_stage, timeout, interval)
    if h is None:
        typer.echo(f"timeout: {for_stage} の確定はまだありません", err=True)
        raise typer.Exit(2)
    typer.echo(json.dumps(h, ensure_ascii=False, indent=1))


@app.command()
def auto(path: DirArg, stage: Annotated[str, typer.Option("--stage", help="interview | design | review")],
         provider: Annotated[str | None, typer.Option("--provider", help="Default: config providers.auto")] = None,
         rewriter: Annotated[str | None, typer.Option("--rewriter", help="Default: config providers.rewriter")] = None) -> None:
    """Let the agent stand in for the person at one checkpoint (not recommended; see the warning)."""
    _stage_choice(stage, AUTO_STAGES, "--stage")
    typer.echo(f"warning: {stand_in.WARNING}", err=True)
    wd = cc.workdir(path)
    cfg = cc.config(path)
    with errors():
        match stage:
            case "interview":
                h = stand_in.auto_interview(wd, cc.llm(cfg, "auto", provider))
            case "design":
                h = stand_in.auto_design(wd)
            case _:
                h = stand_in.auto_review(wd, cc.llm(cfg, "auto", provider), lambda: cc.llm(cfg, "rewriter", rewriter))
    typer.echo(json.dumps(h, ensure_ascii=False, indent=1))


@app.command()
def restart(path: DirArg, from_stage: Annotated[str, typer.Option("--from", help="interview | design | drafting")],
            source: SourceOpt = "agent-chat") -> None:
    """Start a new round instead of going back in place: material and interview stay; the new round gets its own
    design.rN.yaml and draft.rN.md (from drafting, the design is copied over)."""
    wd = cc.workdir(path)
    with errors():
        n = ops.restart(wd, from_stage, cc.source(source))
    typer.echo(f"round {n}: stage {from_stage}; design {wd.design_file.name}, draft {wd.draft_base()}")


@app.command()
def config(target: Annotated[str | None, typer.Argument(help="Work directory, or `init`")] = None,
           as_json: Annotated[bool, typer.Option("--json")] = False,
           user: Annotated[bool, typer.Option("--user", help="With init: the user file ($KUMIMASU_CONFIG or "
                                              "~/.config/kumimasu/config.yaml)")] = False,
           project: Annotated[bool, typer.Option("--project", help="With init: ./kumimasu.yaml")] = False) -> None:
    """Show the effective settings and which layer each comes from; `config init --user|--project` writes a
    commented template (never overwrites)."""
    if target == "init":
        if user == project:
            raise typer.BadParameter("--user か --project のどちらか一つを付けてください")
        with errors():
            path = init_template(user_path() if user else Path.cwd() / PROJECT_FILE)
        typer.echo(f"wrote {path}")
        return
    rows = cc.config(Path(target) if target else None).effective()
    if as_json:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    for r in rows:
        where = r["layer"] + (f" ({r['file']})" if r["file"] and r["layer"] != "default" else "")
        ignored = "".join(f"  [使わない: {f}]" for f in r["ignored"])
        typer.echo(f"{r['key']:26} {r['value']!s:40} {where}{ignored}")


def _split(only: str) -> list[str]:
    return [x.strip() for x in only.split(",") if x.strip()]


def _need_scope(scope: str | None) -> str:
    if scope is None:
        raise typer.BadParameter("--scope user|project を付けてください")
    return scope


@app.command()
def rules(action: Annotated[str, typer.Argument(help="list | on | off | add | edit | rm | diff | save")],
          args: Annotated[list[str] | None, typer.Argument(help="list/on N/off N/add TEXT/edit N TEXT/rm N, or DIR for diff/save")] = None,
          scope: ScopeOpt = None, only: OnlyOpt = "") -> None:
    """The default writing rules (the rules file), not one article's rules (that is `rule DIR …`).

    \b
    rules list --scope user|project
    rules on N | off N | add "…" | edit N "…" | rm N --scope user|project
    rules diff DIR                      this article's rules vs the effective defaults
    rules save DIR --scope user|project [--only 1,3]"""
    a = list(args or [])
    cwd = Path.cwd()
    if action in ("diff", "save"):
        if len(a) != 1:
            raise typer.BadParameter(f"使い方: rules {action} DIR")
        wd = cc.workdir(Path(a[0]))
        cfg = cc.config(wd.root)
        if action == "diff":
            with errors():
                diffs = pf.rules_diff(wd, cfg)
            for d in diffs:
                typer.echo(f"{d.n:2}. {DIFF_LABEL[d.kind]:4} {d.text}" + (f"  （元: {d.old}）" if d.old else ""))
            if not diffs:
                typer.echo("既定との違いはありません")
            return
        scope = _need_scope(scope)
        with errors():
            path, picked, notes = pf.rules_save(wd, cfg, scope, _split(only), cwd)
        for n in notes:
            typer.echo(n)
        typer.echo(f"wrote {path}: {len(picked)} changes")
        return
    scope = _need_scope(scope)
    cfg = cc.config()
    if action == "list":
        with errors():
            path, _ = pf.rules_target(scope, cwd, cfg, create=False)
            rows = pf.read_target(path)
        typer.echo(f"{path}" + ("" if path.exists() else "（まだ無いので、同梱の既定を表示しています）"))
        for i, r in enumerate(rows, 1):
            typer.echo(f"{i:2}. [{'on ' if r.on else 'off'}] {r.text}")
        return
    with errors():
        path, _, notes = pf.edit_rules(scope, action, a, cwd, cfg)
    for n in notes:
        typer.echo(n)
    typer.echo(f"wrote {path}")


@app.command("prefs")
def prefs_cmd(action: Annotated[str, typer.Argument(help="diff | save")], path: DirArg,
              scope: ScopeOpt = None, only: OnlyOpt = "") -> None:
    """Other defaults (register, drop_list, avoid, noise counts, forms): what this article's design changed, and
    writing chosen ones back to the user or project config."""
    _stage_choice(action, ("diff", "save"), "action")
    wd = cc.workdir(path)
    cfg = cc.config(wd.root)
    if action == "diff":
        with errors():
            diffs = pf.prefs_diff(wd, cfg)
        for d in diffs:
            typer.echo(f"{d.n:2}. {d.key:26} {DIFF_LABEL[d.kind]:4} {d.value!r}  （既定: {d.default!r}）")
        if not diffs:
            typer.echo("既定との違いはありません")
        return
    scope = _need_scope(scope)
    with errors():
        target, picked = pf.prefs_save(wd, cfg, scope, _split(only), Path.cwd())
    typer.echo(f"wrote {target}: {', '.join(sorted({d.key for d in picked})) or 'nothing'}")

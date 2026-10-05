"""Checkpoint commands: the CLI mirror of every page operation (same functions in ops.py), plus show / confirm /
wait / auto / restart for an agent that runs the loop and talks with the user."""
from __future__ import annotations

import json
import secrets
from typing import Annotated

import typer

from . import ops
from .cli import DirArg, SourceOpt, _cfg, _fail, _llm, _wd, app
from .model import SOURCES, STAGES
from .errors import StepError


def _source(source: str) -> str:
    if source not in SOURCES or source == "agent":
        raise typer.BadParameter("--source must be one of human-ui, agent-chat, auto")
    return source


def _run(fn):
    try:
        return fn()
    except (StepError, ValueError, KeyError) as e:
        _fail(e)


@app.command()
def show(path: DirArg, stage: Annotated[str | None, typer.Option("--stage", help="Show this stage's part")] = None,
         as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Compact state for the agent to read and explain: stage, what the person can decide now, and the current values."""
    from .show import snapshot, text

    wd = _wd(path)
    if stage is not None and stage not in STAGES:
        raise typer.BadParameter(f"--stage must be one of {', '.join(STAGES)}")
    snap = _run(lambda: snapshot(wd, stage))
    typer.echo(json.dumps(snap, ensure_ascii=False, indent=1) if as_json else text(snap))


@app.command()
def answer(path: DirArg, qid: Annotated[str, typer.Argument(help="Question id, e.g. q1")],
           text: Annotated[str, typer.Argument(help="The answer (empty string clears it)")],
           source: SourceOpt = "agent-chat") -> None:
    """Answer one interview question."""
    wd = _wd(path)
    _run(lambda: ops.answer(wd, qid, text, _source(source)))
    typer.echo(f"{qid}: saved")


def _index(n: str, size: int) -> int:
    if not n.isdigit() or not 1 <= int(n) <= size:
        raise ValueError(f"number must be 1–{size}")
    return int(n) - 1


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
    wd = _wd(path)
    a = list(args or [])
    src = _source(source)
    _run(lambda: ops.require_stage(wd, "design", action="設計の変更"))

    def body() -> dict:
        d = wd.design()
        match what:
            case "unit":
                if len(a) != 1 or use is None:
                    raise ValueError("usage: set DIR unit ID --use deep|mention|drop")
                return {"units": {a[0]: use}}
            case "takeaway" | "avoid" | "research":
                cur = list({"takeaway": d.takeaways, "avoid": d.avoid, "research": d.research}[what])
                if a[:1] == ["add"] and len(a) == 2:
                    cur.append(a[1])
                elif a[:1] == ["rm"] and len(a) == 2:
                    cur.pop(_index(a[1], len(cur)))
                elif len(a) == 2 and what == "takeaway":
                    cur[_index(a[0], len(cur))] = a[1]
                else:
                    raise ValueError(f"usage: set DIR {what} add TEXT | rm N" + (" | N TEXT" if what == "takeaway" else ""))
                if what == "takeaway" and len(cur) > 3:
                    raise ValueError("takeaways are at most 3")
                return {{"takeaway": "takeaways"}.get(what, what): cur}
            case "purpose" | "forms":
                if len(a) != 1:
                    raise ValueError(f"usage: set DIR {what} TEXT")
                return {"purpose" if what == "purpose" else "form_prefs": a[0]}
            case "order":
                return {"order": a}
            case "length":
                if len(a) != 1 or not a[0].isdigit():
                    raise ValueError("usage: set DIR length N")
                return {"target_length": int(a[0])}
            case "skip" | "aside":
                if len(a) != 2 or a[1] not in ("on", "off"):
                    raise ValueError(f"usage: set DIR {what} ID on|off")
                units = {u.id: u for u in wd.units()}
                if a[0] not in units:
                    raise ValueError(f"unknown unit id: {a[0]}")
                if what == "skip":
                    return {"skip": ops.toggled_skip(d, a[0], a[1] == "on", label, units[a[0]].text)}
                return {"aside": ops.toggled_aside(d, a[0], a[1] == "on", where)}
        raise ValueError(f"unknown target: {what}")

    _run(lambda: ops.update_design(wd, body(), src))
    typer.echo(f"{what}: saved")


@app.command()
def rule(path: DirArg, action: Annotated[str, typer.Argument(help="on | off | edit | add | rm | list")],
         args: Annotated[list[str] | None, typer.Argument(help="on N / off N / edit N TEXT / add TEXT / rm N")] = None,
         source: SourceOpt = "agent-chat") -> None:
    """Edit the writing rules (numbers are 1-based, as in `rule DIR list`)."""
    wd = _wd(path)
    a = list(args or [])
    if action != "list":
        _run(lambda: ops.require_stage(wd, "design", action="ルールの変更"))
    rules = [r.model_dump() for r in _run(wd.design).rules]
    if action == "list":
        for i, r in enumerate(rules, 1):
            typer.echo(f"{i:2}. [{'on ' if r['on'] else 'off'}] {r['text']}")
        return

    def body() -> dict:
        match action:
            case "on" | "off" if len(a) == 1:
                rules[_index(a[0], len(rules))]["on"] = action == "on"
            case "edit" if len(a) == 2:
                rules[_index(a[0], len(rules))]["text"] = a[1]
            case "add" if len(a) == 1:
                rules.append({"text": a[0], "on": True})
            case "rm" if len(a) == 1:
                rules.pop(_index(a[0], len(rules)))
            case _:
                raise ValueError("usage: rule DIR on N | off N | edit N TEXT | add TEXT | rm N | list")
        return {"rules": rules}

    _run(lambda: ops.update_design(wd, body(), _source(source)))
    typer.echo(f"rule {action}: saved")


@app.command()
def decide(path: DirArg, item: Annotated[str, typer.Argument(help="Item id from `show`")],
           decision: Annotated[str, typer.Argument(help="keep | delete | rewrite | none")],
           note: Annotated[str | None, typer.Option("--note")] = None,
           result: Annotated[str | None, typer.Option("--result", help="Your own rewrite (locked)")] = None,
           regenerate: Annotated[bool, typer.Option("--regenerate", help="Drop the stored rewrite; the next apply makes a new one")] = False,
           draft_name: Annotated[str | None, typer.Option("--draft")] = None,
           source: SourceOpt = "agent-chat") -> None:
    """Decide one final-check item (same as the buttons in the page)."""
    if decision not in ("keep", "delete", "rewrite", "none"):
        raise typer.BadParameter("decision must be keep, delete, rewrite or none")
    wd = _wd(path)
    x: dict = {"id": item, "decision": "" if decision == "none" else decision}
    if note is not None:
        x["note"] = note
    if result is not None:
        x["result"] = result
    if regenerate:
        x["regenerate"] = True
    _run(lambda: ops.decide(wd, draft_name or wd.review_draft(), {"items": [x]}, _source(source)))
    typer.echo(f"{item}: {decision}")


@app.command("add-item")
def add_item(path: DirArg, start: Annotated[int, typer.Option("--start", help="Character offset in the draft")],
             end: Annotated[int, typer.Option("--end")],
             decision: Annotated[str, typer.Option("--decision", help="keep | delete | rewrite")] = "delete",
             note: Annotated[str, typer.Option("--note")] = "",
             draft_name: Annotated[str | None, typer.Option("--draft")] = None,
             source: SourceOpt = "agent-chat") -> None:
    """Add your own final-check item for a span of the draft (like selecting text in the page)."""
    wd = _wd(path)
    iid = f"user-{secrets.token_hex(4)}"
    _run(lambda: ops.decide(wd, draft_name or wd.review_draft(),
                            {"items": [{"id": iid, "start": start, "end": end, "decision": decision, "note": note}]},
                            _source(source)))
    typer.echo(iid)


@app.command("remove-item")
def remove_item(path: DirArg, item: Annotated[str, typer.Argument()], draft_name: Annotated[str | None, typer.Option("--draft")] = None,
                source: SourceOpt = "agent-chat") -> None:
    """Remove one of your own final-check items."""
    wd = _wd(path)
    _run(lambda: ops.decide(wd, draft_name or wd.review_draft(), {"remove": [item]}, _source(source)))
    typer.echo(f"{item}: removed")


@app.command()
def confirm(path: DirArg, note: Annotated[str, typer.Option("--note", help="Instruction for the agent")] = "",
            agent: Annotated[bool, typer.Option("--agent", help="The agent finished the drafting stage")] = False,
            draft_name: Annotated[str | None, typer.Option("--draft", help="With --agent: the draft to hand over for the final check")] = None,
            rewriter: Annotated[str | None, typer.Option("--rewriter", help="Provider when the final check still has "
                                                         "rewrites to make (default: config providers.rewriter)")] = None,
            source: SourceOpt = "agent-chat") -> None:
    """確定して渡す: finish the current stage and write handoff.json."""
    wd = _wd(path)
    src = "agent" if agent else _source(source)
    cfg = _cfg(path)
    h = _run(lambda: ops.confirm(wd, src, note, lambda: _llm(cfg, "rewriter", rewriter), draft_name or ""))
    typer.echo(json.dumps(h, ensure_ascii=False, indent=1))


@app.command()
def wait(path: DirArg, for_stage: Annotated[str, typer.Option("--for", help="interview | design | drafting | review | done")],
         timeout: Annotated[float | None, typer.Option("--timeout", help="Seconds (default: no limit)")] = None,
         interval: Annotated[float, typer.Option("--interval")] = 2.0) -> None:
    """Block until the handoff that completes this stage (in the current round) exists; print it. Run it in the
    background so the agent is woken up when the person confirms. Exit 2 on timeout."""
    if for_stage not in ("interview", "design", "drafting", "review", "done"):
        raise typer.BadParameter("--for must be interview, design, drafting, review or done")
    wd = _wd(path)
    h = ops.wait_for(wd, for_stage, timeout, interval)
    if h is None:
        typer.echo(f"timeout: no handoff for {for_stage} yet", err=True)
        raise typer.Exit(2)
    typer.echo(json.dumps(h, ensure_ascii=False, indent=1))


@app.command()
def auto(path: DirArg, stage: Annotated[str, typer.Option("--stage", help="interview | design | review")],
         provider: Annotated[str | None, typer.Option("--provider", help="Default: config providers.auto")] = None,
         rewriter: Annotated[str | None, typer.Option("--rewriter", help="Default: config providers.rewriter")] = None) -> None:
    """Let the agent stand in for the person at one checkpoint (not recommended; see the warning)."""
    from . import auto as stand_in

    if stage not in ("interview", "design", "review"):
        raise typer.BadParameter("--stage must be interview, design or review")
    typer.echo(f"warning: {stand_in.WARNING}", err=True)
    wd = _wd(path)
    cfg = _cfg(path)
    match stage:
        case "interview":
            h = _run(lambda: stand_in.auto_interview(wd, _llm(cfg, "auto", provider)))
        case "design":
            h = _run(lambda: stand_in.auto_design(wd))
        case _:
            h = _run(lambda: stand_in.auto_review(wd, _llm(cfg, "auto", provider), lambda: _llm(cfg, "rewriter", rewriter)))
    typer.echo(json.dumps(h, ensure_ascii=False, indent=1))


@app.command()
def restart(path: DirArg, from_stage: Annotated[str, typer.Option("--from", help="interview | design | drafting")],
            source: SourceOpt = "agent-chat") -> None:
    """Start a new round instead of going back in place: material and interview stay; the new round gets its own
    design.rN.yaml and draft.rN.md (from drafting, the design is copied over)."""
    wd = _wd(path)
    n = _run(lambda: ops.restart(wd, from_stage, _source(source)))
    typer.echo(f"round {n}: stage {from_stage}; design {wd.design_file.name}, draft {wd.draft_base()}")


@app.command()
def config(target: Annotated[str | None, typer.Argument(help="Work directory, or `init`")] = None,
           as_json: Annotated[bool, typer.Option("--json")] = False,
           user: Annotated[bool, typer.Option("--user", help="With init: the user file ($KUMIMASU_CONFIG or "
                                              "~/.config/kumimasu/config.yaml)")] = False,
           project: Annotated[bool, typer.Option("--project", help="With init: ./kumimasu.yaml")] = False) -> None:
    """Show the effective settings and which layer each comes from; `config init --user|--project` writes a
    commented template (never overwrites)."""
    from pathlib import Path

    from .config import PROJECT_FILE, ConfigError, init_template, user_path

    if target == "init":
        if user == project:
            raise typer.BadParameter("give exactly one of --user or --project")
        try:
            path = init_template(user_path() if user else Path.cwd() / PROJECT_FILE)
        except ConfigError as e:
            _fail(e)
        typer.echo(f"wrote {path}")
        return
    wd = Path(target) if target else None
    rows = _cfg(wd).effective()
    if as_json:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    for r in rows:
        where = r["layer"] + (f" ({r['file']})" if r["file"] and r["layer"] != "default" else "")
        ignored = "".join(f"  [使わない: {f}]" for f in r["ignored"])
        typer.echo(f"{r['key']:26} {r['value']!s:40} {where}{ignored}")


ScopeOpt = Annotated[str | None, typer.Option("--scope", help="user | project")]
OnlyOpt = Annotated[str, typer.Option("--only", help="Comma-separated item numbers (or keys for prefs) to save")]

KIND_LABEL = {"added": "追加", "removed": "削除", "edited": "書き換え", "disabled": "オフ", "enabled": "オン",
              "changed": "変更"}


def _split(only: str) -> list[str]:
    return [x.strip() for x in only.split(",") if x.strip()]


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
    from pathlib import Path

    from . import prefs

    a = list(args or [])
    cwd = Path.cwd()
    if action in ("diff", "save"):
        if len(a) != 1:
            raise typer.BadParameter(f"usage: rules {action} DIR")
        wd = _wd(Path(a[0]))
        if action == "diff":
            diffs = _run(lambda: prefs.rules_diff(wd))
            for d in diffs:
                typer.echo(f"{d.n:2}. {KIND_LABEL[d.kind]:4} {d.text}" + (f"  （元: {d.old}）" if d.old else ""))
            if not diffs:
                typer.echo("既定との違いはありません")
            return
        if scope is None:
            raise typer.BadParameter("give --scope user|project")
        path, picked, notes = _run(lambda: prefs.rules_save(wd, scope, _split(only), cwd))
        for n in notes:
            typer.echo(n)
        typer.echo(f"wrote {path}: {len(picked)} changes")
        return
    if scope is None:
        raise typer.BadParameter("give --scope user|project")
    if action == "list":
        path, _ = _run(lambda: prefs.rules_target(scope, cwd, create=False))
        rows = _run(lambda: prefs.read_target(path))
        typer.echo(f"{path}" + ("" if path.exists() else "（まだ無いので、同梱の既定を表示しています）"))
        for i, r in enumerate(rows, 1):
            typer.echo(f"{i:2}. [{'on ' if r.on else 'off'}] {r.text}")
        return
    path, _, notes = _run(lambda: prefs.edit_rules(scope, action, a, cwd))
    for n in notes:
        typer.echo(n)
    typer.echo(f"wrote {path}")


@app.command("prefs")
def prefs_cmd(action: Annotated[str, typer.Argument(help="diff | save")], path: DirArg,
              scope: ScopeOpt = None, only: OnlyOpt = "") -> None:
    """Other defaults (register, drop_list, avoid, noise counts, forms): what this article's design changed, and
    writing chosen ones back to the user or project config."""
    from pathlib import Path

    from . import prefs

    wd = _wd(path)
    if action == "diff":
        diffs = _run(lambda: prefs.prefs_diff(wd))
        for d in diffs:
            typer.echo(f"{d.n:2}. {d.key:26} {KIND_LABEL[d.kind]:4} {d.value!r}  （既定: {d.default!r}）")
        if not diffs:
            typer.echo("既定との違いはありません")
        return
    if action != "save":
        raise typer.BadParameter("action must be diff or save")
    if scope is None:
        raise typer.BadParameter("give --scope user|project")
    target, picked = _run(lambda: prefs.prefs_save(wd, scope, _split(only), Path.cwd()))
    typer.echo(f"wrote {target}: {', '.join(sorted({d.key for d in picked})) or 'nothing'}")

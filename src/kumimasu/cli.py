from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from .workdir import StepError, WorkDir

app = typer.Typer(no_args_is_help=True, help="Write an article from the author's unselected material: "
                  "init -> mark -> interview -> design -> draft -> check -> final check.")

DirArg = Annotated[Path, typer.Argument(file_okay=False, help="Work directory")]
WriterOpt = Annotated[str | None, typer.Option("--writer", help="Provider for the one-shot writing call, web tools allowed "
                                               "(default: config providers.writer)")]
JudgeOpt = Annotated[str | None, typer.Option("--judge", help="Provider for coverage and mapping judgements "
                                              "(default: config providers.judge)")]
MetaOpt = Annotated[str | None, typer.Option("--meta-detector", help="Provider for meta-discourse/glue detection, or "
                                             "'rules' (default: config providers.detector)")]
ProviderOpt = Annotated[str | None, typer.Option("--provider", help="Provider (default: from the config)")]
VerifyOpt = Annotated[bool, typer.Option("--verify-links", help="Open links that are not in the material (network)")]
WEB_TOOLS = ("WebSearch", "WebFetch")


def _wd(path: Path) -> WorkDir:
    wd = WorkDir(path)
    if not wd.project_file.exists():
        raise typer.BadParameter(f"{path} は作業ディレクトリではありません（`kumimasu init` で作ります）")
    return wd


def _provider(spec: str, web: bool = False, cache_dir: str | None = None):
    from .llm import get_provider

    return get_provider(spec, cache_dir=cache_dir, allowed_tools=WEB_TOOLS if web and spec.startswith("claude-cli") else ())


def _cfg(path: Path | None = None, **cli):
    from .config import ConfigError, load

    try:
        return load(path, Path.cwd(), cli)
    except ConfigError as e:
        _fail(e)


def _llm(cfg, role: str, override: str | None = None, web: bool = False):
    return _provider(cfg.provider(role, override), web=web, cache_dir=cfg.cache_dir)


def _detector(cfg, override: str | None):
    spec = cfg.provider("detector", override)
    return None if spec == "rules" else _provider(spec, cache_dir=cfg.cache_dir)


def _fail(e: Exception) -> None:
    typer.echo(f"error: {e}", err=True)
    raise typer.Exit(1)


def _guard(wd: WorkDir, *stages: str, action: str) -> None:
    from .ops import require_stage

    try:
        require_stage(wd, *stages, action=action)
    except StepError as e:
        _fail(e)


SourceOpt = Annotated[str, typer.Option("--source", help="Who decided: agent-chat (the user, through the agent), "
                                        "human-ui, auto")]


@app.command()
def init(path: DirArg,
         topic: Annotated[str, typer.Option("--topic")],
         audience: Annotated[str, typer.Option("--audience")],
         material: Annotated[list[Path], typer.Option("--material", "-m", exists=True, dir_okay=False,
                                                      help="Notes, logs, code or link lists (repeatable)")] = [],
         kind: Annotated[str, typer.Option("--kind", help="実用 | 読み物 | 調査")] = "実用",
         length: Annotated[int, typer.Option("--length", min=300)] = 4000) -> None:
    """Copy the material into DIR and split it into material units (units.yaml)."""
    from .model import KINDS, Project
    from .workdir import init_workdir

    if kind not in KINDS:
        raise typer.BadParameter(f"--kind must be one of {' | '.join(KINDS)}")
    try:
        _, units = init_workdir(path, Project(topic=topic, audience=audience, kind=kind, length=length), material)
    except StepError as e:
        _fail(e)
    typer.echo(f"wrote {path / 'project.yaml'} and {path / 'units.yaml'} ({len(units)} units from {len(material)} files)")


@app.command()
def mark(path: DirArg, writer: WriterOpt = None, judge: JudgeOpt = None,
         new_baseline: Annotated[bool, typer.Option("--new-baseline", help="Write baseline/W.md again")] = False) -> None:
    """Mark each unit as searchable or first-hand against one web-enabled generic article (a marker, not a goal)."""
    from .mark import mark as run, mark_counts

    wd = _wd(path)
    _guard(wd, "interview", action="目印付け")
    try:
        cfg = _cfg(path)
        units = run(wd, _llm(cfg, "baseline", writer, web=True), _llm(cfg, "judge", judge), reuse_baseline=not new_baseline)
    except StepError as e:
        _fail(e)
    c = mark_counts(units)
    typer.echo(f"searchable yes {c['yes']}, partial {c['partial']}, no {c['no']}, unjudged {c['unjudged']}  "
               f"(baseline: {path / 'baseline' / 'W.md'})")


@app.command()
def interview(path: DirArg, provider: ProviderOpt = None,
              overwrite: Annotated[bool, typer.Option("--overwrite", help="Replace questions that already have answers")] = False) -> None:
    """Write 4-6 short questions to DIR/interview.yaml; answer them there or in `serve`."""
    from .interview import interview as run

    wd = _wd(path)
    _guard(wd, "interview", action="質問づくり")
    try:
        iv = run(wd, _llm(_cfg(path), "interviewer", provider), overwrite)
    except StepError as e:
        _fail(e)
    for q in iv.questions:
        typer.echo(f"{q.id}. {q.question}")
    typer.echo(f"\nanswer in {wd.interview_file} or with `kumimasu serve {path}`")


@app.command()
def design(path: DirArg, provider: ProviderOpt = None,
           overwrite: Annotated[bool, typer.Option("--overwrite")] = False) -> None:
    """Propose DIR/design.yaml: purpose, takeaways, deep/mention/drop per unit, order hints, forms and rules."""
    from .design import design as run

    wd = _wd(path)
    _guard(wd, "design", action="設計の提案")
    try:
        d = run(wd, _llm(_cfg(path), "designer", provider), overwrite)
    except StepError as e:
        _fail(e)
    n = {u: sum(x.use == u for x in d.units) for u in ("deep", "mention", "drop")}
    typer.echo(f"wrote {wd.design_file}: deep {n['deep']}, mention {n['mention']}, drop {n['drop']}")
    for t in d.takeaways:
        typer.echo(f"  - {t}")
    _echo_design_notes(d)


@app.command()
def serve(path: DirArg, port: Annotated[int | None, typer.Option("--port", help="Default: config serve.port")] = None,
          rewriter: Annotated[str | None, typer.Option("--rewriter", help="Provider for 書き直す in the final check "
                                                       "(default: config providers.rewriter)")] = None) -> None:
    """Local page (127.0.0.1): material marks, interview answers, the design, and the final check of a draft.
    Edits autosave to the YAML files."""
    from .server import WriteApp, serve as run

    wd = _wd(path)
    cfg = _cfg(path)
    run(WriteApp(wd, lambda: _llm(cfg, "rewriter", rewriter), poll_seconds=cfg.get("serve.poll_seconds")),
        cfg.get("serve.port", port))


@app.command()
def apply(path: DirArg, draft_name: Annotated[str | None, typer.Option("--draft")] = None,
          provider: Annotated[str | None, typer.Option("--provider", help="Provider for 書き直す (default: config "
                                                       "providers.rewriter)")] = None,
          regenerate: Annotated[str, typer.Option("--regenerate", help="Comma-separated item ids whose stored rewrite "
                                                  "is dropped and made again")] = "",
          source: SourceOpt = "agent-chat") -> None:
    """Apply the final-check decisions (review.<draft>.yaml): delete and rewrite only the decided spans -> <draft>.final.md.
    The source must be a base draft, not a *.final*.md output."""
    from . import ops
    from .review import load_review, needs_rewrite_call, require_base

    wd = _wd(path)
    draft_name = draft_name or wd.review_draft()
    try:
        require_base(draft_name)
    except ValueError as e:
        _fail(e)
    try:
        regen = tuple(x.strip() for x in regenerate.split(",") if x.strip())
        unknown = set(regen) - {i.id for i in load_review(wd, draft_name).items}
        if unknown:
            raise ValueError(f"unknown item ids: {', '.join(sorted(unknown))}")
        res = ops.apply(wd, draft_name, _llm(_cfg(path), "rewriter", provider)
                        if needs_rewrite_call(wd, draft_name, regen) else None, source, regen)
    except (StepError, ValueError) as e:
        _fail(e)
    typer.echo(f"wrote {wd.root / res.out}: deleted {res.deleted}, rewritten {res.rewritten} (reused {res.reused}), "
               f"skipped {len(res.skipped)}, stale {len(res.stale)}, calls {res.calls}")
    for iid in res.stale:
        typer.echo(f"stale: {iid} (the draft changed since this item was made; regenerate or re-decide it)")
    for tag, line in res.diff:
        if tag != " ":
            typer.echo(f"{tag} {line}")


def _echo_design_notes(d) -> None:
    for sk in d.skip:
        typer.echo(f"skip: {sk.label}" + (f" ({', '.join(sk.units)})" if sk.units else ""))
    for a in d.aside:
        typer.echo(f"aside: {a.id} @ {a.where}")
    for c in d.live_conflicts():
        typer.echo(f"warning: {c.message()}")
    if d.avoid:
        typer.echo("avoid: " + " / ".join(d.avoid))


@app.command()
def noise(path: DirArg, provider: ProviderOpt = None) -> None:
    """Propose 1-3 skipped prerequisites and 1-2 asides for the current design (other uses are kept), then review again."""
    from .design import noise_workdir

    wd = _wd(path)
    _guard(wd, "design", action="前提と脱線の提案")
    try:
        d = noise_workdir(wd, _llm(_cfg(path), "designer", provider))
    except StepError as e:
        _fail(e)
    _echo_design_notes(d)


@app.command()
def review(path: DirArg, provider: ProviderOpt = None,
           keep_avoid: Annotated[bool, typer.Option("--keep-avoid", help="Keep the avoid list as edited")] = False) -> None:
    """Recompute, for the current design, which drop units the kept units would bring in anyway, and the avoid topics."""
    from .design import review_workdir

    wd = _wd(path)
    _guard(wd, "design", action="設計の見直し")
    try:
        d = review_workdir(wd, _llm(_cfg(path), "designer", provider), keep_avoid)
    except StepError as e:
        _fail(e)
    _echo_design_notes(d)


@app.command()
def draft(path: DirArg, writer: WriterOpt = None,
          out: Annotated[str | None, typer.Option("--out", help="File name inside DIR (default: draft.md, draft.rN.md "
                                                  "in round N)")] = None,
          drop_list: Annotated[str | None, typer.Option("--drop-list", help="topics | full | none (default: design.yaml)")] = None) -> None:
    """One writing call with the design and the kept material -> DIR/draft.md."""
    from .draft import draft as run

    if drop_list not in (None, "topics", "full", "none"):
        raise typer.BadParameter("--drop-list must be topics, full or none")
    wd = _wd(path)
    _guard(wd, "drafting", action="下書き")
    out = out or wd.draft_base()
    try:
        text = run(wd, _llm(_cfg(path), "writer", writer, web=True), out, drop_list)
    except StepError as e:
        _fail(e)
    typer.echo(f"wrote {wd.root / out} ({len(text)} chars)")


def _fetch(verify: bool):
    if not verify:
        return None
    from .check import default_fetch

    return default_fetch


@app.command()
def check(path: DirArg, judge: JudgeOpt = None, meta_detector: MetaOpt = None,
          draft_name: Annotated[str | None, typer.Option("--draft", help="Draft file inside DIR")] = None,
          verify_links: VerifyOpt = False,
          surface_only: Annotated[bool, typer.Option("--surface-only", help="Only meta-discourse, glue and lint "
                                                     "(-> check*.surface.json)")] = False,
          surface_runs: Annotated[int | None, typer.Option("--surface-runs", min=1, help="Majority-vote runs for meta/glue "
                                                           "(default: config surface.runs)")] = None) -> None:
    """Check the draft against the design (the design's metamorphic relations) -> DIR/check.json and check.txt."""
    from .check import check as run, report_text

    wd = _wd(path)
    _guard(wd, "drafting", action="検査")
    draft_name = draft_name or wd.draft_base()
    try:
        cfg = _cfg(path)
        rep = run(wd, _llm(cfg, "judge", judge), _detector(cfg, meta_detector), draft_name, _fetch(verify_links),
                  surface_only, cfg.get("surface.runs", surface_runs), cfg.get("surface.min_votes"))
    except StepError as e:
        _fail(e)
    typer.echo(report_text(rep), nl=False)


@app.command()
def revise(path: DirArg, writer: WriterOpt = None, judge: JudgeOpt = None,
           meta_detector: MetaOpt = None, verify_links: VerifyOpt = False) -> None:
    """If structural checks fail, one rewrite with instructions from those checks only -> DIR/draft.v2.md, then check again."""
    from .check import report_text
    from .revise import revise as run

    wd = _wd(path)
    _guard(wd, "drafting", action="書き直し")
    src = wd.draft_base()
    dst = src.removesuffix(".md") + ".v2.md"
    try:
        cfg = _cfg(path)
        rep, todo = run(wd, _llm(cfg, "writer", writer, web=True), _llm(cfg, "judge", judge),
                        _detector(cfg, meta_detector), _fetch(verify_links), src, dst)
    except StepError as e:
        _fail(e)
    if rep is None:
        typer.echo("no failed structural checks; nothing to revise (surface findings go to `polish`)")
        return
    typer.echo(f"{len(todo)} instructions -> {wd.root / dst}")
    typer.echo(report_text(rep), nl=False)


@app.command()
def polish(path: DirArg, draft_name: Annotated[str | None, typer.Option("--draft")] = None,
           provider: Annotated[str | None, typer.Option("--provider", help="Default: config providers.detector")] = None,
           yes: Annotated[bool, typer.Option("--yes", help="Apply: delete or rewrite only the flagged sentences")] = False,
           rules: Annotated[str, typer.Option("--rules", help="Comma-separated: meta, glue, dash")] = "meta,glue,dash",
           runs: Annotated[int | None, typer.Option("--runs", min=1, help="Detection runs per round (default: config)")] = None,
           min_votes: Annotated[int | None, typer.Option("--min-votes", min=1, help="Runs that must pick a sentence "
                                                         "(default: config)")] = None,
           max_rounds: Annotated[int | None, typer.Option("--max-rounds", min=1, help="Default: config")] = None,
           out: Annotated[str | None, typer.Option("--out", help="File name inside DIR (default: <draft>.polished.md)")] = None) -> None:
    """Surface pass: majority-vote detection of meta-discourse, glue and dashes; with --yes, rewrite only those sentences
    and re-detect around the edits until nothing stable is left."""
    from .polish import POLISH_RULES, polish as run

    chosen = tuple(r.strip() for r in rules.split(",") if r.strip())
    if not chosen or any(r not in POLISH_RULES for r in chosen):
        raise typer.BadParameter(f"--rules must be a comma-separated subset of {', '.join(POLISH_RULES)}")
    cfg = _cfg(path)
    runs = cfg.get("surface.runs", runs)
    min_votes = cfg.get("surface.min_votes", min_votes)
    max_rounds = cfg.get("surface.max_rounds", max_rounds)
    if min_votes > runs:
        raise typer.BadParameter("--min-votes must not exceed --runs")
    wd = _wd(path)
    _guard(wd, "drafting", action="表面の仕上げ")
    draft_name = draft_name or wd.draft_base()
    try:
        res = run(wd, _llm(cfg, "detector", provider), draft_name, chosen, yes, runs, min_votes, max_rounds, out)
    except StepError as e:
        _fail(e)
    for rd in res.rounds:
        typer.echo(f"round {rd.round}: scope {rd.scope_chars} chars, {rd.runs} runs, {rd.hits} hits, {rd.edits} edits, "
                   f"{rd.calls} calls")
        for f in rd.flags:
            typer.echo(f"  [{f.id}] {f.rule:14} {f.text}")
    typer.echo(f"calls: {res.calls} ({res.llm_calls} not from the cache)")
    if not yes:
        if res.rounds and res.rounds[0].hits:
            typer.echo("run again with --yes to rewrite only these sentences (the structure is not touched)")
        return
    typer.echo(f"wrote {wd.root / res.out}; log in {res.out.removesuffix('.md')}.json")


@app.command()
def export(path: DirArg, draft_name: Annotated[str | None, typer.Option("--draft")] = None,
           to: Annotated[Path, typer.Option("--to", file_okay=False, help="Directory to copy into")] = Path(".")) -> None:
    """Copy <draft>.final.md to TO/<workdir>-<draft stem>-<YYYYmmdd-HHMM>.md (never overwrites)."""
    from .review import export_final

    wd = _wd(path)
    try:
        dest = export_final(wd, draft_name or wd.review_draft(), to)
    except ValueError as e:
        _fail(e)
    typer.echo(f"wrote {dest}")


from . import cli_flow  # noqa: E402,F401  (registers the checkpoint commands on `app`)


def main() -> None:
    app()

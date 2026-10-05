from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from . import cli_common as cc
from . import (
    cli_flow,  # noqa: F401  # registers the checkpoint commands on app
    ops,
    show,
)
from .check import Votes, report_text
from .check import check as run_check
from .cli_common import (
    DirArg,
    DraftOpt,
    JudgeOpt,
    MetaOpt,
    ProviderOpt,
    ResearcherOpt,
    RewriterOpt,
    SourceOpt,
    VerifyOpt,
    WriterOpt,
    app,
    errors,
    notice,
)
from .config import Config
from .design import design as run_design
from .design import noise_workdir, review_conflicts, sync_design
from .digest import digest as run_digest
from .digest import digest_lines, reassess
from .draft import draft as run_draft
from .factcheck import check_urls
from .files import private_dir
from .interview import interview as run_interview
from .mark import mark as run_mark
from .mark import mark_counts
from .model import KINDS, USES, Design, Project
from .polish import POLISH_RULES
from .polish import polish as run_polish
from .research import Research, load_research, research_name
from .research import research as run_research
from .review import ReviewContext, export_final, require_base
from .revise import revise as run_revise
from .server import WriteApp
from .server import serve as run_server
from .terms import material_load, term_states
from .workdir import WorkDir, check_draft_name, init_workdir


@app.command()
def init(path: DirArg,
         topic: Annotated[str, typer.Option("--topic")],
         audience: Annotated[str, typer.Option("--audience")],
         material: Annotated[list[Path] | None, typer.Option("--material", "-m", exists=True, dir_okay=False,
                                                             help="Notes, logs, code or link lists (repeatable)")] = None,
         kind: Annotated[str, typer.Option("--kind", help="実用 | 読み物 | 調査")] = "実用",
         length: Annotated[int, typer.Option("--length", min=300)] = 4000) -> None:
    """Copy the material into DIR and split it into material units (units.yaml)."""
    if kind not in KINDS:
        raise typer.BadParameter(f"--kind は {' | '.join(KINDS)} のどれかにしてください")
    files = material or []
    with errors():
        root = Path(cc.config().workdir_root)
        if path.resolve().is_relative_to(root.resolve()):
            private_dir(root)
        _, units = init_workdir(path, Project(topic=topic, audience=audience, kind=kind, length=length), files)
    typer.echo(f"wrote {path / 'project.yaml'} and {path / 'units.yaml'} ({len(units)} units from {len(files)} files)")
    for line in digest_lines(WorkDir(path).project().digest):
        typer.echo(line)


@app.command()
def digest(path: DirArg, provider: ProviderOpt = None,
           force: Annotated[bool, typer.Option("--force", help="Go on although questions exist (they move to "
                                               "interview.before-digest.yaml and must be made again)")] = False,
           assess_only: Annotated[bool, typer.Option("--assess", help="Only re-run the deterministic check of whether "
                                                     "the material looks like finished documents (no LLM)")] = False) -> None:
    """Rewrite finished, dense documents into self-contained memos before mark/interview: one call per heading section
    (small sections of a file share a call), code and table rows kept as they are. The original units move to
    DIR/units.raw.yaml and each new unit records `from` (the original ids) and `path`."""
    wd = cc.workdir(path, "interview", action="材料の書き直し（digest）")
    if assess_only:
        with errors():
            st = reassess(wd)
        typer.echo(f"digest recommended: {'yes' if st.recommended else 'no'}")
        for r in st.reasons:
            typer.echo(f"  - {r}")
        return
    cfg = cc.config(path)
    with errors():
        res = run_digest(wd, cc.llm(cfg, "digester", provider), force)
    typer.echo(f"wrote {wd.units_file}: {res.before} units -> {len(res.units)} units ({res.memos} memos, "
               f"{res.kept} kept as they were), {res.calls} calls; originals in {wd.raw_units_file}")
    typer.echo(f"next: kumimasu mark {path} && kumimasu interview {path}")


@app.command()
def mark(path: DirArg, writer: WriterOpt = None, judge: JudgeOpt = None,
         new_baseline: Annotated[bool, typer.Option("--new-baseline", help="Write baseline/W.md again")] = False) -> None:
    """Mark each unit as searchable or first-hand against one web-enabled generic article (a marker, not a goal)."""
    wd = cc.workdir(path, "interview", action="目印付け")
    cfg = cc.config(path)
    if new_baseline or not (wd.root / "baseline" / "W.md").exists():
        notice("ウェブ調査: providers.baseline が WebSearch・WebFetch を使います（送るのは題と読者だけ）")
    with errors():
        units = run_mark(wd, cc.llm(cfg, "baseline", writer, web=True), cc.llm(cfg, "judge", judge),
                         reuse_baseline=not new_baseline)
    c = mark_counts(units)
    typer.echo(f"searchable yes {c['yes']}, partial {c['partial']}, no {c['no']}, unjudged {c['unjudged']}  "
               f"(baseline: {path / 'baseline' / 'W.md'})")


@app.command()
def interview(path: DirArg, provider: ProviderOpt = None,
              overwrite: Annotated[bool, typer.Option("--overwrite", help="Replace questions that already have answers")] = False) -> None:
    """Write 4-6 short questions to DIR/interview.yaml; answer them there or in `serve`."""
    wd = cc.workdir(path, "interview", action="質問づくり")
    cfg = cc.config(path)
    with errors():
        iv = run_interview(wd, cc.llm(cfg, "interviewer", provider), cfg.get("interview.always_ask") or [], overwrite)
    for q in iv.questions:
        typer.echo(f"{q.id}. {q.question}" + (f"\n    参考: {', '.join(q.units)}" if q.units else ""))
    typer.echo(f"\nanswer in {wd.interview_file} or with `kumimasu serve {path}`")


def _echo_design_notes(d: Design, units: list) -> None:
    for sk in d.skip:
        typer.echo(f"skip: {sk.label}" + (f" ({', '.join(sk.units)})" if sk.units else ""))
    for a in d.aside:
        typer.echo(f"aside: {a.id} @ {a.where}")
    for c in d.live_conflicts():
        typer.echo(f"warning: {c.message()}")
    for line in show.term_lines(term_states(d)) + show.load_lines(material_load(d, units)):
        typer.echo(line)
    if d.avoid:
        typer.echo("avoid: " + " / ".join(d.avoid))
    for t in d.research:
        typer.echo(f"research: {t}")


@app.command()
def design(path: DirArg, provider: ProviderOpt = None, judge: JudgeOpt = None,
           overwrite: Annotated[bool, typer.Option("--overwrite")] = False) -> None:
    """Propose DIR/design.yaml: purpose, takeaways, deep/mention/drop per unit, order hints, forms, the web research
    list and rules; then find the terms the reader may not know and keep (mention) a dropped unit that explains each."""
    wd = cc.workdir(path, "design", action="設計の提案")
    cfg = cc.config(path)
    with errors():
        d = run_design(wd, cc.llm(cfg, "designer", provider), cc.llm(cfg, "judge", judge), cc.design_defaults(cfg),
                       overwrite)
    n = {u: sum(x.use == u for x in d.units) for u in USES}
    typer.echo(f"wrote {wd.design_file}: deep {n['deep']}, mention {n['mention']}, drop {n['drop']}")
    for t in d.takeaways:
        typer.echo(f"  - {t}")
    _echo_design_notes(d, wd.units())


@app.command()
def noise(path: DirArg, provider: ProviderOpt = None, judge: JudgeOpt = None) -> None:
    """Propose 1-3 skipped prerequisites and 1-2 asides for the current design (other uses are kept), then review again."""
    wd = cc.workdir(path, "design", action="前提と脱線の提案")
    cfg = cc.config(path)
    with errors():
        d = noise_workdir(wd, cc.llm(cfg, "designer", provider), cc.llm(cfg, "judge", judge),
                          cfg.get("defaults.noise.skip_max"), cfg.get("defaults.noise.aside_max"))
    _echo_design_notes(d, wd.units())


@app.command()
def review(path: DirArg, provider: ProviderOpt = None, judge: JudgeOpt = None) -> None:
    """Recompute, for the current design, which drop units the kept units would bring in anyway and the terms the reader
    may not know (warnings only; uses and avoid topics are left as they are)."""
    wd = cc.workdir(path, "design", action="設計の見直し")
    cfg = cc.config(path)
    with errors():
        d = review_conflicts(wd, cc.llm(cfg, "designer", provider), cc.llm(cfg, "judge", judge))
    _echo_design_notes(d, wd.units())


@app.command()
def serve(path: DirArg, port: Annotated[int | None, typer.Option("--port", help="Default: config serve.port")] = None,
          rewriter: RewriterOpt = None) -> None:
    """Local page (127.0.0.1): material marks, interview answers, the design, and the final check of a draft.
    Edits autosave to the YAML files."""
    wd = cc.workdir(path)
    cfg = cc.config(path)
    run_server(WriteApp(wd, lambda: cc.llm(cfg, "rewriter", rewriter), cfg.get("serve.poll_seconds")),
               cfg.resolve("serve.port", port))


def _research(wd: WorkDir, cfg: Config, researcher: str | None, refresh: bool) -> Research:
    d = sync_design(wd.design(), wd.units())
    old = load_research(wd)
    if old is not None and old.topics == d.research and not refresh:
        return old
    if d.research:
        notice("ウェブ調査: providers.researcher が WebSearch・WebFetch を使います"
               "（送るのは題・読者・ねらい・持ち帰り・調べることだけで、材料と回答は送りません）")
    return run_research(wd, cc.llm(cfg, "researcher", researcher, web=True), d)


@app.command()
def research(path: DirArg, researcher: ResearcherOpt = None) -> None:
    """Web research for the design's research list -> DIR/research.json. Only the topic, audience, purpose, takeaways
    and that list are sent; the material and the answers are not."""
    wd = cc.workdir(path, "drafting", action="ウェブ調査")
    cfg = cc.config(path)
    with errors():
        res = _research(wd, cfg, researcher, refresh=True)
    typer.echo(f"wrote {wd.root / research_name(wd)}: {len(res.findings)} findings")


@app.command()
def draft(path: DirArg, writer: WriterOpt = None, researcher: ResearcherOpt = None,
          new_research: Annotated[bool, typer.Option("--new-research", help="Run the web research again")] = False,
          out: Annotated[str | None, typer.Option("--out", help="File name inside DIR (default: draft.md, draft.rN.md "
                                                  "in round N)")] = None,
          drop_list: Annotated[str | None, typer.Option("--drop-list", help="topics | full | none (default: design.yaml)")] = None) -> None:
    """Web research for the design's research list (only when it changed), then one writing call without tools
    with the design, the kept material and the research findings -> DIR/draft.md."""
    if drop_list not in (None, "topics", "full", "none"):
        raise typer.BadParameter("--drop-list は topics, full, none のどれかにしてください")
    wd = cc.workdir(path, "drafting", action="下書き")
    cfg = cc.config(path)
    with errors():
        out = check_draft_name(out or wd.draft_base())
        found = _research(wd, cfg, researcher, new_research)
        text = run_draft(wd, cc.llm(cfg, "writer", writer), cfg.roles(), out, drop_list, found)
    typer.echo(f"wrote {wd.root / out} ({len(text)} chars)")


def _votes(cfg: Config) -> Votes:
    return Votes(cfg.get("surface.runs"), cfg.get("surface.min_votes"))


def _fetch(verify: bool):
    if not verify:
        return None
    notice("リンクの確認: 材料に無いリンクを開きます（ネットワークを使います。非公開のアドレスには繋ぎません）")
    return check_urls


@app.command()
def check(path: DirArg, judge: JudgeOpt = None, meta_detector: MetaOpt = None, draft_name: DraftOpt = None,
          verify_links: VerifyOpt = False,
          surface_only: Annotated[bool, typer.Option("--surface-only", help="Only meta-discourse, glue and lint "
                                                     "(-> check*.surface.json)")] = False,
          surface_runs: Annotated[int | None, typer.Option("--surface-runs", min=1, help="Majority-vote runs for meta/glue "
                                                           "(default: config surface.runs)")] = None) -> None:
    """Check the draft against the design (the design's metamorphic relations) -> DIR/check.json and check.txt."""
    wd = cc.workdir(path, "drafting", action="検査")
    cfg = cc.config(path)
    with errors():
        name = check_draft_name(draft_name or wd.draft_base())
        votes = Votes(cfg.resolve("surface.runs", surface_runs), cfg.get("surface.min_votes"))
        rep = run_check(wd, cc.llm(cfg, "judge", judge), cc.detector(cfg, meta_detector), name, votes,
                        _fetch(verify_links), surface_only)
    typer.echo(report_text(rep), nl=False)


@app.command()
def revise(path: DirArg, writer: WriterOpt = None, judge: JudgeOpt = None,
           meta_detector: MetaOpt = None, verify_links: VerifyOpt = False) -> None:
    """If structural checks fail, one rewrite with instructions from those checks only -> DIR/draft.v2.md, then check again."""
    wd = cc.workdir(path, "drafting", action="書き直し")
    cfg = cc.config(path)
    src = wd.draft_base()
    dst = src.removesuffix(".md") + ".v2.md"
    with errors():
        rep, todo = run_revise(wd, cc.llm(cfg, "writer", writer), cc.llm(cfg, "judge", judge),
                               cc.detector(cfg, meta_detector), cfg.roles(), _votes(cfg), _fetch(verify_links), src, dst)
    if rep is None:
        typer.echo("no failed structural checks; nothing to revise (surface findings go to `polish`)")
        return
    typer.echo(f"{len(todo)} instructions -> {wd.root / dst}")
    typer.echo(report_text(rep), nl=False)


@app.command()
def polish(path: DirArg, draft_name: DraftOpt = None,
           provider: Annotated[str | None, typer.Option("--provider", help="Default: config providers.detector")] = None,
           yes: Annotated[bool, typer.Option("--yes", help="Apply: delete or rewrite only the flagged sentences")] = False,
           rules: Annotated[str, typer.Option("--rules", help="Comma-separated: meta, caveat, glue, flow, dash")] = "meta,caveat,glue,flow,dash",
           runs: Annotated[int | None, typer.Option("--runs", min=1, help="Detection runs per round (default: config)")] = None,
           min_votes: Annotated[int | None, typer.Option("--min-votes", min=1, help="Runs that must pick a sentence "
                                                         "(default: config)")] = None,
           max_rounds: Annotated[int | None, typer.Option("--max-rounds", min=1, help="Default: config")] = None,
           out: Annotated[str | None, typer.Option("--out", help="File name inside DIR (default: <draft>.polished.md)")] = None) -> None:
    """Surface pass: majority-vote detection of meta-discourse, conservative caveats, glue and dashes; with --yes,
    rewrite only those sentences and re-detect around the edits until nothing stable is left."""
    chosen = tuple(r.strip() for r in rules.split(",") if r.strip())
    if not chosen or any(r not in POLISH_RULES for r in chosen):
        raise typer.BadParameter(f"--rules は {', '.join(POLISH_RULES)} からカンマ区切りで選んでください")
    cfg = cc.config(path)
    runs = cfg.resolve("surface.runs", runs)
    min_votes = cfg.resolve("surface.min_votes", min_votes)
    if min_votes > runs:
        raise typer.BadParameter("--min-votes は --runs 以下にしてください")
    wd = cc.workdir(path, "drafting", action="表面の仕上げ")
    with errors():
        name = check_draft_name(draft_name or wd.draft_base())
        out = check_draft_name(out or name.removesuffix(".md") + ".polished.md")
        res = run_polish(wd, cc.llm(cfg, "detector", provider), name, chosen, yes, Votes(runs, min_votes),
                         cfg.resolve("surface.max_rounds", max_rounds), out)
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
def apply(path: DirArg, draft_name: DraftOpt = None,
          provider: Annotated[str | None, typer.Option("--provider", help="Provider for 書き直す (default: config "
                                                       "providers.rewriter)")] = None,
          regenerate: Annotated[str, typer.Option("--regenerate", help="Comma-separated item ids whose stored rewrite "
                                                  "is dropped and made again")] = "",
          source: SourceOpt = "agent-chat") -> None:
    """Apply the final-check decisions (review.<draft>.yaml): delete and rewrite only the decided spans -> <draft>.final.md.
    The source must be a base draft, not a *.final*.md output."""
    wd = cc.workdir(path)
    with errors():
        name = draft_name or wd.review_draft()
        require_base(name)
        regen = tuple(x.strip() for x in regenerate.split(",") if x.strip())
        ctx = ReviewContext.load(wd, name)
        unknown = set(regen) - {i.id for i in ctx.review.items}
        if unknown:
            raise ValueError(f"知らない項目です: {', '.join(sorted(unknown))}")
        res = ops.apply(wd, name, cc.llm(cc.config(path), "rewriter", provider)
                        if ctx.needs_rewrite_call(regen) else None, cc.source(source), regen)
    typer.echo(f"wrote {wd.root / res.out}: deleted {res.deleted}, rewritten {res.rewritten} (reused {res.reused}), "
               f"skipped {len(res.skipped)}, stale {len(res.stale)}, calls {res.calls}")
    for iid in res.stale:
        typer.echo(f"stale: {iid} (the draft changed since this item was made; regenerate or re-decide it)")
    for tag, line in res.diff:
        if tag != " ":
            typer.echo(f"{tag} {line}")


@app.command()
def export(path: DirArg, draft_name: DraftOpt = None,
           to: Annotated[Path, typer.Option("--to", file_okay=False, help="Directory to copy into")] = Path(".")) -> None:
    """Copy <draft>.final.md to TO/<workdir>-<draft stem>-<YYYYmmdd-HHMM>.md (never overwrites)."""
    wd = cc.workdir(path)
    with errors():
        dest = export_final(wd, draft_name or wd.review_draft(), to)
    typer.echo(f"wrote {dest}")


def main() -> None:
    os.umask(0o077)
    app()

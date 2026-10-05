from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from .config import Config, load
from .design import DesignDefaults
from .errors import KumimasuError
from .llm import Provider, get_provider
from .model import SOURCES
from .ops import require_stage
from .rules import default_rules
from .workdir import WorkDir

app = typer.Typer(no_args_is_help=True, help="Write an article from the author's unselected material: "
                  "init -> mark -> interview -> design -> draft -> check -> final check.")

DirArg = Annotated[Path, typer.Argument(file_okay=False, help="Work directory")]
WriterOpt = Annotated[str | None, typer.Option("--writer", help="Provider for the one-shot writing call, no tools "
                                               "(default: config providers.writer)")]
ResearcherOpt = Annotated[str | None, typer.Option("--researcher", help="Provider for the web research before writing "
                                                   "(default: config providers.researcher)")]
JudgeOpt = Annotated[str | None, typer.Option("--judge", help="Provider for coverage and mapping judgements "
                                              "(default: config providers.judge)")]
MetaOpt = Annotated[str | None, typer.Option("--meta-detector", help="Provider for meta-discourse/glue detection, or "
                                             "'rules' (default: config providers.detector)")]
ProviderOpt = Annotated[str | None, typer.Option("--provider", help="Provider (default: from the config)")]
RewriterOpt = Annotated[str | None, typer.Option("--rewriter", help="Provider for 書き直す in the final check "
                                                 "(default: config providers.rewriter)")]
VerifyOpt = Annotated[bool, typer.Option("--verify-links", help="Open links that are not in the material (network)")]
DraftOpt = Annotated[str | None, typer.Option("--draft", help="Draft file inside DIR")]
SourceOpt = Annotated[str, typer.Option("--source", help="Who decided: agent-chat (the user, through the agent), "
                                        "human-ui, auto")]
WEB_TOOLS = ("WebSearch", "WebFetch")
STATE = {"trust_project": False}


@app.callback()
def options(trust_project: Annotated[bool, typer.Option(
        "--trust-project", envvar="KUMIMASU_TRUST_PROJECT",
        help="Use providers, cache_dir, workdir_root and outside rules files from ./kumimasu.yaml and the work "
             "directory's project.yaml")] = False) -> None:
    STATE["trust_project"] = trust_project


def notice(text: str) -> None:
    typer.echo(text, err=True)


def fail(e: Exception | str) -> NoReturn:
    typer.echo(f"error: {e}", err=True)
    raise typer.Exit(1)


@contextmanager
def errors() -> Iterator[None]:
    try:
        yield
    except (KumimasuError, ValueError, KeyError) as e:
        fail(e)


def workdir(path: Path, *stages: str, action: str = "") -> WorkDir:
    wd = WorkDir(path)
    if not wd.project_file.exists():
        raise typer.BadParameter(f"{path} は作業ディレクトリではありません（`kumimasu init` で作ります）")
    if stages:
        with errors():
            require_stage(wd, *stages, action=action)
    return wd


def config(path: Path | None = None) -> Config:
    with errors():
        cfg = load(path, Path.cwd(), trust=STATE["trust_project"])
    for w in cfg.warnings():
        notice(f"注意: {w}")
    return cfg


def source(value: str) -> str:
    if value not in SOURCES or value == "agent":
        raise typer.BadParameter("--source は human-ui, agent-chat, auto のどれかにしてください")
    return value


def provider(spec: str, web: bool = False, cache_dir: str | None = None) -> Provider:
    return get_provider(spec, cache_dir=cache_dir, allowed_tools=WEB_TOOLS if web and spec.startswith("claude-cli") else ())


def llm(cfg: Config, role: str, cli_value: str | None = None, web: bool = False) -> Provider:
    return provider(cfg.provider(role, cli_value), web=web, cache_dir=cfg.cache_dir)


def detector(cfg: Config, cli_value: str | None) -> Provider | None:
    spec = cfg.provider("detector", cli_value)
    return None if spec == "rules" else provider(spec, cache_dir=cfg.cache_dir)


def design_defaults(cfg: Config) -> DesignDefaults:
    return DesignDefaults(register=cfg.get("defaults.register"), drop_list=cfg.get("defaults.drop_list"),
                          avoid=cfg.get("defaults.avoid") or [], skip_max=cfg.get("defaults.noise.skip_max"),
                          aside_max=cfg.get("defaults.noise.aside_max"), forms=cfg.get("defaults.forms") or "",
                          rules=default_rules(cfg.rules_path()), max_material_ratio=cfg.get("defaults.max_material_ratio"),
                          chars_per_mention=cfg.get("defaults.chars_per_mention"))

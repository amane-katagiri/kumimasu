from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from conftest import always_ask, defaults
from test_steps import PROJECT, SAMPLES, scripted
from typer.testing import CliRunner

from kumimasu import cli_common as cc
from kumimasu import ops
from kumimasu.cli import app
from kumimasu.config import ConfigError, init_template, load
from kumimasu.design import design
from kumimasu.interview import interview
from kumimasu.mark import mark
from kumimasu.model import Project
from kumimasu.workdir import init_workdir


@pytest.fixture
def places(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    user = tmp_path / "user" / "config.yaml"
    user.parent.mkdir()
    monkeypatch.setenv("KUMIMASU_CONFIG", str(user))
    wd, _ = init_workdir(tmp_path / "wd", Project(topic="t", audience="a"), [])
    return cwd, user, wd.root


def write(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_defaults(places):
    cwd, _user, wdir = places
    cfg = load(wdir, cwd)
    assert cfg.provider("writer") == "claude-cli:opus" and cfg.provider("judge") == "claude-cli:sonnet"
    assert cfg.cache_dir == str(Path(os.environ["HOME"]) / ".cache" / "kumimasu")
    assert cfg.get("surface.runs") == 3 and cfg.get("serve.port") == 8792 and cfg.rules_path() is None
    assert cfg.workdir_root == str(Path(os.environ["HOME"]) / ".cache" / "kumimasu" / "work")
    assert {r["key"]: r["layer"] for r in cfg.effective()}["cache_dir"] == "default"


def test_precedence_and_relative_paths(places):
    cwd, user, wdir = places
    write(user, {"providers": {"writer": "user-writer", "judge": "user-judge"}, "cache_dir": "ucache",
                 "surface": {"runs": 5}})
    write(cwd / "kumimasu.yaml", {"providers": {"writer": "project-writer"}, "cache_dir": "pcache"})
    p = yaml.safe_load((wdir / "project.yaml").read_text(encoding="utf-8"))
    p["config"] = {"providers": {"writer": "workdir-writer"}, "workdir_root": "tmpwork"}
    write(wdir / "project.yaml", p)
    cfg = load(wdir, cwd, trust=True)
    assert cfg.provider("writer", "cli-writer") == "cli-writer" and cfg.resolve("surface.runs", None) == 5
    assert cfg.provider("writer") == "workdir-writer" and cfg.provider("judge") == "user-judge"
    assert cfg.get("surface.runs") == 5 and cfg.get("surface.min_votes") == 2
    assert cfg.cache_dir == str((cwd / "pcache").resolve()) and cfg.workdir_root == str((wdir / "tmpwork").resolve())
    assert load(None, cwd, trust=True).provider("writer") == "project-writer"
    assert load(None, cwd / "..").cache_dir == str((user.parent / "ucache").resolve())
    layers = {r["key"]: r["layer"] for r in cfg.effective()}
    assert layers["providers.judge"] == "user" and layers["providers.writer"] == "workdir"


def test_project_and_workdir_layers_are_untrusted_for_sensitive_keys(places, monkeypatch):
    cwd, _user, wdir = places
    write(cwd / "kumimasu.yaml", {"providers": {"writer": "codex-cli"}, "cache_dir": "/tmp/x", "workdir_root": "w",
                                  "rules_file": "../outside.yaml", "surface": {"runs": 4},
                                  "defaults": {"forms": "表にする"}})
    p = yaml.safe_load((wdir / "project.yaml").read_text(encoding="utf-8"))
    p["config"] = {"providers": {"judge": "codex-cli"}, "rules_file": "/etc/rules.yaml"}
    write(wdir / "project.yaml", p)
    cfg = load(wdir, cwd)
    assert cfg.provider("writer") == "claude-cli:opus" and cfg.provider("judge") == "claude-cli:sonnet"
    assert cfg.cache_dir.endswith("/.cache/kumimasu") and cfg.rules_path() is None
    assert cfg.get("surface.runs") == 4 and cfg.get("defaults.forms") == "表にする"
    assert len(cfg.warnings()) == 6 and all("--trust-project" in w for w in cfg.warnings())
    assert load(wdir, cwd, trust=True).provider("writer") == "codex-cli"
    monkeypatch.setenv("KUMIMASU_TRUST_PROJECT", "1")
    assert load(wdir, cwd).provider("judge") == "codex-cli"
    monkeypatch.delenv("KUMIMASU_TRUST_PROJECT")
    out = CliRunner().invoke(app, ["config", str(wdir)]).output
    assert "注意:" in out and "[使わない:" in out


def test_errors_and_rules_file(places):
    cwd, _user, wdir = places
    write(cwd / "kumimasu.yaml", {"providers": {"nope": "x"}})
    with pytest.raises(ConfigError, match="知らないキー 'providers.nope'"):
        load(wdir, cwd)
    write(cwd / "kumimasu.yaml", {"surface": {"runs": "many"}})
    with pytest.raises(ConfigError, match="surface.runs は int"):
        load(wdir, cwd)
    write(cwd / "kumimasu.yaml", {"rules_file": "my-rules.yaml"})
    with pytest.raises(ConfigError, match="がありません"):
        load(wdir, cwd).rules_path()
    (cwd / "my-rules.yaml").write_text("- 一つだけのルール\n", encoding="utf-8")
    assert load(wdir, cwd).rules_path() == (cwd / "my-rules.yaml").resolve()
    home_rules = Path(os.environ["HOME"]) / ".config" / "kumimasu" / "rules.yaml"
    (cwd / "kumimasu.yaml").unlink()
    assert load(wdir, cwd).rules_path() is None
    home_rules.parent.mkdir(parents=True)
    home_rules.write_text("- 家のルール\n", encoding="utf-8")
    assert load(wdir, cwd).rules_path() == home_rules


def test_design_uses_rules_file(places):
    cwd, user, _ = places
    (cwd / "r.yaml").write_text("- 設定ファイルのルール\n", encoding="utf-8")
    write(user, {"rules_file": str(cwd / "r.yaml")})
    p = scripted()
    w, _ = init_workdir(cwd / "w", PROJECT, [SAMPLES / "notes.md"])
    mark(w, p, p)
    interview(w, p, always_ask())
    ops.confirm(w, "agent-chat")
    assert [r.text for r in design(w, p, defaults()).rules] == ["設定ファイルのルール"]


def test_cli_override_and_config_command(places, monkeypatch):
    cwd, _user, wdir = places
    write(cwd / "kumimasu.yaml", {"providers": {"interviewer": "fake:from-project"}, "cache_dir": "c"})
    seen = []
    monkeypatch.setattr(cc, "provider", lambda spec, web=False, **kw: seen.append((spec, kw.get("cache_dir"))) or
                        __import__("kumimasu.llm", fromlist=["FakeProvider"]).FakeProvider(lambda p: '{"questions": []}'))
    r = CliRunner()
    assert r.invoke(app, ["interview", str(wdir)]).exit_code == 0
    assert r.invoke(app, ["--trust-project", "interview", str(wdir), "--overwrite"]).exit_code == 0
    assert r.invoke(app, ["--trust-project", "interview", str(wdir), "--provider", "fake:cli", "--overwrite"]).exit_code == 0
    home_cache = str(Path(os.environ["HOME"]) / ".cache" / "kumimasu")
    assert seen == [("claude-cli:sonnet", home_cache), ("fake:from-project", str((cwd / "c").resolve())),
                    ("fake:cli", str((cwd / "c").resolve()))]
    out = r.invoke(app, ["--trust-project", "config", str(wdir)]).output
    assert "providers.interviewer" in out and "fake:from-project" in out and "project" in out
    rows = json.loads(r.invoke(app, ["config", "--json"]).stdout)
    assert {x["key"]: x["layer"] for x in rows}["providers.writer"] == "default"


def test_config_init_writes_template_only_when_asked(places):
    cwd, user, wdir = places
    r = CliRunner()
    assert not user.exists() and not (cwd / "kumimasu.yaml").exists()
    res = r.invoke(app, ["config", "init", "--user"])
    assert res.exit_code == 0 and user.exists() and "providers:" in user.read_text(encoding="utf-8")
    assert load(wdir, cwd).provider("writer") == "claude-cli:opus"
    res = r.invoke(app, ["config", "init", "--user"])
    assert res.exit_code == 1 and "上書きしません" in res.output
    assert r.invoke(app, ["config", "init", "--project"]).exit_code == 0 and (cwd / "kumimasu.yaml").exists()
    with pytest.raises(ConfigError):
        init_template(cwd / "kumimasu.yaml")
    assert r.invoke(app, ["config", "init"]).exit_code != 0

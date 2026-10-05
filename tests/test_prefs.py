from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from conftest import (
    PROJECT,
    SAMPLES,
    always_ask,
    cli_stdout,
    defaults,
    roles,
    run_cli,
    scripted,
    settings,
)

from kumimasu import ops
from kumimasu.design import design
from kumimasu.draft import draft, draft_prompt, read_used
from kumimasu.interview import interview
from kumimasu.mark import mark
from kumimasu.model import Rule
from kumimasu.prefs import prefs_diff, rules_diff
from kumimasu.rules import packaged_rules
from kumimasu.server import WriteApp
from kumimasu.workdir import WorkDir, init_workdir


@pytest.fixture
def cwd(tmp_path, monkeypatch) -> Path:
    c = tmp_path / "cwd"
    c.mkdir()
    monkeypatch.chdir(c)
    return c


def user_cfg() -> Path:
    return Path(os.environ["KUMIMASU_CONFIG"])


def write_user(data: dict) -> None:
    user_cfg().write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def designed(root: Path) -> WorkDir:
    p = scripted()
    w, _ = init_workdir(root, PROJECT, [SAMPLES / "notes.md", SAMPLES / "rename.sh"])
    mark(w, p, p)
    interview(w, p, always_ask())
    ops.confirm(w, "agent-chat")
    design(w, p, defaults())
    return w


def test_always_ask_is_added_once(cwd):
    write_user({"interview": {"always_ask": ["LINE で受け取った写真だけ EXIF が消えていると気づいたとき、何を考えましたか",
                                             "一番の驚きは？"]}})
    p = scripted()
    w, _ = init_workdir(cwd / "w", PROJECT, [SAMPLES / "notes.md"])
    mark(w, p, p)
    qs = interview(w, p, always_ask()).questions
    assert [q.question for q in qs].count("LINE で受け取った写真だけ EXIF が消えていると気づいたとき、何を考えましたか") == 1
    assert qs[-1].question == "一番の驚きは？" and qs[-1].id == f"q{len(qs)}"


def test_design_takes_the_defaults(cwd):
    write_user({"defaults": {"register": "joutai", "drop_list": "full", "avoid": ["価格の比較"],
                             "noise": {"skip_max": 1, "aside_max": 0}, "forms": "比較は表、手順は番号付きリスト"}})
    w = designed(cwd / "w")
    d = w.design()
    assert d.formality == "joutai" and d.drop_list == "full" and d.avoid[0] == "価格の比較" and len(d.skip) == 1
    assert d.aside == [] and d.form_prefs == "比較は表、手順は番号付きリスト"

    prompt = draft_prompt(w.project(), d, w.units())
    assert "常体" in prompt and "著者の好み: 比較は表、手順は番号付きリスト" in prompt and "## 書かない事柄" in prompt
    assert prefs_diff(w, settings()) == []


def test_used_settings_are_recorded_shown_and_handed_over(cwd):
    w = designed(cwd / "w")
    rules = w.design().rules
    rules[0].on = False
    w.save_design(w.design().model_copy(update={"rules": rules}))
    ops.confirm(w, "agent-chat")
    draft(w, scripted(), roles())
    used = read_used(w, "draft.md")
    assert used["providers"]["writer"] == "fake:fake-1" and used["providers"]["judge"] == "claude-cli:sonnet"
    assert rules[0].text in used["rules_off"] and rules[0].text not in used["rules"] and used["register"] == "keitai"
    assert used["avoid"] == w.design().avoid and used["design"] == "design.yaml"
    ops.confirm(w, "agent")

    assert WriteApp(w, None, 3).review("draft.md")["used"] == used
    h = ops.confirm(w, "agent-chat")
    assert h["used"] == used


def test_rules_edit_user_and_project_scope(cwd):
    out = run_cli("rules", "list", "--scope", "user")
    assert "まだ無い" in out and "道しるべ" in out
    home_rules = Path(os.environ["HOME"]) / ".config" / "kumimasu" / "rules.yaml"
    assert not home_rules.exists()
    out = run_cli("rules", "off", "1", "--scope", "user")
    assert "同梱の既定のルールで作りました" in out and home_rules.exists()
    run_cli("rules", "add", "新しい既定のルール", "--scope", "user")
    run_cli("rules", "edit", "2", "書き換えたルール", "--scope", "user")
    run_cli("rules", "rm", "3", "--scope", "user")
    rows = yaml.safe_load(home_rules.read_text(encoding="utf-8"))
    assert rows[0] == {"text": packaged_rules()[0].text, "on": False} and rows[1] == "書き換えたルール"
    assert rows[-1] == "新しい既定のルール" and len(rows) == len(packaged_rules())
    assert "[off]" in run_cli("rules", "list", "--scope", "user")
    (cwd / "kumimasu.yaml").write_text("# 頭のコメント\ncache_dir: c\n", encoding="utf-8")
    out = run_cli("rules", "add", "プロジェクトのルール", "--scope", "project")
    assert "rules_file: kumimasu.rules.yaml を足しました" in out and "同梱の既定のルールで作りました" in out
    assert (cwd / "kumimasu.yaml").read_text(encoding="utf-8") == "# 頭のコメント\ncache_dir: c\nrules_file: kumimasu.rules.yaml\n"
    assert yaml.safe_load((cwd / "kumimasu.rules.yaml").read_text(encoding="utf-8"))[-1] == "プロジェクトのルール"
    out = run_cli("rules", "rm", "1", "--scope", "project")
    assert "足しました" not in out and "作りました" not in out
    run_cli("rules", "on", "99", "--scope", "user", code=1)
    run_cli("rules", "list", code=2)


def test_rules_diff_and_save(cwd):
    w = designed(cwd / "w")
    d = w.design()
    rules = [r.model_copy() for r in d.rules]
    rules[1].on = False
    rules[2].text = "書き換えた三つ目"
    del rules[4]
    rules.append(Rule(text="この記事で足したルール"))
    w.save_design(d.model_copy(update={"rules": rules}))
    diffs = rules_diff(w, settings())
    assert [(x.kind, x.text) for x in diffs] == [
        ("disabled", rules[1].text), ("edited", "書き換えた三つ目"), ("removed", d.rules[4].text),
        ("added", "この記事で足したルール")]
    assert diffs[1].old == d.rules[2].text
    out = run_cli("rules", "diff", w.root)
    assert "1. オフ" in out and "4. 追加" in out
    run_cli("rules", "save", w.root, code=2)
    out = run_cli("rules", "save", w.root, "--scope", "user", "--only", "1,4")
    assert "2 changes" in out
    left = rules_diff(w, settings())
    assert [x.kind for x in left] == ["edited", "removed"]
    run_cli("rules", "save", w.root, "--scope", "user")
    assert rules_diff(w, settings()) == []


def test_prefs_diff_and_save(cwd):
    (cwd / "kumimasu.yaml").write_text("# このプロジェクトの設定\ncache_dir: c\n", encoding="utf-8")
    w = designed(cwd / "w")
    d = w.design()
    w.save_design(d.model_copy(update={"formality": "joutai", "form_prefs": "表を多めに", "avoid": d.avoid + ["毎回の自己紹介"]}))
    diffs = prefs_diff(w, settings())
    keys = [x.key for x in diffs]
    assert "defaults.register" in keys and "defaults.forms" in keys and keys.count("defaults.avoid") == 1
    out = run_cli("prefs", "diff", w.root)
    assert "defaults.register" in out and "'joutai'" in out
    out = run_cli("prefs", "save", w.root, "--scope", "project", "--only", "defaults.register,defaults.forms")
    text = (cwd / "kumimasu.yaml").read_text(encoding="utf-8")
    assert text.startswith("# このプロジェクトの設定\n") and "register: joutai" in text and "cache_dir: c" in text
    keys = [x.key for x in prefs_diff(w, settings())]
    assert "defaults.register" not in keys and "defaults.forms" not in keys
    n = next(x.n for x in prefs_diff(w, settings()) if x.value == "毎回の自己紹介")
    run_cli("prefs", "save", w.root, "--scope", "user", "--only", str(n))
    assert yaml.safe_load(user_cfg().read_text(encoding="utf-8"))["defaults"]["avoid"] == ["毎回の自己紹介"]
    assert all(x.value != "毎回の自己紹介" for x in prefs_diff(w, settings()))
    run_cli("prefs", "nope", w.root, code=2)
    assert json.loads(cli_stdout("config", w.root, "--json"))[-1]["key"] == "interview.always_ask"

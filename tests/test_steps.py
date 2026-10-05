from __future__ import annotations

import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from conftest import (
    BAD_DRAFT,
    GOOD_DRAFT,
    PROJECT,
    SAMPLES,
    VOTES,
    always_ask,
    defaults,
    roles,
    scripted,
    serving,
    surface_fake,
)
from typer.testing import CliRunner

from kumimasu import cli_common as cc
from kumimasu import factcheck, ops, rules
from kumimasu.check import check, dash_hits, number_flags
from kumimasu.cli import app
from kumimasu.design import design, noise_workdir, parse_review, sync_design
from kumimasu.draft import draft, draft_prompt
from kumimasu.errors import StepError
from kumimasu.factcheck import UrlStatus
from kumimasu.interview import interview, parse_questions, strip_unit_refs
from kumimasu.llm import (
    CachedProvider,
    ClaudeCliProvider,
    CodexCliProvider,
    FakeProvider,
    get_provider,
)
from kumimasu.mark import mark
from kumimasu.model import Design, Rule, UnitUse
from kumimasu.polish import POLISH_RULES, Flag, apply_replacements, neighborhood, polish
from kumimasu.research import load_research, research
from kumimasu.review import export_final
from kumimasu.revise import instructions, revise
from kumimasu.show import snapshot
from kumimasu.show import text as show_text
from kumimasu.surface import detect_surface
from kumimasu.textutil import blocks
from kumimasu.workdir import WorkDir, init_workdir


@pytest.fixture
def wd(tmp_path) -> WorkDir:
    w, _ = init_workdir(tmp_path / "w", PROJECT, [SAMPLES / "notes.md", SAMPLES / "rename.sh"])
    return w


def answered(w: WorkDir, p=None) -> None:
    p = p or scripted()
    mark(w, p, p)
    interview(w, p, always_ask())
    iv = w.interview()
    iv.questions[0].answer = "撮影日が無い写真が LINE 経由だけだったのが意外だった"
    w.save_interview(iv)


def test_init_copies_material_and_keeps_links(wd):
    units = wd.material_units()
    assert (wd.material_dir / "notes.md").exists() and (wd.material_dir / "rename.sh").exists()
    assert [u.id for u in units] == [f"m{i}" for i in range(1, len(units) + 1)]
    assert any("https://exiftool.org/" in u.text for u in units)
    code = [u for u in units if u.source == "rename.sh"]
    assert len(code) == 1 and code[0].kind == "code" and "FileModifyDate" in code[0].text
    assert yaml.safe_load(wd.project_file.read_text(encoding="utf-8"))["materials"] == ["notes.md", "rename.sh"]
    with pytest.raises(StepError):
        init_workdir(wd.root, PROJECT, [])


def test_mark_writes_baseline_and_searchable(wd):
    p = scripted()
    units = mark(wd, p, p)
    assert [u.searchable for u in units[:4]] == ["no", "yes", "yes", "no"]
    assert (wd.root / "baseline" / "W.md").read_text(encoding="utf-8").startswith("# 写真の名前")
    assert "ウェブ検索" in (wd.root / "baseline" / "W.prompt.md").read_text(encoding="utf-8")
    assert not units[1].firsthand and units[0].firsthand
    assert units[8].cluster == "m2" and units[1].cluster == "" and units[2].cluster == ""
    assert json.loads((wd.root / "baseline" / "mark.json").read_text(encoding="utf-8"))["clusters"] == [["m2", "m9"]]
    merged = wd.units()
    m2 = next(u for u in merged if u.id == "m2")
    assert "m9" not in [u.id for u in merged] and m2.members == ["m9"] and "filename.html" in m2.text
    calls = len(p.calls)
    mark(wd, p, p)
    assert len(p.calls) == calls + 2


def test_web_provider_gets_tools_only_for_claude_cli(tmp_path):
    cached = cc.provider("claude-cli:opus", web=True, cache_dir=str(tmp_path))
    assert cached.inner.allowed_tools == ("WebSearch", "WebFetch") and cached.dir == tmp_path
    assert cc.provider("claude-cli:sonnet").allowed_tools == ()
    assert isinstance(cc.provider("fake", web=True), FakeProvider)
    assert get_provider("fake").name == "fake"


def test_interview_questions_and_answers_become_units(wd):
    answered(wd)
    iv = wd.interview()
    assert [q.id for q in iv.questions] == ["q1", "q2", "q3"]
    assert iv.questions[2].question == "読者に一つだけ持ち帰ってほしいことは何ですか" and "always_ask" in iv.questions[2].why
    assert iv.questions[0].units == ["m4"]
    assert iv.questions[0].question == "LINE で受け取った写真だけ EXIF が消えていると気づいたとき、何を考えましたか"
    assert iv.questions[0].context == "LINE 経由の写真は EXIF が消えていて、更新日時で代用した"
    ans = wd.answer_units()
    assert [u.id for u in ans] == ["q1"] and ans[0].firsthand and ans[0].text.startswith("撮影日が無い写真")
    assert "問い" not in ans[0].text and "EXIF が消えている" in ans[0].context
    assert [u.id for u in wd.units()][-1] == "q1"
    with pytest.raises(StepError):
        interview(wd, scripted(), always_ask())
    interview(wd, scripted(), always_ask(), overwrite=True)
    assert wd.answer_units() == []


def test_design_defaults_and_limits(wd):
    p = scripted()
    with pytest.raises(StepError):
        design(wd, p, defaults())
    answered(wd, p)
    d = design(wd, p, defaults())
    uses = {u.id: u.use for u in d.units}
    assert len(d.takeaways) == 3 and d.target_length == 600 and d.formality == "keitai"
    assert uses["m2"] == "drop" and uses["m4"] == "deep" and uses["q1"] == "deep" and "zz" not in uses
    assert uses["m3"] == "drop" and uses["m1"] == "mention"
    assert any("FAQ" in r.text for r in d.rules) and all(r.on for r in d.rules)
    assert "m9" not in uses
    assert d.avoid == ["写真管理アプリの比較", "FAQ"]
    assert [(c.id, c.by) for c in d.conflicts] == [("m3", ["m12"])]
    assert d.live_conflicts()[0].message() == "「コマンドが出る」が出る（原因: m12 / 書かない側: m3）"
    d2 = d.model_copy(update={"units": [u.model_copy(update={"use": "drop"}) if u.id == "m12" else u for u in d.units]})
    assert d2.live_conflicts() == []
    design_text = next(c["prompt"] for c in p.calls if "記事の設計を提案" in c["prompt"])
    assert "問い: LINE で受け取った写真だけ" in design_text and "選び方の指示" in design_text
    with pytest.raises(StepError):
        design(wd, p, defaults())


def test_sync_design_adds_late_answers_and_removes_gone_units(wd):
    answered(wd)
    d = Design(units=[UnitUse(id="m1", use="deep"), UnitUse(id="q9", use="deep")])
    synced = sync_design(d, wd.units())
    ids = [u.id for u in synced.units]
    assert "q9" not in ids and "q1" in ids and synced.use_of("m1") == "deep"


def test_draft_prompt_sections(wd):
    p = scripted()
    answered(wd, p)
    design(wd, p, defaults())
    d, units = wd.design(), wd.units()
    prompt = draft_prompt(wd.project(), d, units)
    deep_part = prompt.split("## 掘り下げる材料", 1)[1].split("## 触れる材料", 1)[0]
    avoid_part = prompt.split("## 書かない話題", 1)[1].split("## ", 1)[0]
    m2 = next(u for u in units if u.id == "m2")
    assert "[q1]" in deep_part and "LINE 経由だけ" in deep_part and "[m4]" in deep_part
    assert "写真管理アプリの比較" in avoid_part and "[m2]" not in prompt and m2.text[:20] not in prompt
    assert "問い" not in prompt and "EXIF が消えていると気づいた" not in prompt
    full = draft_prompt(wd.project(), d, units, "full")
    assert m2.text[:20] in full.split("## 書かない事柄", 1)[1] and "## 書かない話題" not in full
    assert "書かない" not in draft_prompt(wd.project(), d, units, "none").split("## 決まり")[0].split("## 触れる材料")[1]
    assert "敬体" in prompt and "ウェブは使えません" in prompt and "<article>" in prompt and "FAQ" in prompt
    assert prompt.startswith("この依頼に含まれる材料")
    text = draft(wd, p, roles())
    assert text == GOOD_DRAFT and (wd.root / "draft.prompt.md").exists()


def _ready(wd, p):
    answered(wd, p)
    design(wd, p, defaults())
    draft(wd, p, roles())


def test_check_passes_the_design_relations(wd):
    p = scripted()
    _ready(wd, p)
    rep = check(wd, p, None, "draft.md", VOTES, fetch=lambda us: [UrlStatus(url=u, status=200) for u in us])
    by = {c.id: c for c in rep.checks}
    assert by["drop_absent"].passed is True
    assert by["deep_present"].passed is True
    assert by["deep_space"].passed is True
    assert by["takeaways"].passed is True and by["takeaways"].items[0]["evidence"]
    assert by["fabrication"].passed is True
    assert by["numbers"].passed is True, by["numbers"].items
    assert by["meta"].passed is True and by["lint"].passed is True
    assert by["links"].passed is True and by["links"].value == "1/1"
    bad = check(wd, p, None, "draft.md", VOTES, fetch=lambda us: [UrlStatus(url=u, status=404) for u in us])
    assert next(c for c in bad.checks if c.id == "links").passed is False
    assert json.loads((wd.root / "check.json").read_text(encoding="utf-8"))["draft"] == "draft.md"
    assert "deep_space" in (wd.root / "check.txt").read_text(encoding="utf-8")


def test_check_flags_violations(wd):
    p = scripted(draft_text=BAD_DRAFT, present_drop=True, deep_unit_chars=False)
    _ready(wd, p)
    rep = check(wd, p, None, "draft.md", VOTES)
    by = {c.id: c for c in rep.checks}
    assert by["drop_absent"].passed is False and by["drop_absent"].value == "1/2"
    assert [(i["id"], i["status"]) for i in by["drop_absent"].items] == [("m2", "added"), ("m3", "implied")]
    assert by["deep_space"].passed is False
    assert by["fabrication"].passed is False
    assert {i["number"] for i in by["numbers"].items} == {"2000"}
    assert by["meta"].passed is False and by["meta"].items[0]["category"] == "signpost"
    assert {i["rule"] for i in by["lint"].items} >= {"bold-lead-item", "emoji"}
    assert by["links"].passed is None
    assert by["meta"].surface and by["lint"].surface and not by["drop_absent"].surface
    todo = instructions(rep, 600)
    assert len(todo) == len(rep.structural_failed()) < len(rep.failed())
    assert not any("見ていきましょう" in t for t in todo) and any("2000" in t for t in todo)
    assert "exiftool -d" not in todo[0] and "EXIF の DateTimeOriginal" in todo[0]


def test_design_proposes_skips_and_asides(wd):
    p = scripted()
    answered(wd, p)
    d = design(wd, p, defaults())
    assert [(x.label, x.units) for x in d.skip] == [("EXIF とは何か", ["m2"]), ("タイムゾーンの仕組み", [])]
    assert [(a.id, a.where) for a in d.aside] == [("m7", "名前を付け終えたあと")]
    assert d.use_of("m2") == "drop" and d.use_of("m7") == "mention"
    noise_text = next(c["prompt"] for c in p.calls if "本題に要らない話" in c["prompt"])
    assert "EXIF の DateTimeOriginal を exiftool で読みます" in noise_text and "[m4]（use: deep）" in noise_text
    prompt = draft_prompt(wd.project(), d, wd.units())
    skip_part = prompt.split("## 説明しない前提", 1)[1].split("\n## ", 1)[0]
    aside_part = prompt.split("## 脱線", 1)[1].split("\n## ", 1)[0]
    mention_part = prompt.split("## 触れる材料", 1)[1].split("\n## ", 1)[0]
    assert "- EXIF とは何か" in skip_part and "補ったり" in skip_part
    assert "[m7]" in aside_part and "名前を付け終えたあと" in aside_part and "結びつけて" in aside_part
    assert "[m7]" not in mention_part


def test_noise_keeps_other_uses(wd):
    p = scripted()
    answered(wd, p)
    design(wd, p, defaults())
    d = wd.design()
    d = d.model_copy(update={"skip": [], "aside": [],
                             "units": [u.model_copy(update={"use": "deep"}) if u.id == "m10" else u for u in d.units]})
    wd.save_design(d)

    d2 = noise_workdir(wd, p, 3, 2)
    assert d2.use_of("m10") == "deep" and [a.id for a in d2.aside] == ["m7"] and d2.avoid == d.avoid
    wd.save_design(d2.model_copy(update={"aside": [],
                                         "units": [u.model_copy(update={"use": "drop"}) if u.id == "m7" else u for u in d2.units]}))
    d3 = noise_workdir(wd, p, 3, 2)
    assert d3.aside == [] and d3.use_of("m7") == "drop"


def test_check_skip_and_aside_relations(wd):
    p = scripted(skip_explained=True)
    _ready(wd, p)
    rep = check(wd, p, None, "draft.md", VOTES)
    by = {c.id: c for c in rep.checks}
    assert by["skip_unexplained"].passed is False and by["skip_unexplained"].items[0]["skip"] == "EXIF とは何か"
    assert by["aside_present"].passed is True and by["aside_present"].value == "1/1"
    todo = instructions(rep, 600)
    assert any("説明しないと決めた" in t and "EXIF とは何か" in t for t in todo)
    assert not any(c.id in ("glue", "meta", "lint") for c in rep.structural_failed())


def test_surface_rules_and_trace_exclusion():
    md = ("# 題\n\nこの記事では写真の話を見ていきます。Binary Eye は読み取ると GET を送ります。"
          "これにより、手で入力する必要がなくなります。これにより、スキャナーとサーバーの結合が弱くなります。\n")
    fake = FakeProvider(surface_fake)
    rep = detect_surface(md, fake, ["スキャナーとサーバーの結合が弱くなる"], 3, 2)
    assert [(h.category, h.votes) for h in rep.hits] == [("signpost", 2), ("glue", 2)]
    assert len(rep.traced) == 1 and "結合" in rep.traced[0].text and rep.runs_used == 2 and len(fake.calls) == 2
    assert "（判定 1）" in fake.calls[0]["prompt"] and "glue:" in fake.calls[0]["prompt"]


def _votes_provider(picks: dict[int, list[str]]):
    def respond(prompt: str) -> str:
        run = int(re.search(r"（判定 (\d+)）", prompt)[1])
        return json.dumps({"items": [{"id": i, "category": "glue"} for i in picks[run]]})
    return respond


def test_surface_majority_runs_are_independent_and_cached(tmp_path):
    md = "# 題\n\n一つ目の文です。二つ目の文です。三つ目の文です。\n"
    inner = FakeProvider(_votes_provider({1: ["M1", "M2"], 2: ["M1", "M3"], 3: ["M1", "M2"]}))
    cached = CachedProvider(inner, tmp_path / "cache")
    rep = detect_surface(md, cached, [], 3, 2)
    assert rep.runs_used == 3 and [(h.id, h.votes) for h in rep.hits] == [("M1", 3), ("M2", 2)] and rep.union == 3
    assert cached.misses == 3 and len(list((tmp_path / "cache").rglob("*.json"))) == 3
    again = detect_surface(md, cached, [], 3, 2)
    assert cached.misses == 3 and cached.hits == 3 and [h.id for h in again.hits] == ["M1", "M2"]
    one = detect_surface(md, FakeProvider(_votes_provider({1: ["M2"], 2: ["M3"], 3: ["M1"]})), [], 3, 1)
    assert [h.id for h in one.hits] == ["M1", "M2", "M3"]


def test_surface_early_stop_when_two_runs_agree():
    p = FakeProvider(_votes_provider({1: ["M2"], 2: ["M2"], 3: ["M1"]}))
    rep = detect_surface("# 題\n\n一つ目の文です。二つ目の文です。\n", p, [], 3, 2)
    assert rep.runs_used == 2 and len(p.calls) == 2 and [h.id for h in rep.hits] == ["M2"]


POLISH_DOC = """# 題

## 一

最初の段落です。

前の話です。つなぎAです。

次の段落の文です。つなぎBです。

## 二

遠い段落一です。

遠い段落二です。

遠い段落三です。
"""


def _round_provider(always_new: bool = False):
    state = {"n": 0}

    def respond(prompt: str) -> str:
        if "一覧の文だけを直して" in prompt:
            ids = re.findall(r"^\[(F\d+)\]", prompt, re.MULTILINE)
            state["n"] += 1
            return json.dumps({"items": [{"id": i, "replacement": f"新しいつなぎ{state['n']}です。" if always_new else ""}
                                         for i in ids]})
        picks = []
        for m in re.finditer(r"^\[(M\d+)\] (.*?)(?:  ←.*)?$", prompt, re.MULTILINE):
            text = m[2]
            if text.startswith(("つなぎA", "新しいつなぎ")) or (text.startswith("つなぎB") and "つなぎA" not in prompt):
                picks.append({"id": m[1], "category": "glue"})
        return json.dumps({"items": picks})
    return FakeProvider(respond)


def test_polish_rounds_redetect_the_edited_neighborhood(wd):
    wd.write("p.md", POLISH_DOC)
    p = _round_provider()
    res = polish(wd, p, "p.md", ("meta", "glue"), True, VOTES, 3, "p.polished.md")
    assert [(r.hits, r.edits) for r in res.rounds] == [(1, 1), (1, 1), (0, 0)]
    assert [r.runs for r in res.rounds] == [2, 2, 2] and res.calls == 2 + 1 + 2 + 1 + 2
    later = [c["prompt"] for c in p.calls[3:] if "判定" in c["prompt"]]
    assert later and all("遠い段落三" not in x for x in later) and all("つなぎB" in x or "次の段落" in x for x in later)
    text = (wd.root / "p.polished.md").read_text(encoding="utf-8")
    assert "つなぎ" not in text and "遠い段落三です。" in text and "\n\n\n" not in text
    log = json.loads((wd.root / "p.polished.json").read_text(encoding="utf-8"))
    assert [r["round"] for r in log["rounds"]] == [1, 2, 3]


def test_polish_stops_at_max_rounds_and_lists_without_apply(wd):
    wd.write("p.md", POLISH_DOC)
    res = polish(wd, _round_provider(always_new=True), "p.md", ("glue",), True, VOTES, 2, "p.polished.md")
    assert len(res.rounds) == 2 and [r.edits for r in res.rounds] == [1, 2]
    assert "新しいつなぎ2です。" in (wd.root / "p.polished.md").read_text(encoding="utf-8")
    wd.write("q.md", POLISH_DOC)
    listed = polish(wd, _round_provider(), "q.md", POLISH_RULES, False, VOTES, 3, "q.polished.md")
    assert len(listed.rounds) == 1 and listed.rounds[0].hits == 1 and listed.out == ""
    assert not (wd.root / "q.polished.md").exists()


def test_blocks_keep_fences_and_neighborhood_adds_heading():
    text = "# 題\n\n## 節\n\n一。\n\n```\na\n\nb\n```\n\n二。\n\n三。\n\n四。\n"
    assert len(blocks(text)) == 7
    scope = neighborhood(text, [text.index("三")])
    assert scope.startswith("## 節") and "二。" in scope and "四。" in scope and "一。" not in scope


def test_default_rules_packaged_and_rules_file(tmp_path):
    packaged = [r.text for r in rules.default_rules()]
    assert len(packaged) == 13 and "読者が知っている前提を丁寧に言い直さない。説明は一度だけ、必要な所で" in packaged
    assert any("「まとめ」の節で繰り返さない" in t for t in packaged)
    user = tmp_path / "user.yaml"
    user.write_text("- 自分のルール\n", encoding="utf-8")
    assert [r.text for r in rules.default_rules(user)] == ["自分のルール"]
    user.write_text("a: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        rules.default_rules(user)


def test_off_rules_stay_in_design_but_not_in_prompt(wd):
    p = scripted()
    answered(wd, p)
    d = design(wd, p, defaults())
    d = d.model_copy(update={"rules": [Rule(text="使うルール"), Rule(text="止めたルール", on=False), Rule(text=" ")]})
    wd.save_design(d)
    again = wd.design()
    assert [(r.text, r.on) for r in again.rules] == [("使うルール", True), ("止めたルール", False), (" ", True)]
    prompt = draft_prompt(wd.project(), again, wd.units())
    rules_part = prompt.split("## 決まり", 1)[1].split("\n\n", 2)[1]
    assert rules_part == "- 使うルール" and "止めたルール" not in prompt


def test_dash_hits_skip_code_and_tables():
    md = "読み取りは速い——ただし暗いと遅い。\n\n| a — b |\n|---|\n\n```\nx — y\n```\n\n普通の文です。\n"
    assert dash_hits(md) == ["読み取りは速い——ただし暗いと遅い。"]


def test_polish_rewrites_only_flagged_sentences(wd):
    p = scripted(draft_text=BAD_DRAFT + "\n## 写真の名前は日付が一番だ\n\n日時は秒まで入れる——連写があるから。\n")
    _ready(wd, p)
    res = polish(wd, p, "draft.md", POLISH_RULES, True, VOTES, 1, "draft.polished.md")
    flags = res.rounds[0].flags
    assert [f.rule for f in flags] == ["signpost", "claim_heading", "dash"]
    text = (wd.root / res.out).read_text(encoding="utf-8")
    assert res.out == "draft.polished.md" and [x["status"] for x in res.rounds[0].log] == ["deleted", "rewritten", "rewritten"]
    assert "見ていきましょう" not in text and "## 撮影日時は EXIF にあります。" in text and "——" not in text
    assert "- **EXIF**: 撮影日時を記録する仕組みです 📷" in text and "\n\n\n" not in text


def test_apply_replacements_reports_missing():
    text, log, edits = apply_replacements("一つ目の文。\n二つ目\nの文。三つ目。\n",
                                          [Flag(id="F1", rule="x", text="二つ目の文。"), Flag(id="F2", rule="x", text="無い文。"),
                                           Flag(id="F3", rule="x", text="一つ目の文。"), Flag(id="F4", rule="x", text="三つ目。"),
                                           Flag(id="F5", rule="x", text="一つ目の文。")],
                                          {"F1": "", "F2": "a", "F4": "3。", "F5": "一つ目の 文。"})
    assert text == "一つ目の文。\n3。\n"
    assert [x["status"] for x in log] == ["deleted", "not found", "no answer", "rewritten", "unchanged"]
    assert edits == [7, 7]


def test_number_flags_cite_and_material():
    md = "撮影は 312 枚。速度は 45 MB/s でした。\n\n公式では 1.5 倍とされます（[出典](https://ex.com/a)）。\n\n```sh\necho 999\n```\n"
    flagged, cited = number_flags(md, "312 枚")
    assert [f["number"] for f in flagged] == ["45"] and [c["number"] for c in cited] == ["1.5"]


def test_revise_rewrites_once_and_rechecks(wd):
    p = scripted(draft_text=BAD_DRAFT, present_drop=True)
    _ready(wd, p)
    rep, todo = revise(wd, p, p, None, roles(), VOTES, None, "draft.md", "draft.v2.md")
    assert todo and rep is not None and rep.draft == "draft.v2.md"
    prompt = (wd.root / "draft.v2.prompt.md").read_text(encoding="utf-8")
    assert "# 直す点" in prompt and BAD_DRAFT.strip() in prompt
    assert (wd.root / "draft.v2.md").read_text(encoding="utf-8") == GOOD_DRAFT
    assert (wd.root / "check.v2.json").exists() and (wd.root / "check.json").exists()


def test_revise_does_nothing_without_failures(wd):
    p = scripted()
    _ready(wd, p)
    check(wd, p, None, "draft.md", VOTES)
    rep_path = wd.root / "check.json"
    data = json.loads(rep_path.read_text(encoding="utf-8"))
    data["checks"] = [c for c in data["checks"] if c["passed"] is not False]
    rep_path.write_text(json.dumps(data), encoding="utf-8")
    rep, todo = revise(wd, p, p, None, roles(), VOTES, None, "draft.md", "draft.v2.md")
    assert rep is None and todo == [] and not (wd.root / "draft.v2.md").exists()


def test_server_state_answers_and_design(wd):
    p = scripted()
    mark(wd, p, p)
    interview(wd, p, always_ask())
    with serving(wd) as c:
        code, page = c.get("/")
        assert code == 200 and "インタビュー" in page
        code, st = c.get("/api/state")
        assert st["design"] is None and st["interview"]["questions"][0]["id"] == "q1"
        assert st["units"][0]["firsthand"] is True and st["units"][1]["firsthand"] is False
        code, body = c.put("/api/interview", {"answers": {"q2": "EXIF の無い写真の扱い"}})
        assert code == 200 and body["units"][-1]["id"] == "q2"
        assert wd.interview().questions[1].answer == "EXIF の無い写真の扱い"
        code, _ = c.put("/api/design", {"units": {"m1": "deep"}})
        assert code == 409
        design(wd, p, defaults())

        ops.set_stage(wd, "design")
        assert c.get("/api/state")[1]["conflicts"] == [{"id": "m3", "by": ["m12"], "level": "yes", "note": "コマンドが出る"}]
        code, body = c.put("/api/design", {"units": {"m1": "deep", "m12": "drop"}, "takeaways": ["a", " ", "b"],
                                           "target_length": 800, "avoid": ["価格の比較"]})
        d = wd.design()
        assert code == 200 and d.use_of("m1") == "deep" and d.takeaways == ["a", "b"] and d.target_length == 800
        assert d.avoid == ["価格の比較"] and body["conflicts"] == []
        assert c.get("/api/state")[1]["units"][1]["members"] == ["m9"]
        rules = [{"text": r.text, "on": r.text != wd.design().rules[0].text} for r in wd.design().rules]
        code, body = c.put("/api/design", {"rules": rules + [{"text": "足したルール", "on": True}]})
        st = body["design"]["rules"]
        assert code == 200 and st[0]["on"] is False and st[-1] == {"text": "足したルール", "on": True}
        assert wd.design().rules[0].on is False and len(wd.design().rules) == len(rules) + 1
        assert "書き方のルール" in c.get("/")[1]
        code, _ = c.put("/api/design", {"skip": [{"label": "命名規則", "units": ["m6"]}], "aside": [{"id": "m8", "where": "最初"}]})
        d = wd.design()
        assert code == 200 and d.use_of("m6") == "drop" and d.use_of("m8") == "mention"
        c.put("/api/design", {"units": {"m6": "mention", "m8": "drop"}})
        d = wd.design()
        assert d.skip[0].units == [] and d.aside == [] and d.use_of("m6") == "mention"
        assert next(u for u in d.units if u.id == "m1").why == "手で変更"
        code, _ = c.put("/api/design", {"units": {"m1": "maybe"}})
        assert code == 400
        assert c.get("/api/nope")[0] == 404


def test_full_fake_run_through_the_cli(tmp_path, monkeypatch):
    p = scripted(draft_text=BAD_DRAFT, present_drop=True)
    monkeypatch.setattr(cc, "provider", lambda spec, web=False, **kw: p)
    d = tmp_path / "run"
    r = CliRunner()

    def run(*args):
        res = r.invoke(app, [*args])
        assert res.exit_code == 0, res.output
        return res.output

    out = run("init", str(d), "--topic", PROJECT.topic, "--audience", PROJECT.audience, "--length", "600",
              "-m", str(SAMPLES / "notes.md"), "-m", str(SAMPLES / "rename.sh"))
    assert "units from 2 files" in out
    assert "searchable yes 2" in run("mark", str(d))
    assert "q1." in run("interview", str(d))
    assert r.invoke(app, ["design", str(d)]).exit_code == 1
    run("answer", str(d), "q1", "LINE 経由の写真だけ撮影日時が無かった")
    assert yaml.safe_load((d / "interview.yaml").read_text(encoding="utf-8"))["questions"][0]["source"] == "agent-chat"
    assert '"next": "design"' in run("confirm", str(d))
    assert "deep 3" in run("design", str(d))
    assert "aside: m7" in run("noise", str(d))
    out = run("review", str(d), "--keep-avoid")
    assert "warning: 「コマンドが出る」が出る（原因: m12 / 書かない側: m3）" in out and "写真管理アプリの比較" in out
    run("set", str(d), "unit", "m1", "--use", "deep")
    run("confirm", str(d), "--note", "短めに")
    assert r.invoke(app, ["set", str(d), "unit", "m1", "--use", "drop"]).exit_code == 1
    run("draft", str(d))
    assert "2 findings" in run("research", str(d)) and (d / "research.json").exists()
    run("draft", str(d), "--drop-list", "full", "--out", "draft.A.md")
    assert "## 書かない事柄" in (d / "draft.A.prompt.md").read_text(encoding="utf-8")
    assert "## 書かない話題" in (d / "draft.prompt.md").read_text(encoding="utf-8")
    out = run("check", str(d), "--meta-detector", "rules")
    assert "NG  drop_absent" in out and "status: implied" in out and "NG  表面 meta" in out
    run("check", str(d), "--meta-detector", "rules", "--draft", "draft.A.md")
    assert (d / "check.A.json").exists()
    out = run("revise", str(d), "--meta-detector", "rules")
    assert "draft.v2.md" in out and (d / "check.v2.txt").exists()
    out = run("polish", str(d))
    assert "--yes" in out and "calls:" in out and not (d / "draft.polished.md").exists()
    run("check", str(d), "--meta-detector", "rules", "--surface-only")
    assert (d / "check.surface.json").exists()
    out = run("polish", str(d), "--yes", "--rules", "meta,glue", "--out", "draft.p2.md")
    assert (d / "draft.p2.md").exists() and "dash" not in out and "round 1:" in out
    assert r.invoke(app, ["polish", str(d), "--runs", "1"]).exit_code != 0
    assert r.invoke(app, ["polish", str(d), "--rules", "bold"]).exit_code != 0
    res = r.invoke(app, ["mark", str(tmp_path / "missing")])
    assert res.exit_code != 0
    assert r.invoke(app, ["confirm", str(d)]).exit_code == 1
    assert '"next": "review"' in run("confirm", str(d), "--agent")
    out = run("show", str(d))
    assert "最終チェック draft.md" in out
    item = json.loads(run("show", str(d), "--json"))["review"]["items"][0]["id"]
    run("decide", str(d), item, "delete")
    h = json.loads(run("confirm", str(d)))
    assert h["stage"] == "review" and h["final"].endswith("draft.final.md") and h["article"]["register"] == "keitai"
    assert (d / "draft.final.md").exists() and "完了" in run("show", str(d))
    shutil.rmtree(d)


def test_link_checks_refuse_private_addresses_and_are_capped(monkeypatch):
    for target in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "::ffff:127.0.0.1"):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, t=target, **k: [
            (socket.AF_INET6 if ":" in t else socket.AF_INET, socket.SOCK_STREAM, 6, "", (t, 80))])
        with pytest.raises(factcheck.RefusedAddress):
            factcheck._public_socket("example.com", 80, 1)
    st = factcheck.check_url("http://example.com/")
    assert st.verdict == "unreachable" and "公開されていないアドレス" in st.error
    assert factcheck.check_url("file:///etc/passwd").verdict == "unreachable"
    req = urllib.request.Request("https://example.com/")
    with pytest.raises(factcheck.RefusedAddress):
        factcheck._WebOnlyRedirects().redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd")
    monkeypatch.setattr(factcheck, "MAX_URLS", 2)

    def slow(url, method, timeout):
        if "slow" in url:
            time.sleep(1)
        return 200

    got = factcheck.check_urls(["https://a.example/", "https://slow.example/", "https://c.example/"], slow, total=0.3)
    assert [s.verdict for s in got] == ["ok", "unchecked", "unchecked"]


def test_research_gets_no_material_and_the_writer_gets_no_tools(wd, tmp_path, monkeypatch):
    p = scripted()
    answered(wd, p)
    ops.set_stage(wd, "design")
    d = design(wd, p, defaults())
    assert d.research == ["exiftool の -d の書式"]
    ops.set_stage(wd, "drafting")
    res = research(wd, p, d)
    prompt = p.calls[-1]["prompt"]
    assert "exiftool の -d の書式" in prompt and d.purpose in prompt and d.takeaways[0] in prompt
    for u in wd.units():
        assert u.text[:15] not in prompt
    assert [f.source for f in res.findings] == ["https://exiftool.org/filename.html", "https://example.com/line"]
    assert res.findings[1].topic == 0 and load_research(wd) == res
    text = draft_prompt(wd.project(), d, wd.units(), research=res)
    assert "exiftool の -d は strftime の書式を受け取る。（出典: https://exiftool.org/filename.html）" in text
    seen = []
    monkeypatch.setattr(cc, "provider", lambda spec, web=False, **kw: seen.append((spec, web)) or p)
    runner = CliRunner()
    res = runner.invoke(app, ["draft", str(wd.root), "--new-research"])
    assert res.exit_code == 0, res.output
    assert seen == [("claude-cli:sonnet", True), ("claude-cli:opus", False)] and "材料と回答は送りません" in res.output
    seen.clear()
    assert runner.invoke(app, ["draft", str(wd.root), "--out", "draft.B.md"]).exit_code == 0
    assert seen == [("claude-cli:opus", False)]
    assert runner.invoke(app, ["draft", str(wd.root), "--out", "../x.md"]).exit_code == 1


def test_cli_providers_are_isolated(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["cwd"]))
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "ok"}), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda x: "/bin/" + x)
    assert ClaudeCliProvider("sonnet").complete("hi") == "ok"
    cmd, cwd = calls[-1]
    assert cmd[cmd.index("--tools") + 1] == "" and "--allowedTools" not in cmd
    assert {"--safe-mode", "--strict-mcp-config", "--no-session-persistence"} <= set(cmd)
    assert cmd[cmd.index("--setting-sources") + 1] == "" and cwd.startswith(tempfile.gettempdir()) and not Path(cwd).exists()
    ClaudeCliProvider("sonnet", allowed_tools=("WebSearch", "WebFetch")).complete("hi")
    assert calls[-1][0][calls[-1][0].index("--allowedTools") + 1] == "WebSearch,WebFetch"
    CodexCliProvider().complete("hi", json_schema={"type": "object"})
    cmd, cwd = calls[-1]
    assert {"--ignore-user-config", "--ignore-rules", "--ephemeral"} <= set(cmd) and cmd[cmd.index("-C") + 1] == cwd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only" and 'web_search="disabled"' in cmd
    assert {"shell_tool", "browser_use", "computer_use", "apps", "plugins"} <= {cmd[i + 1] for i, x in enumerate(cmd)
                                                                                 if x == "--disable"}
    with pytest.raises(ValueError):
        get_provider("claude-cli:opus --dangerously-skip-permissions")
    with pytest.raises(ValueError):
        get_provider("codex-cli:gpt;rm")
    assert get_provider("claude-cli:claude-opus-4-1[1m]").model == "claude-opus-4-1[1m]"


def test_private_files_and_dirs(tmp_path, monkeypatch):
    def mode(p):
        return stat.S_IMODE(os.stat(p).st_mode)

    monkeypatch.setattr(os, "umask", os.umask)
    old = os.umask(0o022)
    try:
        w, _ = init_workdir(tmp_path / "w", PROJECT, [SAMPLES / "notes.md"])
        ops.record(w, "agent-chat", "x")
        cached = CachedProvider(FakeProvider(), tmp_path / "cache")
        cached.complete("p")
        files = list((tmp_path / "cache").rglob("*.json"))
        assert mode(w.root) == 0o700 and mode(w.material_dir) == 0o700 and mode(w.material_dir / "notes.md") == 0o600
        assert mode(w.root / "history.jsonl") == 0o600 and mode(w.project_file) == 0o600
        assert mode(tmp_path / "cache") == 0o700 and mode(files[0].parent) == 0o700 and mode(files[0]) == 0o600
    finally:
        os.umask(old)
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(StepError):
        CachedProvider(FakeProvider(), shared).complete("p")


def test_workdir_root_is_private_and_names_are_checked(tmp_path, monkeypatch):
    root = Path(os.environ["HOME"]) / ".cache" / "kumimasu" / "work"
    runner = CliRunner()
    res = runner.invoke(app, ["init", str(root / "a"), "--topic", "t", "--audience", "a", "-m", str(SAMPLES / "notes.md")])
    assert res.exit_code == 0, res.output
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    root.chmod(0o777)
    res = runner.invoke(app, ["init", str(root / "b"), "--topic", "t", "--audience", "a"])
    assert res.exit_code == 1 and "ほかのユーザーも書き込める" in res.output
    root.chmod(0o700)
    w = WorkDir(root / "a")

    ops.set_stage(w, "drafting")
    for args in (["check", str(w.root), "--draft", "../../etc/passwd"], ["polish", str(w.root), "--out", "x.md"],
                 ["export", str(w.root), "--draft", "/etc/passwd"], ["confirm", str(w.root), "--agent", "--draft", "../a.md"]):
        res = runner.invoke(app, args)
        assert res.exit_code == 1 and "下書きのファイル名" in res.output, (args, res.output)
    p = yaml.safe_load(w.project_file.read_text(encoding="utf-8"))
    p["review_draft"] = "../secret.md"
    w.project_file.write_text(yaml.safe_dump(p), encoding="utf-8")
    with pytest.raises(StepError, match="review_draft"):
        w.review_draft()


def test_export_never_follows_a_symlink(tmp_path):
    w, _ = init_workdir(tmp_path / "w", PROJECT, [])
    w.write("draft.final.md", "final\n")
    out = tmp_path / "out"
    out.mkdir()
    target = tmp_path / "victim.txt"
    (out / "w-draft-20261005-1407.md").symlink_to(target)
    dest = export_final(w, "draft.md", out, datetime(2026, 10, 5, 14, 7, tzinfo=UTC))
    assert dest.name == "w-draft-20261005-1407-2.md" and not target.exists()


@pytest.mark.parametrize(("text", "want"), [
    ("[m38] の件は、なぜ諦めたのですか", "なぜ諦めたのですか"),
    ("m4 の LINE の写真で何が起きましたか", "LINE の写真で何が起きましたか"),
    ("LINE の写真（m4、m5）で何が起きましたか", "LINE の写真で何が起きましたか"),
    ("単位 m12 では何を試しましたか？ [m38]の件はどうですか", "何を試しましたか？ どうですか"),
    ("M2 Mac の m2 と m99 の話", "M2 Mac の m2 と m99 の話"),
])
def test_strip_unit_refs(text, want):
    assert strip_unit_refs(text, {"m4", "m5", "m12", "m38"}) == want


def test_questions_without_text_after_stripping_are_dropped(wd):
    units = wd.material_units()
    iv = parse_questions({"questions": [{"question": "[m1]", "units": ["m1"]},
                                        {"question": "m1 について。なぜですか", "context": "m1・m2 のメモ", "units": ["m1", "m1", "zz"]}]},
                         units)
    assert [(q.id, q.question, q.context, q.units) for q in iv.questions] == [("q1", "なぜですか", "", ["m1"])]


def test_waiting_follows_stage_and_files(wd):
    p = scripted()
    w = ops.waiting(wd)
    assert w["doing"] == "questions" and [x["state"] for x in w["steps"]] == ["done", "now", "todo"]
    answered(wd, p)
    assert ops.waiting(wd) is None
    ops.confirm(wd, "agent-chat")
    w = ops.waiting(wd)
    assert w["doing"] == "design" and w["title"] == "エージェントが設計を作っています"
    assert w["steps"][-1] == {"label": "設計の確認（あなた）", "state": "todo"}
    design(wd, p, defaults())
    assert ops.waiting(wd) is None
    ops.confirm(wd, "human-ui")
    w = ops.waiting(wd)
    assert w["doing"] == "research" and [x["label"] for x in w["steps"]][1:4] == ["ウェブで調べる", "下書きを書く", "検査する"]
    research(wd, p, wd.design())
    assert ops.waiting(wd)["doing"] == "draft"
    draft(wd, p, roles())
    assert ops.waiting(wd)["doing"] == "check"
    check(wd, p, None, "draft.md", VOTES)
    w = ops.waiting(wd)
    assert w["doing"] == "finish" and [x["state"] for x in w["steps"]] == ["done"] * 4 + ["now", "todo"]
    ops.confirm(wd, "agent")
    assert ops.waiting(wd) is None and ops.stage_info(wd)["waiting"] is None


def test_waiting_skips_research_without_topics(wd):
    p = scripted()
    answered(wd, p)
    ops.confirm(wd, "agent-chat")
    design(wd, p, defaults())
    ops.update_design(wd, {"research": []}, "human-ui")
    ops.confirm(wd, "human-ui")
    w = ops.waiting(wd)
    assert w["doing"] == "draft" and "ウェブで調べる" not in [x["label"] for x in w["steps"]]
    wd.save_design(wd.design().model_copy(update={"research": ["exiftool の書式"]}))
    draft(wd, p, roles())
    assert ops.waiting(wd)["doing"] == "check" and ops.waiting(wd)["steps"][1] == {"label": "ウェブで調べる", "state": "done"}


def test_server_reports_waiting_in_version(wd):
    p = scripted()
    answered(wd, p)
    ops.confirm(wd, "agent-chat")
    with serving(wd) as c:
        code, v = c.get("/api/version")
        assert code == 200 and v["waiting"]["doing"] == "design"
        design(wd, p, defaults())
        code, v2 = c.get("/api/version")
        assert v2["waiting"] is None and v2["version"] != v["version"]


def test_conflict_notes_lose_unit_refs_and_show_lists_causes(wd):
    p = scripted()
    answered(wd, p)
    d = design(wd, p, defaults())
    d = parse_review({"conflicts": [{"id": "m3", "level": "partial", "by": ["m12", "m3"], "note": "m12 のコマンドの書式"}]}, d)
    assert [(c.id, c.by, c.note) for c in d.conflicts] == [("m3", ["m12"], "コマンドの書式")]
    wd.save_design(d)
    ops.set_stage(wd, "design")
    snap = snapshot(wd)
    c = snap["design"]["conflicts"][0]
    assert c["note"] == "コマンドの書式" and [r["id"] for r in c["refs"]] == ["m12"] and c["refs"][0]["text"]
    out = show_text(snap)
    assert "警告: 「コマンドの書式」が一部出る（書かない側: m3）" in out and "    原因: m12「" in out


def test_show_puts_question_first_and_refs_compact(wd):
    p = scripted()
    mark(wd, p, p)
    interview(wd, p, always_ask())
    out = show_text(snapshot(wd))
    lines = out.splitlines()
    i = lines.index("q1. LINE で受け取った写真だけ EXIF が消えていると気づいたとき、何を考えましたか")
    assert lines[i + 1].startswith("    背景: LINE 経由") and lines[i + 2].startswith("    参考: m4「")

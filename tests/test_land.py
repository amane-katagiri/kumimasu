from __future__ import annotations

import json
import re
import shutil
import threading

import pytest
from conftest import (
    GOOD_DRAFT,
    PROJECT,
    SAMPLES,
    VOTES,
    always_ask,
    defaults,
    roles,
    run_cli,
    scripted,
    serving,
)

from kumimasu import cli_common as cc
from kumimasu import ops
from kumimasu.check import check, map_prompt, surface_checks
from kumimasu.design import design, land_workdir
from kumimasu.draft import draft, draft_prompt, read_used
from kumimasu.infounits import info_units
from kumimasu.interview import interview
from kumimasu.land import (
    NOTE_QUESTION,
    bare_names,
    note_items,
    note_question,
    note_units,
    spread_pick,
    with_answer,
)
from kumimasu.llm import FakeProvider
from kumimasu.mark import mark
from kumimasu.polish import polish
from kumimasu.review import load_review
from kumimasu.show import snapshot
from kumimasu.show import text as show_text
from kumimasu.workdir import WorkDir, init_workdir


def _section(prompt: str, head: str) -> str:
    return prompt.split(f"## {head}", 1)[1].split("\n## ", 1)[0]


@pytest.fixture
def wd(tmp_path) -> WorkDir:
    w, _ = init_workdir(tmp_path / "w", PROJECT, [SAMPLES / "notes.md", SAMPLES / "rename.sh"])
    p = scripted()
    mark(w, p, p)
    interview(w, p, always_ask())
    iv = w.interview()
    iv.questions[0].answer = "撮影日が無い写真が LINE 経由だけだったのが意外だった"
    w.save_interview(iv)
    ops.confirm(w, "agent-chat")
    design(w, p, p, defaults())
    return w


def test_design_marks_results_and_mentions_bare(wd):
    d = wd.design()
    land = {u.id: (u.use, u.land, u.label) for u in d.units}
    assert land["m4"] == ("deep", "bare", "LINE の写真に撮影日時が無い")
    assert land["q1"] == ("deep", "bare", "意外だった")
    assert land["m5"] == ("deep", None, "")
    assert land["m2"][1] is None
    assert land["m7"] == ("mention", None, "")
    assert all(u.land == "bare" for u in d.units if u.use == "mention" and u.id != "m7")
    assert [i["id"] for i in note_items(d, wd.units())] == ["m4", "q1"]


def test_draft_prompt_names_bare_units_and_adds_the_one_word(wd):
    ops.update_design(wd, {"notes": {"q1": "正直ちょっと拍子抜けした"}}, "agent-chat")
    d = wd.design()
    assert d.use_of("q1") == "deep" and next(u for u in d.units if u.id == "q1").land == "author"
    prompt = draft_prompt(wd.project(), d, wd.units())
    land = _section(prompt, "結果の着地")
    bare_part, author_part = land.split("著者の一言を添えて")
    assert "- [m4] LINE の写真に撮影日時が無い" in bare_part and "- 「触れる材料」のすべて" in bare_part
    assert "足さないことを断ったりもしません" in bare_part and "[q1]" not in bare_part
    assert "- [q1] 意外だった" in author_part and "整えすぎません" in author_part
    assert "地の文の文体で言い切ります" in author_part and "「〜と思いました」" in author_part
    assert "必要な文数で書き出します" in author_part and "短い 1 文" not in author_part
    assert "一言に書かれていることは、著者自身の材料です" in author_part
    assert "1 文のまま書き、文を割って説明調にしません" in author_part
    assert "一言だけで段落を作るのは、記事全体で多くても 1 回までにします" in author_part
    assert "材料に無い文で段落を埋めません" in author_part and "段落の頭・途中・末尾" not in author_part
    assert "著者の一言: 正直ちょっと拍子抜けした" in _section(prompt, "掘り下げる材料")
    assert "[m7]" not in land


def test_mention_units_are_listed_when_not_all_bare(wd):
    ops.update_design(wd, {"land": {"m6": "author"}}, "agent-chat")
    d = wd.design()
    names = bare_names(d, wd.units())
    assert "「触れる材料」のすべて" not in names and not any(n.startswith("[m6]") for n in names)
    assert any(n.startswith("[m9]") for n in names) == (d.use_of("m9") == "mention")


def test_land_edits_are_checked(wd):
    with pytest.raises(ValueError, match="bare にできません"):
        ops.update_design(wd, {"notes": {"m4": "これは悔しかった"}, "land": {"m4": "bare"}}, "agent-chat")
    with pytest.raises(ValueError, match="land は"):
        ops.update_design(wd, {"land": {"m4": "loud"}}, "agent-chat")
    with pytest.raises(ValueError, match="知らない単位"):
        ops.update_design(wd, {"notes": {"m99": "x"}}, "agent-chat")
    with pytest.raises(ValueError, match="書かない単位"):
        ops.update_design(wd, {"notes": {"m2": "x"}}, "agent-chat")
    mention = [u.id for u in wd.design().units if u.use in ("deep", "mention")][:6]
    with pytest.raises(ValueError, match="5 個まで"):
        ops.update_design(wd, {"notes": {i: "一言" for i in mention}}, "agent-chat")
    ops.update_design(wd, {"notes": {"m4": "これは悔しかった"}}, "agent-chat")
    ops.update_design(wd, {"notes": {"m4": ""}}, "agent-chat")
    u = next(u for u in wd.design().units if u.id == "m4")
    assert (u.land, u.note) == ("bare", "")


def test_unit_becoming_mention_gets_bare_and_unchanged_notes_keep_land(wd):
    ops.update_design(wd, {"units": {"m2": "mention"}}, "agent-chat")
    assert next(u for u in wd.design().units if u.id == "m2").land == "bare"
    ops.update_design(wd, {"land": {"m4": "author"}}, "agent-chat")
    ops.update_design(wd, {"notes": {"m4": "", "q1": "意外"}}, "agent-chat")
    by = {u.id: u for u in wd.design().units}
    assert by["m4"].land == "author" and by["q1"].land == "author"


def test_land_command_keeps_notes_and_counts_one_call(wd, monkeypatch):
    ops.update_design(wd, {"notes": {"q1": "意外"}}, "agent-chat")
    p = scripted()
    d = land_workdir(wd, p)
    assert len(p.calls) == 1 and "掘り下げると決めた" in p.calls[0]["prompt"]
    by = {u.id: u for u in d.units}
    assert (by["q1"].land, by["q1"].note) == ("author", "意外") and by["m4"].land == "bare"
    monkeypatch.setattr(cc, "provider", lambda spec, web=False, **kw: p)
    out = run_cli("land", wd.root)
    assert NOTE_QUESTION in out and "q1 [author] 意外だった  一言: 意外" in out


def test_note_units_are_firsthand_material_for_the_check(wd):
    ops.update_design(wd, {"notes": {"m4": "LINE は写真を加工しているらしい"}}, "agent-chat")
    units = wd.units()
    notes = note_units(wd.design(), units)
    assert [(u.id, u.origin, u.firsthand) for u in notes] == [("m4n", "answer", True)]
    prompt = map_prompt(info_units("# 題\n\n本文です。\n"), [*units, *notes], [], [])
    assert "[m4n]" in prompt and "LINE は写真を加工しているらしい" in prompt
    assert "結果だけの材料" not in prompt and "一言のある材料" not in prompt


def test_show_page_and_cli_parity(tmp_path, wd):
    snap = snapshot(wd)
    assert [i["id"] for i in snap["design"]["notes"]] == ["m4", "q1"]
    text = show_text(snap)
    assert f"[一言] {note_question(5)}（いま 0/5" in text and "m4 [bare] LINE の写真に撮影日時が無い" in text
    assert "結果だけ（bare）: [m4] LINE の写真に撮影日時が無い / [q1] 意外だった / 「触れる材料」のすべて" in text
    other = WorkDir(tmp_path / "b")
    shutil.copytree(wd.root, other.root)
    with serving(wd) as c:
        _, state = c.get("/api/state")
        assert state["labels"]["note_question"] == NOTE_QUESTION
        c.put("/api/design", {"notes": {"m4": "LINE は写真を加工しているらしい"}})
        c.put("/api/design", {"land": {"m6": "author"}})
    run_cli("set", other.root, "note", "m4", "LINE は写真を加工しているらしい")
    run_cli("set", other.root, "land", "m6", "author")
    assert wd.design_file.read_text(encoding="utf-8") == other.design_file.read_text(encoding="utf-8")
    assert json.dumps(snapshot(other)["design"]["notes"], ensure_ascii=False).count("加工") == 1


def test_note_limit_is_a_design_setting(wd):
    deep_and_mention = [u.id for u in wd.design().units if u.use in ("deep", "mention")][:6]
    with pytest.raises(ValueError, match="5 個まで"):
        ops.update_design(wd, {"notes": {i: "一言" for i in deep_and_mention}}, "agent-chat")
    run_cli("set", wd.root, "note-limit", "6")
    ops.update_design(wd, {"notes": {i: "一言" for i in deep_and_mention}}, "agent-chat")
    assert sum(bool(u.note) for u in wd.design().units) == 6
    assert note_question(6) in show_text(snapshot(wd)) and "（いま 6/6" in show_text(snapshot(wd))
    with pytest.raises(ValueError, match="1 以上"):
        ops.update_design(wd, {"note_limit": 0}, "agent-chat")


def test_one_word_on_a_unit_that_is_not_a_result(wd):
    assert next(u for u in wd.design().units if u.id == "m5").land is None
    ops.update_design(wd, {"notes": {"m5": "地味に効いた"}}, "agent-chat")
    d = wd.design()
    m5 = next(u for u in d.units if u.id == "m5")
    assert (m5.land, m5.note) == ("author", "地味に効いた")
    assert "m5" in [i["id"] for i in note_items(d, wd.units())]
    assert not any(n.startswith("[m5]") for n in bare_names(d, wd.units()))
    prompt = draft_prompt(wd.project(), d, wd.units())
    author_part = _section(prompt, "結果の着地").split("著者の一言を添えて")[1]
    assert "- [m5]" in author_part and "著者の一言: 地味に効いた" in _section(prompt, "掘り下げる材料")
    assert [u.id for u in note_units(d, wd.units())] == ["m5n"]
    p = scripted()
    assert land_workdir(wd, p).units[[u.id for u in d.units].index("m5")].land == "author"


NOTE_SENTENCE = "それでも並び順はだいたい保たれるので、私はこれで十分だと判断しました。"


def _guarding_provider(base):
    def respond(prompt: str) -> str:
        if "情報を運ばない文と見出しを選び" in prompt:
            ids = [m[1] for m in re.finditer(r"^\[(M\d+)\] (.*)$", prompt, re.MULTILINE) if "十分だと判断" in m[2]]
            return json.dumps({"items": [{"id": i, "category": "wrapup"} for i in ids]})
        if "# 下書きの単位" in prompt:
            data = json.loads(base.responses(prompt))
            hit = [u.id for u in info_units(GOOD_DRAFT) if "十分だと判断" in u.text]
            for row in data["units"]:
                if row["id"] in hit:
                    row["from"] = ["m4n"]
            return json.dumps(data)
        if "一覧の文だけを直して" in prompt:
            ids = re.findall(r"^\[(F\d+)\]", prompt, re.MULTILINE)
            return json.dumps({"items": [{"id": i, "replacement": ""} for i in ids]})
        return base.responses(prompt)
    return FakeProvider(respond)


def test_sentences_from_one_words_are_not_flagged_or_deleted(wd):
    assert NOTE_SENTENCE in GOOD_DRAFT
    ops.update_design(wd, {"notes": {"m4": "これで十分"}}, "agent-chat")
    ops.confirm(wd, "agent-chat")
    p = _guarding_provider(scripted())
    draft(wd, p, roles())
    unguarded = surface_checks(GOOD_DRAFT, wd.units(), p, VOTES, set())
    assert any(NOTE_SENTENCE in i["text"] for c in unguarded for i in c.items)
    rep = check(wd, p, p, "draft.md", VOTES)
    assert not any(NOTE_SENTENCE in str(i.get("text", "")) for c in rep.checks if c.surface for i in c.items)
    assert "著者の一言から来た文 1 は数えない" in next(c for c in rep.checks if c.id == "flow").detail
    assert not any(NOTE_SENTENCE in i.text for i in load_review(wd, "draft.md").items)
    res = polish(wd, p, "draft.md", ("flow",), True, VOTES, 2, "draft.polished.md")
    assert res.rounds[0].hits == 0 and NOTE_SENTENCE in wd.read("draft.polished.md")
    surface_only = check(wd, p, p, "draft.md", VOTES, surface_only=True)
    assert not any(NOTE_SENTENCE in str(i.get("text", "")) for c in surface_only.checks for i in c.items)


def test_draft_and_check_after_cleanup(wd):
    ops.update_design(wd, {"notes": {"q1": "意外"}}, "agent-chat")
    ops.confirm(wd, "agent-chat")
    p = scripted()
    draft(wd, p, roles())
    assert set(read_used(wd, "draft.md")["land"]) == {"bare", "author"}
    ids = {c.id for c in check(wd, p, None, "draft.md", VOTES).checks}
    assert "land_bare" not in ids and "note_hold" not in ids
    assert not any("（判定" not in x["prompt"] and "# 結果だけの材料" in x["prompt"] for x in p.calls)


def test_spread_pick_takes_front_and_back_in_turn():
    order = [f"m{i}" for i in range(1, 11)]
    assert spread_pick(["m2", "m3", "m8", "m9"], order, 2) == ["m2", "m8"]
    assert spread_pick(["m2", "m3"], order, 2) == ["m2", "m3"]
    assert spread_pick(["m9"], order, 2) == ["m9"]
    assert with_answer("ハックっぽい工作で面白い", "既存製品の口を見つける") == "ハックっぽい工作で面白い。既存製品の口を見つける"
    assert with_answer("ちゃんとしてくれ～", "落ちすぎ") == "ちゃんとしてくれ～落ちすぎ"


def _followup_design(wd):
    deep = [u.id for u in wd.design().units if u.use in ("deep", "mention")]
    front, back = deep[0], deep[-1]
    notes = {front: "工作で面白い", "m4": "ちゃんとしてくれ～", back: "地味に面白い", "q1": "これも面白い"}
    ops.update_design(wd, {"notes": notes}, "agent-chat")
    return front, back


def test_followup_asks_thin_one_words_up_to_the_limit(wd):
    front, back = _followup_design(wd)
    p = scripted()
    d = ops.followup(wd, p, "agent")
    calls = [c for c in p.calls if "付けた一言です" in c["prompt"]]
    assert len(calls) == 1 and "[m4] 材料:" in calls[0]["prompt"]
    by = {u.id: u for u in d.units}
    asked = [u.id for u in d.units if u.followup_state == "asked"]
    assert len(asked) == 2 and front in asked and (back in asked or "q1" in asked)
    assert by["m4"].followup_state == "none" and by["m4"].followup == ""
    assert all(by[i].followup.endswith("どのへんが？") and "[" not in by[i].followup for i in asked)
    assert ops.followup(wd, p, "agent") and len([c for c in p.calls if "付けた一言です" in c["prompt"]]) == 1
    text = show_text(snapshot(wd))
    assert "聞き返し（答え待ち）:" in text and "set DIR followup ID" in text
    run_cli("set", wd.root, "followup", asked[0], "既存製品の小さな口を見つけるところ")
    run_cli("set", wd.root, "followup", asked[1], "")
    by = {u.id: u for u in wd.design().units}
    assert by[asked[0]].note.endswith("。既存製品の小さな口を見つけるところ") and by[asked[0]].followup_state == "answered"
    assert by[asked[1]].followup_state == "skipped" and by[asked[1]].note in ("地味に面白い", "これも面白い")
    assert "聞き返し（答えた）" in show_text(snapshot(wd)) and "聞き返し（飛ばした）" in show_text(snapshot(wd))
    prompt = draft_prompt(wd.project(), wd.design(), wd.units())
    assert "既存製品の小さな口を見つけるところ" in _section(prompt, "掘り下げる材料") + _section(prompt, "触れる材料")
    with pytest.raises(ValueError, match="聞き返していない"):
        ops.update_design(wd, {"followups": {asked[0]: "もう一度"}}, "agent-chat")
    assert ops.followup(wd, p, "agent") and len([c for c in p.calls if "付けた一言です" in c["prompt"]]) == 1


def test_rewriting_a_one_word_clears_its_followup(wd):
    front, _ = _followup_design(wd)
    ops.followup(wd, scripted(), "agent")
    assert next(u for u in wd.design().units if u.id == front).followup_state == "asked"
    ops.update_design(wd, {"notes": {front: "工作で面白い。口を見つけるのが好き"}}, "agent-chat")
    u = next(u for u in wd.design().units if u.id == front)
    assert (u.followup, u.followup_state) == ("", None)
    run_cli("set", wd.root, "followup-limit", "0")
    p = scripted()
    ops.followup(wd, p, "agent")
    assert not p.calls


def test_followup_page_asks_outside_the_lock(wd):
    _followup_design(wd)
    started, go = threading.Event(), threading.Event()
    inner = scripted()

    def slow(prompt: str) -> str:
        started.set()
        go.wait(5)
        return inner.complete(prompt)

    p = FakeProvider(slow)
    with serving(wd, judge=lambda: p) as c:
        out = {}
        t = threading.Thread(target=lambda: out.update(r=c.post("/api/followup")))
        t.start()
        assert started.wait(5)
        assert c.put("/api/design", {"purpose": "途中で直した"})[0] == 200
        go.set()
        t.join(5)
        assert out["r"][0] == 409 and out["r"][1]["stale"] is True
        assert wd.design().purpose == "途中で直した" and all(u.followup_state is None for u in wd.design().units)
        code, state = c.post("/api/followup")
        assert code == 200 and any(u["followup_state"] == "asked" for u in state["design"]["units"])
        assert len(p.calls) == 2


def test_followup_page_and_stage(tmp_path, wd):
    _followup_design(wd)
    p = scripted()
    with serving(wd, judge=lambda: p) as c:
        status, state = c.post("/api/followup")
        assert status == 200
        asked = [u for u in state["design"]["units"] if u["followup_state"] == "asked"]
        assert len(asked) == 2
        status, _ = c.put("/api/design", {"followups": {asked[0]["id"]: "口を見つけるところ"}})
        assert status == 200
    assert next(u for u in wd.design().units if u.id == asked[0]["id"]).followup_state == "answered"
    with serving(wd) as c:
        assert c.post("/api/followup")[0] == 409
    ops.confirm(wd, "human-ui")
    with pytest.raises(ops.StageError):
        ops.followup(wd, p, "agent")


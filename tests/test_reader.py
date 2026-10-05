from __future__ import annotations

import json

import pytest
from conftest import (
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

from kumimasu import ops, terms
from kumimasu.check import check
from kumimasu.design import design, review_conflicts
from kumimasu.draft import draft, draft_prompt
from kumimasu.interview import interview
from kumimasu.llm import FakeProvider
from kumimasu.mark import mark
from kumimasu.model import Aside, Design, Term, Unit, UnitUse
from kumimasu.review import load_review
from kumimasu.revise import instructions, revise
from kumimasu.show import snapshot
from kumimasu.show import text as show_text
from kumimasu.terms import find_terms, material_load, term_states
from kumimasu.workdir import WorkDir, init_workdir

TERMS = [
    {"term": "exiftool の -d", "used_by": ["m12", "zz"], "defined_by": ["m3"], "reader_knows": False, "why": "書式の指定"},
    {"term": "EXIF", "used_by": ["m4"], "defined_by": ["m2"], "reader_knows": True, "why": ""},
    {"term": "更新日時で代用", "used_by": ["m5"], "defined_by": ["zz"], "reader_knows": False, "why": "何のずれか"},
    {"term": "%-c", "used_by": ["m6"], "defined_by": ["m6"], "reader_knows": False, "why": "書式"},
]
READER = [
    {"kind": "term", "quote": "更新日時で代用しました", "why": "更新日時が何か分からない", "fix": "保存した日時だと書く", "unit": "m5"},
    {"kind": "number", "quote": "312 枚のうち 9 枚", "why": "何の割合か分からない", "fix": "撮影日時の無い枚数だと書く", "unit": "zz"},
    {"kind": "jump", "quote": "本文に無い文", "why": "前提が無い", "fix": "前提を足す", "unit": ""},
    {"kind": "other", "quote": "x", "why": "", "fix": "", "unit": ""},
]


def designed(root, p) -> WorkDir:
    w, _ = init_workdir(root, PROJECT, [SAMPLES / "notes.md", SAMPLES / "rename.sh"])
    mark(w, p, p)
    interview(w, p, always_ask())
    ops.confirm(w, "agent-chat")
    design(w, p, p, defaults())
    return w


def states(w: WorkDir) -> dict[str, dict]:
    return {s["term"]: s for s in term_states(w.design())}


def test_first_proposal_promotes_the_dropped_definition(tmp_path):
    w = designed(tmp_path / "w", scripted(terms=TERMS))
    d = w.design()
    m3 = next(u for u in d.units if u.id == "m3")
    assert m3.use == "mention" and m3.promoted_for == "exiftool の -d" and "用語" in m3.why
    assert [t.term for t in d.terms] == ["exiftool の -d", "更新日時で代用", "%-c"]
    assert d.terms[0].used_by == ["m12"] and d.terms[1].defined_by == []
    st = states(w)
    assert st["exiftool の -d"]["status"] == "kept" and st["exiftool の -d"]["promoted"] == ["m3"]
    assert st["更新日時で代用"]["status"] == "missing" and st["%-c"]["status"] == "kept"
    assert all(c.id != "m3" for c in d.live_conflicts())
    snap = snapshot(w)
    assert snap["design"]["promoted"] == [{"id": "m3", "term": "exiftool の -d"}]
    assert "[読者が知らない用語]" in show_text(snap) and "書き手が初出で説明する" in show_text(snap)


def test_after_edits_terms_only_warn(tmp_path):
    p = scripted(terms=TERMS)
    w = designed(tmp_path / "w", p)
    ops.update_design(w, {"units": {"m3": "drop"}}, "human-ui")
    d = w.design()
    assert d.use_of("m3") == "drop" and next(u for u in d.units if u.id == "m3").promoted_for == ""
    st = states(w)["exiftool の -d"]
    assert st["status"] == "dropped" and st["candidates"] == ["m3"]
    assert "警告: 用語: exiftool の -d" in show_text(snapshot(w))
    review_conflicts(w, p, p)
    assert w.design().use_of("m3") == "drop" and states(w)["exiftool の -d"]["status"] == "dropped"
    with pytest.raises(ValueError):
        ops.update_design(w, {"explain": "%-c"}, "human-ui")
    with pytest.raises(ValueError):
        ops.update_design(w, {"explain": "知らない"}, "human-ui")
    run_cli("set", w.root, "explain", "exiftool の -d")
    m3 = next(u for u in w.design().units if u.id == "m3")
    assert m3.use == "mention" and m3.promoted_for == "exiftool の -d"
    ops.update_design(w, {"units": {"m12": "drop"}}, "human-ui")
    assert "exiftool の -d" not in states(w)


def test_missing_definition_reaches_the_draft_prompt(tmp_path):
    p = scripted(terms=TERMS)
    w = designed(tmp_path / "w", p)
    prompt = draft_prompt(w.project(), w.design(), w.units())
    section = prompt.split("## 初出で説明する用語", 1)[1].split("\n## ", 1)[0]
    assert "更新日時で代用（材料に説明が無い" in section and "exiftool の -d（説明の材料: m3）" in section
    assert "EXIF" not in section
    ops.update_design(w, {"units": {"m3": "drop"}}, "human-ui")
    section = draft_prompt(w.project(), w.design(), w.units()).split("## 初出で説明する用語", 1)[1]
    assert "exiftool の -d（材料に説明が無い" in section


def test_terms_are_batched_and_merged(monkeypatch):
    monkeypatch.setattr(terms, "TERMS_BATCH_CHARS", 10)
    monkeypatch.setattr(terms, "TERMS_MIN_SLICE", 100)
    units = [Unit(id="k1", text="独自性スコアが 0.6 だった"), Unit(id="d1", text="独自性スコアとは" + "a" * 80),
             Unit(id="d2", text="独自性スコアの求め方" + "b" * 80)]
    d = Design(units=[UnitUse(id="k1", use="deep"), UnitUse(id="d1", use="drop"), UnitUse(id="d2", use="drop")])
    seen = []

    def respond(prompt: str) -> str:
        seen.append(prompt)
        name, unit = ("独自性スコア", "d1") if len(seen) == 1 else ("独自性 スコア", "d2")
        return json.dumps({"terms": [{"term": name, "used_by": ["k1"], "defined_by": [unit, "k9"], "reader_knows": False,
                                      "why": "作った指標"}]})

    found = find_terms(d, PROJECT, units, FakeProvider(respond))
    assert len(seen) == 2 and "すでに挙がった言葉: 独自性スコア" in seen[1] and "[d1]" not in seen[1]
    assert [(t.term, t.defined_by) for t in found] == [("独自性スコア", ["d1", "d2"])]


def _units(n_mention: int) -> list[Unit]:
    out = [Unit(id="k", text="d" * 500)]
    out += [Unit(id="y", text="y" * 300, searchable="yes"), Unit(id="n", text="n" * 400, searchable="no"),
            Unit(id="p", text="p" * 200, searchable="partial"), Unit(id="a", text="a" * 100, searchable="yes"),
            Unit(id="t", text="t" * 100, searchable="yes")]
    return out[:1 + n_mention]


def test_overload_ratio_and_suggestions():
    units = _units(5)
    d = Design(target_length=500, units=[UnitUse(id="k", use="deep")]
               + [UnitUse(id=u.id, use="mention", promoted_for="x" if u.id == "t" else "") for u in units[1:]],
               aside=[Aside(id="a")], terms=[Term(term="x", used_by=["k"], defined_by=["t"])])
    load = material_load(d, units)
    assert load["kept_chars"] == 1600 and load["ratio"] == 3.2 and load["mention"] == 5 and load["mention_max"] == 3
    assert load["over"] and len(load["reasons"]) == 2 and load["suggest_length"] == 800
    assert [s["id"] for s in load["suggestions"]] == ["y", "p", "n"]
    assert load["suggestions"][0]["reason"] == "検索で届く"
    assert "## 材料の量" in draft_prompt(PROJECT, d, units)
    calm = d.model_copy(update={"target_length": 2000})
    assert not material_load(calm, units)["over"] and material_load(calm, units)["suggestions"] == []
    assert "## 材料の量" not in draft_prompt(PROJECT, calm, units)
    lenient = d.model_copy(update={"max_material_ratio": 4.0, "chars_per_mention": 100})
    assert not material_load(lenient, units)["over"]


def test_overload_and_terms_in_the_page_and_show(tmp_path):
    w = designed(tmp_path / "w", scripted(terms=TERMS))
    ops.update_design(w, {"units": {"m3": "drop"}}, "human-ui")
    with serving(w) as c:
        code, state = c.get("/api/state")
        assert code == 200 and state["load"]["over"] and state["load"]["suggestions"]
        assert {t["term"]: t["status"] for t in state["terms"]}["exiftool の -d"] == "dropped"
        code, state = c.put("/api/design", {"explain": "exiftool の -d"})
        assert code == 200 and {t["term"]: t["status"] for t in state["terms"]}["exiftool の -d"] == "kept"
        code, body = c.put("/api/design", {"explain": "%-c"})
        assert code == 400 and "書かないにした説明" in body["error"]
    text = show_text(snapshot(w))
    assert "使う材料:" in text and "警告: 触れる材料が" in text and "減らす候補" in text


def test_reader_check_feeds_revise_and_the_final_check(tmp_path):
    p = scripted(terms=TERMS, reader=READER)
    w = designed(tmp_path / "w", p)
    draft(w, p, roles())
    rep = check(w, p, None, "draft.md", VOTES)
    c = next(x for x in rep.checks if x.id == "reader")
    assert c.passed is False and not c.surface and c.value == 3 and c in rep.structural_failed()
    assert [i["kind"] for i in c.items] == ["term", "number", "jump"]
    assert c.items[0]["unit"] == "m5" and c.items[1]["unit"] == "" and c.items[2]["placed"] is False
    assert "reader" in (w.root / "check.txt").read_text(encoding="utf-8")
    assert any("[m3]" in q["prompt"] and "[m2]" not in q["prompt"] for q in p.calls if "この読者になりきって" in q["prompt"])
    todo = instructions(rep, 600)
    reader = next(t for t in todo if "読者には分かりません" in t)
    assert "「更新日時で代用しました」（用語）" in reader and "材料 m5:" in reader and "作りません" in reader
    items = [i for i in load_review(w, "draft.md").items if i.kind == "reader"]
    assert {i.category for i in items} == {"用語", "数字", "飛躍"}
    term = next(i for i in items if i.category == "用語")
    assert term.start is not None and "直し方: 保存した日時だと書く" in term.reason and term.unit["id"] == "m5"
    assert next(i for i in items if i.category == "飛躍").start is None
    _, done = revise(w, p, p, None, roles(), VOTES, None, "draft.md", "draft.v2.md")
    assert any("読者には分かりません" in t for t in done)
    assert "読者には分かりません" in (w.root / "draft.v2.prompt.md").read_text(encoding="utf-8")


def test_definitions_unrelated_to_the_term_are_dropped():
    by = {"m1": Unit(id="m1", text="LLM 注釈をバッチにまとめて呼び出し回数を減らした"),
          "m2": Unit(id="m2", text="意味の木とは、文書を節と段落の木として持つ表現のこと"),
          "m3": Unit(id="m3", text="木の節ごとに意味を付ける")}
    assert terms.rank_definitions("意味の木", ["m1", "m3", "m2"], by) == ["m2"]
    assert terms.relevance("意味の木", by["m1"].text) < terms.DEFINED_MIN_RELEVANCE


def test_review_keeps_the_avoid_list(tmp_path):
    p = scripted(terms=TERMS)
    w = designed(tmp_path / "w", p)
    ops.update_design(w, {"avoid": [*w.design().avoid, "Zenn の記事の分析"]}, "human-ui")
    before = w.design()
    review_conflicts(w, p, p)
    after = w.design()
    assert after.avoid == before.avoid and "Zenn の記事の分析" in after.avoid
    assert after.avoid_proposed == before.avoid_proposed

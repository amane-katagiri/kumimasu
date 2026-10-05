from __future__ import annotations

import json
import re

import pytest
import yaml
from conftest import PROJECT, SAMPLES, always_ask, run_cli, scripted, serving

from kumimasu import ops
from kumimasu.digest import BEFORE_DIGEST, digest
from kumimasu.errors import StepError
from kumimasu.interview import interview, unit_lines
from kumimasu.llm import FakeProvider
from kumimasu.mark import mark
from kumimasu.show import snapshot
from kumimasu.show import text as show_text
from kumimasu.workdir import WorkDir, init_workdir

DIGEST_MARK = "それだけで意味が通るメモに書き直して"


def dense_doc() -> str:
    out = ["# 実験の記録\n"]
    for i in range(1, 11):
        out.append(f"## {i}. 実験 {i}\n")
        for j in range(1, 5):
            out.append(f"### {i}.{j} 結果\n")
            out += [f"条件 {k} では {10 + i}.{j}{k} だった。比は {k + 2}.{i} 倍。\n" for k in range(8)]
    out.append("## 付録\n\n| 条件 | 値 |\n|---|---|\n| A | 12.5 |\n\n```sh\nrun --all\n```\n")
    return "\n".join(out)


@pytest.fixture
def dense(tmp_path):
    src = tmp_path / "dense.md"
    src.write_text(dense_doc(), encoding="utf-8")
    return src


def digest_fake(invent: bool = True):
    def respond(prompt: str) -> str:
        memos = []
        for sec in prompt.split("\n## 節: ")[1:]:
            path = sec.split("\n", 1)[0]
            ids = re.findall(r"^\[(m\d+)\]（(?:散文|項目|引用)）", sec, re.MULTILINE)
            texts = dict(re.findall(r"^\[(m\d+)\]（[^）]+）\n(.+)$", sec, re.MULTILINE))
            keep = ids[:-1] if path.endswith("1.1 結果") else ids
            for a in range(0, len(keep), 2):
                pair = keep[a:a + 2]
                memos.append({"from": pair, "text": f"{path} の実験で、" + " ".join(texts[x] for x in pair)})
            if invent and ids:
                memos.append({"from": ids[:1], "text": "材料に無い 999 という値"})
                memos.append({"from": ["zz"], "text": "無い単位から"})
        return json.dumps({"memos": memos})
    return respond


def test_detection_dense_vs_notes(tmp_path, dense):
    w, units = init_workdir(tmp_path / "d", PROJECT, [dense])
    st = w.project().digest
    assert st.recommended and len(st.reasons) >= 2 and any("見出しが多い" in r for r in st.reasons)
    assert units[0].path == "dense.md › 実験の記録 › 1. 実験 1 › 1.1 結果"
    assert "提案: 材料が仕上がった文書のようです" in show_text(snapshot(w))
    n, _ = init_workdir(tmp_path / "n", PROJECT, [SAMPLES / "notes.md", SAMPLES / "rename.sh"])
    assert not n.project().digest.recommended and "提案" not in show_text(snapshot(n))
    assert n.material_units()[-1].path == "rename.sh"
    assert "（場所: dense.md › 実験の記録 › 1. 実験 1 › 1.1 結果）" in unit_lines(units[:1])


def test_init_prints_the_suggestion(tmp_path, dense):
    out = run_cli("init", tmp_path / "w", "--topic", "t", "--audience", "a", "-m", dense)
    assert "提案: 材料が仕上がった文書のようです" in out and "kumimasu digest DIR" in out


def test_digest_writes_self_contained_units_and_archives(tmp_path, dense):
    w, raw = init_workdir(tmp_path / "d", PROJECT, [dense])
    p = FakeProvider(digest_fake())
    res = digest(w, p)
    units = w.material_units()
    assert w.raw_units_file.exists() and len(w.raw_units()) == len(raw)
    assert res.calls == len(p.calls) and res.calls < 41 and len(units) < len(raw)
    assert all(u.origin == "digest" and u.from_units for u in units)
    memo = units[0]
    assert memo.from_units == ["m1", "m2"] and memo.path == raw[0].path and "1.1 結果 の実験で" in memo.text
    assert not any("999" in u.text or "無い単位から" in u.text for u in units)
    left = next(u for u in units if u.from_units == [raw[7].id])
    assert left.text == raw[7].text
    code = next(u for u in units if u.kind == "code")
    row = next(u for u in units if u.kind == "row")
    assert code.text == "run --all" and row.text == "条件: A / 値: 12.5" and len(code.from_units) == 1
    assert [u.id for u in units] == [f"m{i}" for i in range(1, len(units) + 1)]
    saved = yaml.safe_load(w.units_file.read_text(encoding="utf-8"))
    assert saved[0]["from"] == ["m1", "m2"] and saved[0]["origin"] == "digest"
    st = w.project().digest
    assert st.done_at and st.units_before == len(raw) and st.units_after == len(units)
    assert "書き直し済み" in show_text(snapshot(w))
    digest(w, FakeProvider(digest_fake(invent=False)), force=True)
    assert len(w.raw_units()) == len(raw) and w.raw_units()[0].text == raw[0].text


def test_digest_guard_with_questions(tmp_path, dense):
    p = scripted()
    w, _ = init_workdir(tmp_path / "d", PROJECT, [dense])
    interview(w, p, always_ask())
    ops.answer(w, "q1", "答え", "agent-chat")
    with pytest.raises(StepError, match="--force"):
        digest(w, FakeProvider(digest_fake()))
    digest(w, FakeProvider(digest_fake()), force=True)
    assert not w.interview_file.exists() and (w.root / BEFORE_DIGEST).exists()


def test_mark_and_interview_after_digest(tmp_path, dense):
    base = scripted()
    p = FakeProvider(lambda prompt: digest_fake()(prompt) if DIGEST_MARK in prompt else base.responses(prompt))
    w, _ = init_workdir(tmp_path / "d", PROJECT, [dense])
    digest(w, p)
    units = mark(w, p, p)
    assert all(u.searchable for u in units) and all(u.from_units for u in units)
    assert any("（場所: dense.md" in c["prompt"] for c in p.calls if "同じ情報を述べている" in c["prompt"])
    iv = interview(w, p, always_ask())
    assert iv.questions
    with serving(w) as c:
        code, state = c.get("/api/state")
        assert code == 200 and state["project"]["digest"]["done_at"]
        first = state["units"][0]
        assert first["from"] == ["m1", "m2"] and first["path"].startswith("dense.md › ")
        assert set(first["from"]) <= set(state["originals"]) and state["originals"]["m1"]["text"].startswith("条件 0")
        code, page = c.get("/")
        assert "digest-note" in page and "書き直す前の単位" in page


def test_cli_digest_assess_and_stage(tmp_path, dense):
    w, _ = init_workdir(tmp_path / "d", PROJECT, [dense])
    out = run_cli("digest", w.root, "--assess")
    assert "digest recommended: yes" in out
    ops.set_stage(WorkDir(w.root), "design")
    run_cli("digest", w.root, "--assess", code=1)

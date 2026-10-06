from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml
from conftest import VOTES, serving
from typer.testing import CliRunner

from kumimasu import cli_common as cc
from kumimasu.check import Check, CheckReport, dash_hits, surface_checks
from kumimasu.cli import app
from kumimasu.errors import LLMError
from kumimasu.infounits import info_units
from kumimasu.keep import KeepStore, text_hash
from kumimasu.llm import CachedProvider, CountingProvider, FakeProvider, ask_json
from kumimasu.model import Design, Project
from kumimasu.polish import find_flags
from kumimasu.render import render
from kumimasu.review import (
    Item,
    Review,
    ReviewContext,
    Rewrite,
    apply_review,
    base_drafts,
    base_of,
    export_final,
    final_changes,
    final_name,
    is_final,
    load_review,
    mark_flags,
    review_path,
    save_decisions,
)
from kumimasu.textutil import apply_edits, locate
from kumimasu.workdir import WorkDir, init_workdir

ROOT = Path(__file__).resolve().parent.parent
PROJECT = Project(topic="縦書き", audience="個人サイトを作る人", length=600)

DRAFT = """# 縦書きのウェブページ

## 括弧の向き

この記事では、括弧の扱いを見ていきます。**縦中横**は `text-combine-upright` で指定します。ここで 2 つの記号を直しました。

- 一つ目の項目です。
- これにより、読みやすくなります。

`…` と `―` は `mixed` に戻しました——理由は後述します。

```css
p { text-orientation: upright; } /* この記事では */
```

最後の段落です。手元で 120 回試しました。
"""


@pytest.fixture
def wd(tmp_path) -> WorkDir:
    w, _ = init_workdir(tmp_path / "w", PROJECT.model_copy(update={"stage": "review"}),
                        [ROOT / "tests" / "samples" / "notes.md"])
    w.write("draft.md", DRAFT)
    return w


def report(*checks: Check, sources: dict[int, list[str]] | None = None) -> CheckReport:
    return CheckReport(draft="draft.md", chars=0, checks=list(checks), sources=sources or {})


def write_report(wd: WorkDir, rep: CheckReport, name: str = "check.json") -> None:
    (wd.root / name).write_text(rep.model_dump_json(), encoding="utf-8")


def standard_report(wd: WorkDir) -> CheckReport:
    last = next(u for u in info_units(DRAFT) if u.text.startswith("最後の段落"))
    rep = report(
        Check(id="meta", relation="r", passed=False, detail="3 回の判定の多数決", runs=3, surface=True,
              items=[{"id": "M1", "category": "signpost", "text": "この記事では、括弧の扱いを見ていきます。", "votes": 3}]),
        Check(id="glue", relation="r", passed=False, detail="3 回の判定の多数決。", runs=3, surface=True,
              items=[{"id": "M5", "text": "これにより、読みやすくなります。", "votes": 2}]),
        Check(id="lint", relation="lint", passed=False, surface=True,
              items=[{"rule": "dash", "text": "`…` と `―` は `mixed` に戻しました——理由は後述します。"}]),
        Check(id="numbers", relation="数値", passed=False, items=[{"number": "120", "context": "最後の段落です。手元で 120 回試しました。"}]),
        Check(id="fabrication", relation="体験", passed=False, detail="材料に無い",
              items=[{"unit": last.id, "text": "手元で 120 回試しました。", "source": "judge"}]),
        Check(id="drop_absent", relation="drop", passed=False,
              items=[{"id": "m2", "status": "added", "text": "EXIF の DateTimeOriginal"}]),
        sources={last.id: ["m2"]},
    )
    write_report(wd, rep)
    return rep


def test_items_have_offsets_that_match_the_source_and_rendered_spans(wd):
    standard_report(wd)
    rev = load_review(wd, "draft.md")
    by_kind = {i.kind: i for i in rev.items}
    assert set(by_kind) == {"meta", "glue", "lint", "number", "fabrication", "drop"}
    assert DRAFT[by_kind["meta"].start:by_kind["meta"].end] == "この記事では、括弧の扱いを見ていきます。"
    assert DRAFT[by_kind["glue"].start:by_kind["glue"].end] == "これにより、読みやすくなります。"
    assert DRAFT[by_kind["number"].start:by_kind["number"].end] == "120"
    assert DRAFT[by_kind["fabrication"].start:by_kind["fabrication"].end].startswith("最後の段落")
    assert by_kind["drop"].start == by_kind["fabrication"].start and by_kind["drop"].unit["id"] == "m2"
    assert by_kind["meta"].votes == "3/3" and by_kind["glue"].votes == "2/3" and "道しるべ" in by_kind["meta"].reason
    html, _ = render(DRAFT)

    starts = sorted(int(m[1]) for m in re.finditer(r'data-s="(\d+)"', html))
    for it in rev.items:
        assert any(s <= it.start < s + 200 for s in starts)
    assert locate("a **b** c", "abc") == (0, 9)


def test_decisions_persist_and_keep_is_remembered(wd):
    standard_report(wd)
    rev = load_review(wd, "draft.md")
    meta = next(i for i in rev.items if i.kind == "meta")
    glue = next(i for i in rev.items if i.kind == "glue")
    a = DRAFT.index("最後の段落です。")
    saved = save_decisions(wd, "draft.md", {"items": [
        {"id": meta.id, "decision": "keep"}, {"id": glue.id, "decision": "rewrite", "note": "短く"},
        {"id": "user-1", "decision": "delete", "start": a, "end": a + len("最後の段落です。")}]})
    assert review_path(wd, "draft.md").exists()
    again = load_review(wd, "draft.md")
    got = {i.id: (i.decision, i.note) for i in again.items}
    assert got[meta.id] == ("keep", "") and got[glue.id] == ("rewrite", "短く") and got["user-1"] == ("delete", "")
    assert next(i for i in saved.items if i.id == "user-1").text == "最後の段落です。"
    assert text_hash(meta.text) in KeepStore(wd).hashes()
    save_decisions(wd, "draft.md", {"items": [{"id": meta.id, "decision": ""}]})
    assert text_hash(meta.text) not in KeepStore(wd).hashes() and any(i.kind == "user" for i in load_review(wd, "draft.md").items)
    save_decisions(wd, "draft.md", {"remove": ["user-1"]})
    assert all(i.kind != "user" for i in load_review(wd, "draft.md").items)
    with pytest.raises(ValueError):
        save_decisions(wd, "draft.md", {"items": [{"id": "meta-nope", "decision": "keep"}]})
    with pytest.raises(ValueError):
        save_decisions(wd, "draft.md", {"items": [{"id": "user-2", "start": 5, "end": 99999}]})


def test_keep_from_another_review_marks_new_items_and_suppresses_checks(wd):
    standard_report(wd)
    meta = next(i for i in load_review(wd, "draft.md").items if i.kind == "meta")
    save_decisions(wd, "draft.md", {"items": [{"id": meta.id, "decision": "keep"}]})
    wd.write("draft.v2.md", DRAFT)
    write_report(wd, standard_report(wd), "check.v2.json")
    assert next(i for i in load_review(wd, "draft.v2.md").items if i.kind == "meta").decision == "keep"
    keep = KeepStore(wd).hashes()
    checks = {c.id: c for c in surface_checks(DRAFT, [], None, VOTES, keep)}
    assert all("見ていきます" not in x["text"] for x in checks["meta"].items) and "残すと決めた文 1" in checks["meta"].detail
    flags, _ = find_flags(DRAFT, DRAFT, FakeProvider(lambda p: json.dumps({"items": [{"id": "M1", "category": "signpost"}]})),
                          [], ("meta",), VOTES, keep)
    assert flags == []


def test_dash_lint_ignores_code_and_quoted_dashes():
    md = "`…` と `―` は戻しました。\n\n「―」を縦にすると崩れます。\n\n理由は後述します——たぶん。\n\n```\na — b\n```\n"
    assert dash_hits(md) == ["理由は後述します——たぶん。"]


def test_deletions_tidy_and_never_touch_code():
    src = DRAFT
    s1 = src.index("この記事では、括弧")
    sent = "この記事では、括弧の扱いを見ていきます。"
    item = "- これにより、読みやすくなります。"
    code_at = src.index("/* この記事では */")
    para = src.index("最後の段落です。手元で 120 回試しました。")
    spans = [(s1, s1 + len(sent)), (src.index(item) + 2, src.index(item) + len(item)), (code_at, code_at + 5),
             (para, para + len("最後の段落です。手元で 120 回試しました。"))]
    out = apply_edits(src, [(a, b, "") for a, b in spans])
    assert sent not in out and "\n- これにより" not in out and "- \n" not in out
    assert "**縦中横**は" in out and out.count("/* この記事では */") == 1 and "最後の段落" not in out
    assert "\n\n\n" not in out and out.endswith("```\n") and "- 一つ目の項目です。\n\n`…`" in out


def test_apply_deletes_and_rewrites_with_one_call(wd):
    standard_report(wd)
    rev = load_review(wd, "draft.md")
    meta = next(i for i in rev.items if i.kind == "meta")
    glue = next(i for i in rev.items if i.kind == "glue")
    save_decisions(wd, "draft.md", {"items": [{"id": meta.id, "decision": "delete"},
                                              {"id": glue.id, "decision": "rewrite", "note": "具体的に"}]})
    with pytest.raises(ValueError):
        apply_review(wd, "draft.md", None)
    p = FakeProvider(lambda prompt: json.dumps({"items": [{"id": glue.id, "replacement": "縦書きで読める。"}]}))
    res = apply_review(wd, "draft.md", p)
    final = (wd.root / "draft.final.md").read_text(encoding="utf-8")
    assert res.out == "draft.final.md" and res.deleted == 1 and res.rewritten == 1 and res.calls == 1
    assert "見ていきます" not in final and "- 縦書きで読める。" in final and "/* この記事では */" in final
    assert "著者のメモ: 具体的に" in p.calls[0]["prompt"] and "- 一つ目の項目です。" in p.calls[0]["prompt"]
    assert ["-", "この記事では、括弧の扱いを見ていきます。**縦中横**は `text-combine-upright` で指定します。ここで 2 つの記号を直しました。"] in res.diff
    save_decisions(wd, "draft.md", {"items": [{"id": glue.id, "decision": "keep"}]})
    res2 = apply_review(wd, "draft.md", None)
    assert res2.calls == 0 and res2.rewritten == 0


def test_server_rewrites_outside_the_lock_and_refuses_a_stale_result(wd):
    standard_report(wd)
    rev = load_review(wd, "draft.md")
    meta = next(i for i in rev.items if i.kind == "meta")
    glue = next(i for i in rev.items if i.kind == "glue")
    save_decisions(wd, "draft.md", {"items": [{"id": glue.id, "decision": "rewrite"}]})
    started, go = threading.Event(), threading.Event()

    def slow(prompt: str) -> str:
        started.set()
        go.wait(5)
        return json.dumps({"items": [{"id": glue.id, "replacement": "縦書きで読める。"}]})

    rewriter = FakeProvider(slow)
    with serving(wd, lambda: rewriter) as c:
        out = {}
        t = threading.Thread(target=lambda: out.update(r=c.post("/api/apply/draft.md")))
        t.start()
        assert started.wait(5)
        code, _ = c.put("/api/review/draft.md", {"items": [{"id": meta.id, "decision": "delete"}]})
        assert code == 200
        go.set()
        t.join(5)
        code, body = out["r"]
        assert code == 409 and body["stale"] is True and not (wd.root / "draft.final.md").exists()
        assert len(rewriter.calls) == 1
        code, r = c.post("/api/apply/draft.md")
        assert code == 200 and r["result"]["calls"] == 1 and r["result"]["deleted"] == 1 and len(rewriter.calls) == 2
        assert "- 縦書きで読める。" in r["final"]["markdown"]


def test_server_final_check_round_trip(wd):
    standard_report(wd)
    wd.write("draft.prompt.md", "x")
    rewriter = FakeProvider(lambda prompt: json.dumps({"items": []}))
    with serving(wd, lambda: rewriter) as c:
        assert c.get("/api/drafts") == (200, {"drafts": [{"name": "draft.md", "final": None}]})
        assert c.get("/api/final/draft.md")[0] == 404
        assert c.get("/api/download/draft.md")[0] == 404
        code, r = c.get("/api/review/draft.md")
        assert code == 200 and 'data-s="' in r["html"] and len(r["items"]) == 6 and r["checked"]
        meta = next(i for i in r["items"] if i["kind"] == "meta")
        code, r = c.put("/api/review/draft.md", {"items": [{"id": meta["id"], "decision": "delete"}]})
        assert code == 200 and next(i for i in r["items"] if i["id"] == meta["id"])["decision"] == "delete"
        assert r["version"] == c.get("/api/version")[1]["version"] and "html" not in r
        a = DRAFT.index("最後の段落")
        code, r = c.put("/api/review/draft.md", {"items": [{"start": a, "end": a + 5, "decision": "keep"}]})
        assert code == 200 and any(i["kind"] == "user" and i["id"].startswith("user-") for i in r["items"])
        assert yaml.safe_load(review_path(wd, "draft.md").read_text(encoding="utf-8"))["items"]
        code, r = c.post("/api/apply/draft.md")
        res = r["result"]
        assert code == 200 and res["out"] == "draft.final.md" and res["deleted"] == 1 and res["calls"] == 0
        assert r["review"]["dirty"] is False and "見ていきます" not in r["final"]["markdown"]
        assert c.get("/api/drafts")[1]["drafts"] == [{"name": "draft.md", "final": "draft.final.md"}]
        code, f = c.get("/api/final/draft.md")
        assert code == 200 and f["final"] == "draft.final.md" and "見ていきます" not in f["markdown"] and "data-s" in f["html"]
        code, body, headers = c.request("/api/download/draft.md")
        assert code == 200 and body == f["markdown"]
        disp = headers["Content-Disposition"]
        assert disp.startswith("attachment; filename*=UTF-8''w-draft-") and disp.endswith(".md")
        for bad in ("draft.final.md", "draft.final.final.md"):
            assert c.get(f"/api/review/{bad}")[0] == 404 and c.post(f"/api/apply/{bad}")[0] == 404
        assert c.get("/api/review/draft.prompt.md")[0] == 404
        assert c.get("/api/review/..%2Fproject.yaml")[0] == 404
        assert "最終チェック" in c.page()[:2][1]
        wd.write("draft-日本語.md", DRAFT)
        assert {"name": "draft-日本語.md", "final": None} in c.get("/api/drafts")[1]["drafts"]
        code, r = c.get(f"/api/review/{quote('draft-日本語.md')}")
        assert code == 200 and r["draft"] == "draft-日本語.md"


def test_server_rejects_ill_typed_review_bodies(wd):
    standard_report(wd)
    path = review_path(wd, "draft.md")
    with serving(wd) as c:
        for body in ({"items": ["x"]}, {"items": [{"start": 0}]}, {"items": [{"id": "user-1", "end": 3}]},
                     {"items": [{"id": "user-1", "start": 0, "end": 3, "note": ["x"]}]}, {"remove": "user-1"},
                     {"items": [], "extra": 1}):
            code, err = c.put("/api/review/draft.md", body)
            assert code == 400 and "error" in err, body
    assert not path.exists()


def test_server_refuses_symlinked_drafts(wd, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("秘密\n", encoding="utf-8")
    (wd.root / "draft.link.md").symlink_to(secret)
    (wd.root / "draft.final.md").symlink_to(secret)
    with serving(wd) as c:
        assert c.get("/api/review/draft.link.md")[0] == 404
        assert c.get("/api/download/draft.md")[0] == 404
        assert c.get("/api/final/draft.md")[0] == 404
        assert c.get("/api/drafts")[1] == {"drafts": [{"name": "draft.md", "final": None}]}


def test_cli_apply(wd, monkeypatch):
    standard_report(wd)
    glue = next(i for i in load_review(wd, "draft.md").items if i.kind == "glue")
    save_decisions(wd, "draft.md", {"items": [{"id": glue.id, "decision": "rewrite"}]})
    p = FakeProvider(lambda prompt: json.dumps({"items": [{"id": glue.id, "replacement": "別の文。"}]}))
    monkeypatch.setattr(cc, "provider", lambda spec, web=False, **kw: p)
    res = CliRunner().invoke(app, ["apply", str(wd.root)])
    assert res.exit_code == 0, res.output
    assert "rewritten 1" in res.output and "+ - 別の文。" in res.output


def test_base_drafts_and_finals(wd):
    for n in ("draft.final.md", "draft.final.final.md", "draft.v2.md", "draft.v2.prompt.md", "draft.C.polished.md",
              "draft.C.polished.final.md", "draftfinal.md"):
        wd.write(n, "x\n")
    assert base_drafts(wd) == ["draft.C.polished.md", "draft.md", "draft.v2.md", "draftfinal.md"]
    assert is_final("draft.final.final.md") and not is_final("draft.finality.md") and base_of("draft.v2.final.final.md") == "draft.v2.md"
    assert final_name("draft.C.md") == "draft.C.final.md"


def test_apply_and_export_reject_finals(wd, tmp_path):
    wd.write("draft.final.md", DRAFT)
    with pytest.raises(ValueError, match="元の下書き draft.md"):
        apply_review(wd, "draft.final.md", None)
    res = CliRunner().invoke(app, ["apply", str(wd.root), "--draft", "draft.final.md"])
    assert res.exit_code == 1 and "反映の出力" in res.output
    with pytest.raises(ValueError, match="反映の出力"):
        export_final(wd, "draft.final.md", tmp_path / "out")


def test_export_naming_never_overwrites(wd, tmp_path):
    with pytest.raises(ValueError):
        export_final(wd, "draft.md", tmp_path / "out")
    wd.write("draft.final.md", "final text\n")
    t = datetime(2026, 10, 5, 14, 7, tzinfo=UTC)
    a = export_final(wd, "draft.md", tmp_path / "out", t)
    b = export_final(wd, "draft.md", tmp_path / "out", t)
    c = export_final(wd, "draft.md", tmp_path / "out", t)
    assert [p.name for p in (a, b, c)] == ["w-draft-20261005-1407.md", "w-draft-20261005-1407-2.md", "w-draft-20261005-1407-3.md"]
    assert a.read_text(encoding="utf-8") == "final text\n"
    res = CliRunner().invoke(app, ["export", str(wd.root), "--to", str(tmp_path / "cli")])
    assert res.exit_code == 0 and len(list((tmp_path / "cli").glob("w-draft-*.md"))) == 1


def _two_rewrites(wd):
    standard_report(wd)
    rev = load_review(wd, "draft.md")
    meta = next(i for i in rev.items if i.kind == "meta")
    glue = next(i for i in rev.items if i.kind == "glue")
    return meta, glue


def _echo_provider(tag: str):
    def respond(prompt: str) -> str:
        ids = re.findall(r"^## 項目 (\S+)$", prompt, re.MULTILINE)
        return json.dumps({"items": [{"id": i, "replacement": f"{tag}{n}。"} for n, i in enumerate(ids)]})
    return FakeProvider(respond)


def test_rewrites_are_locked_and_only_new_items_are_sent(wd):
    meta, glue = _two_rewrites(wd)
    save_decisions(wd, "draft.md", {"items": [{"id": glue.id, "decision": "rewrite"}]})
    p1 = _echo_provider("一回目")
    r1 = apply_review(wd, "draft.md", p1)
    assert r1.calls == 1 and r1.reused == 0
    stored = next(i for i in load_review(wd, "draft.md").items if i.id == glue.id).rewrite
    assert stored.result == "一回目0。" and stored.source == "llm" and stored.made_from == "これにより、読みやすくなります。"
    save_decisions(wd, "draft.md", {"items": [{"id": meta.id, "decision": "rewrite", "note": "短く"}]})
    p2 = _echo_provider("二回目")
    r2 = apply_review(wd, "draft.md", p2)
    prompt = p2.calls[0]["prompt"]
    assert r2.calls == 1 and r2.reused == 1 and glue.id not in prompt and meta.id in prompt
    final = (wd.root / "draft.final.md").read_text(encoding="utf-8")
    assert "一回目0。" in final and "二回目0。" in final
    r3 = apply_review(wd, "draft.md", None)
    assert r3.calls == 0 and r3.reused == 2 and (wd.root / "draft.final.md").read_text(encoding="utf-8") == final


def test_regenerate_user_edit_note_hint_and_stale(wd):
    meta, glue = _two_rewrites(wd)
    save_decisions(wd, "draft.md", {"items": [{"id": glue.id, "decision": "rewrite"}, {"id": meta.id, "decision": "rewrite"}]})
    apply_review(wd, "draft.md", _echo_provider("初"))
    rev = save_decisions(wd, "draft.md", {"items": [{"id": glue.id, "note": "もっと具体的に"},
                                                    {"id": meta.id, "result": "手で直した文。"}]})
    by = {i.id: i for i in rev.items}
    assert by[glue.id].note_changed and by[glue.id].rewrite.result.startswith("初")
    assert by[meta.id].rewrite.source == "user" and by[meta.id].rewrite.result == "手で直した文。" and not by[meta.id].note_changed
    assert apply_review(wd, "draft.md", None).calls == 0
    assert "手で直した文。" in (wd.root / "draft.final.md").read_text(encoding="utf-8")
    p = _echo_provider("再")
    r = apply_review(wd, "draft.md", p, regenerate=(glue.id,))
    assert r.calls == 1 and meta.id not in p.calls[0]["prompt"] and "もっと具体的に" in p.calls[0]["prompt"]
    assert not next(i for i in load_review(wd, "draft.md").items if i.id == glue.id).note_changed
    rev = save_decisions(wd, "draft.md", {"items": [{"id": meta.id, "regenerate": True}]})
    assert next(i for i in rev.items if i.id == meta.id).rewrite is None
    wd.write("draft.md", DRAFT.replace("これにより、読みやすくなります。", "これにより、とても読みやすくなります。"))
    write_report(wd, standard_report(wd))
    rev = load_review(wd, "draft.md")
    stale = [i for i in rev.items if i.stale]
    assert [i.id for i in stale] == [glue.id]
    r = apply_review(wd, "draft.md", _echo_provider("新"))
    assert glue.id in r.stale and "とても読みやすくなります" in (wd.root / "draft.final.md").read_text(encoding="utf-8")


def test_cli_apply_regenerate(wd, monkeypatch):
    _, glue = _two_rewrites(wd)
    save_decisions(wd, "draft.md", {"items": [{"id": glue.id, "decision": "rewrite"}]})
    p = _echo_provider("x")
    monkeypatch.setattr(cc, "provider", lambda spec, web=False, **kw: p)
    r = CliRunner()
    assert "calls 1" in r.invoke(app, ["apply", str(wd.root)]).output
    assert "calls 0" in r.invoke(app, ["apply", str(wd.root)]).output
    out = r.invoke(app, ["apply", str(wd.root), "--regenerate", glue.id]).output
    assert "calls 1" in out and len(p.calls) == 2
    bad = r.invoke(app, ["apply", str(wd.root), "--regenerate", "nope"])
    assert bad.exit_code == 1 and "知らない項目です" in bad.output


def test_rewrite_reanchors_when_the_sentence_moved(wd):
    _, glue = _two_rewrites(wd)
    save_decisions(wd, "draft.md", {"items": [{"id": glue.id, "decision": "rewrite"}]})
    apply_review(wd, "draft.md", _echo_provider("固"))
    moved = DRAFT.replace("最初", "最初").replace("## 括弧の向き\n\n", "## 括弧の向き\n\n前に一文を足しました。\n\n")
    wd.write("draft.md", moved)
    write_report(wd, standard_report(wd))
    it = next(i for i in load_review(wd, "draft.md").items if i.id == glue.id)
    assert not it.stale and moved[it.start:it.end] == "これにより、読みやすくなります。"
    r = apply_review(wd, "draft.md", None)
    assert r.calls == 0 and r.reused == 1 and "固0。" in (wd.root / "draft.final.md").read_text(encoding="utf-8")
    twice = moved + "\nこれにより、読みやすくなります。\n"
    wd.write("draft.md", twice)
    write_report(wd, standard_report(wd))

    x = Item(id="x", kind="glue", start=0, end=3, text="t",
             rewrite=Rewrite(result="r", made_from="これにより、読みやすくなります。"))
    mark_flags(x, twice)
    assert x.stale
    u = Item(id="user-1", kind="user", start=0, end=4, text="前に一文を")
    mark_flags(u, moved)
    assert not u.stale and moved[u.start:u.end] == "前に一文を"


def test_final_changes_locates_rewrites_and_deletions():
    base = "前置きの文です。消す文です。残る文です。直す文です。"
    final = "前置きの文です。残る文です。直した文です。"
    rev = Review(draft="draft.md", items=[
        Item(id="d", kind="meta", start=8, end=14, text="消す文です。", decision="delete"),
        Item(id="r", kind="glue", start=20, end=26, text="直す文です。", decision="rewrite",
             rewrite=Rewrite(result="直した文です。", made_from="直す文です。")),
    ])
    got = {c["id"]: c for c in final_changes(base, final, rev)}
    assert got["d"]["kind"] == "deleted" and got["d"]["start"] == got["d"]["end"] == 8
    assert final[got["r"]["start"]:got["r"]["end"]] == "直した文です。"
    assert got["r"]["before"] == "直す文です。"


def test_needs_apply_tracks_decisions_since_last_apply(wd):
    def needs_apply(wd, name):
        return ReviewContext.load(wd, name).needs_apply()

    standard_report(wd)
    meta = next(i for i in load_review(wd, "draft.md").items if i.kind == "meta")
    assert needs_apply(wd, "draft.md")
    save_decisions(wd, "draft.md", {"items": [{"id": meta.id, "decision": "delete"}]})
    apply_review(wd, "draft.md", None)
    assert not needs_apply(wd, "draft.md")
    save_decisions(wd, "draft.md", {"items": [{"id": meta.id, "decision": "keep"}]})
    assert needs_apply(wd, "draft.md")
    save_decisions(wd, "draft.md", {"items": [{"id": meta.id, "decision": "delete"}]})
    assert not needs_apply(wd, "draft.md")


@pytest.mark.parametrize(("src", "span", "reply", "want"), [
    ("# 題\n\n## 古い見出し\n\n本文。\n", "古い見出し", "## 新しい見出し", "新しい見出し"),
    ("- 古い項目\n", "古い項目", "- 新しい項目", "新しい項目"),
    ("1. 古い手順\n", "古い手順", "1. 新しい手順", "新しい手順"),
    ("本文の文です。続きです。\n", "続きです。", "- 箇条書きっぽい返事", "- 箇条書きっぽい返事"),
])
def test_rewrite_results_do_not_repeat_block_marks(src, span, reply, want):
    from kumimasu.review import strip_block_marks

    assert strip_block_marks(src, src.index(span), reply) == want


def test_rewrite_of_part_of_a_sentence_replaces_the_whole_sentence():
    from kumimasu.review import Item, Rewrite, rewrite_prompt, rewrite_span
    from kumimasu.textutil import enclosing_sentences

    src = "# 題\n\n前の文です。この設定はかなり便利で、毎回使えます。次の文です。\n\n## 見出しの一部\n"
    a = src.index("かなり便利で")
    b = a + len("かなり便利で")
    s, e = enclosing_sentences(src, a, b)
    assert src[s:e] == "この設定はかなり便利で、毎回使えます。"
    it = Item(id="x", kind="glue", start=a, end=b, text=src[a:b], decision="rewrite")
    prompt = rewrite_prompt(src, [it])
    assert "直す文:\nこの設定はかなり便利で、毎回使えます。" in prompt and "特に直す箇所（文の一部）: かなり便利で" in prompt
    it.rewrite = Rewrite(result="この設定は毎回使えます。", scope="sentence")
    assert rewrite_span(src, it) == (s, e)
    it.rewrite = Rewrite(result="便利で", scope="span")
    assert rewrite_span(src, it) == (a, b)
    h = src.index("一部")
    assert src[slice(*enclosing_sentences(src, h, h + 2))] == "見出しの一部"


def test_regenerate_makes_a_new_call_through_the_cache(wd, tmp_path):
    _, glue = _two_rewrites(wd)
    save_decisions(wd, "draft.md", {"items": [{"id": glue.id, "decision": "rewrite"}]})
    seen = []

    def respond(prompt: str) -> str:
        seen.append(prompt)
        return json.dumps({"items": [{"id": glue.id, "replacement": f"{len(seen)}回目の文。"}]})

    cached = CachedProvider(FakeProvider(respond), tmp_path / "cache")
    apply_review(wd, "draft.md", cached)
    save_decisions(wd, "draft.md", {"items": [{"id": glue.id, "regenerate": True}]})
    apply_review(wd, "draft.md", cached)
    assert len(seen) == 2 and "作り直し 1 回目" in seen[1]
    assert next(i for i in load_review(wd, "draft.md").items if i.id == glue.id).rewrite.result == "2回目の文。"
    apply_review(wd, "draft.md", cached, regenerate=(glue.id,))
    assert len(seen) == 3 and "作り直し 2 回目" in seen[2]


def test_bad_replies_are_not_served_from_the_cache(wd, tmp_path):
    _, glue = _two_rewrites(wd)
    save_decisions(wd, "draft.md", {"items": [{"id": glue.id, "decision": "rewrite"}]})
    good = json.dumps({"items": [{"id": glue.id, "replacement": "直した文。"}]})
    inner = FakeProvider(["JSON ではない", '{"items": []}', good])
    cached = CachedProvider(inner, tmp_path / "cache")
    with pytest.raises(LLMError):
        apply_review(wd, "draft.md", cached)
    r = apply_review(wd, "draft.md", cached)
    assert r.rewritten == 0 and glue.id in r.skipped
    r = apply_review(wd, "draft.md", cached)
    assert r.rewritten == 1 and len(inner.calls) == 3
    save_decisions(wd, "draft.md", {"items": [{"id": glue.id, "decision": "keep"}]})
    with pytest.raises(LLMError):
        ask_json(CountingProvider(cached), "p", {})
    assert ask_json(cached, "p", {}) == {"items": []} and cached.misses == 5
    assert ask_json(cached, "p", {}) == {"items": []} and cached.hits == 1


def test_server_confirm_rewrites_a_pending_item_with_one_call(wd):
    standard_report(wd)
    wd.save_design(Design())
    glue = next(i for i in load_review(wd, "draft.md").items if i.kind == "glue")
    save_decisions(wd, "draft.md", {"items": [{"id": glue.id, "decision": "rewrite"}]})
    rewriter = FakeProvider(lambda prompt: json.dumps({"items": [{"id": glue.id, "replacement": "縦書きで読める。"}]}))
    with serving(wd, lambda: rewriter) as c:
        code, body = c.post("/api/confirm", {"note": ""})
    assert code == 200 and body["handoff"]["stage"] == "review" and len(rewriter.calls) == 1
    assert "縦書きで読める。" in (wd.root / "draft.final.md").read_text(encoding="utf-8")

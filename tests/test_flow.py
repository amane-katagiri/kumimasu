from __future__ import annotations

import json
import re
import shutil
import threading
import time

import pytest
import yaml
from conftest import (
    PROJECT,
    SAMPLES,
    always_ask,
    defaults,
    roles,
    run_cli,
    scripted,
    serving,
)
from typer.testing import CliRunner

from kumimasu import auto as stand_in
from kumimasu import cli_common as cc
from kumimasu import ops
from kumimasu.check import Check, CheckReport
from kumimasu.cli import app
from kumimasu.design import design
from kumimasu.draft import draft
from kumimasu.interview import interview
from kumimasu.llm import FakeProvider
from kumimasu.mark import mark
from kumimasu.review import load_review
from kumimasu.workdir import WorkDir, init_workdir


def make_wd(root, p=None) -> WorkDir:
    p = p or scripted()
    w, _ = init_workdir(root, PROJECT, [SAMPLES / "notes.md", SAMPLES / "rename.sh"])
    mark(w, p, p)
    interview(w, p, always_ask())
    return w


def to_design(w: WorkDir, p=None) -> None:
    p = p or scripted()
    ops.confirm(w, "agent-chat")
    design(w, p, defaults())


def to_review(w: WorkDir, p=None) -> None:
    p = p or scripted()
    to_design(w, p)
    ops.confirm(w, "agent-chat")
    draft(w, p, roles())
    ops.confirm(w, "agent")


@pytest.fixture
def wd(tmp_path) -> WorkDir:
    return make_wd(tmp_path / "w")


def test_stage_guards_in_ops_cli_and_confirm(wd):
    with pytest.raises(ops.StageError):
        ops.update_design(wd, {"purpose": "x"}, "agent-chat")
    out = run_cli("set", wd.root, "purpose", "x", code=1)
    assert "「設計」の段階でしかできません" in out and "今は「インタビュー」" in out
    with pytest.raises(ops.StageError):
        ops.confirm(wd, "agent")
    to_design(wd)
    assert ops.stage(wd) == "design"
    run_cli("answer", wd.root, "q1", "遅すぎた答え", code=1)
    run_cli("design", wd.root, code=1)
    ops.confirm(wd, "human-ui")
    with pytest.raises(ops.StageError, match="エージェントが書き終えて"):
        ops.confirm(wd, "human-ui")
    run_cli("decide", wd.root, "meta-x", "keep", code=1)


def test_page_and_cli_write_identical_files(tmp_path):
    a = make_wd(tmp_path / "a")
    to_design(a)
    b_root = tmp_path / "b"
    shutil.copytree(a.root, b_root)
    b = WorkDir(b_root)
    with serving(a) as c:
        rules = [r.model_dump() for r in a.design().rules]
        rules[0]["on"] = False
        c.put("/api/design", {"units": {"m1": "deep"}})
        c.put("/api/design", {"takeaways": a.design().takeaways})
        c.put("/api/design", {"rules": rules})
        c.put("/api/design", {"toggle_skip": {"unit": "m6", "on": True, "label": "命名の話"}})
        run_cli("set", b.root, "unit", "m1", "--use", "deep")
        run_cli("set", b.root, "takeaway", "1", b.design().takeaways[0])
        run_cli("rule", b.root, "off", "1")
        run_cli("set", b.root, "skip", "m6", "on", "--label", "命名の話")
        assert a.design_file.read_text(encoding="utf-8") == b.design_file.read_text(encoding="utf-8")
        hist_a = [h["source"] for h in ops.history(a) if h["op"] == "design"]
        hist_b = [h["source"] for h in ops.history(b) if h["op"] == "design"]
        assert set(hist_a) == {"human-ui"} and set(hist_b) == {"agent-chat"} and len(hist_a) == len(hist_b) == 4


def test_decisions_parity_and_provenance(tmp_path):
    a = make_wd(tmp_path / "a")
    to_review(a)
    run_cli("check", a.root, "--meta-detector", "rules", code=1)

    rep = CheckReport(draft="draft.md", chars=0, checks=[Check(
        id="meta", relation="r", passed=False, surface=True, detail="3 回の判定の多数決", runs=3,
        items=[{"id": "M1", "category": "signpost", "text": "スマホとデジカメで IMG_1234.JPG と DSC01234.JPG のように名前がばらばらでした。", "votes": 2}])])
    (a.root / "check.json").write_text(rep.model_dump_json(), encoding="utf-8")
    b_root = tmp_path / "b"
    shutil.copytree(a.root, b_root)
    b = WorkDir(b_root)

    item = load_review(a, "draft.md").items[0].id
    ops.decide(a, "draft.md", {"items": [{"id": item, "decision": "rewrite", "note": "短く"}]}, "human-ui")
    run_cli("decide", b.root, item, "rewrite", "--note", "短く")

    def norm(w):
        data = yaml.safe_load((w.root / "review.draft.yaml").read_text(encoding="utf-8"))
        data.pop("updated_at")
        for i in data["items"]:
            i.pop("source")
        return data

    assert norm(a) == norm(b)
    assert load_review(a, "draft.md").items[0].source == "human-ui" and load_review(b, "draft.md").items[0].source == "agent-chat"
    uid = run_cli("add-item", b.root, "--start", 0, "--end", 5).strip()
    assert any(i.id == uid and i.kind == "user" for i in load_review(b, "draft.md").items)
    run_cli("remove-item", b.root, uid)
    assert all(i.id != uid for i in load_review(b, "draft.md").items)


def test_confirm_handoff_and_wait(wd):
    found: dict = {}

    def waiter():
        found["h"] = ops.wait_for(wd, "interview", timeout=5, interval=0.05)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.2)
    assert "h" not in found
    ops.answer(wd, "q1", "LINE の写真だけ無かった", "human-ui")
    ops.confirm(wd, "human-ui", note="ISBN の話は短く")
    t.join(5)
    h = found["h"]
    assert h["stage"] == "interview" and h["next"] == "design" and h["who"] == "human" and h["note"] == "ISBN の話は短く"
    assert json.loads((wd.root / "handoff.json").read_text(encoding="utf-8")) == h
    assert ops.wait_for(wd, "design", timeout=0.1, interval=0.05) is None
    out = run_cli("wait", wd.root, "--for", "interview", "--timeout", "1")
    assert json.loads(out)["note"] == "ISBN の話は短く"
    run_cli("wait", wd.root, "--for", "design", "--timeout", "0.2", "--interval", "0.05", code=2)
    assert wd.interview().questions[0].source == "human-ui"
    assert [h["source"] for h in ops.history(wd)] == ["human-ui", "human-ui"]


def test_review_confirm_applies_and_hands_over_the_article(wd):
    to_review(wd)
    assert wd.review_draft() == "draft.md"
    h = ops.confirm(wd, "agent-chat", "ブログに載せる前に見せて")
    assert ops.stage(wd) == "done" and h["final"].endswith("draft.final.md") and (wd.root / "draft.final.md").exists()
    assert h["article"]["title"] == "写真の名前を撮影日時にそろえる" and h["article"]["register"] == "keitai"
    assert h["article"]["takeaways"] and h["who"] == "human"
    with pytest.raises(ops.StageError):
        ops.confirm(wd, "agent-chat")
    assert ops.find_handoff(wd, "done")["final"] == h["final"]
    assert "確定した記事" in run_cli("show", wd.root)


def test_auto_interview_answers_only_selection_questions(wd):
    def respond(prompt):
        return json.dumps({"answers": [{"id": "q1", "kind": "firsthand", "answer": "とても驚いた（作り話）"},
                                       {"id": "q2", "kind": "selection", "answer": "EXIF の無い写真の扱いを持ち帰ってほしい"}]})

    h = stand_in.auto_interview(wd, FakeProvider(respond))
    qs = {q.id: q for q in wd.interview().questions}
    assert qs["q1"].answer == "" and qs["q2"].answer.startswith("EXIF") and qs["q2"].source == "auto"
    assert h["who"] == "auto" and [x["op"] for x in h["auto"]] == ["answer"] and ops.stage(wd) == "design"
    assert "推奨しません" in stand_in.WARNING


def test_auto_review_never_rewrites_and_keeps_material_sentences(wd, monkeypatch):
    to_review(wd)

    traced = "試したら、スマホの写真 312 枚のうち 9 枚に撮影日時がありませんでした。"
    rep = CheckReport(draft="draft.md", chars=0, checks=[
        Check(id="glue", relation="r", passed=False, surface=True, detail="3 回の判定の多数決", runs=3,
              items=[{"id": "M3", "text": traced, "votes": 2},
                     {"id": "M9", "text": "それでも並び順はだいたい保たれるので、私はこれで十分だと判断しました。", "votes": 2}]),
        Check(id="meta", relation="r", passed=False, surface=True, detail="3 回の判定の多数決", runs=3,
              items=[{"id": "M8", "category": "signpost", "text": "更新日時は写真を保存した日時なので、撮影日とは数日ずれることがあります。", "votes": 3}])])
    (wd.root / "check.json").write_text(rep.model_dump_json(), encoding="utf-8")
    asked = []

    def respond(prompt):
        asked.append(prompt)
        return json.dumps({"items": [{"id": i, "decision": "rewrite", "reason": "x"} if n == 0 else
                                     {"id": i, "decision": "delete", "reason": "x"}
                                     for n, i in enumerate(re.findall(r"^\[([\w-]+)\]", prompt, re.MULTILINE))]})

    h = stand_in.auto_review(wd, FakeProvider(respond))
    items = load_review(wd, "draft.md").items
    assert all(i.decision != "rewrite" for i in items)
    kept = next(i for i in items if i.text == traced)
    assert kept.decision == "keep" and kept.source == "auto" and traced not in asked[0]
    assert h["who"] == "auto" and ops.stage(wd) == "done" and (wd.root / "draft.final.md").exists()


def test_restart_rounds(wd):
    to_review(wd)
    ops.confirm(wd, "agent-chat")
    n = ops.restart(wd, "design", "agent-chat")
    assert n == 2 and ops.stage(wd) == "design" and wd.design_file.name == "design.r2.yaml" and not wd.design_file.exists()
    assert wd.draft_base() == "draft.r2.md" and (wd.root / "design.yaml").exists() and wd.interview_file.exists()
    assert ops.wait_for(wd, "design", timeout=0) is None
    design(wd, scripted(), defaults())
    ops.confirm(wd, "agent-chat")
    draft(wd, scripted(), roles(), wd.draft_base())
    assert (wd.root / "draft.r2.md").exists() and (wd.root / "draft.md").exists()
    out = run_cli("restart", wd.root, "--from", "drafting")
    assert "round 3" in out and (wd.root / "design.r3.yaml").read_text(encoding="utf-8") == \
        (wd.root / "design.r2.yaml").read_text(encoding="utf-8")
    run_cli("restart", wd.root, "--from", "review", code=1)


def test_version_endpoint_etag_and_live_changes(wd):
    with serving(wd) as c:
        code, v, headers = c.request("/api/version")
        assert code == 200 and headers["ETag"] == f'"{v["version"]}"' and v["stage"] == "interview"
        etag = {"If-None-Match": f'"{v["version"]}"'}
        assert c.request("/api/version", headers=etag)[0] == 304
        time.sleep(0.01)
        run_cli("answer", wd.root, "q1", "CLI から")
        code, now = c.get("/api/version", headers=etag)
        assert code == 200 and now["version"] != v["version"]
        code, body = c.post("/api/confirm", {"note": "n"})
        assert body["handoff"]["source"] == "human-ui" and body["stage"]["stage"] == "design"
        assert c.put("/api/interview", {"answers": {"q1": "x"}})[0] == 409


def test_server_rejects_cross_origin_rebinding_and_missing_token(wd):
    with serving(wd) as c:
        port = c.base.rsplit(":", 1)[1]
        assert c.get("/api/state", token=None)[0] == 403
        assert c.get("/api/state", token="wrong")[0] == 403
        assert c.get("/api/state", headers={"Host": "evil.example"})[0] == 421
        assert c.get("/", headers={"Host": f"evil.example:{port}"})[0] == 421
        assert c.get("/api/state", headers={"Host": f"localhost:{port}"})[0] == 200
        assert c.post("/api/confirm", {"note": "x"}, headers={"Origin": "https://evil.example"})[0] == 403
        assert c.post("/api/confirm", headers={"Origin": f"http://localhost:{port}"})[0] != 403
        assert c.request("/api/confirm", "POST", raw=b'{"note": "x"}', headers={"Content-Type": "text/plain"})[0] == 415
        assert c.request("/api/confirm", "POST", raw=b"[1]")[0] == 400
        assert c.request("/api/confirm", "POST", raw=b"{nope")[0] == 400
        assert c.request("/api/confirm", "POST", raw=b"{}", headers={"Content-Length": "-1"})[0] == 400
        assert c.request("/api/confirm", "POST", raw=b"{}", headers={"Content-Length": str(2 * 1024 * 1024 + 1)})[0] == 413
        assert ops.stage(wd) == "design"
        code, page, headers = c.request("/", token=None)
        assert code == 200 and "test-token" in page and headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff" and headers["Referrer-Policy"] == "no-referrer"
        code, body = c.get("/api/review/draft.md")
        assert code == 404 and str(wd.root) not in json.dumps(body, ensure_ascii=False)


def test_cli_auto_prints_warning(wd, monkeypatch):
    monkeypatch.setattr(cc, "provider", lambda spec, web=False, **kw: FakeProvider(
        lambda p: json.dumps({"answers": []})))
    res = CliRunner().invoke(app, ["auto", str(wd.root), "--stage", "interview"])
    assert res.exit_code == 0 and "推奨しません" in res.output and ops.stage(wd) == "design"

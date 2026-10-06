from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kumimasu.check import Votes
from kumimasu.cli import app
from kumimasu.cli_common import design_defaults
from kumimasu.config import load
from kumimasu.llm import FakeProvider
from kumimasu.model import Project
from kumimasu.server import WriteApp, make_server

TOKEN = "test-token"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KUMIMASU_CONFIG", str(home / "no-config.yaml"))
    monkeypatch.delenv("KUMIMASU_TRUST_PROJECT", raising=False)


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def request(self, path: str, method: str = "GET", body=None, headers: dict | None = None,
                token: str | None = TOKEN, raw: bytes | None = None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json"} if data is not None else {}
        if token is not None:
            h["X-Kumimasu-Token"] = token
        if method in ("POST", "PUT") and token is not None:
            h["X-Kumimasu-Version"] = self.version(token)
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h | (headers or {}))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                text = r.read().decode()
                ctype = r.headers.get("Content-Type", "")
                return r.status, json.loads(text) if "json" in ctype else text, r.headers
        except urllib.error.HTTPError as e:
            text = e.read().decode()
            return e.code, json.loads(text) if text.startswith("{") else text, e.headers

    def version(self, token: str = TOKEN) -> str:
        code, body, _ = self.request("/api/version", token=token)
        return body["version"] if code == 200 else ""

    def get(self, path: str, **kw):
        return self.request(path, **kw)[:2]

    def page(self, **kw):
        return self.request(f"/?token={TOKEN}", token=None, **kw)

    def put(self, path: str, body, **kw):
        return self.request(path, "PUT", body, **kw)[:2]

    def post(self, path: str, body=None, **kw):
        return self.request(path, "POST", {} if body is None else body, **kw)[:2]


@contextmanager
def serving(wd, rewriter=None, judge=None):
    server = make_server(WriteApp(wd, rewriter, 3, judge), 0, TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield Client(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()


def settings():
    return load(None, Path.cwd())


def defaults():
    return design_defaults(settings())


def always_ask() -> list[str]:
    return settings().get("interview.always_ask") or []


def roles() -> dict[str, str]:
    return settings().roles()


def run_cli(*args, code: int = 0) -> str:
    res = CliRunner().invoke(app, [*map(str, args)])
    assert res.exit_code == code, res.output
    return res.output


def cli_stdout(*args) -> str:
    res = CliRunner().invoke(app, [*map(str, args)])
    assert res.exit_code == 0, res.output
    return res.stdout


VOTES = Votes(3, 2)
ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "samples"
PROJECT = Project(topic="写真の名前を撮影日時にそろえる", audience="写真の整理に困っている人", kind="実用", length=600)

GOOD_DRAFT = """# 写真の名前を撮影日時にそろえる

スマホとデジカメで IMG_1234.JPG と DSC01234.JPG のように名前がばらばらでした。

試したら、スマホの写真 312 枚のうち 9 枚に撮影日時がありませんでした。どれも LINE で受け取った写真で、LINE 経由の写真は EXIF が消えています。そこでファイルの更新日時で代用しました。更新日時は写真を保存した日時なので、撮影日とは数日ずれることがあります。それでも並び順はだいたい保たれるので、私はこれで十分だと判断しました。

`exiftool -d '%Y%m%d-%H%M%S%%-c.%%e' '-FileName<DateTimeOriginal' DIR` の 1 行で済みます。書式は [exiftool の説明](https://exiftool.org/filename.html#codes) にあります。
"""

BAD_DRAFT = """# 写真の名前を撮影日時にそろえる

この記事では、写真の名前をそろえる方法を見ていきましょう。

- **EXIF**: 撮影日時を記録する仕組みです 📷
- **タイムゾーン**: 海外で撮った写真は現地時刻のまま並びます

私は 2000 枚の写真で試してみたところ、3 時間かかりました。
"""


def surface_fake(prompt: str) -> str:
    return json.dumps({"items": [{"id": m[1], "category": m[2]}
                                 for m in re.finditer(r"^\[([MH]\d+)\] .*?← 規則: (\w+)$", prompt, re.MULTILINE)]})


def scripted(draft_text: str = GOOD_DRAFT, present_drop: bool = False, takeaway_ok: bool = True, deep_unit_chars: bool = True,
             skip_explained: bool = False, terms: list[dict] | None = None, reader: list[dict] | None = None):
    def respond(prompt: str) -> str:
        if "著者が材料（番号付きの単位）に付けた一言です" in prompt:
            ids = re.findall(r"^\[([mq]\d+)\] 材料:", prompt, re.MULTILINE)
            notes = dict(re.findall(r"^\[([mq]\d+)\] 材料:.*?\n一言: (.*)$", prompt, re.MULTILINE))
            return json.dumps({"notes": [{"id": i, "thin": "yes" if "面白い" in notes.get(i, "") else "no",
                                          "question": f"[{i}] {notes.get(i, '')[:4]}って、どのへんが？"} for i in ids]})
        if "掘り下げると決めた材料と著者の回答" in prompt:
            return json.dumps({"units": [{"id": "m4", "result": "yes", "label": "LINE の写真に撮影日時が無い"},
                                         {"id": "q1", "result": "yes", "label": "[q1] 意外だった"},
                                         {"id": "m5", "result": "no", "label": "手順"},
                                         {"id": "m2", "result": "yes", "label": "deep でない"}]})
        if "この読者が説明なしでは分からないもの" in prompt:
            return json.dumps({"terms": terms or []})
        if "この読者になりきって" in prompt:
            return json.dumps({"findings": reader or []})
        if "ウェブで下調べ" in prompt:
            return json.dumps({"findings": [
                {"topic": 1, "claim": "exiftool の -d は strftime の書式を受け取る。", "source": "https://exiftool.org/filename.html"},
                {"topic": 1, "claim": "出典の無い主張", "source": "not a url"},
                {"topic": 9, "claim": "LINE は画像の EXIF を消す。", "source": "https://example.com/line"}]})
        if "本題に要らない話が混じって" in prompt:
            return json.dumps({"skip": [{"label": "EXIF とは何か", "units": ["m2", "m4", "m99"], "why": "一般的"},
                                        {"label": "x" * 40, "units": [], "why": ""},
                                        {"label": "タイムゾーンの仕組み", "units": [], "why": "書き足しがち"}],
                               "aside": [{"id": "m7", "where": "名前を付け終えたあと", "why": "気にしないと決めた"},
                                         {"id": "m3", "where": "", "why": "検索で届く"}]})
        if "情報を運ばない文と見出しを選び" in prompt:
            return surface_fake(prompt)
        if "同じ情報を述べている単位の組" in prompt:
            return json.dumps({"groups": [["m9", "m2", "m9"], ["m3", "zz"]]})
        if "# 使わない材料" in prompt:
            return json.dumps({"conflicts": [{"id": "m3", "level": "yes", "by": ["m12", "m2"], "note": "コマンドが出る"},
                                             {"id": "m4", "level": "yes", "by": ["m5"], "note": ""},
                                             {"id": "m2", "level": "no", "by": [], "note": ""}],
                               "avoid": ["写真管理アプリの比較", "FAQ", "x" * 40]})
        if "一覧の文だけを直して" in prompt:
            ids = re.findall(r"^\[(F\d+)\]", prompt, re.MULTILINE)
            return json.dumps({"items": [{"id": i, "replacement": "" if k == 0 else "撮影日時は EXIF にあります。"}
                                         for k, i in enumerate(ids)]})
        if "下書きの単位" in prompt:
            n = len(re.findall(r"^\[(\d+)\]（", prompt.split("# 下書きの単位", 1)[1], re.MULTILINE))
            units = []
            for i in range(1, n + 1):
                if i == 2 and deep_unit_chars:
                    units.append({"id": i, "from": ["m4", "m5", "q1"], "firsthand": "yes"})
                elif i == 2:
                    units.append({"id": i, "from": [], "firsthand": "yes"})
                else:
                    units.append({"id": i, "from": ["m1"] if i == 1 else ["m3"], "firsthand": "no"})
            return json.dumps({"units": units, "takeaways": [{"index": k, "present": "yes" if takeaway_ok else "no",
                                                               "evidence": [2]} for k in (1, 2, 3)],
                               "skips": [{"index": k, "explained": "yes" if skip_explained and k == 1 else "no",
                                          "evidence": [1]} for k in (1, 2)]})
        if "# 対象の記事の単位" in prompt:
            ids = [int(x) for x in re.findall(r"^\[(\d+)\]（", prompt.split("# 対象の記事の単位", 1)[1], re.MULTILINE)]
            if "## W" in prompt:
                return json.dumps({"units": [{"id": i, "v": "yes" if i in (2, 3) else "no", "in": ["W"]}
                                             for i in ids]})
            return json.dumps({"units": [{"id": i, "v": "yes" if (i not in (2, 3) or present_drop) else "no", "in": ["D"]}
                                         for i in ids]})
        if "質問を" in prompt:
            return json.dumps({"questions": [
                {"question": "[m4] の件で、LINE で受け取った写真だけ EXIF が消えていると気づいたとき、何を考えましたか",
                 "context": "LINE 経由の写真は EXIF が消えていて、更新日時で代用した（m4）", "why": "deep の候補",
                 "units": ["m4", "m99"]},
                {"question": "読者に 1 つだけ持ち帰ってもらうなら何ですか", "why": "持ち帰り", "units": []},
                {"question": "", "why": "", "units": []}]})
        if "記事の設計を提案" in prompt:
            return json.dumps({"purpose": "撮影日時で名前をそろえる手順と、EXIF の無い写真の扱いが分かる",
                               "takeaways": ["EXIF の無い写真は更新日時で代用する", "二つ目", "三つ目", "四つ目"],
                               "units": [{"id": "m2", "use": "drop", "why": "一般的"},
                                         {"id": "m4", "use": "deep", "why": "手元だけ"},
                                         {"id": "q1", "use": "deep", "why": "回答"},
                                         {"id": "m5", "use": "deep", "why": "手元だけ"},
                                         {"id": "zz", "use": "deep", "why": "無い単位"}],
                               "order": ["m4 から始める"], "forms": ["m3 はコード"],
                               "research": ["exiftool の -d の書式", "x" * 80]})
        if "直す点" in prompt:
            return f"<article>{GOOD_DRAFT}</article>"
        if "一緒に決めた設計" in prompt:
            return f"<article>{draft_text}</article>"
        if "ウェブ検索" in prompt:
            return "<article># 写真の名前\n\nEXIF の DateTimeOriginal を exiftool で読みます。</article>"
        if '{"items"' in prompt:
            return '{"items": []}'
        raise AssertionError(prompt[:200])

    return FakeProvider(respond)

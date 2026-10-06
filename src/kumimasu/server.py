from __future__ import annotations

import json
import os
import secrets
import threading
import traceback
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, unquote, urlparse

from pydantic import ValidationError

from . import ops
from .check import check_stem
from .design import sync_design
from .draft import read_used
from .errors import LLMError, StepError
from .figures import figure_markers
from .interview import SEARCHABLE_LABEL
from .land import NOTE_QUESTION
from .model import REGISTER_LABEL, USE_LABEL
from .render import render
from .review import (
    DECISION_LABEL,
    ITEM_KIND_LABEL,
    ReviewContext,
    base_drafts,
    download_name,
    final_changes,
    final_name,
    require_base,
)
from .terms import material_load, term_states
from .workdir import WorkDir

if TYPE_CHECKING:
    from .llm import Provider

HOST = "127.0.0.1"
SOURCE = "human-ui"
MAX_BODY = 2 * 1024 * 1024
TOKEN_HEADER = "X-Kumimasu-Token"
VERSION_HEADER = "X-Kumimasu-Version"
TOKEN_SLOT = b"{{KUMIMASU_TOKEN}}"
NONCE_SLOT = b"{{KUMIMASU_NONCE}}"
REQUEST_TIMEOUT = 30
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
PAGE_CSP = ("default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
TOKEN_KEY = "kumimasu-token"
PAGE_PATHS = ("/", "/index.html")
NO_TOKEN = "kumimasu serve が表示した URL（token 付き）を開いてください"
# The page drops the token from the address bar and keeps it in this tab's sessionStorage, so a reload lands here.
RELOAD_PAGE = ('<!doctype html><meta charset="utf-8"><title>kumimasu</title><p>' + NO_TOKEN + '</p>'
               '<script nonce="{{KUMIMASU_NONCE}}">try { const t = sessionStorage.getItem("' + TOKEN_KEY + '"); '
               'if (t) location.replace("/?token=" + encodeURIComponent(t)); } catch (e) {}</script>').encode()
LABELS = {"searchable": SEARCHABLE_LABEL, "use": USE_LABEL, "decision": DECISION_LABEL, "kind": ITEM_KIND_LABEL,
          "register": REGISTER_LABEL, "human_stages": list(ops.HUMAN_STAGES), "note_question": NOTE_QUESTION}


class HttpError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class NotFound(HttpError):
    def __init__(self) -> None:
        super().__init__(404, "見つかりません")


class NoToken(HttpError):
    def __init__(self) -> None:
        super().__init__(403, NO_TOKEN)


class Stale(HttpError):
    def __init__(self) -> None:
        super().__init__(409, "ほかの所（エージェントや別のタブ）で先に変更されています")


def text_field(body: dict, key: str) -> str:
    value = body.get(key, "")
    if not isinstance(value, str):
        raise TypeError(f"{key} は文字列にしてください")
    return value


class WriteApp:
    def __init__(self, wd: WorkDir, rewriter: Callable[[], Provider] | None, poll_seconds: float,
                 judge: Callable[[], Provider] | None = None) -> None:
        self.wd = wd
        self.rewriter = rewriter
        self.judge = judge
        self.poll_seconds = poll_seconds
        self.local = threading.local()

    def write(self, sent: str, call: Callable[[], dict]) -> dict:
        pre = self.local.pre = ops.Prefetch(self.wd)

        def step() -> dict:
            if sent != ops.version(self.wd):
                raise Stale
            return call()
        return pre.run(step)

    def provider(self, make: Callable[[], Provider]) -> Provider:
        return self.local.pre.provider(make)

    def drafts(self) -> dict:
        return {"drafts": [{"name": b, "final": final_name(b) if self.wd.is_plain_file(final_name(b)) else None}
                           for b in base_drafts(self.wd)]}

    def draft_name(self, name: str) -> str:
        try:
            require_base(name)
        except ValueError:
            raise NotFound from None
        if not self.wd.is_plain_file(name):
            raise NotFound
        return name

    def final_file(self, name: str) -> str:
        out = final_name(name)
        if not self.wd.is_plain_file(out):
            raise NotFound
        return out

    def public(self, message: str) -> str:
        for root in {str(self.wd.root.resolve()), str(self.wd.root)}:
            message = message.replace(root + os.sep, "").replace(root, ".")
        return message

    def _final(self, ctx: ReviewContext) -> dict:
        out = self.final_file(ctx.draft)
        text = self.wd.read(out)
        return {"draft": ctx.draft, "final": out, "markdown": text, "html": render(text)[0],
                "changes": final_changes(ctx.src, text, ctx.review)}

    def final(self, name: str) -> dict:
        return self._final(ReviewContext.load(self.wd, self.draft_name(name)))

    def download(self, name: str) -> tuple[str, bytes]:
        name = self.draft_name(name)
        return download_name(self.wd, name), self.wd.read(self.final_file(name)).encode()

    def _review(self, ctx: ReviewContext, version: str, with_html: bool = True) -> dict:
        checked = bool(ctx.review.items) or any((self.wd.root / f"{check_stem(ctx.draft, s)}.json").exists()
                                               for s in (False, True))
        return ({"html": render(ctx.src)[0]} if with_html else {}) | {
            "draft": ctx.draft, "items": [i.model_dump() for i in ctx.review.items], "dirty": ctx.needs_apply(),
            "used": read_used(self.wd, ctx.draft), "checked": checked, "version": version,
            "figures": figure_markers(ctx.src)}

    def review(self, name: str) -> dict:
        name = self.draft_name(name)
        v = ops.version(self.wd)
        return self._review(ReviewContext.load(self.wd, name), v)

    def save_review(self, name: str, body: dict) -> dict:
        name = self.draft_name(name)
        rev = ops.decide(self.wd, name, body, SOURCE)
        return self._review(ReviewContext(self.wd, name, self.wd.read(name), rev), ops.version(self.wd), with_html=False)

    def apply(self, name: str) -> dict:
        name = self.draft_name(name)
        ctx = ReviewContext.load(self.wd, name)
        res = ops.apply(self.wd, name, self.provider(self.rewriter) if ctx.needs_rewrite_call() and self.rewriter else None,
                        SOURCE, ctx=ctx)
        after = ReviewContext.load(self.wd, name)
        return {"result": res.model_dump(), "review": self._review(after, ops.version(self.wd), with_html=False), "final": self._final(after)}

    def confirm(self, body: dict) -> dict:
        rewriter = self.rewriter
        handoff = ops.confirm(self.wd, SOURCE, text_field(body, "note"),
                              (lambda: self.provider(rewriter)) if rewriter else None)
        return {"handoff": handoff} | self.state()

    def restart(self, body: dict) -> dict:
        from_stage, mode = body.get("from"), body.get("mode")
        if not isinstance(from_stage, str) or not isinstance(mode, str):
            raise TypeError("from と mode は文字列にしてください")
        event = ops.restart(self.wd, from_stage, SOURCE, mode, text_field(body, "note"))
        return {"restart": event} | self.state()

    def state(self) -> dict:
        wd = self.wd
        v = ops.version(wd)
        units = wd.units()
        wanted = {x for u in units for x in u.from_units}
        design = sync_design(wd.design(), units) if wd.design_file.exists() else None
        return {"project": wd.project().model_dump(), "version": v, "stage": ops.stage_info(wd),
                "poll_seconds": self.poll_seconds, "labels": LABELS,
                "units": [u.model_dump() | {"firsthand": u.firsthand} for u in units],
                "originals": {u.id: u.model_dump() for u in wd.raw_units() if u.id in wanted} if wanted else {},
                "interview": wd.interview().model_dump() if wd.interview_file.exists() else None,
                "design": design.model_dump() if design else None,
                "conflicts": [c.model_dump() for c in design.live_conflicts()] if design else [],
                "terms": term_states(design) if design else [], "load": material_load(design, units) if design else None}

    def save_answers(self, body: dict) -> dict:
        answers = body.get("answers")
        if not isinstance(answers, dict):
            raise TypeError("answers はオブジェクトにしてください")
        ops.save_answers(self.wd, answers, SOURCE)
        return self.state()

    def followup(self, body: dict) -> dict:
        if self.judge is None:
            raise StepError("聞き返しの判定役が設定されていません")
        ops.followup(self.wd, self.provider(self.judge), SOURCE)
        return self.state()

    def save_design(self, body: dict) -> dict:
        ops.update_design(self.wd, body, SOURCE)
        return self.state()


def make_handler(app: WriteApp, token: str) -> type[BaseHTTPRequestHandler]:
    page = resources.files("kumimasu").joinpath("index.html").read_bytes().replace(TOKEN_SLOT, token.encode())
    token_bytes = token.encode()
    exact = {("GET", "/api/drafts"): lambda body: app.drafts(),
             ("GET", "/api/state"): lambda body: app.state(),
             ("POST", "/api/confirm"): app.confirm,
             ("POST", "/api/restart"): app.restart,
             ("POST", "/api/followup"): app.followup,
             ("PUT", "/api/interview"): app.save_answers,
             ("PUT", "/api/design"): app.save_design}
    named = {("GET", "review"): lambda name, body: app.review(name),
             ("PUT", "review"): app.save_review,
             ("GET", "final"): lambda name, body: app.final(name),
             ("POST", "apply"): lambda name, body: app.apply(name)}

    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT

        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str | None, headers: dict | None = None) -> None:
            self.send_response(code)
            for k, v in (SECURITY_HEADERS | (headers or {})).items():
                self.send_header(k, v)
            if ctype is not None:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200, headers: dict | None = None) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", headers)

        def _page(self, code: int, html: bytes) -> None:
            nonce = secrets.token_urlsafe(16)
            self._send(code, html.replace(NONCE_SLOT, nonce.encode()), "text/html; charset=utf-8",
                       {"Content-Security-Policy": PAGE_CSP.format(nonce=nonce)})

        def _check(self, path: str, query: str) -> None:
            port = self.server.server_address[1]
            hosts = {f"{HOST}:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in hosts:
                raise HttpError(421, "Host が違います")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{h}" for h in hosts}:
                raise HttpError(403, "別のオリジンからの要求は受けません")
            if path.startswith("/api/"):
                if self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
                    raise HttpError(403, "別のオリジンからの要求は受けません")
                if not secrets.compare_digest(self.headers.get(TOKEN_HEADER, "").encode(), token_bytes):
                    raise HttpError(403, "トークンがありません")
            elif "token" not in (q := parse_qs(query)) and path in PAGE_PATHS:
                raise NoToken
            elif not secrets.compare_digest(q.get("token", [""])[0].encode(), token_bytes):
                raise HttpError(403, NO_TOKEN)

        def _body(self) -> dict:
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                raise HttpError(415, "Content-Type は application/json にしてください")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise HttpError(400, "Content-Length が数ではありません") from None
            if n < 0:
                raise HttpError(400, "Content-Length が負です")
            if n > MAX_BODY:
                raise HttpError(413, "本文が大きすぎます")
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                raise HttpError(400, "本文が JSON ではありません") from None
            if not isinstance(data, dict):
                raise HttpError(400, "本文は JSON のオブジェクトにしてください")
            return data

        def _route(self, method: str, path: str) -> None:
            if method == "GET" and path in PAGE_PATHS:
                self._page(200, page)
                return
            if method == "GET" and path == "/api/version":
                v = ops.version(app.wd)
                etag = {"ETag": f'"{v}"'}
                if self.headers.get("If-None-Match") == etag["ETag"]:
                    self._send(304, b"", None, etag)
                else:
                    self._json({"version": v} | ops.stage_info(app.wd), headers=etag)
                return
            parts = [unquote(x) for x in path.strip("/").split("/")]
            if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "download"]:
                fname, data = app.download(parts[2])
                self._send(200, data, "text/markdown; charset=utf-8",
                           {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})
                return
            if (handler := exact.get((method, path))) is not None:
                call = handler
            elif len(parts) == 3 and parts[0] == "api" and (named_handler := named.get((method, parts[1]))) is not None:
                def call(body: dict) -> dict:
                    return named_handler(parts[2], body)
            else:
                raise NotFound
            if method == "GET":
                self._json(call({}))
                return
            body = self._body()
            sent = self.headers.get(VERSION_HEADER)
            if sent is None:
                raise HttpError(428, f"{VERSION_HEADER} がありません")
            self._json(app.write(sent, lambda: call(body)))

        def _handle(self, method: str) -> None:
            url = urlparse(self.path)
            try:
                self._check(url.path, url.query)
                self._route(method, url.path)
            except NoToken:
                self._page(403, RELOAD_PAGE)
            except HttpError as e:
                self._json({"error": str(e)} | ({"stale": True} if isinstance(e, Stale) else {}), e.code)
            except StepError as e:
                self._json({"error": app.public(str(e))}, 409)
            except (ValueError, TypeError, ValidationError, LLMError) as e:
                self._json({"error": app.public(str(e))}, 400)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self._json({"error": "サーバーの内部エラーです"}, 500)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PUT(self) -> None:
            self._handle("PUT")

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets another process bind the same port and take the connections.
    allow_reuse_address = os.name != "nt"


def make_server(app: WriteApp, port: int, token: str) -> Server:
    return Server((HOST, port), make_handler(app, token))


def serve(app: WriteApp, port: int) -> None:
    token = secrets.token_urlsafe(32)
    try:
        server = make_server(app, port, token)
    except OSError as e:
        raise StepError(f"{HOST}:{port} で待ち受けられません（{e.strerror or e}）。--port で別のポートを指定してください") from None
    print(f"kumimasu: http://{HOST}:{server.server_address[1]}/?token={token}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

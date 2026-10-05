from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse

from pydantic import ValidationError

from . import ops
from .check import check_stem
from .design import sync_design
from .draft import read_used
from .errors import LLMError, StepError
from .interview import SEARCHABLE_LABEL
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
    is_final,
)
from .workdir import DRAFT_NAME, WorkDir

if TYPE_CHECKING:
    from .llm import Provider

HOST = "127.0.0.1"
SOURCE = "human-ui"
MAX_BODY = 2 * 1024 * 1024
TOKEN_HEADER = "X-Kumimasu-Token"
TOKEN_SLOT = b"{{KUMIMASU_TOKEN}}"
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'; default-src 'self'; script-src 'self' 'unsafe-inline'; "
                               "style-src 'self' 'unsafe-inline'; img-src 'self' data:",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
LABELS = {"searchable": SEARCHABLE_LABEL, "use": USE_LABEL, "decision": DECISION_LABEL, "kind": ITEM_KIND_LABEL,
          "register": REGISTER_LABEL, "human_stages": list(ops.HUMAN_STAGES)}


class WriteApp:
    def __init__(self, wd: WorkDir, rewriter: Callable[[], Provider] | None, poll_seconds: float) -> None:
        self.wd = wd
        self.rewriter = rewriter
        self.poll_seconds = poll_seconds

    def drafts(self) -> dict:
        return {"drafts": [{"name": b, "final": final_name(b) if self.wd.is_plain_file(final_name(b)) else None}
                           for b in base_drafts(self.wd)]}

    def draft_name(self, name: str) -> str:
        if not DRAFT_NAME.match(name) or name.endswith(".prompt.md") or is_final(name) or not self.wd.is_plain_file(name):
            raise KeyError(name)
        return name

    def public(self, message: str) -> str:
        for root in {str(self.wd.root.resolve()), str(self.wd.root)}:
            message = message.replace(root + os.sep, "").replace(root, ".")
        return message

    def _final(self, ctx: ReviewContext) -> dict:
        out = final_name(ctx.draft)
        if not self.wd.is_plain_file(out):
            raise KeyError(out)
        text = self.wd.read(out)
        return {"draft": ctx.draft, "final": out, "markdown": text, "html": render(text)[0],
                "changes": final_changes(ctx.src, text, ctx.review)}

    def final(self, name: str) -> dict:
        return self._final(ReviewContext.load(self.wd, self.draft_name(name)))

    def download(self, name: str) -> tuple[str, bytes]:
        name = self.draft_name(name)
        out = final_name(name)
        if not self.wd.is_plain_file(out):
            raise KeyError(out)
        return download_name(self.wd, name), self.wd.read(out).encode()

    def _review(self, ctx: ReviewContext, with_html: bool = True) -> dict:
        checked = bool(ctx.review.items) or any((self.wd.root / f"{check_stem(ctx.draft, s)}.json").exists()
                                               for s in (False, True))
        return ({"html": render(ctx.src)[0]} if with_html else {}) | {
            "draft": ctx.draft, "items": [i.model_dump() for i in ctx.review.items], "dirty": ctx.needs_apply(),
            "used": read_used(self.wd, ctx.draft), "checked": checked, "version": ops.version(self.wd)}

    def review(self, name: str) -> dict:
        return self._review(ReviewContext.load(self.wd, self.draft_name(name)))

    def save_review(self, name: str, body: dict) -> dict:
        name = self.draft_name(name)
        rev = ops.decide(self.wd, name, body, SOURCE)
        return self._review(ReviewContext(self.wd, name, self.wd.read(name), rev), with_html=False)

    def apply(self, name: str) -> dict:
        name = self.draft_name(name)
        with ops.LOCK:
            ctx = ReviewContext.load(self.wd, name)
            res = ops.apply(self.wd, name, self.rewriter() if ctx.needs_rewrite_call() and self.rewriter else None, SOURCE,
                            ctx=ctx)
            after = ReviewContext.load(self.wd, name)
        return {"result": res.model_dump(), "review": self._review(after, with_html=False), "final": self._final(after)}

    def confirm(self, body: dict) -> dict:
        handoff = ops.confirm(self.wd, SOURCE, str(body.get("note", "")), self.rewriter)
        return {"handoff": handoff} | self.state()

    def version(self) -> str:
        return ops.version(self.wd)

    def stage(self) -> dict:
        return ops.stage_info(self.wd)

    def state(self) -> dict:
        wd = self.wd
        units = wd.units()
        design = sync_design(wd.design(), units) if wd.design_file.exists() else None
        return {"project": wd.project().model_dump(), "version": ops.version(wd), "stage": ops.stage_info(wd),
                "poll_seconds": self.poll_seconds, "labels": LABELS,
                "units": [u.model_dump() | {"firsthand": u.firsthand} for u in units],
                "interview": wd.interview().model_dump() if wd.interview_file.exists() else None,
                "design": design.model_dump() if design else None,
                "conflicts": [c.model_dump() for c in design.live_conflicts()] if design else []}

    def save_answers(self, body: dict) -> dict:
        answers = body.get("answers")
        if not isinstance(answers, dict):
            raise TypeError("answers はオブジェクトにしてください")
        ops.save_answers(self.wd, answers, SOURCE)
        return self.state()

    def save_design(self, body: dict) -> dict:
        ops.update_design(self.wd, body, SOURCE)
        return self.state()


class HttpError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


ERRORS: tuple[tuple[tuple[type[Exception], ...], int], ...] = (
    ((KeyError,), 404),
    ((StepError,), 409),
    ((ValueError, TypeError, ValidationError, LLMError), 400),
)


def make_handler(app: WriteApp, token: str) -> type[BaseHTTPRequestHandler]:
    page = resources.files("kumimasu").joinpath("index.html").read_bytes().replace(TOKEN_SLOT, token.encode())
    exact = {("GET", "/api/drafts"): lambda body: app.drafts(),
             ("GET", "/api/state"): lambda body: app.state(),
             ("POST", "/api/confirm"): app.confirm,
             ("PUT", "/api/interview"): app.save_answers,
             ("PUT", "/api/design"): app.save_design}
    named = {("GET", "review"): lambda name, body: app.review(name),
             ("PUT", "review"): app.save_review,
             ("GET", "final"): lambda name, body: app.final(name),
             ("POST", "apply"): lambda name, body: app.apply(name)}

    class Handler(BaseHTTPRequestHandler):
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

        def _check(self, path: str) -> None:
            port = self.server.server_address[1]
            hosts = {f"{HOST}:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in hosts:
                raise HttpError(421, "Host が違います")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{h}" for h in hosts}:
                raise HttpError(403, "別のオリジンからの要求は受けません")
            if path.startswith("/api/") and not secrets.compare_digest(self.headers.get(TOKEN_HEADER, ""), token):
                raise HttpError(403, "トークンがありません")

        def _body(self) -> dict:
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
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
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise HttpError(400, "本文が JSON ではありません") from None
            if not isinstance(data, dict):
                raise HttpError(400, "本文は JSON のオブジェクトにしてください")
            return data

        def _route(self, method: str, path: str) -> None:
            if method == "GET" and path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
                return
            if method == "GET" and path == "/api/version":
                v = app.version()
                etag = {"ETag": f'"{v}"'}
                if self.headers.get("If-None-Match") == etag["ETag"]:
                    self._send(304, b"", None, etag)
                else:
                    self._json({"version": v} | app.stage(), headers=etag)
                return
            parts = path.strip("/").split("/")
            if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "download"]:
                fname, data = app.download(parts[2])
                self._send(200, data, "text/markdown; charset=utf-8",
                           {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})
                return
            body = self._body() if method in ("POST", "PUT") else {}
            if (handler := exact.get((method, path))) is not None:
                self._json(handler(body))
            elif len(parts) == 3 and parts[0] == "api" and (named_handler := named.get((method, parts[1]))) is not None:
                self._json(named_handler(parts[2], body))
            else:
                raise KeyError(path)

        def _handle(self, method: str) -> None:
            path = urlparse(self.path).path
            try:
                self._check(path)
                self._route(method, path)
            except HttpError as e:
                self._json({"error": str(e)}, e.code)
            except Exception as e:  # noqa: BLE001
                code = next((c for kinds, c in ERRORS if isinstance(e, kinds)), 500)
                message = "見つかりません" if code == 404 else app.public(str(e)) if code != 500 else "サーバーの内部エラーです"
                self._json({"error": message}, code)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PUT(self) -> None:
            self._handle("PUT")

    return Handler


def make_server(app: WriteApp, port: int, token: str | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, port), make_handler(app, token or secrets.token_urlsafe(32)))
    server.daemon_threads = True
    return server


def serve(app: WriteApp, port: int) -> None:
    server = make_server(app, port)
    print(f"kumimasu: http://{HOST}:{server.server_address[1]}/  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

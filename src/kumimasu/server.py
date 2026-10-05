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
from .render import render
from .review import (
    ReviewContext,
    base_drafts,
    download_name,
    final_changes,
    final_name,
    is_final,
    load_review,
)
from .workdir import DRAFT_NAME, WorkDir

if TYPE_CHECKING:
    from .llm import Provider

HOST = "127.0.0.1"
SOURCE = "human-ui"


class WriteApp:
    def __init__(self, wd: WorkDir, rewriter: Callable[[], Provider] | None = None, poll_seconds: float = 3.0) -> None:
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

    def final(self, name: str) -> dict:
        name = self.draft_name(name)
        out = final_name(name)
        if not self.wd.is_plain_file(out):
            raise KeyError(out)
        text = self.wd.read(out)
        changes = final_changes(self.wd.read(name), text, load_review(self.wd, name))
        return {"draft": name, "final": out, "markdown": text, "html": render(text)[0], "changes": changes}

    def download(self, name: str) -> tuple[str, bytes]:
        name = self.draft_name(name)
        out = final_name(name)
        if not self.wd.is_plain_file(out):
            raise KeyError(out)
        return download_name(self.wd, name), self.wd.read(out).encode()

    def review(self, name: str) -> dict:
        name = self.draft_name(name)
        ctx = ReviewContext.load(self.wd, name)
        rev = ctx.review
        html, _ = render(ctx.src)

        return {"draft": name, "html": html, "items": [i.model_dump() for i in rev.items], "dirty": ctx.needs_apply(),
                "used": read_used(self.wd, name),
                "checked": bool(rev.items) or any((self.wd.root / f).exists() for f in self._check_files(name))}

    def _check_files(self, name: str) -> list[str]:
        return [f"{check_stem(name, s)}.json" for s in (False, True)]

    def save_review(self, name: str, body: dict) -> dict:
        name = self.draft_name(name)
        ops.decide(self.wd, name, body, SOURCE)
        return self.review(name)

    def apply(self, name: str) -> dict:
        name = self.draft_name(name)
        with ops.LOCK:
            ctx = ReviewContext.load(self.wd, name)
            res = ops.apply(self.wd, name, self.rewriter() if ctx.needs_rewrite_call() and self.rewriter else None, SOURCE,
                            ctx=ctx)
        return res.model_dump()

    def confirm(self, body: dict) -> dict:
        handoff = ops.confirm(self.wd, SOURCE, str(body.get("note", "")), self.rewriter)
        return {"handoff": handoff} | self.state()

    def version(self) -> dict:
        return {"version": ops.version(self.wd)} | ops.stage_info(self.wd)

    def state(self) -> dict:
        wd = self.wd
        units = wd.units()
        design = sync_design(wd.design(), units) if wd.design_file.exists() else None
        return {"project": wd.project().model_dump(), "version": ops.version(wd), "stage": ops.stage_info(wd),
                "poll_seconds": self.poll_seconds,
                "units": [u.model_dump() | {"firsthand": u.firsthand} for u in units],
                "interview": wd.interview().model_dump() if wd.interview_file.exists() else None,
                "design": design.model_dump() if design else None,
                "warnings": [c.message() for c in design.live_conflicts()] if design else []}

    def save_answers(self, body: dict) -> dict:
        answers = body.get("answers")
        if not isinstance(answers, dict):
            raise TypeError("answers はオブジェクトにしてください")
        ops.save_answers(self.wd, answers, SOURCE)
        return self.state()

    def save_design(self, body: dict) -> dict:
        ops.update_design(self.wd, body, SOURCE)
        return self.state()


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


class HttpError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def make_handler(app: WriteApp, token: str) -> type[BaseHTTPRequestHandler]:
    page = resources.files("kumimasu").joinpath("index.html").read_bytes().replace(TOKEN_SLOT, token.encode())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(code)
            for k, v in (SECURITY_HEADERS | (headers or {})).items():
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _origins(self) -> set[str]:
            port = self.server.server_address[1]
            return {f"{HOST}:{port}", f"localhost:{port}"}

        def _check(self, path: str) -> None:
            hosts = self._origins()
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

        def _handle(self, method: str) -> None:
            path = urlparse(self.path).path
            try:
                self._check(path)
                getattr(self, f"_{method}")(path)
            except HttpError as e:
                self._json({"error": str(e)}, e.code)
            except KeyError:
                self._json({"error": "見つかりません"}, 404)
            except StepError as e:
                self._json({"error": app.public(str(e))}, 409)
            except (ValueError, TypeError, ValidationError, LLMError) as e:
                self._json({"error": app.public(str(e))}, 400)
            except Exception:  # noqa: BLE001
                self._json({"error": "サーバーの内部エラーです"}, 500)

        def do_GET(self) -> None:
            self._handle("get")

        def do_POST(self) -> None:
            self._handle("post")

        def do_PUT(self) -> None:
            self._handle("put")

        def _get(self, path: str) -> None:
            if path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/api/drafts":
                self._json(app.drafts())
            elif (name := self._review_name(path)) is not None:
                self._json(app.review(name))
            elif (name := self._review_name(path, "final")) is not None:
                self._json(app.final(name))
            elif (name := self._review_name(path, "download")) is not None:
                fname, body = app.download(name)
                self._send(200, body, "text/markdown; charset=utf-8",
                           {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})
            elif path == "/api/version":
                v = app.version()
                if self.headers.get("If-None-Match") == f'"{v["version"]}"':
                    self.send_response(304)
                    self.send_header("ETag", f'"{v["version"]}"')
                    self.end_headers()
                    return
                self._send(200, json.dumps(v, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                           {"ETag": f'"{v["version"]}"'})
            elif path == "/api/state":
                self._json(app.state())
            else:
                raise KeyError(path)

        def _review_name(self, path: str, prefix: str = "review") -> str | None:
            parts = path.strip("/").split("/")
            return parts[2] if len(parts) == 3 and parts[:2] == ["api", prefix] else None

        def _post(self, path: str) -> None:
            body = self._body()
            if path == "/api/confirm":
                self._json(app.confirm(body))
            elif (name := self._review_name(path, "apply")) is not None:
                self._json(app.apply(name))
            else:
                raise KeyError(path)

        def _put(self, path: str) -> None:
            body = self._body()
            if (name := self._review_name(path)) is not None:
                self._json(app.save_review(name, body))
            elif path == "/api/interview":
                self._json(app.save_answers(body))
            elif path == "/api/design":
                self._json(app.save_design(body))
            else:
                raise KeyError(path)

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

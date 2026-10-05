from __future__ import annotations

import json
import re
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse

from pydantic import ValidationError

from .llm import LLMError
from . import ops
from .design import sync_design
from .workdir import StepError, WorkDir

if TYPE_CHECKING:
    from .llm import Provider

HOST = "127.0.0.1"
SOURCE = "human-ui"


DRAFT_NAME = re.compile(r"^draft[\w.\-]*\.md$")


class WriteApp:
    def __init__(self, wd: WorkDir, rewriter: Callable[[], Provider] | None = None, poll_seconds: float = 3.0) -> None:
        self.wd = wd
        self.rewriter = rewriter
        self.poll_seconds = poll_seconds

    def drafts(self) -> dict:
        from .review import base_drafts, final_name

        return {"drafts": [{"name": b, "final": final_name(b) if (self.wd.root / final_name(b)).is_file() else None}
                           for b in base_drafts(self.wd)]}

    def draft_name(self, name: str) -> str:
        from .review import is_final

        if not DRAFT_NAME.match(name) or name.endswith(".prompt.md") or is_final(name) \
                or not (self.wd.root / name).is_file():
            raise KeyError(name)
        return name

    def final(self, name: str) -> dict:
        from .render import render
        from .review import final_changes, final_name, load_review

        name = self.draft_name(name)
        out = final_name(name)
        if not (self.wd.root / out).is_file():
            raise KeyError(out)
        text = self.wd.read(out)
        changes = final_changes(self.wd.read(name), text, load_review(self.wd, name))
        return {"draft": name, "final": out, "markdown": text, "html": render(text)[0], "changes": changes}

    def download(self, name: str) -> tuple[str, bytes]:
        from .review import download_name, final_name

        name = self.draft_name(name)
        out = self.wd.root / final_name(name)
        if not out.is_file():
            raise KeyError(out.name)
        return download_name(self.wd, name), out.read_bytes()

    def review(self, name: str) -> dict:
        from .render import render
        from .review import load_review, needs_apply

        name = self.draft_name(name)
        html, _ = render(self.wd.read(name))
        rev = load_review(self.wd, name)
        from .draft import read_used

        return {"draft": name, "html": html, "items": [i.model_dump() for i in rev.items], "dirty": needs_apply(self.wd, name),
                "used": read_used(self.wd, name),
                "checked": bool(rev.items) or any((self.wd.root / f).exists() for f in self._check_files(name))}

    def _check_files(self, name: str) -> list[str]:
        from .check import check_stem

        return [f"{check_stem(name, s)}.json" for s in (False, True)]

    def save_review(self, name: str, body: dict) -> dict:
        name = self.draft_name(name)
        ops.decide(self.wd, name, body, SOURCE)
        return self.review(name)

    def apply(self, name: str) -> dict:
        from .review import needs_rewrite_call

        name = self.draft_name(name)
        with ops.LOCK:
            needs = needs_rewrite_call(self.wd, name)
            res = ops.apply(self.wd, name, self.rewriter() if needs and self.rewriter else None, SOURCE)
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
            raise ValueError("answers must be an object")
        ops.save_answers(self.wd, answers, SOURCE)
        return self.state()

    def save_design(self, body: dict) -> dict:
        ops.update_design(self.wd, body, SOURCE)
        return self.state()


def make_handler(app: WriteApp) -> type[BaseHTTPRequestHandler]:
    page = resources.files("kumimasu").joinpath("index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/api/drafts":
                self._json(app.drafts())
            elif (name := self._review_name(path)) is not None:
                try:
                    self._json(app.review(name))
                except KeyError:
                    self._json({"error": "unknown draft"}, 404)
            elif (name := self._review_name(path, "final")) is not None:
                try:
                    self._json(app.final(name))
                except KeyError:
                    self._json({"error": "no final for this draft"}, 404)
            elif (name := self._review_name(path, "download")) is not None:
                try:
                    fname, body = app.download(name)
                except KeyError:
                    self._json({"error": "no final for this draft"}, 404)
                    return
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
                try:
                    self._json(app.state())
                except StepError as e:
                    self._json({"error": str(e)}, 409)
            else:
                self._json({"error": "not found"}, 404)

        def _review_name(self, path: str, prefix: str = "review") -> str | None:
            parts = path.strip("/").split("/")
            return parts[2] if len(parts) == 3 and parts[:2] == ["api", prefix] else None

        def _body(self) -> dict:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/confirm":
                try:
                    self._json(app.confirm(self._body()))
                except StepError as e:
                    self._json({"error": str(e)}, 409)
                except (ValueError, LLMError) as e:
                    self._json({"error": str(e)}, 400)
                return
            name = self._review_name(path, "apply")
            if name is None:
                self._json({"error": "not found"}, 404)
                return
            try:
                self._json(app.apply(name))
            except KeyError:
                self._json({"error": "unknown draft"}, 404)
            except StepError as e:
                self._json({"error": str(e)}, 409)
            except (ValueError, LLMError) as e:
                self._json({"error": str(e)}, 400)

        def do_PUT(self) -> None:
            path = urlparse(self.path).path
            if (name := self._review_name(path)) is not None:
                try:
                    self._json(app.save_review(name, self._body()))
                except KeyError:
                    self._json({"error": "unknown draft"}, 404)
                except StepError as e:
                    self._json({"error": str(e)}, 409)
                except (ValueError, TypeError, ValidationError) as e:
                    self._json({"error": str(e)}, 400)
                return
            handler = {"/api/interview": app.save_answers, "/api/design": app.save_design}.get(path)
            if handler is None:
                self._json({"error": "not found"}, 404)
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                self._json(handler(body))
            except StepError as e:
                self._json({"error": str(e)}, 409)
            except (ValueError, TypeError, ValidationError) as e:
                self._json({"error": str(e)}, 400)

    return Handler


def make_server(app: WriteApp, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((HOST, port), make_handler(app))


def serve(app: WriteApp, port: int) -> None:
    server = make_server(app, port)
    print(f"kumimasu: http://{HOST}:{server.server_address[1]}/  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

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
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h | (headers or {}))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                text = r.read().decode()
                ctype = r.headers.get("Content-Type", "")
                return r.status, json.loads(text) if "json" in ctype else text, r.headers
        except urllib.error.HTTPError as e:
            text = e.read().decode()
            return e.code, json.loads(text) if text.startswith("{") else text, e.headers

    def get(self, path: str, **kw):
        return self.request(path, **kw)[:2]

    def put(self, path: str, body, **kw):
        return self.request(path, "PUT", body, **kw)[:2]

    def post(self, path: str, body=None, **kw):
        return self.request(path, "POST", {} if body is None else body, **kw)[:2]


@contextmanager
def serving(wd, rewriter=None):
    server = make_server(WriteApp(wd, rewriter, 3), 0, TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield Client(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()


def settings():
    from pathlib import Path

    from kumimasu.config import load

    return load(None, Path.cwd())


def defaults():
    from kumimasu.cli_common import design_defaults

    return design_defaults(settings())


def always_ask() -> list[str]:
    return settings().get("interview.always_ask") or []


def roles() -> dict[str, str]:
    return settings().roles()

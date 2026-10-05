from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .errors import LLMError
from .files import atomic_write, private_dir


class Provider(Protocol):
    name: str
    model: str

    def complete(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> str: ...


def _schema_hint(json_schema: dict | None) -> str:
    if not json_schema:
        return ""
    return "\n\nRespond with a single JSON object matching this JSON Schema, and nothing else:\n" + json.dumps(json_schema, ensure_ascii=False)


def extract_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if m := re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL):
        return json.loads(m[1])
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError(f"no JSON found in: {text[:120]!r}")
    depth = 0
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    in_str = esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            esc = ch == "\\" and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError(f"unterminated JSON in: {text[:120]!r}")


class FakeProvider:
    name = "fake"

    def __init__(self, responses: dict[str, str] | list[str] | Callable[[str], str] | None = None,
                 default: str = '{"answer": "no", "reason": "fake"}', model: str = "fake-1") -> None:
        self.model = model
        self.responses = responses
        self.default = default
        self.calls: list[dict] = []
        self._seq = 0

    def complete(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> str:
        self.calls.append({"prompt": prompt, "system": system, "json_schema": json_schema})
        r = self.responses
        if callable(r):
            return r(prompt)
        if isinstance(r, list):
            out = r[self._seq % len(r)] if r else self.default
            self._seq += 1
            return out
        if isinstance(r, dict):
            for pattern, answer in r.items():
                if re.search(pattern, prompt, re.DOTALL):
                    return answer
        return self.default


class ClaudeCliProvider:
    name = "claude-cli"

    def __init__(self, model: str = "haiku", timeout: float = 900, executable: str = "claude",
                 allowed_tools: tuple[str, ...] = ()) -> None:
        self.model = model
        self.timeout = timeout
        self.executable = executable
        self.allowed_tools = tuple(allowed_tools)

    def command(self, system: str | None = None) -> list[str]:
        tools = ",".join(self.allowed_tools)
        # Keeps the user's CLAUDE.md, hooks, plugins and MCP servers (persona, memory) out of these calls.
        cmd = [self.executable, "-p", "--output-format", "json", "--model", self.model, "--tools", tools,
               "--setting-sources", "", "--safe-mode", "--strict-mcp-config", "--disable-slash-commands",
               "--no-session-persistence"]
        if tools:
            cmd += ["--allowedTools", tools]
        if system:
            cmd += ["--system-prompt", system]
        return cmd

    def complete(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> str:
        with tempfile.TemporaryDirectory(prefix="kumimasu-") as tmp:
            out = _run_cli(self.command(system), prompt + _schema_hint(json_schema), self.timeout, tmp)
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise LLMError(f"claude -p の出力が JSON ではありません: {out[:200]!r}") from e
        if not isinstance(data, dict) or data.get("is_error"):
            raise LLMError(f"claude -p がエラーを返しました: {data.get('result') if isinstance(data, dict) else data}")
        return str(data.get("result", ""))


def _run_cli(cmd: list[str], stdin: str, timeout: float, cwd: str) -> str:
    if not shutil.which(cmd[0]):
        raise LLMError(f"{cmd[0]} が PATH にありません")
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, cwd=cwd, check=False)
    except subprocess.TimeoutExpired as e:
        raise LLMError(f"{cmd[0]} が {timeout:.0f} 秒で終わりませんでした") from e
    if proc.returncode != 0:
        raise LLMError(f"{cmd[0]} が失敗しました（{proc.returncode}）: {proc.stderr[-500:]}")
    return proc.stdout


CODEX_DISABLED_FEATURES = ("shell_tool", "unified_exec", "browser_use", "browser_use_external", "computer_use",
                           "in_app_browser", "apps", "plugins", "hooks", "image_generation", "multi_agent")


class CodexCliProvider:
    name = "codex-cli"

    def __init__(self, model: str | None = None, timeout: float = 900, executable: str = "codex") -> None:
        self.model = model or "default"
        self._model_arg = model
        self.timeout = timeout
        self.executable = executable

    def command(self, tmp: str, schema_path: str | None) -> list[str]:
        cmd = [self.executable, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
               "--sandbox", "read-only", "-C", tmp, "-c", 'web_search="disabled"',
               "--output-last-message", str(Path(tmp) / "last.txt")]
        for feature in CODEX_DISABLED_FEATURES:
            cmd += ["--disable", feature]
        if self._model_arg:
            cmd += ["--model", self._model_arg]
        if schema_path:
            cmd += ["--output-schema", schema_path]
        return [*cmd, "-"]

    def complete(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> str:
        with tempfile.TemporaryDirectory(prefix="kumimasu-") as tmp:
            schema_path = None
            if json_schema:
                schema_path = str(Path(tmp) / "schema.json")
                Path(schema_path).write_text(json.dumps(json_schema), encoding="utf-8")
            stdout = _run_cli(self.command(tmp, schema_path), (f"{system}\n\n" if system else "") + prompt,
                              self.timeout, tmp)
            out = Path(tmp) / "last.txt"
            return out.read_text(encoding="utf-8") if out.exists() else stdout


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = "claude-haiku-4-5", max_tokens: int = 1024) -> None:
        import anthropic

        self.model = model
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic()

    def complete(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> str:
        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens,
                                  "messages": [{"role": "user", "content": prompt + _schema_hint(json_schema)}]}
        if system:
            kwargs["system"] = system
        msg = self._client.messages.create(**kwargs)
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str = "gpt-5-mini") -> None:
        import openai

        self.model = model
        self._client = openai.OpenAI()

    def complete(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt + _schema_hint(json_schema)}]
        resp = self._client.chat.completions.create(model=self.model, messages=messages)
        return resp.choices[0].message.content or ""


class CachedProvider:
    def __init__(self, inner: Provider, cache_dir: str | os.PathLike) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.dir = Path(cache_dir)
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()

    def _key(self, prompt: str, system: str | None, json_schema: dict | None) -> str:
        parts = [self.inner.name, self.inner.model, list(getattr(self.inner, "allowed_tools", ())), system, json_schema,
                 prompt]
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def complete(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> str:
        path = self.dir / self.inner.name / (self._key(prompt, system, json_schema) + ".json")
        if path.is_file() and not path.is_symlink():
            with self._lock:
                self.hits += 1
            return json.loads(path.read_text(encoding="utf-8"))["response"]
        with self._lock:
            self.misses += 1
        out = self.inner.complete(prompt, system=system, json_schema=json_schema)
        private_dir(self.dir)
        private_dir(path.parent)
        atomic_write(path, json.dumps({"model": self.inner.model, "prompt": prompt, "system": system, "response": out},
                                      ensure_ascii=False))
        return out


MODEL_SPEC = re.compile(r"^[\w.:\[\]-]+$")


def get_provider(spec: str, cache_dir: str | None = None, allowed_tools: tuple[str, ...] = ()) -> Provider:
    kind, _, model = spec.partition(":")
    if model and not MODEL_SPEC.match(model):
        raise ValueError(f"モデルの名前に使えない文字があります: {model!r}")
    if allowed_tools and kind != "claude-cli":
        raise ValueError(f"ツールを使えるのは claude-cli だけです（{kind} では使えません）")
    p: Provider
    match kind:
        case "fake":
            return FakeProvider(model=model or "fake-1")
        case "claude-cli":
            p = ClaudeCliProvider(model or "haiku", allowed_tools=allowed_tools)
        case "codex-cli":
            p = CodexCliProvider(model or None)
        case "anthropic":
            p = AnthropicProvider(model or "claude-haiku-4-5")
        case "openai":
            p = OpenAIProvider(model or "gpt-5-mini")
        case _:
            raise ValueError(f"知らない provider です: {spec}")
    return CachedProvider(p, cache_dir) if cache_dir else p

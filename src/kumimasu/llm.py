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
        raise ValueError(f"JSON が見つかりません: {text[:120]!r}")
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
    raise ValueError(f"JSON が閉じていません: {text[:120]!r}")


def forget(provider: Provider, prompt: str, schema: dict | None = None) -> None:
    if (drop := getattr(provider, "forget", None)) is not None:
        drop(prompt, json_schema=schema)


def ask_json(provider: Provider, prompt: str, schema: dict) -> dict:
    raw = provider.complete(prompt, json_schema=schema)
    try:
        data = extract_json(raw)
    except ValueError as e:
        forget(provider, prompt, schema)
        raise LLMError(f"LLM の応答から JSON を読めません: {e}") from e
    if not isinstance(data, dict):
        forget(provider, prompt, schema)
        raise LLMError(f"LLM の応答が JSON のオブジェクトではありません: {raw[:120]!r}")
    return data


def rows(data: dict, key: str) -> list[dict]:
    value = data.get(key)
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def strings(data: dict, key: str) -> list[str]:
    value = data.get(key)
    return [str(x).strip() for x in value if isinstance(x, (str, int, float))] if isinstance(value, list) else []


def ask_replacements(provider: Provider, prompt: str, ids: list[str]) -> dict[str, str]:
    schema = obj(items=arr(obj(id=STR, replacement=STR)))
    data = ask_json(provider, prompt, schema)
    got = {str(x.get("id")): str(x.get("replacement", "")).strip() for x in rows(data, "items")}
    if not set(ids) <= got.keys():
        forget(provider, prompt, schema)
    return got


STR: dict = {"type": "string"}
INT: dict = {"type": "integer"}
BOOL: dict = {"type": "boolean"}


def ids_in(value: Any, known: set[str]) -> list[str]:
    return list(dict.fromkeys(x for x in value if isinstance(x, str) and x in known)) if isinstance(value, list) else []


def enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


def arr(items: dict, max_items: int | None = None) -> dict:
    return {"type": "array", "items": items} | ({"maxItems": max_items} if max_items is not None else {})


def obj(**props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


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
        import anthropic  # optional dependency: kumimasu[anthropic]

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
        import openai  # optional dependency: kumimasu[openai]

        self.model = model
        self._client = openai.OpenAI()

    def complete(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt + _schema_hint(json_schema)}]
        resp = self._client.chat.completions.create(model=self.model, messages=messages)
        return resp.choices[0].message.content or ""


class CountingProvider:
    def __init__(self, inner: Provider) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> str:
        with self._lock:
            self.calls += 1
        return self.inner.complete(prompt, system=system, json_schema=json_schema)

    @property
    def misses(self) -> int:
        return getattr(self.inner, "misses", self.calls)

    def forget(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> None:
        if (drop := getattr(self.inner, "forget", None)) is not None:
            drop(prompt, system=system, json_schema=json_schema)


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

    def _path(self, prompt: str, system: str | None, json_schema: dict | None) -> Path:
        return self.dir / self.inner.name / (self._key(prompt, system, json_schema) + ".json")

    def forget(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> None:
        self._path(prompt, system, json_schema).unlink(missing_ok=True)

    def complete(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> str:
        path = self._path(prompt, system, json_schema)
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

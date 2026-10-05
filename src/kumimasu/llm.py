from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol


class LLMError(RuntimeError):
    pass


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
    if m := re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S):
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
                if re.search(pattern, prompt, re.S):
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
        # An empty --setting-sources keeps the user's CLAUDE.md (persona, memory) out of judge prompts.
        cmd = [self.executable, "-p", "--output-format", "json", "--model", self.model, "--tools", tools,
               "--setting-sources", "", "--no-session-persistence"]
        if tools:
            cmd += ["--allowedTools", tools]
        if system:
            cmd += ["--system-prompt", system]
        return cmd

    def complete(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> str:
        if not shutil.which(self.executable):
            raise LLMError(f"{self.executable} not found on PATH")
        proc = subprocess.run(self.command(system), input=prompt + _schema_hint(json_schema), capture_output=True, text=True,
                              timeout=self.timeout)
        if proc.returncode != 0:
            raise LLMError(f"claude -p failed ({proc.returncode}): {proc.stderr[:500]}")
        data = json.loads(proc.stdout)
        if data.get("is_error"):
            raise LLMError(f"claude -p error: {data.get('result')}")
        return str(data.get("result", ""))


class CodexCliProvider:
    name = "codex-cli"

    def __init__(self, model: str | None = None, timeout: float = 900, executable: str = "codex") -> None:
        self.model = model or "default"
        self._model_arg = model
        self.timeout = timeout
        self.executable = executable

    def complete(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> str:
        if not shutil.which(self.executable):
            raise LLMError(f"{self.executable} not found on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "last.txt"
            cmd = [self.executable, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "-C", tmp,
                   "--output-last-message", str(out)]
            if self._model_arg:
                cmd += ["--model", self._model_arg]
            if json_schema:
                schema_path = Path(tmp) / "schema.json"
                schema_path.write_text(json.dumps(json_schema))
                cmd += ["--output-schema", str(schema_path)]
            cmd.append("-")
            full = (f"{system}\n\n" if system else "") + prompt + _schema_hint(json_schema)
            proc = subprocess.run(cmd, input=full, capture_output=True, text=True, timeout=self.timeout)
            if proc.returncode != 0:
                raise LLMError(f"codex exec failed ({proc.returncode}): {proc.stderr[-500:]}")
            return out.read_text(encoding="utf-8") if out.exists() else proc.stdout


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

    def _key(self, prompt: str, system: str | None, json_schema: dict | None) -> str:
        parts: list[Any] = [self.inner.name, self.inner.model, system, json_schema, prompt]
        # Appended only when set, so entries cached for tool-less calls keep their keys.
        if tools := getattr(self.inner, "allowed_tools", ()):
            parts.append(list(tools))
        blob = json.dumps(parts, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def complete(self, prompt: str, *, system: str | None = None, json_schema: dict | None = None) -> str:
        path = self.dir / self.inner.name / (self._key(prompt, system, json_schema) + ".json")
        if path.exists():
            self.hits += 1
            return json.loads(path.read_text(encoding="utf-8"))["response"]
        self.misses += 1
        out = self.inner.complete(prompt, system=system, json_schema=json_schema)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": self.inner.model, "prompt": prompt, "system": system, "response": out},
                                   ensure_ascii=False), encoding="utf-8")
        return out


def get_provider(spec: str, cache_dir: str | None = None, allowed_tools: tuple[str, ...] = ()) -> Provider:
    kind, _, model = spec.partition(":")
    if allowed_tools and kind != "claude-cli":
        raise ValueError(f"allowed_tools is supported only by claude-cli, not {kind}")
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
            raise ValueError(f"unknown provider: {spec}")
    return CachedProvider(p, cache_dir) if cache_dir else p

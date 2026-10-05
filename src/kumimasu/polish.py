from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING

from pydantic import BaseModel

from .check import dash_hits, load_keep, text_hash
from .generate import DATA_NOTE_JA
from .llm import extract_json
from .surface import GLUE, MIN_VOTES, RUNS, detect_surface
from .workdir import WorkDir

if TYPE_CHECKING:
    from .llm import Provider


class Flag(BaseModel):
    id: str
    rule: str
    text: str


POLISH_RULES = ("meta", "glue", "dash")
MAX_ROUNDS = 3


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


POLISH_PROMPT_JA = DATA_NOTE_JA + """

次は記事の下書きと、その中で直す文の一覧です。一覧の文だけを直してください。ほかの文・段落・見出しの順番・表・コードには触れません。

直し方:
- 情報を運ばない文（道しるべ・決め台詞・「A ではなく B」の言い直し・立場や範囲の宣言・自分への但し書き）は、replacement を空文字にして消します。文の一部が情報を運んでいるなら、その情報だけを残した文に書き換えます。
- 主張の見出し（claim_heading）は、内容を指す短い名詞句に書き換えます。
- つなぎの効用文（glue）は、話題を読者の役立ちや結果に結びつけるだけの文です。replacement を空文字にして消します。前に書いていない事実を含むなら、その事実だけを残した文に書き換えます。
- ダッシュ（dash）を含む文は、ダッシュを使わない文に書き換えます。意味は変えません。
- 前後の文とつながるように、言い回しは最小限だけ変えます。

{{"items": [{{"id": "F1", "replacement": "…"}}]}} の形の JSON で、一覧のすべての文について答えてください。

# 直す文

{flags}

# 下書き

{draft}"""


def polish_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {"items": {
        "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "replacement"],
                                   "properties": {"id": {"type": "string"}, "replacement": {"type": "string"}}}}}}


def polish_prompt(draft: str, flags: list[Flag]) -> str:
    return POLISH_PROMPT_JA.format(flags="\n".join(f"[{f.id}]（{f.rule}）{f.text}" for f in flags), draft=draft.strip())


def _locate(text: str, sentence: str) -> re.Match | None:
    pattern = r"\s*".join(re.escape(ch) for ch in re.sub(r"\s+", "", sentence))
    return re.search(pattern, text)


def _locate(text: str, sentence: str) -> re.Match | None:
    pattern = r"\s*".join(re.escape(ch) for ch in re.sub(r"\s+", "", sentence))
    return re.search(pattern, text)


def apply_replacements(draft: str, flags: list[Flag], repl: dict[str, str]) -> tuple[str, list[dict], list[int]]:
    log: list[dict] = []
    edits: list[int] = []
    for f in flags:
        if f.id not in repl:
            log.append({"id": f.id, "status": "no answer", "text": f.text})
            continue
        m = _locate(draft, f.text)
        if m is None:
            log.append({"id": f.id, "status": "not found", "text": f.text})
            continue
        new = repl[f.id].strip()
        if re.sub(r"\s+", "", new) == re.sub(r"\s+", "", m.group()):
            log.append({"id": f.id, "status": "unchanged", "text": f.text, "rule": f.rule})
            continue
        delta = len(new) - (m.end() - m.start())
        edits = [e + delta if e > m.start() else e for e in edits] + [m.start()]
        draft = draft[:m.start()] + new + draft[m.end():]
        log.append({"id": f.id, "status": "deleted" if not new else "rewritten", "text": f.text, "replacement": new,
                    "rule": f.rule})
    return draft, log, edits


def tidy(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def blocks(text: str) -> list[tuple[int, int]]:
    out, start, pos, fence = [], None, 0, False
    for line in text.splitlines(keepends=True):
        s = line.strip()
        if s.startswith(("```", "~~~")):
            fence = not fence
        if not s and not fence:
            if start is not None:
                out.append((start, pos))
                start = None
        elif start is None:
            start = pos
        pos += len(line)
    if start is not None:
        out.append((start, pos))
    return out


def neighborhood(text: str, edits: list[int], radius: int = 1) -> str:
    bs = blocks(text)
    if not bs or not edits:
        return ""
    chosen: set[int] = set()
    for e in edits:
        i = next((k for k, (a, b) in enumerate(bs) if e <= b), len(bs) - 1)
        chosen.update(range(max(0, i - radius), min(len(bs), i + radius + 1)))
    parts = []
    for k in sorted(chosen):
        a, b = bs[k]
        block = text[a:b].strip()
        if not block.startswith("#"):
            heading = next((text[x:y].strip() for x, y in reversed(bs[:k]) if text[x:y].lstrip().startswith("#")), "")
            if heading and heading not in parts:
                parts.append(heading)
        if block and block not in parts:
            parts.append(block)
    return "\n\n".join(parts) + "\n"


class Round(BaseModel):
    round: int
    scope_chars: int
    runs: int
    hits: int
    edits: int = 0
    calls: int = 0
    flags: list[Flag] = []
    log: list[dict] = []


class PolishResult(BaseModel):
    out: str = ""
    rounds: list[Round] = []
    calls: int = 0
    llm_calls: int = 0


def find_flags(scope: str, full: str, provider: Provider, material: list[str], rules: tuple[str, ...], runs: int,
               min_votes: int, keep: set[str] | frozenset[str] = frozenset()) -> tuple[list[Flag], int]:
    found: list[tuple[str, str]] = []
    used = 0
    if "meta" in rules or GLUE in rules:
        rep = detect_surface(scope, provider, material, runs, min_votes)
        used = rep.runs_used
        found += [(h.category, h.text) for h in rep.hits
                  if (h.category == GLUE and GLUE in rules) or (h.category != GLUE and "meta" in rules)]
    if "dash" in rules:
        found += [("dash", s) for s in dash_hits(scope)]
    out, seen = [], set()
    for rule, text in found:
        if text not in seen and text_hash(text) not in keep and _locate(full, text):
            seen.add(text)
            out.append(Flag(id=f"F{len(out) + 1}", rule=rule, text=text))
    return out, used


def polish(wd: WorkDir, provider: Provider, draft_name: str, rules: tuple[str, ...] = POLISH_RULES, apply: bool = False,
           runs: int = RUNS, min_votes: int = MIN_VOTES, max_rounds: int = MAX_ROUNDS, out: str | None = None) -> PolishResult:
    counter = CountingProvider(provider)
    start_misses = counter.misses
    material = [u.text for u in wd.units()]
    keep = load_keep(wd)
    text = wd.read(draft_name)
    scope = text
    res = PolishResult()
    for r in range(1, max_rounds + 1):
        before = counter.calls
        flags, used = find_flags(scope, text, counter, material, rules, runs, min_votes, keep)
        rd = Round(round=r, scope_chars=len(scope), runs=used, hits=len(flags), flags=flags)
        res.rounds.append(rd)
        if not flags or not apply:
            rd.calls = counter.calls - before
            break
        raw = counter.complete(polish_prompt(text, flags), json_schema=polish_schema())
        repl = {str(x.get("id")): str(x.get("replacement", "")) for x in extract_json(raw).get("items", [])}
        new, log, edits = apply_replacements(text, flags, repl)
        rd.log, rd.edits, rd.calls = log, len(edits), counter.calls - before
        scope = neighborhood(new, edits)
        text = tidy(new)
        if not scope.strip():
            break
    res.calls = counter.calls
    res.llm_calls = counter.misses - start_misses
    if apply:
        res.out = out or draft_name.removesuffix(".md") + ".polished.md"
        wd.write(res.out, text)
        wd.write_json(res.out.removesuffix(".md") + ".json", res.model_dump())
    return res

from __future__ import annotations

import bisect
from typing import TYPE_CHECKING

from pydantic import BaseModel

from .check import Votes, dash_hits, fresh_report, noted_texts
from .design import sync_design
from .generate import DATA_NOTE_JA
from .infounits import info_units
from .keep import KeepStore, text_hash
from .land import guarded, note_units
from .llm import CountingProvider, ask_replacements
from .metadiscourse import split_sentences
from .surface import BRIDGE, CAVEAT, FLOW, GLUE, SLOT, WRAPUP, detect_surface
from .textutil import blocks, collapse_blank_lines, edit_text, locate, norm
from .workdir import WorkDir

if TYPE_CHECKING:
    from .llm import Provider


class Flag(BaseModel):
    id: str
    rule: str
    text: str


POLISH_RULES = ("meta", "caveat", "glue", "flow", "dash")


POLISH_PROMPT_JA = DATA_NOTE_JA + """

次は記事の下書きと、その中で直す文の一覧です。一覧の文だけを直してください。ほかの文・段落・見出しの順番・表・コードには触れません。

直し方:
- 情報を運ばない文（道しるべ・決め台詞・「A ではなく B」の言い直し・立場や範囲の宣言・自分への但し書き）は、replacement を空文字にして消します。文の一部が情報を運んでいるなら、その情報だけを残した文に書き換えます。
- 保守的な但し書き（caveat）は、結果の読み方を変えない限界・未確認の断りです。replacement を空文字にして消します。結果の読み方を変える条件（効く範囲・前提・比べた条件）を含むなら、その条件だけを残した文に書き換えます。
- 主張の見出し（claim_heading）は、内容を指す短い名詞句に書き換えます。
- つなぎの効用文（glue）は、話題を読者の役立ちや結果に結びつけるだけの文です。replacement を空文字にして消します。前に書いていない事実を含むなら、その事実だけを残した文に書き換えます。
- 段落の頭の理由づけ（{bridge}）は、前の段落の結果を言い直して次の話の理由に結びつける部分です。その部分（「そこで、」などの接続の語も含む）だけを消し、残りを段落の頭として読める文に書き換えます。文全体が結びつけだけなら、replacement を空文字にして消します。
- 段落の結び（{wrapup}）は、段落で述べたことを解釈・教訓として言い直して結ぶだけの文です。replacement を空文字にして消します。段落の中にまだ書いていない事実や著者の判断を含むなら、それだけを残した文に書き換えます。
- ダッシュ（dash）を含む文は、ダッシュを使わない文に書き換えます。意味は変えません。
- 前後の文とつながるように、言い回しは最小限だけ変えます。

{{"items": [{{"id": "F1", "replacement": "…"}}]}} の形の JSON で、一覧のすべての文について答えてください。

# 直す文

{flags}

# 下書き

{draft}"""


def polish_prompt(draft: str, flags: list[Flag]) -> str:
    return POLISH_PROMPT_JA.format(bridge=BRIDGE, wrapup=WRAPUP, flags="\n".join(f"[{f.id}]（{f.rule}）{f.text}" for f in flags), draft=draft.strip())


def apply_replacements(draft: str, flags: list[Flag], repl: dict[str, str]) -> tuple[str, list[dict], list[int]]:
    log: dict[str, dict] = {}
    edits: list[tuple[int, int, str]] = []
    owners: list[Flag] = []
    for f in flags:
        span = locate(draft, f.text)
        if f.id not in repl:
            log[f.id] = {"id": f.id, "status": "no answer", "text": f.text}
        elif span is None:
            log[f.id] = {"id": f.id, "status": "not found", "text": f.text}
        elif norm(repl[f.id]) == norm(draft[span[0]:span[1]]):
            log[f.id] = {"id": f.id, "status": "unchanged", "text": f.text, "rule": f.rule}
        else:
            edits.append((*span, repl[f.id].strip()))
            owners.append(f)
    text, placed = edit_text(draft, edits)
    for i, f in enumerate(owners):
        new = edits[i][2]
        log[f.id] = ({"id": f.id, "status": "deleted" if not new else "rewritten", "text": f.text, "replacement": new,
                      "rule": f.rule} if i in placed else {"id": f.id, "status": "skipped", "text": f.text, "rule": f.rule})
    return text, [log[f.id] for f in flags], [placed[i] for i in range(len(owners)) if i in placed]


def neighborhood(text: str, edits: list[int], radius: int = 1) -> str:
    bs = blocks(text)
    if not bs or not edits:
        return ""
    ends = [b for _, b in bs]
    chosen: set[int] = set()
    for e in edits:
        i = min(bisect.bisect_left(ends, e), len(bs) - 1)
        chosen.update(range(max(0, i - radius), min(len(bs), i + radius + 1)))
    heading_before: list[str] = []
    last = ""
    for a, b in bs:
        heading_before.append(last)
        if text[a:b].lstrip().startswith("#"):
            last = text[a:b].strip()
    parts: list[str] = []
    for k in sorted(chosen):
        a, b = bs[k]
        block = text[a:b].strip()
        if not block.startswith("#") and heading_before[k] and heading_before[k] not in parts:
            parts.append(heading_before[k])
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


def rule_of(category: str) -> str:
    return category if category in (GLUE, CAVEAT) else "flow" if category in FLOW else "meta"


def original_slots(text: str) -> dict[str, str]:
    return {norm(u.text): u.slot for u in split_sentences(text) if u.slot}


def find_flags(scope: str, full: str, provider: Provider, material: list[str], rules: tuple[str, ...], votes: Votes,
               keep: set[str], slots: dict[str, str] | None = None, protect: tuple[str, ...] = ()) -> tuple[list[Flag], int]:
    found: list[tuple[str, str]] = []
    used = 0
    if {"meta", CAVEAT, GLUE, "flow"} & set(rules):
        rep = detect_surface(scope, provider, material, votes.runs, votes.min_votes)
        used = rep.runs_used
        # A deleted wrap-up makes the sentence before it the paragraph's last, so later rounds would peel the paragraph
        # sentence by sentence; only sentences that held the slot in the original draft count.
        found += [(h.category, h.text) for h in rep.hits if rule_of(h.category) in rules
                  and (h.category not in FLOW or slots is None or slots.get(norm(h.text)) == SLOT[h.category])]
    if "dash" in rules:
        found += [("dash", s) for s in dash_hits(scope)]
    out, seen = [], set()
    for rule, text in found:
        if text not in seen and text_hash(text) not in keep and not guarded(text, protect) and locate(full, text):
            seen.add(text)
            out.append(Flag(id=f"F{len(out) + 1}", rule=rule, text=text))
    return out, used


def polish(wd: WorkDir, provider: Provider, draft_name: str, rules: tuple[str, ...], apply: bool, votes: Votes,
           max_rounds: int, out: str) -> PolishResult:
    counter = CountingProvider(provider)
    start_misses = counter.misses
    units = wd.units()
    notes = note_units(sync_design(wd.design(), units), units) if wd.design_file.exists() else []
    material = [u.text for u in units + notes]
    keep = KeepStore(wd).hashes()
    text = wd.read(draft_name)
    slots = original_slots(text)
    checked = fresh_report(wd, draft_name, False)
    protect = noted_texts(info_units(text), checked.sources) if checked else ()
    scope = text
    res = PolishResult()
    for r in range(1, max_rounds + 1):
        before = counter.calls
        flags, used = find_flags(scope, text, counter, material, rules, votes, keep, slots, protect)
        rd = Round(round=r, scope_chars=len(scope), runs=used, hits=len(flags), flags=flags)
        res.rounds.append(rd)
        if not flags or not apply:
            rd.calls = counter.calls - before
            break
        repl = ask_replacements(counter, polish_prompt(text, flags))
        new, log, edits = apply_replacements(text, flags, repl)
        rd.log, rd.edits, rd.calls = log, len(edits), counter.calls - before
        scope = neighborhood(new, edits)
        text = collapse_blank_lines(new)
        if not scope.strip():
            break
    res.calls = counter.calls
    res.llm_calls = counter.misses - start_misses
    if apply:
        res.out = out
        wd.write(res.out, text)
        wd.write_json(res.out.removesuffix(".md") + ".json", res.model_dump())
    return res

from __future__ import annotations

import re
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import StepError
from .generate import DATA_NOTE_JA
from .infounits import PROSE_SUFFIXES
from .llm import STR, arr, ask_json, obj, rows
from .model import DigestState, Unit
from .parts.markdown import parse

if TYPE_CHECKING:
    from .llm import Provider
    from .workdir import WorkDir

LONG_FILE = 8000
HEADINGS_PER_FILE = 6
HEADINGS_MIN_FILE = 3000
NUMBERS_SHARE = 0.15
NUMBERS_MIN = 10
SHORT_UNITS_MIN = 60
SHORT_MEDIAN = 150
SHORT_PROSE_CHARS = 10000
SAME_HEADING = 15
SIGNALS_NEEDED = 2
TEXT_KINDS = ("prose", "item", "quote")
CHUNK_CHARS = 3000
SECTION_SPLIT = 6000
WORKERS = 4
BEFORE_DIGEST = "interview.before-digest.yaml"

_METRIC = re.compile(r"\d+(?:[.,]\d+)?\s*[%％]?")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def _metrics(text: str) -> list[str]:
    return [m for m in _METRIC.findall(text) if len(m.strip()) >= 2]


def contextless_number(u: Unit) -> bool:
    """A cut-out sentence that carries numbers but too little text to say what they measured."""
    if u.kind not in TEXT_KINDS:
        return False
    n = len(_metrics(u.text))
    return n > 0 and (len(u.text) < 120 or len(u.text) / n < 60)


def assess(material_dir: Path, names: list[str], units: list[Unit]) -> DigestState:
    prose_files = [n for n in names if Path(n).suffix.lower() in PROSE_SUFFIXES]
    sizes: dict[str, int] = {}
    headings: dict[str, int] = {}
    for n in prose_files:
        text = (material_dir / n).read_text(encoding="utf-8", errors="replace")
        sizes[n] = len(text)
        headings[n] = sum(p.kind == "section" for p in parse(text).parts())
    prose = [u for u in units if u.source in sizes and u.kind in TEXT_KINDS]
    numeric = [u for u in prose if contextless_number(u)]
    median = int(statistics.median(len(u.text) for u in prose)) if prose else 0
    per_path = Counter(u.path for u in units if u.source in sizes)
    top_path, top_n = per_path.most_common(1)[0] if per_path else ("", 0)
    big = [n for n in prose_files if sizes[n] >= HEADINGS_MIN_FILE]
    avg_headings = round(sum(headings[n] for n in big) / len(big), 1) if big else 0.0
    longest = max(sizes.values(), default=0)
    reasons = []
    if longest >= LONG_FILE:
        reasons.append(f"長い文書がある（最長 {longest} 字）")
    if avg_headings >= HEADINGS_PER_FILE:
        reasons.append(f"見出しが多い（{HEADINGS_MIN_FILE} 字以上のファイル 1 つあたり平均 {avg_headings} 個）")
    if len(numeric) >= NUMBERS_MIN and prose and len(numeric) / len(prose) >= NUMBERS_SHARE:
        reasons.append(f"何の数値か分からない短い単位が多い（{len(numeric)} / {len(prose)} 単位）")
    if len(prose) >= SHORT_UNITS_MIN and median <= SHORT_MEDIAN and sum(sizes.values()) >= SHORT_PROSE_CHARS:
        reasons.append(f"長い文書が短い断片に分かれている（{len(prose)} 単位、中央値 {median} 字）")
    if top_n >= SAME_HEADING:
        reasons.append(f"同じ見出しの下に単位が多い（最大 {top_n} 個: {top_path}）")
    stats = {"prose_files": len(prose_files), "prose_chars": sum(sizes.values()), "longest_file": longest,
             "avg_headings": avg_headings, "units": len(units), "prose_units": len(prose), "median_chars": median,
             "contextless_numbers": len(numeric), "max_units_under_heading": top_n}
    return DigestState(recommended=len(reasons) >= SIGNALS_NEEDED, reasons=reasons, stats=stats)


DIGEST_PROMPT_JA = DATA_NOTE_JA + """

次は、著者が記事の材料として出した文書の一部です。仕上がった文書を機械的に切り分けたので、多くの単位は見出しや前後の文から離れると意味が通りません（何の実験の数字か、何と比べた結果かが分からない）。

これを、それだけで意味が通るメモに書き直してください。

- 1 つのメモには 1 つの事柄（実験・比較・判断・手順・事実）を書きます。何の話か（どの実験・どの話題か）、何をしたか・何を測ったか・何と比べたか、結果どうだったかを、メモの中に書きます。
- 数値は、それが何の値か（何を測った値か、何と比べた値か、単位）と一緒に書きます。丸めたり計算し直したりしません。
- 「節:」の行は見出しの階層です。文脈として使い、要るなら言葉にしてメモに入れます。
- 材料に無い事実・数値・解釈を足しません。分かりやすく言い換えるのは構いませんが、主張を強めたり弱めたりしません。
- from には、メモのもとになった単位の番号を全部入れます。どの単位も、どれかのメモの from に入るようにします。
- コード（```）と表の行は書き直しません。文脈として読むだけにして、from にも入れません。
- メモは 1 つ 80–300 字くらいにします。短い単位はまとめ、長い単位は事柄ごとに分けて構いません。

{{"memos": [{{"from": ["m12", "m13"], "text": "…"}}]}} の形の JSON で答えてください。

# 文書（{source}）

{sections}"""

KIND_LABEL = {"prose": "散文", "quote": "引用", "item": "項目", "row": "表の行", "code": "コード"}


def digest_schema() -> dict:
    return obj(memos=arr(obj(**{"from": arr(STR)}, text=STR)))


def _line(u: Unit) -> str:
    body = f"```\n{u.text.rstrip()}\n```" if u.kind == "code" else u.text
    return f"[{u.id}]（{KIND_LABEL.get(u.kind, '散文')}）\n{body}"


def digest_prompt(chunk: list[list[Unit]]) -> str:
    sections = []
    for group in chunk:
        sections.append(f"## 節: {group[0].path or group[0].source}\n\n" + "\n\n".join(_line(u) for u in group))
    return DIGEST_PROMPT_JA.format(source=chunk[0][0].source, sections="\n\n".join(sections))


def _groups(units: list[Unit]) -> list[list[Unit]]:
    out: list[list[Unit]] = []
    for u in units:
        if out and out[-1][-1].source == u.source and out[-1][-1].path == u.path:
            out[-1].append(u)
        else:
            out.append([u])
    split = []
    for g in out:
        cur: list[Unit] = []
        for u in g:
            if cur and sum(len(x.text) for x in cur) + len(u.text) > SECTION_SPLIT:
                split.append(cur)
                cur = []
            cur.append(u)
        split.append(cur)
    return split


def _size(group: list[Unit]) -> int:
    return sum(len(u.text) for u in group)


def chunks(units: list[Unit]) -> list[list[list[Unit]]]:
    """Small sections of one file share a call; each keeps its own heading path in the prompt."""
    out: list[list[list[Unit]]] = []
    for g in _groups(units):
        last = out[-1] if out else None
        if last and last[0][0].source == g[0].source and sum(map(_size, last)) + _size(g) <= CHUNK_CHARS:
            last.append(g)
        else:
            out.append([g])
    return out


def _needs_call(chunk: list[list[Unit]], prose_files: set[str]) -> bool:
    return chunk[0][0].source in prose_files and any(u.kind in TEXT_KINDS for g in chunk for u in g)


def parse_memos(data: dict, chunk: list[list[Unit]]) -> list[tuple[list[str], str]]:
    units = [u for g in chunk for u in g]
    text_ids = {u.id for u in units if u.kind in TEXT_KINDS}
    numbers = set(_NUM.findall("\n".join([u.text for u in units] + [u.path for u in units])))
    out, used = [], set()
    for row in rows(data, "memos"):
        text = str(row.get("text", "")).strip()
        src = [x for x in dict.fromkeys(row.get("from", []) if isinstance(row.get("from"), list) else [])
               if x in text_ids]
        if not text or not src or not set(_NUM.findall(text)) <= numbers:
            continue
        used.update(src)
        out.append((src, text))
    return out


@dataclass
class DigestResult:
    units: list[Unit]
    before: int
    calls: int
    memos: int
    kept: int


def _rebuild(raw: list[Unit], pieces: list[tuple[list[str], Unit]]) -> list[Unit]:
    order = {u.id: i for i, u in enumerate(raw)}
    pieces.sort(key=lambda p: min(order[x] for x in p[0]))
    return [u.model_copy(update={"id": f"m{i}"}) for i, (_, u) in enumerate(pieces, 1)]


def digest_units(raw: list[Unit], prose_files: set[str], provider: Provider) -> DigestResult:
    parts = chunks(raw)
    todo = [c for c in parts if _needs_call(c, prose_files)]
    with ThreadPoolExecutor(WORKERS) as pool:
        answers = dict(zip(map(id, todo), pool.map(lambda c: ask_json(provider, digest_prompt(c), digest_schema()), todo)))
    by_id = {u.id: u for u in raw}
    pieces: list[tuple[list[str], Unit]] = []
    covered: set[str] = set()
    memos = 0
    for c in parts:
        if id(c) in answers:
            for src, text in parse_memos(answers[id(c)], c):
                first = by_id[src[0]]
                pieces.append((src, Unit(id="", origin="digest", source=first.source, kind="prose",
                                         section=first.section, path=first.path, text=text, from_units=src)))
                covered.update(src)
                memos += 1
        for u in (u for g in c for u in g):
            if u.id not in covered:
                pieces.append(([u.id], Unit(id="", origin="digest", source=u.source, kind=u.kind, section=u.section,
                                            path=u.path, text=u.text, from_units=[u.id])))
    units = _rebuild(raw, pieces)
    return DigestResult(units=units, before=len(raw), calls=len(todo), memos=memos, kept=len(units) - memos)


def plain(u: Unit) -> Unit:
    return u.model_copy(update={"searchable": None, "found_in": [], "cluster": "", "members": []})


def digest(wd: WorkDir, provider: Provider, force: bool = False) -> DigestResult:
    if wd.interview_file.exists() and not force:
        raise StepError("もう質問（と答え）があります。digest は材料の単位と番号を作り直すので、質問も作り直しになります。"
                        f"進めるなら --force を付けてください（今の質問と答えは {BEFORE_DIGEST} に移します）")
    p = wd.project()
    raw = wd.raw_units() or [plain(u) for u in wd.material_units()]
    prose_files = {n for n in p.materials if Path(n).suffix.lower() in PROSE_SUFFIXES}
    res = digest_units(raw, prose_files, provider)
    if not wd.raw_units_file.exists():
        wd.save_raw_units(raw)
    if wd.interview_file.exists():
        wd.interview_file.replace(wd.root / BEFORE_DIGEST)
    wd.save_units(res.units)
    state = p.digest.model_copy(update={"done_at": datetime.now(UTC).isoformat(timespec="seconds"),
                                        "units_before": res.before, "units_after": len(res.units), "calls": res.calls})
    wd.save_project(wd.project().model_copy(update={"digest": state}))
    return res


def reassess(wd: WorkDir) -> DigestState:
    p = wd.project()
    units = wd.raw_units() or wd.material_units()
    fresh = assess(wd.material_dir, p.materials, units)
    state = p.digest.model_copy(update={"recommended": fresh.recommended, "reasons": fresh.reasons, "stats": fresh.stats})
    wd.save_project(p.model_copy(update={"digest": state}))
    return state


def digest_lines(state: DigestState) -> list[str]:
    if state.done_at:
        return [f"材料: 書き直し済み（digest、{state.units_before} 単位 → {state.units_after} 単位、元は units.raw.yaml）"]
    if not state.recommended:
        return []
    return ["提案: 材料が仕上がった文書のようです（" + "・".join(state.reasons) + "）。",
            ("    切り出した単位は前後の文脈が無いと意味が通りにくいので、目印付けとインタビューの前に "
             "`kumimasu digest DIR` で自己完結したメモに書き直せます（任意。節ごとに LLM を呼びます）。")]

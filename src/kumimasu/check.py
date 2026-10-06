from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING

from pydantic import BaseModel

from .coverage import coverage_prompt, coverage_schema, parse_coverage
from .design import sync_design
from .factcheck import UrlStatus, extract_urls, firsthand_hits
from .figures import figure_markers
from .generate import DATA_NOTE_JA
from .infounits import InfoUnit, as_info_units, info_units, units_block
from .interview import unit_lines
from .keep import KeepStore, text_hash
from .land import from_notes, guarded, note_units
from .llm import INT, STR, arr, ask_json, enum, obj, rows
from .metadiscourse import split_sentences
from .model import Design, Project, Unit
from .parts.lint import lint as parts_lint
from .parts.markdown import parse as parse_parts
from .surface import (
    CAVEAT,
    FLOW,
    GLUE,
    SurfaceHit,
    SurfaceReport,
    detect_surface,
    rule_hints,
)
from .terms import material_load
from .textutil import (
    blocks,
    code_free_lines,
    code_ranges,
    excerpt,
    locate,
    overlaps,
    sentences,
)
from .workdir import WorkDir

if TYPE_CHECKING:
    from .llm import Provider

DRAFT = "D"
LENGTH_TOLERANCE = 0.2
FIRSTHAND_MIN = 0.8
LINT_RULES = ("bold-lead-item", "emoji", "decor-symbol", "bold-density")


SURFACE = ("meta", "caveat", "glue", "flow", "lint")


class Check(BaseModel):
    id: str
    relation: str
    surface: bool = False
    passed: bool | None
    value: float | int | str | None = None
    detail: str = ""
    items: list[dict] = []
    runs: int | None = None


class CheckReport(BaseModel):
    draft: str
    chars: int
    checks: list[Check]
    sources: dict[int, list[str]] = {}
    figures: list[dict] = []

    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.passed is False]

    def structural_failed(self) -> list[Check]:
        return [c for c in self.failed() if not c.surface]


MAP_PROMPT_JA = DATA_NOTE_JA + """

次は、著者の材料（[m3] や [q1] の番号の単位）と、それをもとに書かれた記事の下書き（[1] のような番号の単位）です。

(a) units: 下書きの単位ごとに、次の 2 つを答えてください。
  - from: その単位が伝えている情報のもとになった材料の単位の番号（最大 6 つ）。材料に無い情報だけなら空の配列。
  - firsthand: その単位が、書き手自身がやったこと・見たこと・測ったこと・感じたことを書き手の体験として述べているなら yes、そうでなければ no。
(b) takeaways: 下の「持ち帰り」のそれぞれについて、下書きを読んだ読者がそれを持ち帰れるか（present: yes / no）と、根拠になる下書きの単位の番号（evidence、最大 4 つ）。
(c) skips: 下の「前提」のそれぞれについて、下書きがそれを説明しているか（explained: yes / no）と、説明している下書きの単位の番号（evidence、最大 4 つ）。名前を出すだけ・使うだけなら no、何であるか・なぜそうなるかを説明していれば yes。

# 持ち帰り

{takeaways}

# 前提

{skips}

# 材料

{material}

# 下書きの単位

{draft}

下書きのすべての単位について、番号の順に答えてください。"""


def map_schema() -> dict:
    verdict = enum("yes", "no")
    return obj(units=arr(obj(id=INT, **{"from": arr(STR)}, firsthand=verdict)),
               takeaways=arr(obj(index=INT, present=verdict, evidence=arr(INT))),
               skips=arr(obj(index=INT, explained=verdict, evidence=arr(INT))))


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {t}" for i, t in enumerate(items, 1)) or "（なし）"


def map_prompt(draft_units: list[InfoUnit], units: list[Unit], takeaways: list[str], skips: list[str] = ()) -> str:
    return MAP_PROMPT_JA.format(takeaways=_numbered(takeaways), skips=_numbered(skips),
                                material=unit_lines(units, with_mark=False), draft=units_block(draft_units))


class DraftMap(BaseModel):
    sources: dict[int, list[str]] = {}
    firsthand: set[int] = set()
    takeaways: dict[int, tuple[bool, list[int]]] = {}
    skips: dict[int, tuple[bool, list[int]]] = {}


def parse_map(data: dict, draft_units: list[InfoUnit], units: list[Unit], n_takeaways: int, n_skips: int = 0) -> DraftMap:
    ids = {u.id for u in draft_units}
    mids = {u.id for u in units}
    m = DraftMap()
    for row in rows(data, "units"):
        i = row.get("id")
        if i in ids and i not in m.sources:
            m.sources[i] = [x for x in row.get("from", []) if x in mids]
            if row.get("firsthand") == "yes":
                m.firsthand.add(i)
    for row in rows(data, "takeaways"):
        k = row.get("index")
        if isinstance(k, int) and 1 <= k <= n_takeaways and k not in m.takeaways:
            m.takeaways[k] = (row.get("present") == "yes", [e for e in row.get("evidence", []) if e in ids])
    for row in rows(data, "skips"):
        k = row.get("index")
        if isinstance(k, int) and 1 <= k <= n_skips and k not in m.skips:
            m.skips[k] = (row.get("explained") == "yes", [e for e in row.get("evidence", []) if e in ids])
    return m


def draft_chars(draft_units: list[InfoUnit]) -> int:
    return sum(u.chars for u in draft_units)


def space_per_unit(draft_units: list[InfoUnit], m: DraftMap) -> dict[str, float]:
    out: dict[str, float] = {}
    for du in draft_units:
        src = m.sources.get(du.id, [])
        for s in src:
            out[s] = out.get(s, 0.0) + du.chars / len(src)
    return out


_NUM = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*(?![\w])")
_LINK = re.compile(r"\]\(https?://|https?://")


def _blocks(markdown: str) -> list[str]:
    code = code_ranges(markdown)
    return [markdown[a:b] for a, b in blocks(markdown)
            if not overlaps(a, b, code) and not markdown[a:b].lstrip().startswith("#")]


def number_flags(markdown: str, material: str) -> tuple[list[dict], list[dict]]:
    flagged, cited = [], []
    seen: set[str] = set()
    for block in _blocks(markdown):
        plain = re.sub(r"\]\([^)]*\)|https?://\S+|`[^`]*`", " ", block)
        for n in _NUM.findall(plain):
            if n in seen or (len(n) == 1) or n in material:
                continue
            seen.add(n)
            row = {"number": n, "context": excerpt(block, 120)}
            (cited if _LINK.search(block) else flagged).append(row)
    return flagged, cited


Fetch = Callable[[list[str]], list[UrlStatus]]


def _c(id: str, relation: str, passed: bool | None, value=None, detail: str = "", items: list[dict] | None = None,
       runs: int | None = None) -> Check:
    return Check(id=id, relation=relation, passed=passed, value=value, detail=detail, items=items or [],
                 surface=id in SURFACE, runs=runs)


_DASH = re.compile(r"[—―]{1,2}|\s–\s")
_DASH_IGNORE = re.compile(r"`[^`\n]*`|[「『（(\"“]\s*[—―–…‥]+\s*[」』）)\"”]")


def dash_hits(markdown: str) -> list[str]:
    """Dashes quoted as the thing being discussed (「―」) are not punctuation."""
    return [s for line in code_free_lines(markdown) if not line.lstrip().startswith(("|", "#"))
            for s in sentences(line) if _DASH.search(_DASH_IGNORE.sub("", s))]


@dataclass(frozen=True)
class Votes:
    runs: int
    min_votes: int


@dataclass
class Facts:
    draft: str
    units: list[Unit]
    d: Design
    presence: dict[str, str]
    dunits: list[InfoUnit]
    m: DraftMap

    @cached_property
    def use(self) -> dict[str, str | None]:
        return {u.id: self.d.use_of(u.id) for u in self.units}

    @cached_property
    def by_id(self) -> dict[str, Unit]:
        return {u.id: u for u in self.units}

    def with_use(self, *uses: str) -> list[Unit]:
        return [u for u in self.units if self.use[u.id] in uses]

    def missing(self, units: list[Unit]) -> list[Unit]:
        return [u for u in units if self.presence.get(u.id, "no") == "no"]

    def evidence(self, ids: list[int]) -> list[str]:
        return [excerpt(self.dunits[e - 1].text, 80) for e in ids]


def _short(u: Unit) -> dict:
    return {"id": u.id, "text": excerpt(u.text, 80)}


def _share(n: int, total: int) -> str:
    return f"{n}/{total}"


def check_drop_absent(f: Facts) -> Check:
    drop = f.with_use("drop")
    implied = {c.id: c for c in f.d.live_conflicts()}
    present = [u for u in drop if f.presence.get(u.id) == "yes"]
    added = [u for u in present if u.id not in implied]
    return _c("drop_absent", "drop にした単位 → 本文に無い（使う単位から出てしまうもの＝implied は除く）",
              not added if drop else None, _share(len(added), len(drop)),
              f"本文に出た drop の単位（一部だけ出たものは数えない）。implied {len(present) - len(added)}",
              [_short(u) | {"status": "implied" if u.id in implied else "added"}
               | ({"by": "・".join(implied[u.id].by)} if u.id in implied else {}) for u in present])


def check_deep_present(f: Facts) -> Check:
    deep = f.with_use("deep")
    missing = f.missing(deep)
    return _c("deep_present", "deep にした単位 → 本文にある", not missing if deep else None,
              _share(len(deep) - len(missing), len(deep)), "本文に無い deep の単位", [_short(u) for u in missing])


def check_deep_space(f: Facts) -> Check:
    relation = "deep の単位あたりの字数 > mention の単位あたりの字数"
    deep = f.with_use("deep")
    aside = f.d.aside_ids()
    mention = [u for u in f.with_use("mention") if u.id not in aside]
    if not deep or not mention:
        return _c("deep_space", relation, None, None, "deep か mention の単位が無い")
    space = space_per_unit(f.dunits, f.m)
    dm = sum(space.get(u.id, 0.0) for u in deep) / len(deep)
    mm = sum(space.get(u.id, 0.0) for u in mention) / len(mention)
    return _c("deep_space", relation, dm > mm, round(dm / mm, 2) if mm else None,
              f"deep {dm:.0f} 字 / mention {mm:.0f} 字（本文の単位を材料へ割り戻した平均）",
              [{"id": u.id, "use": f.use[u.id], "chars": round(space.get(u.id, 0.0))} for u in deep + mention])


def check_firsthand(f: Facts) -> Check:
    over = material_load(f.d, f.units)["over"]
    fh = [u for u in f.with_use(*(("deep",) if over else ("deep", "mention"))) if u.firsthand]
    lost = f.missing(fh)
    share = (len(fh) - len(lost)) / len(fh) if fh else None
    which = "掘り下げると決めた手元だけの単位（材料が多いので触れる単位は数えない）" if over else "使うと決めた手元だけの単位"
    return _c("firsthand_retained", f"{which} → {FIRSTHAND_MIN:.0%} 以上が本文に残る",
              None if share is None else share >= FIRSTHAND_MIN, None if share is None else round(share, 2),
              "本文に無い手元だけの単位", [_short(u) for u in lost])


def _judged(labels: list[str], found: dict[int, tuple[bool, list[int]]], f: Facts, key: str, flag: str) -> list[dict]:
    out = []
    for k, label in enumerate(labels, 1):
        ok, ev = found.get(k, (False, []))
        out.append({key: label, flag: ok, "evidence": f.evidence(ev)})
    return out


def check_takeaways(f: Facts) -> Check:
    items = _judged(f.d.takeaways, f.m.takeaways, f, "takeaway", "present")
    return _c("takeaways", "持ち帰り → 本文から読み取れる", all(x["present"] for x in items) if items else None,
              _share(sum(x["present"] for x in items), len(items)), "", items)


def check_skips(f: Facts) -> Check:
    items = _judged([x.label for x in f.d.skip], f.m.skips, f, "skip", "explained")
    explained = [x for x in items if x["explained"]]
    return _c("skip_unexplained", "skip にした前提 → 本文で説明しない", not explained if items else None,
              _share(len(explained), len(items)), "説明されてしまった前提", explained)


def check_fabrication(f: Facts) -> Check:
    fab = [{"unit": du.id, "text": excerpt(du.text, 120), "source": "judge"} for du in f.dunits
           if du.id in f.m.firsthand and not any(f.by_id[s].firsthand for s in f.m.sources.get(du.id, []))]
    seen = {x["unit"] for x in fab}
    unmapped = [du for du in f.dunits if not f.m.sources.get(du.id)]
    for s in firsthand_hits(f.draft):
        at = locate(f.draft, s)
        hit = next((du for du in unmapped if at and du.start <= at[0] < du.end), None)
        if hit and hit.id not in seen:
            seen.add(hit.id)
            fab.append({"unit": hit.id, "text": s[:120], "source": "rule"})
    return _c("fabrication", "材料に無い一人称の体験 → 無い", not fab, len(fab),
              "書き手の体験として書かれているのに、もとになる手元の材料が無い単位", fab)


def check_numbers(draft: str, material: str) -> Check:
    flagged, cited = number_flags(draft, material)
    return _c("numbers", "材料に無い数値 → 出典のリンクがある", not flagged, len(flagged),
              f"出典のリンクが同じ段落にある数値 {len(cited)} 個は数えない", flagged)


def check_links(urls: list[str], statuses: list[UrlStatus] | None) -> Check:
    relation = "材料に無いリンク → 開ける"
    if statuses is None:
        return _c("links", relation, None, len(urls), "確かめていない（--verify-links）", [{"url": u} for u in urls])
    bad = [s for s in statuses if s.verdict in ("dead", "unreachable")]
    return _c("links", relation, not bad, _share(len(urls) - len(bad), len(urls)), "",
              [{"url": s.url, "verdict": s.verdict, "status": s.status} for s in statuses])


def check_asides(f: Facts) -> Check:
    asides = [f.by_id[a.id] for a in f.d.aside if a.id in f.by_id]
    lost = f.missing(asides)
    return _c("aside_present", "aside にした脱線 → 本文にある", not lost if asides else None,
              _share(len(asides) - len(lost), len(asides)), "本文に無い脱線", [_short(u) for u in lost])


READER_PROMPT_JA = DATA_NOTE_JA + """

あなたは次の記事の読者です。読者は「{audience}」で、記事の種類は「{kind}」、題は「{topic}」です。この読者になりきって下書きを最初から読み、つまずく所を挙げてください。

- term: 説明なしに使われている用語・略語・この記事で作られた言葉（初めて出る所で、何であるかが書かれていない）
- number: 何を測ったか・何と比べたか・なぜ大事かが書かれていない数値や指標
- jump: 前提が抜けていて、前の文から次の文へ論理が飛んでいる所
- density: 一つの段落や文に数値・項目・比較が詰まっていて、表や番号付きリストにした方が読みやすい所（fix に、どの形にするかを書く）
- figure: 文だけでは関係や流れがつかみにくく、図があると分かりやすい所（fix に、何を示す図かを 1 文で書く）。すでに `<!-- 図: … -->` の目印がある所は挙げない

それぞれについて:
- kind: term / number / jump / density / figure
- quote: 下書きの中の該当する文字列を、そのまま写す（1 文の中の 40 字くらいまで。言い換えない）
- why: この読者にとって何が分からないかを 1 文で
- fix: 何を足せば分かるかを 1 文で
- unit: それを説明している材料の単位の番号（下の材料にあれば。無ければ空）

この読者がふつう知っていることは挙げません。あとで説明が出てくる場合も、初めて出る所で分からなければ挙げます。読者のつまずきが大きい順に、{max_items} 個まで。

# 材料（説明を探すため）

{material}

# 下書き

{draft}"""

READER_KIND_LABEL = {"term": "用語", "number": "数字", "jump": "飛躍", "density": "密度", "figure": "図"}
FORM_KINDS = ("density", "figure")
READER_MAX = 16


def reader_schema() -> dict:
    return obj(findings=arr(obj(kind=enum(*READER_KIND_LABEL), quote=STR, why=STR, fix=STR, unit=STR), READER_MAX))


def reader_material(d: Design, units: list[Unit]) -> list[Unit]:
    want = {u.id for u in d.units if u.use in ("deep", "mention")} | {i for t in d.terms for i in t.defined_by}
    return [u for u in units if u.id in want]


def reader_prompt(p: Project, d: Design, units: list[Unit], draft: str) -> str:
    return READER_PROMPT_JA.format(audience=p.audience, kind=d.kind, topic=p.topic, max_items=READER_MAX,
                                   material=unit_lines(reader_material(d, units), with_mark=False) or "（なし）",
                                   draft=draft.strip())


def parse_reader(data: dict, draft: str, units: list[Unit]) -> list[dict]:
    by = {u.id: u for u in units}
    out, seen = [], set()
    for row in rows(data, "findings"):
        kind, quote = row.get("kind"), str(row.get("quote", "")).strip()
        if kind not in READER_KIND_LABEL or not quote or quote in seen:
            continue
        seen.add(quote)
        unit = row.get("unit") if row.get("unit") in by else ""
        out.append({"kind": kind, "quote": quote, "why": str(row.get("why", "")).strip(),
                    "fix": str(row.get("fix", "")).strip(), "unit": unit,
                    "unit_text": excerpt(by[unit].text, 200) if unit else "", "placed": locate(draft, quote) is not None})
    return out


def check_reader(findings: list[dict], audience: str) -> Check:
    findings = [f for f in findings if f["kind"] not in FORM_KINDS]
    placed = sum(f["placed"] for f in findings)
    return _c("reader", f"読者（{audience}）が初めて読んで分からない所 → 無い", not findings, len(findings),
              f"用語 {sum(f['kind'] == 'term' for f in findings)}・数字 {sum(f['kind'] == 'number' for f in findings)}・"
              f"飛躍 {sum(f['kind'] == 'jump' for f in findings)}（本文で位置が分かったもの {placed}）", findings)


def check_form(findings: list[dict], audience: str) -> Check:
    findings = [f for f in findings if f["kind"] in FORM_KINDS]
    return _c("form", f"読者（{audience}）にとって、表・リストにすると読みやすい所や、図があると分かる所 → 無い", not findings,
              len(findings), f"密度 {sum(f['kind'] == 'density' for f in findings)}・"
              f"図 {sum(f['kind'] == 'figure' for f in findings)}", findings)


def check_length(chars: int, target: int) -> Check:
    ratio = chars / target if target else None
    return _c("length", f"字数 → 目標の ±{LENGTH_TOLERANCE:.0%}", None if ratio is None else abs(ratio - 1) <= LENGTH_TOLERANCE,
              None if ratio is None else round(ratio, 2), f"{chars} 字 / 目標 {target} 字")


def run_checks(name: str, draft: str, p: Project, units: list[Unit], d: Design, judge: Provider, meta: Provider | None,
               votes: Votes, keep: set[str], fetch: Fetch | None = None) -> CheckReport:
    d = sync_design(d, units)
    units = [*units, *note_units(d, units)]
    infos, idmap = as_info_units(units)
    dunits = info_units(draft)
    skips = [x.label for x in d.skip]
    material = "\n".join(u.text for u in units)
    urls = [u for u in extract_urls(draft) if u not in material]
    with ThreadPoolExecutor(5) as pool:
        cov = pool.submit(ask_json, judge, coverage_prompt(infos, {DRAFT: draft}), coverage_schema())
        mapped = pool.submit(ask_json, judge, map_prompt(dunits, units, d.takeaways, skips), map_schema())
        reader = pool.submit(ask_json, judge, reader_prompt(p, d, units, draft), reader_schema())
        surface = pool.submit(surface_hits, draft, units, meta, votes)
        links = pool.submit(fetch, urls) if fetch is not None else None
        presence = {idmap[i]: c.v for i, c in parse_coverage(cov.result(), infos, [DRAFT]).items()}
        m = parse_map(mapped.result(), dunits, units, len(d.takeaways), len(skips))
        f = Facts(draft, units, d, presence, dunits, m)
        checks = [check_drop_absent(f), check_deep_present(f), check_deep_space(f), check_firsthand(f),
                  check_takeaways(f), check_fabrication(f), check_numbers(draft, material),
                  check_links(urls, links.result() if links else None), check_skips(f), check_asides(f),
                  *reader_checks(parse_reader(reader.result(), draft, units), p.audience),
                  *surface_report_checks(draft, surface.result(), keep, noted_texts(dunits, m.sources)),
                  check_length(draft_chars(dunits), d.target_length)]
    return CheckReport(draft=name, chars=draft_chars(dunits), checks=checks, sources=m.sources,
                       figures=figure_markers(draft))


def reader_checks(findings: list[dict], audience: str) -> list[Check]:
    return [check_reader(findings, audience), check_form(findings, audience)]


def surface_hits(draft: str, units: list[Unit], meta: Provider | None, votes: Votes) -> SurfaceReport:
    if meta is None:
        su = split_sentences(draft)
        hints = rule_hints(su)
        return SurfaceReport(units=len(su), hits=[SurfaceHit(id=u.id, category=hints[u.id], text=u.text, heading=u.heading,
                                                             votes=1) for u in su if u.id in hints])
    return detect_surface(draft, meta, [u.text for u in units], votes.runs, votes.min_votes)


def noted_texts(dunits: list[InfoUnit], sources: dict) -> tuple[str, ...]:
    return from_notes({str(du.id): du.text for du in dunits}, {str(k): v for k, v in sources.items()})


def surface_checks(draft: str, units: list[Unit], meta: Provider | None, votes: Votes, keep: set[str],
                   protect: tuple[str, ...] = ()) -> list[Check]:
    return surface_report_checks(draft, surface_hits(draft, units, meta, votes), keep, protect)


def surface_report_checks(draft: str, sr: SurfaceReport, keep: set[str], protect: tuple[str, ...]) -> list[Check]:
    noted = [h for h in sr.hits if guarded(h.text, protect)]
    held = [h for h in sr.hits if text_hash(h.text) in keep and h not in noted]
    kept = held + noted
    meta_hits = [h for h in sr.hits if h.category not in (GLUE, CAVEAT, *FLOW) and h not in kept]
    flow_hits = [h for h in sr.hits if h.category in FLOW and h not in kept]
    caveat_hits = [h for h in sr.hits if h.category == CAVEAT and h not in kept]
    glue_hits = [h for h in sr.hits if h.category == GLUE and h not in kept]
    runs = sr.runs_used or None
    how = f"{runs} 回の判定の多数決" if runs else "規則だけ"
    how += f"。著者の一言から来た文 {len(noted)} は数えない" if noted else ""
    out = [_c("meta", "メタ言説 → 無い", not meta_hits, len(meta_hits),
              how + (f"。残すと決めた文 {len(held)} は数えない" if held else ""),
              [{"id": h.id, "category": h.category, "text": h.text, "votes": h.votes} for h in meta_hits], runs),
           _c("caveat", "結論の読み方を変えない保守的な但し書き → 無い", not caveat_hits, len(caveat_hits), how,
              [{"id": h.id, "text": h.text, "votes": h.votes} for h in caveat_hits], runs),
           _c("glue", "材料の事実を運ばない、話題を読者の役立ちに結びつけるだけの文 → 無い", not glue_hits, len(glue_hits),
              f"{how}。材料の文をほぼ繰り返すので除いた文 {len(sr.traced)}",
              [{"id": h.id, "text": h.text, "votes": h.votes} for h in glue_hits], runs),
           _c("flow", "段落の頭で前の段落を受けて理由づけするだけの部分と、段落を解釈で結ぶだけの文 → 無い", not flow_hits,
              len(flow_hits), how, [{"id": h.id, "category": h.category, "text": h.text, "votes": h.votes} for h in flow_hits],
              runs)]
    lf = [{"rule": f.rule, "text": f.excerpt} for f in parts_lint(parse_parts(draft)) if f.rule in LINT_RULES]
    lf += [{"rule": "dash", "text": s} for s in dash_hits(draft) if text_hash(s) not in keep]
    out.append(_c("lint", "太字で始まる箇条書き・ダッシュ・絵文字・飾り記号・太字の多用 → 無い", not lf, len(lf), "", lf))
    return out


MARK = {True: "OK ", False: "NG ", None: "-  "}


def report_text(rep: CheckReport) -> str:
    n_struct = sum(not c.surface for c in rep.checks)
    lines = [(f"{rep.draft}: {rep.chars} 字、構造・内容の失敗 {len(rep.structural_failed())} / {n_struct}、"
              f"表面の失敗 {len(rep.failed()) - len(rep.structural_failed())} / {len(rep.checks) - n_struct}")]
    for c in sorted(rep.checks, key=lambda c: c.surface):
        lines.append(f"{MARK[c.passed]} {('表面 ' if c.surface else '') + c.id:20} {'' if c.value is None else c.value!s:>8}  {c.relation}")
        if c.detail and (c.passed is False or c.items):
            lines.append(f"      {c.detail}")
        if c.passed is not True or c.id == "drop_absent":
            for it in c.items[:8]:
                lines.append("      - " + " / ".join(f"{k}: {v}" for k, v in it.items()))
            if len(c.items) > 8:
                lines.append(f"      …ほか {len(c.items) - 8} 件")
    if rep.figures:
        lines.append(f"情報  図の目印 {len(rep.figures)} 個（失敗ではない。後で図にする所）")
        lines += [f"      - {f['text']}（近く: {f['near']}）" for f in rep.figures]
    return "\n".join(lines) + "\n"


def check(wd: WorkDir, judge: Provider, meta: Provider | None, name: str, votes: Votes, fetch: Fetch | None = None,
          surface_only: bool = False) -> CheckReport:
    text = wd.read(name)
    keep = KeepStore(wd).hashes()
    if surface_only:
        full = fresh_report(wd, name, False)
        protect = noted_texts(info_units(text), full.sources) if full else ()
        rep = CheckReport(draft=name, chars=draft_chars(info_units(text)),
                          checks=surface_checks(text, wd.units(), meta, votes, keep, protect), figures=figure_markers(text))
    else:
        rep = run_checks(name, text, wd.project(), wd.units(), wd.design(), judge, meta, votes, keep, fetch)
    stem = check_stem(name, surface_only)
    wd.write_json(f"{stem}.json", rep.model_dump())
    wd.write(f"{stem}.txt", report_text(rep))
    return rep


def fresh_report(wd: WorkDir, draft: str, surface_only: bool) -> CheckReport | None:
    p = wd.root / f"{check_stem(draft, surface_only)}.json"
    if p.is_file() and p.stat().st_mtime >= (wd.root / draft).stat().st_mtime:
        return CheckReport.model_validate_json(p.read_text(encoding="utf-8"))
    return None


def check_stem(draft_name: str, surface_only: bool = False) -> str:
    return draft_name.removesuffix(".md").replace("draft", "check") + (".surface" if surface_only else "")


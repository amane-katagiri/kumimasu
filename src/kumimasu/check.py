from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel

from .design import sync_design
from .factcheck import UrlStatus, extract_urls, firsthand_hits
from .generate import DATA_NOTE_JA
from .interview import unit_lines
from .llm import extract_json
from .metadiscourse import code_free_lines, sentences, split_units
from .model import Design, Unit
from .parts.lint import lint as parts_lint
from .parts.markdown import parse as parse_parts
from .payload import (
    InfoUnit,
    coverage_prompt,
    coverage_schema,
    info_units,
    parse_coverage,
    units_block,
)
from .surface import (
    GLUE,
    MIN_VOTES,
    RUNS,
    SurfaceHit,
    SurfaceReport,
    detect_surface,
    rule_hints,
)
from .workdir import WorkDir, as_info_units

if TYPE_CHECKING:
    from .llm import Provider

DRAFT = "D"
LENGTH_TOLERANCE = 0.2
FIRSTHAND_MIN = 0.8
LINT_RULES = ("bold-lead-item", "emoji", "decor-symbol", "bold-density")


SURFACE = ("meta", "glue", "lint")


class Check(BaseModel):
    id: str
    relation: str
    surface: bool = False
    passed: bool | None
    value: float | int | str | None = None
    detail: str = ""
    items: list[dict] = []


class CheckReport(BaseModel):
    draft: str
    chars: int
    checks: list[Check]
    sources: dict[int, list[str]] = {}

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
    return {"type": "object", "additionalProperties": False, "required": ["units", "takeaways"], "properties": {
        "units": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                             "required": ["id", "from", "firsthand"],
                                             "properties": {"id": {"type": "integer"},
                                                            "from": {"type": "array", "items": {"type": "string"}},
                                                            "firsthand": {"type": "string", "enum": ["yes", "no"]}}}},
        "takeaways": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                 "required": ["index", "present", "evidence"],
                                                 "properties": {"index": {"type": "integer"},
                                                                "present": {"type": "string", "enum": ["yes", "no"]},
                                                                "evidence": {"type": "array", "items": {"type": "integer"}}}}},
        "skips": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                             "required": ["index", "explained", "evidence"],
                                             "properties": {"index": {"type": "integer"},
                                                            "explained": {"type": "string", "enum": ["yes", "no"]},
                                                            "evidence": {"type": "array", "items": {"type": "integer"}}}}}}}


def map_prompt(draft_units: list[InfoUnit], units: list[Unit], takeaways: list[str], skips: list[str] = ()) -> str:
    return MAP_PROMPT_JA.format(takeaways="\n".join(f"{i}. {t}" for i, t in enumerate(takeaways, 1)) or "（なし）",
                                skips="\n".join(f"{i}. {t}" for i, t in enumerate(skips, 1)) or "（なし）",
                                material=unit_lines(units, with_mark=False), draft=units_block(draft_units))


class DraftMap(BaseModel):
    sources: dict[int, list[str]] = {}
    firsthand: set[int] = set()
    takeaways: dict[int, tuple[bool, list[int]]] = {}
    skips: dict[int, tuple[bool, list[int]]] = {}


def parse_map(raw: str, draft_units: list[InfoUnit], units: list[Unit], n_takeaways: int, n_skips: int = 0) -> DraftMap:
    data = extract_json(raw)
    ids = {u.id for u in draft_units}
    mids = {u.id for u in units}
    m = DraftMap()
    for row in data.get("units", []):
        i = row.get("id")
        if i in ids and i not in m.sources:
            m.sources[i] = [x for x in row.get("from", []) if x in mids]
            if row.get("firsthand") == "yes":
                m.firsthand.add(i)
    for row in data.get("takeaways", []):
        k = row.get("index")
        if isinstance(k, int) and 1 <= k <= n_takeaways and k not in m.takeaways:
            m.takeaways[k] = (row.get("present") == "yes", [e for e in row.get("evidence", []) if e in ids])
    for row in data.get("skips", []):
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
    text = "\n".join(code_free_lines(markdown))
    return [b for b in re.split(r"\n\s*\n", text) if b.strip() and not b.lstrip().startswith("#")]


def number_flags(markdown: str, material: str) -> tuple[list[dict], list[dict]]:
    flagged, cited = [], []
    seen: set[str] = set()
    for block in _blocks(markdown):
        plain = re.sub(r"\]\([^)]*\)|https?://\S+|`[^`]*`", " ", block)
        for n in _NUM.findall(plain):
            if n in seen or (len(n) == 1) or n in material:
                continue
            seen.add(n)
            row = {"number": n, "context": re.sub(r"\s+", " ", block)[:120]}
            (cited if _LINK.search(block) else flagged).append(row)
    return flagged, cited


Fetch = Callable[[list[str]], list[UrlStatus]]


def _c(id: str, relation: str, passed: bool | None, value=None, detail: str = "", items: list[dict] | None = None) -> Check:
    return Check(id=id, relation=relation, passed=passed, value=value, detail=detail, items=items or [],
                 surface=id in SURFACE)


_DASH = re.compile(r"[—―]{1,2}|\s–\s")
_DASH_IGNORE = re.compile(r"`[^`\n]*`|[「『（(\"“]\s*[—―–…‥]+\s*[」』）)\"”]")
KEEP_FILE = "keep.yaml"


def dash_hits(markdown: str) -> list[str]:
    """Dashes quoted as the thing being discussed (「―」) are not punctuation."""
    return [s for line in code_free_lines(markdown) if not line.lstrip().startswith(("|", "#"))
            for s in sentences(line) if _DASH.search(_DASH_IGNORE.sub("", s))]


def text_hash(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", "", text).encode()).hexdigest()[:16]


def load_keep(wd: WorkDir) -> set[str]:
    p = wd.root / KEEP_FILE
    if not p.exists():
        return set()
    return {row["hash"] for row in yaml.safe_load(p.read_text(encoding="utf-8")) or []}


def run_checks(draft: str, units: list[Unit], d: Design, judge: Provider, meta: Provider | None,
               fetch: Fetch | None = None, surface_runs: int = RUNS,
               keep: set[str] | frozenset[str] = frozenset(), min_votes: int = MIN_VOTES) -> CheckReport:
    d = sync_design(d, units)
    use = {u.id: d.use_of(u.id) for u in units}
    by_id = {u.id: u for u in units}
    infos, idmap = as_info_units(units)
    cov = parse_coverage(judge.complete(coverage_prompt(infos, {DRAFT: draft}), json_schema=coverage_schema()), infos, [DRAFT])
    presence = {idmap[i]: c.v for i, c in cov.items()}
    dunits = info_units(draft)
    skips = [x.label for x in d.skip]
    m = parse_map(judge.complete(map_prompt(dunits, units, d.takeaways, skips), json_schema=map_schema()), dunits, units,
                  len(d.takeaways), len(skips))
    space = space_per_unit(dunits, m)
    checks: list[Check] = []

    def short(u: Unit) -> str:
        return re.sub(r"\s+", " ", u.text)[:80]

    drop = [u for u in units if use[u.id] == "drop"]
    implied = {c.id: c for c in d.live_conflicts()}
    present_drop = [u for u in drop if presence.get(u.id) == "yes"]
    added = [u for u in present_drop if u.id not in implied]
    checks.append(_c("drop_absent", "drop にした単位 → 本文に無い（使う単位から出てしまうもの＝implied は除く）",
                     not added if drop else None, f"{len(added)}/{len(drop)}",
                     f"本文に出た drop の単位（一部だけ出たものは数えない）。implied {len(present_drop) - len(added)}",
                     [{"id": u.id, "status": "implied" if u.id in implied else "added", "text": short(u)}
                      | ({"by": "・".join(implied[u.id].by)} if u.id in implied else {}) for u in present_drop]))
    deep = [u for u in units if use[u.id] == "deep"]
    missing_deep = [u for u in deep if presence.get(u.id, "no") == "no"]
    checks.append(_c("deep_present", "deep にした単位 → 本文にある", not missing_deep if deep else None,
                     f"{len(deep) - len(missing_deep)}/{len(deep)}", "本文に無い deep の単位",
                     [{"id": u.id, "text": short(u)} for u in missing_deep]))
    mention = [u for u in units if use[u.id] == "mention" and u.id not in d.aside_ids()]
    if deep and mention:
        dm = sum(space.get(u.id, 0.0) for u in deep) / len(deep)
        mm = sum(space.get(u.id, 0.0) for u in mention) / len(mention)
        checks.append(_c("deep_space", "deep の単位あたりの字数 > mention の単位あたりの字数", dm > mm,
                         round(dm / mm, 2) if mm else None, f"deep {dm:.0f} 字 / mention {mm:.0f} 字（本文の単位を材料へ割り戻した平均）",
                         [{"id": u.id, "use": use[u.id], "chars": round(space.get(u.id, 0.0))} for u in deep + mention]))
    else:
        checks.append(_c("deep_space", "deep の単位あたりの字数 > mention の単位あたりの字数", None, None,
                         "deep か mention の単位が無い"))
    fh = [u for u in units if u.firsthand and use[u.id] in ("deep", "mention")]
    kept_fh = [u for u in fh if presence.get(u.id, "no") != "no"]
    share = len(kept_fh) / len(fh) if fh else None
    checks.append(_c("firsthand_retained", f"使うと決めた手元だけの単位 → {FIRSTHAND_MIN:.0%} 以上が本文に残る",
                     None if share is None else share >= FIRSTHAND_MIN, None if share is None else round(share, 2),
                     "本文に無い手元だけの単位", [{"id": u.id, "text": short(u)} for u in fh if u not in kept_fh]))
    tk_items = []
    for k, t in enumerate(d.takeaways, 1):
        ok, ev = m.takeaways.get(k, (False, []))
        tk_items.append({"takeaway": t, "present": ok,
                         "evidence": [re.sub(r"\s+", " ", dunits[e - 1].text)[:80] for e in ev]})
    checks.append(_c("takeaways", "持ち帰り → 本文から読み取れる",
                     all(x["present"] for x in tk_items) if tk_items else None,
                     f"{sum(x['present'] for x in tk_items)}/{len(tk_items)}", "", tk_items))
    fab = []
    for du in dunits:
        src = m.sources.get(du.id, [])
        if du.id in m.firsthand and not any(by_id[s].firsthand for s in src):
            fab.append({"unit": du.id, "text": re.sub(r"\s+", " ", du.text)[:120], "source": "judge"})
    unmapped = [du for du in dunits if not m.sources.get(du.id)]
    for s in firsthand_hits(draft):
        hit = next((du for du in unmapped if s[:20] in du.text), None)
        if hit and not any(f["unit"] == hit.id for f in fab):
            fab.append({"unit": hit.id, "text": s[:120], "source": "rule"})
    checks.append(_c("fabrication", "材料に無い一人称の体験 → 無い", not fab, len(fab),
                     "書き手の体験として書かれているのに、もとになる手元の材料が無い単位", fab))
    material = "\n".join(u.text for u in units)
    flagged, cited = number_flags(draft, material)
    checks.append(_c("numbers", "材料に無い数値 → 出典のリンクがある", not flagged, len(flagged),
                     f"出典のリンクが同じ段落にある数値 {len(cited)} 個は数えない", flagged))
    new_urls = [u for u in extract_urls(draft) if u not in material]
    if fetch is None:
        checks.append(_c("links", "材料に無いリンク → 開ける", None, len(new_urls), "確かめていない（--verify-links）",
                         [{"url": u} for u in new_urls]))
    else:
        st = fetch(new_urls)
        bad = [s for s in st if s.verdict in ("dead", "unreachable")]
        checks.append(_c("links", "材料に無いリンク → 開ける", not bad, f"{len(new_urls) - len(bad)}/{len(new_urls)}",
                         "", [{"url": s.url, "verdict": s.verdict, "status": s.status} for s in st]))
    sk_items = []
    for k, label in enumerate(skips, 1):
        explained, ev = m.skips.get(k, (False, []))
        sk_items.append({"skip": label, "explained": explained,
                         "evidence": [re.sub(r"\s+", " ", dunits[e - 1].text)[:80] for e in ev]})
    checks.append(_c("skip_unexplained", "skip にした前提 → 本文で説明しない",
                     not any(x["explained"] for x in sk_items) if sk_items else None,
                     f"{sum(x['explained'] for x in sk_items)}/{len(sk_items)}", "説明されてしまった前提",
                     [x for x in sk_items if x["explained"]]))
    asides = [by_id[a.id] for a in d.aside if a.id in by_id]
    lost = [u for u in asides if presence.get(u.id, "no") == "no"]
    checks.append(_c("aside_present", "aside にした脱線 → 本文にある", not lost if asides else None,
                     f"{len(asides) - len(lost)}/{len(asides)}", "本文に無い脱線", [{"id": u.id, "text": short(u)} for u in lost]))
    checks += surface_checks(draft, units, meta, surface_runs, keep, min_votes)
    chars = draft_chars(dunits)
    ratio = chars / d.target_length if d.target_length else None
    checks.append(_c("length", f"字数 → 目標の ±{LENGTH_TOLERANCE:.0%}",
                     None if ratio is None else abs(ratio - 1) <= LENGTH_TOLERANCE,
                     None if ratio is None else round(ratio, 2), f"{chars} 字 / 目標 {d.target_length} 字"))
    return CheckReport(draft="", chars=chars, checks=checks, sources=m.sources)


def surface_hits(draft: str, units: list[Unit], meta: Provider | None, runs: int = RUNS,
                 min_votes: int = MIN_VOTES) -> SurfaceReport:
    if meta is None:
        su = split_units(draft)
        by_id = {u.id: u for u in su}
        hits = [SurfaceHit(id=i, category=c, text=by_id[i].text, heading=by_id[i].heading, votes=1)
                for i, c in rule_hints(su).items()]
        return SurfaceReport(units=len(su), hits=sorted(hits, key=lambda h: su.index(by_id[h.id])))
    return detect_surface(draft, meta, [u.text for u in units], runs, min_votes)


def surface_checks(draft: str, units: list[Unit], meta: Provider | None, runs: int = RUNS,
                   keep: set[str] | frozenset[str] = frozenset(), min_votes: int = MIN_VOTES) -> list[Check]:
    sr = surface_hits(draft, units, meta, runs, min_votes)
    kept = [h for h in sr.hits if text_hash(h.text) in keep]
    meta_hits = [h for h in sr.hits if h.category != GLUE and h not in kept]
    glue_hits = [h for h in sr.hits if h.category == GLUE and h not in kept]
    votes = f"{sr.runs_used} 回の判定の多数決" if sr.runs_used else "規則だけ"
    out = [_c("meta", "メタ言説 → 無い", not meta_hits, len(meta_hits), votes,
              [{"id": h.id, "category": h.category, "text": h.text, "votes": h.votes} for h in meta_hits]),
           _c("glue", "材料の事実を運ばない、話題を読者の役立ちに結びつけるだけの文 → 無い", not glue_hits, len(glue_hits),
              f"{votes}。材料の文をほぼ繰り返すので除いた文 {len(sr.traced)}",
              [{"id": h.id, "text": h.text, "votes": h.votes} for h in glue_hits])]
    lf = [{"rule": f.rule, "text": f.excerpt} for f in parts_lint(parse_parts(draft)) if f.rule in LINT_RULES]
    lf += [{"rule": "dash", "text": s} for s in dash_hits(draft) if text_hash(s) not in keep]
    if kept:
        out[0].detail += f"。残すと決めた文 {len(kept)} は数えない"
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
    return "\n".join(lines) + "\n"


def check(wd: WorkDir, judge: Provider, meta: Provider | None, name: str = "draft.md",
          fetch: Fetch | None = None, surface_only: bool = False, surface_runs: int = RUNS,
          min_votes: int = MIN_VOTES) -> CheckReport:
    text = wd.read(name)
    if surface_only:
        rep = CheckReport(draft=name, chars=draft_chars(info_units(text)),
                          checks=surface_checks(text, wd.units(), meta, surface_runs, load_keep(wd), min_votes))
    else:
        rep = run_checks(text, wd.units(), wd.design(), judge, meta, fetch, surface_runs, load_keep(wd), min_votes)
    rep.draft = name
    stem = check_stem(name, surface_only)
    wd.write_json(f"{stem}.json", rep.model_dump())
    wd.write(f"{stem}.txt", report_text(rep))
    return rep


def check_stem(draft_name: str, surface_only: bool = False) -> str:
    return draft_name.removesuffix(".md").replace("draft", "check") + (".surface" if surface_only else "")


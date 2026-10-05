from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .generate import DATA_NOTE_JA
from .interview import unit_lines
from .llm import BOOL, STR, arr, ask_json, ids_in, obj, rows
from .model import Design, Project, Term, Unit, UnitUse
from .textutil import excerpt, norm

if TYPE_CHECKING:
    from .llm import Provider

TERMS_PROMPT_JA = DATA_NOTE_JA + """

著者が「{topic}」について記事を書きます。読者は{audience}、記事の種類は「{kind}」です。下の「使う材料」は記事に載せると決めた単位、「ほかの材料」は今は載せないことにした単位です（どちらも番号付き）。

使う材料に出てくるもののうち、この読者が説明なしでは分からないものを挙げ、それを説明している単位を探してください。

- 挙げるもの: この材料の中で作られた言葉（プロジェクトで名付けた概念・手法・段階の名前）、独自の指標やスコア、略語、何を測ったか・何と比べたかが分からないと意味の取れない数値、この読者になじみの薄い専門用語。
- 挙げないもの: この読者がふつう知っている一般的な言葉（reader_knows を true にするか、挙げない）。
- term: 名前（30 字以内。材料での書き方のまま。数値なら「〜の 0.62」のように何の数値かが分かる形）
- used_by: その言葉を使っている「使う材料」の単位の番号
- defined_by: それが何か・何を測ったか・どう求めたか・何と比べたかを説明している単位の番号。使う材料・ほかの材料のどちらからでも選び、よく説明している順に最大 4 つ。無ければ空
- reader_knows: この読者が説明なしで分かるなら true
- why: この読者に分からない理由を 1 文で{known}

# 使う材料

{kept}

# ほかの材料

{other}"""

KNOWN_NOTE_JA = ("\n\nすでに挙がった言葉: {terms}\n同じものを挙げるときは同じ名前にし、"
                 "この依頼のほかの材料にある説明の単位を defined_by に入れてください。")

TERMS_BATCH_CHARS = 60000
TERMS_MIN_SLICE = 20000
TERMS_MAX = 30
TERM_CHARS = 40
DEFINED_MAX = 4
DEFINED_MIN_RELEVANCE = 0.5
SUGGEST_MAX = 8
TIE_LOW = 0.15
KEPT = ("deep", "mention")
STATUS_ORDER = {"dropped": 0, "missing": 1, "kept": 2, "skip": 3}
STATUS_LABEL = {"dropped": "説明が書かない側", "missing": "材料に説明が無い", "kept": "説明を使う", "skip": "説明しない前提"}


def terms_schema() -> dict:
    return obj(terms=arr(obj(term=STR, used_by=arr(STR), defined_by=arr(STR), reader_knows=BOOL,
                             why=STR), TERMS_MAX))


def terms_prompt(p: Project, kept: list[Unit], other: list[Unit], known: list[Term]) -> str:
    note = KNOWN_NOTE_JA.format(terms=" / ".join(t.term for t in known)) if known else ""
    return TERMS_PROMPT_JA.format(topic=p.topic, audience=p.audience, kind=p.kind, known=note,
                                  kept=unit_lines(kept, with_mark=False) or "（なし）",
                                  other=unit_lines(other, with_mark=False) or "（なし）")


def parse_terms(data: dict, kept_ids: set[str], shown_ids: set[str]) -> list[Term]:
    out = []
    for row in rows(data, "terms"):
        name = str(row.get("term", "")).strip()
        if not name or len(name) > TERM_CHARS or row.get("reader_knows") is True:
            continue
        used = ids_in(row.get("used_by"), kept_ids)
        defined = ids_in(row.get("defined_by"), shown_ids)[:DEFINED_MAX]
        if used:
            out.append(Term(term=name, used_by=used, defined_by=defined, why=str(row.get("why", ""))))
    return out


def _key(name: str) -> str:
    return "".join(name.split()).lower()


def _chunks(units: list[Unit], budget: int) -> list[list[Unit]]:
    out: list[list[Unit]] = [[]]
    size = 0
    for u in units:
        if out[-1] and size + len(u.text) > budget:
            out.append([])
            size = 0
        out[-1].append(u)
        size += len(u.text)
    return out


def relevance(term: str, text: str) -> float:
    t, body = norm(term).lower(), norm(text).lower()
    if not t:
        return 0.0
    if t in body:
        return 1.0
    grams = _bigrams(term)
    return round(len(grams & _bigrams(text)) / len(grams), 2) if grams else 0.0


def rank_definitions(term: str, ids: list[str], by: dict[str, Unit]) -> list[str]:
    scored = [(relevance(term, by[i].text), n, i) for n, i in enumerate(ids) if i in by]
    return [i for score, _, i in sorted(scored, key=lambda x: (-x[0], x[1])) if score >= DEFINED_MIN_RELEVANCE]


def find_terms(d: Design, p: Project, units: list[Unit], provider: Provider) -> list[Term]:
    kept = [u for u in units if d.use_of(u.id) in KEPT]
    if not kept:
        return []
    other = [u for u in units if d.use_of(u.id) not in KEPT]
    budget = max(TERMS_BATCH_CHARS - sum(len(u.text) for u in kept), TERMS_MIN_SLICE)
    kept_ids = {u.id for u in kept}
    merged: dict[str, Term] = {}
    for part in _chunks(other, budget):
        data = ask_json(provider, terms_prompt(p, kept, part, list(merged.values())), terms_schema())
        for t in parse_terms(data, kept_ids, kept_ids | {u.id for u in part}):
            if (old := merged.get(_key(t.term))) is None:
                merged[_key(t.term)] = t
            else:
                old.used_by = list(dict.fromkeys(old.used_by + t.used_by))
                old.defined_by = list(dict.fromkeys(old.defined_by + t.defined_by))
    by = {u.id: u for u in units}
    for t in merged.values():
        t.defined_by = rank_definitions(t.term, t.defined_by, by)[:DEFINED_MAX]
    return list(merged.values())


def term_states(d: Design) -> list[dict]:
    skipped = {i for s in d.skip for i in s.units}
    out = []
    for t in d.terms:
        used = [i for i in t.used_by if d.use_of(i) in KEPT]
        if not used:
            continue
        explained = [i for i in t.defined_by if d.use_of(i) in KEPT]
        candidates = [i for i in t.defined_by if d.use_of(i) == "drop" and i not in skipped]
        if explained:
            status = "kept"
        elif any(i in skipped for i in t.defined_by):
            status = "skip"
        elif candidates:
            status = "dropped"
        else:
            status = "missing"
        out.append({"term": t.term, "why": t.why, "used_by": used, "explained_by": explained, "candidates": candidates,
                    "status": status, "label": STATUS_LABEL[status],
                    "promoted": [u.id for u in d.units if u.promoted_for == t.term and u.use in KEPT]})
    return sorted(out, key=lambda x: STATUS_ORDER[x["status"]])


def _promote(d: Design, unit_id: str, term: str) -> Design:
    units = [u.model_copy(update={"use": "mention", "why": f"用語「{term}」の説明", "promoted_for": term})
             if u.id == unit_id else u for u in d.units]
    return d.model_copy(update={"units": units})


def promote_definitions(d: Design) -> Design:
    for t in d.terms:
        st = next((s for s in term_states(d) if s["term"] == t.term), None)
        if st and st["status"] == "dropped":
            d = _promote(d, st["candidates"][0], t.term)
    return d


def explain_term(d: Design, term: str) -> Design:
    st = next((s for s in term_states(d) if s["term"] == term), None)
    if st is None:
        raise ValueError(f"知らない用語です: {term}")
    if st["status"] != "dropped":
        raise ValueError(f"「{term}」には書かないにした説明の単位がありません（{st['label']}）")
    return _promote(d, st["candidates"][0], term)


def clear_promotion(u: UnitUse, use: str) -> UnitUse:
    return u if use in KEPT else u.model_copy(update={"promoted_for": ""})


def first_use_terms(d: Design) -> list[dict]:
    return [s for s in term_states(d) if s["status"] != "skip"]


def _bigrams(text: str) -> set[str]:
    t = "".join(text.split()).lower()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _tie(u: Unit, focus: set[str]) -> float:
    grams = _bigrams(u.text)
    return round(len(grams & focus) / len(grams), 2) if grams else 0.0


def _drop_rank(u: Unit, defines: set[str], focus: set[str]) -> tuple[int, float, int]:
    score = {"yes": 0, "partial": 1, "no": 2}.get(u.searchable or "", 3) if u.origin == "material" else 3
    return score + (2 if u.id in defines else 0), _tie(u, focus), -len(u.text)


def _drop_reason(u: Unit) -> str:
    if u.origin == "answer":
        return "回答"
    return {"yes": "検索で届く", "partial": "一部は検索で届く"}.get(u.searchable or "", "手元だけ")


def material_load(d: Design, units: list[Unit]) -> dict:
    by = {u.id: u for u in units}
    deep = [by[x.id] for x in d.units if x.use == "deep" and x.id in by]
    mention = [by[x.id] for x in d.units if x.use == "mention" and x.id in by]
    deep_chars, mention_chars = sum(len(u.text) for u in deep), sum(len(u.text) for u in mention)
    kept_chars = deep_chars + mention_chars
    target = d.target_length
    ratio = round(kept_chars / target, 2) if target else 0.0
    mention_max = target // d.chars_per_mention if d.chars_per_mention else len(mention)
    reasons = []
    if ratio > d.max_material_ratio:
        reasons.append(f"使う材料が目標の字数の {ratio} 倍あります（目安は {d.max_material_ratio} 倍まで）")
    if len(mention) > mention_max:
        reasons.append(f"触れる材料が {len(mention)} 個あります（{target} 字なら {mention_max} 個くらいまで）")
    keep = d.aside_ids() | {x.id for x in d.units if x.promoted_for}
    defines = {i for s in term_states(d) for i in s["explained_by"]}
    focus = _bigrams(" ".join([d.purpose, *d.takeaways]))
    picks = sorted((u for u in mention if u.id not in keep), key=lambda u: _drop_rank(u, defines, focus))[:SUGGEST_MAX]
    return {"deep": len(deep), "mention": len(mention), "deep_chars": deep_chars, "mention_chars": mention_chars,
            "kept_chars": kept_chars, "target": target, "ratio": ratio, "max_ratio": d.max_material_ratio,
            "mention_max": mention_max, "over": bool(reasons), "reasons": reasons,
            "suggest_length": math.ceil(kept_chars / d.max_material_ratio / 100) * 100
            if ratio > d.max_material_ratio and d.max_material_ratio else None,
            "suggestions": [{"id": u.id, "chars": len(u.text), "tie": _tie(u, focus),
                             "reason": _drop_reason(u) + ("・用語の説明" if u.id in defines else "")
                             + ("・持ち帰りと離れている" if focus and _tie(u, focus) < TIE_LOW else ""),
                             "text": excerpt(u.text, 40, ellipsis=True)} for u in picks] if reasons else []}

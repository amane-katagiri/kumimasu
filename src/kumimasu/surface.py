from __future__ import annotations

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from .generate import DATA_NOTE_JA
from .glue import GLUE_RULE, material_grams, traceable
from .llm import STR, arr, ask_json, enum, obj, rows
from .metadiscourse import CATEGORIES, LEADS, Sentence, rule_hits, split_sentences

if TYPE_CHECKING:
    from .llm import Provider

GLUE = "glue"
CAVEAT = "caveat"
BRIDGE = "bridge"
WRAPUP = "wrapup"
FLOW = (BRIDGE, WRAPUP)
SLOT = {BRIDGE: "first", WRAPUP: "last"}
SLOT_LABEL = {"first": "段落の頭", "last": "段落の終わり"}
BRIDGE_RULE = re.compile(r"^(そこで|ところが|それでも|とはいえ|その結果|このため|そのため)、|(ことから|を受けて|を踏まえて|だけでは)、")
CAVEAT_RULE = re.compile(r"標本が(少な|小さ)|探索的|未確認|未検証|検証していな|確かめていな|一般化(でき|は難し|には注意)|"
                         r"(単一|一人|1 人)の(読み手|著者|評価者)|あくまで[^。]{0,20}(結果|傾向)|可能性があります|可能性は否定でき|"
                         r"注意が必要です|留意してください|限界があります")
SURFACE_CATEGORIES: dict[str, str] = CATEGORIES | {
    CAVEAT: "保守的な但し書き。限界・未確認・注意の断りのうち、それを消しても読者の結果の読み方が変わらないもの"
            "（「標本が少ないので探索的な所見です」「単一の読み手による評価です」「未検証の可能性もあります」）。"
            "結果の読み方を変える但し書き（適用範囲・前提・比較の条件を限るもの。例:「この差は opus のときだけで、sonnet では出なかった」）は選ばない",
    GLUE: "つなぎの効用文。直前・直後の話題を、読者にとっての役立ち・利点・結果に結びつけることだけが役目で、"
          "新しい事実・条件・手順・数値・理由を運ばない文（「これにより、手作業の入力が不要になります。」"
          "「この構成にしておくと、あとで差し替えるときに安心です。」「つまり、Binary Eye がスキャナーの役を果たすということです。」）",
    BRIDGE: "段落の頭の理由づけ。「（段落の頭）」の付いた文で、前の段落や節で述べた結果・状況を言い直し、次に何をしたか・何を書くかの理由や"
            "きっかけに結びつける部分（「冗長さが見つからなくても、〜」「好まれたのが具体的な場面だったことから、〜ことにしました」"
            "「そこで、〜」）。結びつけの部分を消しても、段落の残りが前の段落の後にそのまま読めるもの。文の一部だけでも選ぶ",
    WRAPUP: "段落の結び。「（段落の終わり）」の付いた文で、その段落で述べた事実や数値を解釈・意義・教訓として言い直して結ぶだけの文"
            "（「今の LLM の性能が十分に高いことを、この結果で実感しました。」「局所的な形の量にも U 字がありました。」）。"
            "段落の中にまだ書いていない事実・数値・条件・著者の判断を運ぶ文は選ばない",
}

class Pick(BaseModel):
    id: str
    category: str


class SurfaceHit(BaseModel):
    id: str
    category: str
    text: str
    heading: str = ""
    votes: int = 0


class SurfaceReport(BaseModel):
    units: int = 0
    runs: list[list[Pick]] = Field(default_factory=list)
    rule: list[str] = Field(default_factory=list)
    hits: list[SurfaceHit] = Field(default_factory=list)
    traced: list[SurfaceHit] = Field(default_factory=list)
    union: int = 0

    @property
    def runs_used(self) -> int:
        return len(self.runs)


def surface_prompt(units: list[Sentence], hints: dict[str, str], run: int) -> str:
    lines, current = [], None
    for u in units:
        if u.kind == "sentence" and u.section != current:
            current = u.section
            lines.append(f"=== 節: {u.heading or '（冒頭）'}")
        tag = f"  ← 規則: {hints[u.id]}" if u.id in hints else ""
        lead = f"  （直後に{LEADS[u.leads_into]}）" if u.leads_into else ""
        slot = f"  （{SLOT_LABEL[u.slot]}）" if u.slot else ""
        lines.append(f"[{u.id}] {'見出し: ' if u.kind == 'heading' else ''}{u.text}{slot}{lead}{tag}")
    cats = "\n".join(f"- {k}: {v}" for k, v in SURFACE_CATEGORIES.items())
    return (
        DATA_NOTE_JA + "\n\n次は技術ブログ記事（またはその一部）を文に分けたものです。[M12] は文の番号、[H3] は見出しの番号です。"
        "コード・表・引用は省いてあります。「← 規則:」は機械的な手がかり語が当たったことを示します。"
        "当たっていても該当しないことは多く、当たっていない文が該当することもあります。\n\n"
        "情報を運ばない文と見出しを選び、型を付けてください。目安は「消しても（見出しなら名詞句に直しても）読み手が失う情報が無い」ことです。"
        "事実・条件・手順・具体例・理由・数値・著者の体験や感想を述べている文は、言い回しが下の型に似ていても選ばないでください。"
        "文の一部だけが型にあたり、残りが情報を運んでいる場合も選ばないでください（段落の頭の理由づけ（bridge）だけは、文の一部でも選びます）。"
        "保守的な但し書き（caveat）は、条件を述べていても選んで構いません。ただし、その文を消すと読者が結果を読み違える"
        "（効く範囲・前提・比べた条件が変わる）なら選ばないでください。迷ったら選びません。"
        "「（直後に箇条書き）」などの付いた文が、すぐ後の箇条書き・表・コードを導入しているだけなら選ばないでください。\n\n"
        f"型:\n{cats}\n\n"
        "該当するものだけを {\"items\": [{\"id\": \"M12\", \"category\": \"glue\"}]} の形の JSON で答えてください。"
        "該当が無ければ {\"items\": []}。\n\n" + "\n".join(lines)
        # The run marker keeps each run's cache entry separate, so the k runs are independent samples.
        + f"\n\n（判定 {run}）"
    )


def surface_schema() -> dict:
    return obj(items=arr(obj(id=STR | {"pattern": "^[MH][0-9]+$"}, category=enum(*SURFACE_CATEGORIES))))


def parse_picks(data: dict, by_id: dict[str, Sentence]) -> list[Pick]:
    out, seen = [], set()
    for item in rows(data, "items"):
        uid, cat = item.get("id"), item.get("category")
        if uid not in by_id or cat not in SURFACE_CATEGORIES or uid in seen:
            continue
        if (by_id[uid].kind == "heading") != (cat == "claim_heading"):
            continue
        if cat in SLOT and by_id[uid].slot != SLOT[cat]:
            continue
        seen.add(uid)
        out.append(Pick(id=uid, category=cat))
    return out


def rule_hints(units: list[Sentence]) -> dict[str, str]:
    hints = {h.id: h.category for h in rule_hits(units)}
    for u in units:
        if u.kind == "sentence" and u.id not in hints and GLUE_RULE.search(u.text):
            hints[u.id] = GLUE
        elif u.kind == "sentence" and u.id not in hints and CAVEAT_RULE.search(u.text):
            hints[u.id] = CAVEAT
        elif u.slot == SLOT[BRIDGE] and u.id not in hints and BRIDGE_RULE.search(u.text):
            hints[u.id] = BRIDGE
    return hints


def detect_surface(markdown: str, provider: Provider, material: list[str], runs: int, min_votes: int) -> SurfaceReport:
    units = split_sentences(markdown)
    by_id = {u.id: u for u in units}
    hints = rule_hints(units)
    if not any(u.kind == "sentence" for u in units):
        return SurfaceReport(units=len(units), rule=sorted(hints))

    def one(i: int) -> list[Pick]:
        return parse_picks(ask_json(provider, surface_prompt(units, hints, i), surface_schema()), by_id)

    first = min(2, runs)
    with ThreadPoolExecutor(max(1, runs)) as ex:
        results = list(ex.map(one, range(1, first + 1)))
        agree = first == 2 and {p.id for p in results[0]} == {p.id for p in results[1]}
        if runs > first and not agree:
            results += list(ex.map(one, range(first + 1, runs + 1)))
    need = 1 if len(results) == 1 else (2 if agree and len(results) == 2 else min_votes)
    votes: Counter[str] = Counter(p.id for r in results for p in r)
    cats: dict[str, Counter[str]] = {}
    for r in results:
        for p in r:
            cats.setdefault(p.id, Counter())[p.category] += 1
    grams = material_grams(material)
    hits, traced = [], []
    for u in units:
        if votes[u.id] >= need:
            cat = cats[u.id].most_common(1)[0][0]
            h = SurfaceHit(id=u.id, category=cat, text=u.text, heading=u.heading, votes=votes[u.id])
            (traced if cat in (GLUE, WRAPUP) and grams and traceable(u.text, grams) else hits).append(h)
    return SurfaceReport(units=len(units), runs=results, rule=sorted(hints), hits=hits, traced=traced, union=len(votes))

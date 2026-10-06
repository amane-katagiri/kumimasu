from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .design import sync_design
from .generate import (
    DATA_NOTE_JA,
    OUTPUT_FORMAT_JA,
    REGISTER_REQUEST_JA,
    SOURCES_RULES_JA,
    article_from,
)
from .interview import unit_lines
from .land import bare_names, bare_units, land_name, noted
from .mark import KIND_LABEL
from .model import Design, Project, Unit
from .research import Research, research_block
from .terms import first_use_terms, material_load
from .workdir import WorkDir

if TYPE_CHECKING:
    from .llm import Provider

DROP_SHOWN = 80


def kept(d: Design, units: list[Unit], use: str) -> list[Unit]:
    aside = d.aside_ids()
    return [u for u in units if d.use_of(u.id) == use and u.id not in aside]


def noise_sections(d: Design, units: list[Unit]) -> list[str]:
    out = []
    if d.skip:
        out.append("## 説明しない前提（読者は知っているか、自分で調べられる）\n\n"
                   "次の事柄は説明しません。言い換え・前置き・「〜とは」の一文などで補ったり、説明が無いことを断ったりもしません。\n\n"
                   + _bullets([s.label for s in d.skip]))
    by_id = {u.id: u for u in units}
    asides = [(a, by_id[a.id]) for a in d.aside if a.id in by_id]
    if asides:
        out.append("## 脱線（著者が実際に出くわした話。本題には要らない）\n\n"
                   "次の話は、手がかりの場所に入れます。本題との関係を説明したり、読者の役に立つ話に結びつけて本題へ戻したりしません。\n\n"
                   + "\n\n".join(f"{unit_lines([u], with_mark=False)}\n（場所の手がかり: {a.where or 'おまかせ'}）" for a, u in asides))
    return out


def material_lines(d: Design, units: list[Unit]) -> str:
    notes = {x.id: x.note.strip() for x, _ in noted(d, units)}
    return "\n\n".join(unit_lines([u], with_mark=False) + (f"\n著者の一言: {notes[u.id]}" if u.id in notes else "")
                       for u in units)


NOTE_REQUEST = ("次の材料には、著者の一言を添えてあります（材料の「著者の一言」）。一言の判断や感想を、地の文の文体で言い切る短い 1 文にして、"
                "その材料を述べた所に置きます。「〜と思いました」「〜と感じました」「〜という印象です」などで包んだり、引用符で囲んだりしません。"
                "言い回しには一言の語と温度（ぼやきならぼやき、素直な驚きなら素直さ）を残し、整えすぎません。"
                "教訓や読者への効用には広げません。敬体なら「〜ですね」「〜でした」のような言い切りで構いません。"
                "例: 一言「ドキュメント読んでも全然わからんかった」→「ドキュメントを読んでも、全然わかりませんでした。」"
                "一言を段落の途中に置いたときも、一言の後に意味づけ・教訓・読者への効用を足して段落を締めません。"
                "一言の後は、材料の続き（事実・手順・次の話題）で進むか、そこで段落を終えます。材料に無い文で段落を埋めません。")


def land_section(d: Design, units: list[Unit]) -> str:
    parts = []
    if names := bare_names(d, units):
        parts.append("次の材料は、結果を述べたら、意味づけ・教訓・読者への効用を足さずに次へ進みます。"
                     "足さないことを断ったりもしません。\n\n" + _bullets(names))
    if author := noted(d, units):
        parts.append(NOTE_REQUEST + "\n\n" + _bullets([land_name(x, u) for x, u in author]))
    return "## 結果の着地\n\n" + "\n\n".join(parts) if parts else ""


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {x}" for x in items)


def drop_section(d: Design, drop: list[Unit], mode: str) -> str:
    if mode == "full" and drop:
        return ("## 書かない事柄（材料にはあるが、この記事では使わない）\n\n"
                + _bullets([(u.text[:DROP_SHOWN] + ("…" if len(u.text) > DROP_SHOWN else "")).replace("\n", " ") for u in drop]))
    if mode == "topics" and d.avoid:
        return "## 書かない話題\n\n" + _bullets(d.avoid)
    return ""


def terms_section(d: Design) -> str:
    rows = []
    for t in first_use_terms(d):
        how = (f"説明の材料: {', '.join(t['explained_by'])}" if t["status"] == "kept"
               else "材料に説明が無い。材料から分かる範囲で一言で説明し、分からなければ作らずに、この言葉を使わない書き方にする")
        rows.append(f"{t['term']}（{how}）")
    return ("## 初出で説明する用語（読者はこれを知らない。初めて出す所で、何であるか・何を測ったかを一言説明する）\n\n"
            + _bullets(rows)) if rows else ""


def load_section(d: Design, units: list[Unit]) -> str:
    load = material_load(d, units)
    if not load["over"]:
        return ""
    return (f"## 材料の量\n\n使う材料が、目標の長さに比べて多めです（材料 {load['kept_chars']} 字・目標の {load['ratio']} 倍、"
            f"触れる材料 {load['mention']} 個）。全部に触れるより、説明できる数に絞ってください。"
            "掘り下げる材料と、上の用語の説明を優先します。触れる材料は、持ち帰りに要らなければ省いて構いません。"
            "一つの段落に材料を詰め込んで、説明の無い名前や数字を並べないでください。")


FIGURE_MARK = "<!-- 図:"


def forms_section(d: Design) -> str:
    rules = d.active_rules()
    parts = []
    if d.forms:
        parts.append("設計で決めた形（必ず使う）:\n" + _bullets(d.forms))
    if d.form_prefs:
        parts.append(f"著者の好み: {d.form_prefs}")
    parts.append("ほかの所でも、内容が詰まっている所や比べている所（数値が並ぶ比較、順序のある手順など）は、"
                 "散文に詰め込まずに表や番号付きリストにして構いません。決まりにある箇条書きの禁止（太字で始まる箇条書き、"
                 "同じ形の並列を何段も続けること）は、ここでも守ります。")
    if any(FIGURE_MARK in r for r in rules):
        parts.append("図があると分かりやすい所には、図を描かずに、その場所に `<!-- 図: 何を示す図か -->` の目印を 1 行で置きます"
                     "（目印は後で人や別の工程が図にします）。")
    return "## 形\n\n" + "\n\n".join(parts)


def caveat_section(d: Design) -> str:
    if not any("但し書き" in r for r in d.active_rules()):
        return ""
    return ("## 但し書き\n\n限界・未確認・注意の断りは、それで結果の読み方が変わる所にだけ、まとめて 1 か所で書きます。"
            "読み方を変えない保守的な断り（標本が少ない、探索的、未検証の可能性など）は書きません。")


def design_block(p: Project, d: Design, units: list[Unit], drop_list: str | None = None) -> str:
    deep, mention, drop = (kept(d, units, x) for x in ("deep", "mention", "drop"))
    parts = [
        (f"「{p.topic}」について、{p.audience}向けの{KIND_LABEL[d.kind]}を書いてください。"
         f"長さは{d.target_length}字くらいです。" + REGISTER_REQUEST_JA[d.formality]),
        ("次は、この記事の著者（あなたが代わりに書く人）と一緒に決めた設計と、著者の手元の材料です。"
         "材料にある体験・試したこと・結果と、著者の回答は、著者のものとして一人称で書いて構いません。"),
        f"## 記事のねらい\n\n{d.purpose}" if d.purpose else "",
        "## 読者が持ち帰るもの（大事な順。どれも本文から読み取れるようにする）\n\n" + _bullets(d.takeaways) if d.takeaways else "",
        "## 掘り下げる材料（記事の中心。紙幅の大半をここに使う）\n\n" + material_lines(d, deep) if deep else "",
        "## 触れる材料（一言か短い段落で）\n\n" + material_lines(d, mention) if mention else "",
        terms_section(d),
        load_section(d, units),
        drop_section(d, drop, drop_list or d.drop_list),
        *noise_sections(d, units),
        land_section(d, units),
        "## 順番の手がかり（緩いもの。節の構成はあなたが決める）\n\n" + _bullets(d.order) if d.order else "",
        forms_section(d),
        caveat_section(d),
        "## 決まり\n\n" + _bullets(d.active_rules()) if d.active_rules() else "",
    ]
    return "\n\n".join(x for x in parts if x)


def draft_prompt(p: Project, d: Design, units: list[Unit], drop_list: str | None = None,
                 research: Research | None = None) -> str:
    return "\n\n".join(x for x in (DATA_NOTE_JA, design_block(p, d, units, drop_list), research_block(research),
                                    SOURCES_RULES_JA, OUTPUT_FORMAT_JA) if x)


def draft(wd: WorkDir, writer: Provider, roles: dict[str, str], name: str = "draft.md", drop_list: str | None = None,
          research: Research | None = None) -> str:
    p = wd.project()
    units = wd.units()
    d = sync_design(wd.design(), units)
    prompt = draft_prompt(p, d, units, drop_list, research)
    wd.write(name.replace(".md", ".prompt.md"), prompt)
    write_used(wd, name, d, units, drop_list or d.drop_list, roles | {"writer": f"{writer.name}:{writer.model}"})
    text = article_from(writer.complete(prompt))
    wd.write(name, text)
    return text


def used_name(draft_name: str) -> str:
    return draft_name.removesuffix(".md") + ".used.json"


def write_used(wd: WorkDir, draft_name: str, d: Design, units: list[Unit], drop_list: str, providers: dict[str, str]) -> dict:
    """What this draft was written with, so the final check and the handoff can show it."""
    used = {"draft": draft_name, "rules": d.active_rules(), "rules_off": [r.text for r in d.rules if not r.on],
            "avoid": d.avoid, "register": d.formality, "drop_list": drop_list, "forms": d.forms,
            "form_prefs": d.form_prefs, "skip": [s.label for s in d.skip], "aside": [a.id for a in d.aside],
            "land": {"bare": [x.id for x, _ in bare_units(d, units)],
                     "author": {x.id: x.note.strip() for x, _ in noted(d, units)}},
            "terms": [t["term"] for t in first_use_terms(d)],
            "providers": providers, "design": wd.design_file.name}
    wd.write_json(used_name(draft_name), used)
    return used


def read_used(wd: WorkDir, draft_name: str) -> dict | None:
    path = wd.root / used_name(draft_name)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

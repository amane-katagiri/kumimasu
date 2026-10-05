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


def design_block(p: Project, d: Design, units: list[Unit], drop_list: str | None = None) -> str:
    deep, mention, drop = (kept(d, units, x) for x in ("deep", "mention", "drop"))
    parts = [
        (f"「{p.topic}」について、{p.audience}向けの{KIND_LABEL[d.kind]}を書いてください。"
         f"長さは{d.target_length}字くらいです。" + REGISTER_REQUEST_JA[d.formality]),
        ("次は、この記事の著者（あなたが代わりに書く人）と一緒に決めた設計と、著者の手元の材料です。"
         "材料にある体験・試したこと・結果と、著者の回答は、著者のものとして一人称で書いて構いません。"),
        f"## 記事のねらい\n\n{d.purpose}" if d.purpose else "",
        "## 読者が持ち帰るもの（大事な順。どれも本文から読み取れるようにする）\n\n" + _bullets(d.takeaways) if d.takeaways else "",
        "## 掘り下げる材料（記事の中心。紙幅の大半をここに使う）\n\n" + unit_lines(deep, with_mark=False) if deep else "",
        "## 触れる材料（一言か短い段落で）\n\n" + unit_lines(mention, with_mark=False) if mention else "",
        terms_section(d),
        load_section(d, units),
        drop_section(d, drop, drop_list or d.drop_list),
        *noise_sections(d, units),
        "## 順番の手がかり（緩いもの。節の構成はあなたが決める）\n\n" + _bullets(d.order) if d.order else "",
        "## 形（内容が並び・手順・比較・コードのときだけ使う）\n\n"
        + "\n".join(x for x in (_bullets(d.forms) if d.forms else "", f"著者の好み: {d.form_prefs}" if d.form_prefs else "") if x)
        if d.forms or d.form_prefs else "",
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
    write_used(wd, name, d, drop_list or d.drop_list, roles | {"writer": f"{writer.name}:{writer.model}"})
    text = article_from(writer.complete(prompt))
    wd.write(name, text)
    return text


def used_name(draft_name: str) -> str:
    return draft_name.removesuffix(".md") + ".used.json"


def write_used(wd: WorkDir, draft_name: str, d: Design, drop_list: str, providers: dict[str, str]) -> dict:
    """What this draft was written with, so the final check and the handoff can show it."""
    used = {"draft": draft_name, "rules": d.active_rules(), "rules_off": [r.text for r in d.rules if not r.on],
            "avoid": d.avoid, "register": d.formality, "drop_list": drop_list, "forms": d.forms,
            "form_prefs": d.form_prefs, "skip": [s.label for s in d.skip], "aside": [a.id for a in d.aside],
            "terms": [t["term"] for t in first_use_terms(d)],
            "providers": providers, "design": wd.design_file.name}
    wd.write_json(used_name(draft_name), used)
    return used


def read_used(wd: WorkDir, draft_name: str) -> dict | None:
    path = wd.root / used_name(draft_name)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

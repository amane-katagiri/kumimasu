from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .generate import DATA_NOTE_JA
from .interview import strip_unit_refs, unit_lines
from .llm import STR, arr, ask_json, enum, obj, rows
from .model import Design, Project, Unit, UnitUse
from .textutil import excerpt, norm

if TYPE_CHECKING:
    from .llm import Provider

LABEL_MAX = 30
NOTE_QUESTION = "この中で何か思ったものだけ一言。無ければ飛ばしてよい"
NOTE_SUFFIX = "n"
NOTE_ID = re.compile(rf"^[mq]\d+{NOTE_SUFFIX}$")

LAND_PROMPT_JA = DATA_NOTE_JA + """

著者が「{topic}」について記事を書きます（読者: {audience}、種類: {kind}）。下は、記事の中心として掘り下げると決めた材料と著者の回答（番号付きの単位）です。

それぞれの単位について答えてください。
- result: その単位が、やってみた結果・観察したこと・測った数字・起きたこと（エラーやつまずき、諦めた判断とその理由を含む）を運んでいれば yes。方針・手順・仕組みや背景の説明・設定・コード・リンクだけなら no。
- label: その単位の中身を 20 字以内の名詞句で。著者が一覧で見て、どの話か思い出せるようにします。番号は書きません。

# 掘り下げる材料

{units}"""


def land_schema() -> dict:
    return obj(units=arr(obj(id=STR, result=enum("yes", "no"), label=STR)))


def land_prompt(p: Project, deep: list[Unit]) -> str:
    return LAND_PROMPT_JA.format(topic=p.topic, audience=p.audience, kind=p.kind,
                                 units=unit_lines(deep, with_mark=False, with_context=True))


def parse_land(data: dict, ids: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows(data, "units"):
        uid = row.get("id")
        if uid in ids and uid not in out and row.get("result") == "yes":
            out[uid] = strip_unit_refs(str(row.get("label", "")), ids)[:LABEL_MAX]
    return out


def propose_land(d: Design, p: Project, units: list[Unit], provider: Provider) -> Design:
    by_id = {u.id: u for u in units}
    deep = [by_id[x.id] for x in d.units if x.use == "deep" and x.id in by_id]
    results = parse_land(ask_json(provider, land_prompt(p, deep), land_schema()), {u.id for u in deep}) if deep else {}
    aside = d.aside_ids()
    out = []
    for x in d.units:
        if x.note.strip():
            x = x.model_copy(update={"land": "author", "label": results.get(x.id, x.label)})
        elif x.use == "deep" and x.id in results:
            x = x.model_copy(update={"land": "bare", "label": results[x.id]})
        elif x.use == "mention" and x.id not in aside:
            x = x.model_copy(update={"land": "bare"})
        else:
            x = x.model_copy(update={"land": None, "label": ""})
        out.append(x)
    return d.model_copy(update={"units": out})


def note_question(limit: int) -> str:
    return f"{NOTE_QUESTION}（{limit} 個まで）"


def land_name(x: UnitUse, u: Unit) -> str:
    return f"[{u.id}] {x.label or excerpt(u.text, 24, ellipsis=True)}"


def written(d: Design, units: list[Unit]) -> list[tuple[UnitUse, Unit]]:
    by_id = {u.id: u for u in units}
    aside = d.aside_ids()
    return [(x, by_id[x.id]) for x in d.units if x.use in ("deep", "mention") and x.id in by_id and x.id not in aside]


def bare_units(d: Design, units: list[Unit]) -> list[tuple[UnitUse, Unit]]:
    return [(x, u) for x, u in written(d, units) if x.land == "bare"]


def noted(d: Design, units: list[Unit]) -> list[tuple[UnitUse, Unit]]:
    return [(x, u) for x, u in written(d, units) if x.land == "author" and x.note.strip()]


def bare_names(d: Design, units: list[Unit]) -> list[str]:
    bare = bare_units(d, units)
    mention = [x for x, _ in written(d, units) if x.use == "mention"]
    all_mention = bool(mention) and all(x.land == "bare" for x in mention)
    names = [land_name(x, u) for x, u in bare if not (all_mention and x.use == "mention")]
    return names + (["「触れる材料」のすべて"] if all_mention else [])


def note_items(d: Design, units: list[Unit]) -> list[dict]:
    by_id = {u.id: u for u in units}
    return [{"id": x.id, "label": x.label or excerpt(by_id[x.id].text, 40, ellipsis=True), "land": x.land, "note": x.note,
             "followup": x.followup, "followup_state": x.followup_state}
            for x in d.units if x.id in by_id and ((x.use == "deep" and x.land is not None)
                                                   or (x.use in ("deep", "mention") and x.note.strip()))]


def note_units(d: Design, units: list[Unit]) -> list[Unit]:
    return [Unit(id=x.id + NOTE_SUFFIX, origin="answer", source="design", text=x.note.strip(),
                 context=f"{u.id}（{x.label or excerpt(u.text, 24, ellipsis=True)}）について一言")
            for x, u in noted(d, units)]


def from_notes(texts_by_id: dict[str, str], sources: dict) -> tuple[str, ...]:
    return tuple(norm(t) for i, t in texts_by_id.items() if any(NOTE_ID.match(x) for x in sources.get(i, [])))


def guarded(text: str, protect: tuple[str, ...]) -> bool:
    t = norm(text)
    return bool(t) and any(t in p for p in protect)


FOLLOWUP_PROMPT_JA = DATA_NOTE_JA + """

著者が「{topic}」について記事を書きます（読者: {audience}）。下は、著者が材料（番号付きの単位）に付けた一言です。

一言ごとに答えてください。
- thin: 一言が評価や判断の言葉だけで、何が・なぜ・具体的にはどうなのかが書かれていなければ yes。ぼやきや素直な反応（「ちゃんとしてくれ～」「新鮮だった」のようなもの）は、掘ると説明っぽくなるので no。理由や具体をすでに含む一言も no。
- question: thin が yes のとき、著者に聞き返す質問を 1 つ。一言の言葉を使い、なぜそう思ったか、または具体的には何かを、くだけた短い問い（30 字くらいまで）で聞きます（例: 「ハックっぽいって、どのへんが？」）。番号は書きません。答えの候補は並べません。thin が no なら空にします。

# 一言

{notes}"""
FOLLOWUP_SHOWN = 300


def followup_schema() -> dict:
    return obj(notes=arr(obj(id=STR, thin=enum("yes", "no"), question=STR)))


def followup_prompt(p: Project, pairs: list[tuple[UnitUse, Unit]]) -> str:
    notes = "\n\n".join(f"[{u.id}] 材料: {excerpt(u.text, FOLLOWUP_SHOWN, ellipsis=True)}\n一言: {x.note.strip()}" for x, u in pairs)
    return FOLLOWUP_PROMPT_JA.format(topic=p.topic, audience=p.audience, notes=notes)


def parse_followups(data: dict, ids: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows(data, "notes"):
        uid = row.get("id")
        if uid in ids and uid not in out:
            q = strip_unit_refs(str(row.get("question", "")), ids).strip()
            out[uid] = q if row.get("thin") == "yes" else ""
    return out


def spread_pick(ids: list[str], order: list[str], n: int) -> list[str]:
    """Front and back halves of the material take turns, since feelings were missing from the front (§9.9)."""
    pos = {i: k for k, i in enumerate(order)}
    ranked = sorted(ids, key=lambda i: pos.get(i, len(order)))
    mid = len(order) / 2
    halves = [[i for i in ranked if pos.get(i, len(order)) < mid], [i for i in ranked if pos.get(i, len(order)) >= mid]]
    picks: list[str] = []
    k = 0
    while len(picks) < n and (halves[0] or halves[1]):
        side = halves[k % 2] or halves[(k + 1) % 2]
        picks.append(side.pop(0))
        k += 1
    return picks


def propose_followups(d: Design, p: Project, units: list[Unit], provider: Provider) -> tuple[Design, int]:
    pending = [(x, u) for x, u in noted(d, units) if x.followup_state is None]
    used = sum(x.followup_state in ("asked", "answered", "skipped") for x in d.units)
    room = d.followup_limit - used
    if not pending or room <= 0:
        return d, 0
    judged = parse_followups(ask_json(provider, followup_prompt(p, pending), followup_schema()), {x.id for x, _ in pending})
    picks = set(spread_pick([i for i, q in judged.items() if q], [x.id for x in d.units], room))
    out = []
    for x in d.units:
        if x.id in picks:
            x = x.model_copy(update={"followup": judged[x.id], "followup_state": "asked"})
        elif x.id in judged and not judged[x.id]:
            x = x.model_copy(update={"followup": "", "followup_state": "none"})
        out.append(x)
    return d.model_copy(update={"units": out}), 1


def with_answer(note: str, answer: str) -> str:
    note, answer = note.strip(), answer.strip()
    return f"{note}{answer}" if note.endswith(("。", "！", "？", "!", "?", "～", "…")) else f"{note}。{answer}"


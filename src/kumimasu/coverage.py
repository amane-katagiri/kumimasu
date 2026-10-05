from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .generate import DATA_NOTE_JA
from .infounits import InfoUnit, units_block
from .llm import INT, STR, arr, enum, obj, rows

Verdict = Literal["yes", "partial", "no"]


COVERAGE_PROMPT_JA = DATA_NOTE_JA + """

次の「対象の記事」を情報の単位に分け、番号を付けました。単位ごとに、その情報が「比べる文書」のどれかに書かれているかを判定してください。

判定（v）:
- yes: 単位の中心になる情報が、比べる文書のどれかに書かれている。言い回し・順番・言語が違ってもよい。
- partial: 話題や一般論、同じ種類の事柄は書かれているが、単位の具体的な中身（固有の数値・名前・手順・例・観察・判断・理由づけ）の一部が書かれていない。
- no: 単位の中心になる情報が、比べる文書のどこにも書かれていない。
判定は情報があるかどうかだけで決めます。正しさ・書き方の良し悪し・重要さは判定に入れません。

in: v が yes か partial のとき、その情報が書かれている比べる文書の記号（{labels}）をすべて。no のときは空の配列。

# 比べる文書

{baselines}

# 対象の記事の単位

{units}

すべての単位について、番号の順に 1 つずつ答えてください。"""


def coverage_schema() -> dict:
    return obj(units=arr(obj(id=INT, v=enum("yes", "partial", "no"), **{"in": arr(STR)})))


def coverage_prompt(units: list[InfoUnit], baselines: dict[str, str]) -> str:
    blocks = "\n\n".join(f"## {label}\n\n{text.strip()}" for label, text in baselines.items())
    return COVERAGE_PROMPT_JA.format(labels="・".join(baselines), baselines=blocks, units=units_block(units))


class Coverage(BaseModel):
    id: int
    v: Verdict
    in_: list[str] = Field(default_factory=list, alias="in")

    model_config = {"populate_by_name": True}


def parse_coverage(data: dict, units: list[InfoUnit], labels: list[str]) -> dict[int, Coverage]:
    ids = {u.id for u in units}
    out: dict[int, Coverage] = {}
    for row in rows(data, "units"):
        try:
            c = Coverage.model_validate(row)
        except ValueError:
            continue
        if c.id in ids and c.id not in out:
            c.in_ = [x for x in c.in_ if x in labels] if c.v != "no" else []
            out[c.id] = c
    return out

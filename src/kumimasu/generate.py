from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field


if TYPE_CHECKING:
    pass


class Brief(BaseModel):
    topic: str
    audience: str
    goal: str = ""
    key_points: list[str] = Field(default_factory=list)
    target_length: int = 3000
    language: str = "ja"
    kind: str = ""
    formality: str = ""


REGISTER_REQUEST_JA = {"keitai": "文体は敬体（です・ます調）で統一してください。",
                       "joutai": "文体は常体（だ・である調）で統一してください。"}


def _request_ja(brief: Brief) -> str:
    points = "\n".join(f"- {p}" for p in brief.key_points)
    kind = brief.kind or "技術ブログ記事"
    return (
        f"「{brief.topic}」について、{brief.audience}向けの{kind}を書いてください。\n\n"
        + (f"ねらい: {brief.goal}\n\n" if brief.goal else "")
        + (f"次の点は必ず入れてください。\n{points}\n\n" if points else "")
        + f"長さは{brief.target_length}字くらいでお願いします。"
    )


def generation_prompt(brief: Brief) -> str:
    if brief.language.lower().startswith("ja"):
        return (_request_ja(brief) + "Markdown で、タイトルを # の見出しにして、記事の本文だけを出力してください。"
                + REGISTER_REQUEST_JA.get(brief.formality, ""))
    points = "\n".join(f"- {p}" for p in brief.key_points)
    kind = brief.kind or "technical blog post"
    return (
        f"Could you write a {kind} about {brief.topic} for {brief.audience}?\n\n"
        f"Goal: {brief.goal}\n\n"
        f"Please make sure it covers:\n{points}\n\n"
        f"Aim for about {brief.target_length} words. Use Markdown with the title as a # heading, "
        "and output only the article itself."
    )


PLAN_OPEN, PLAN_CLOSE, ARTICLE_OPEN, ARTICLE_CLOSE = "<plan>", "</plan>", "<article>", "</article>"


class TaggedOutputError(ValueError):
    pass


def split_planned(text: str) -> tuple[str, str]:
    a = text.find(ARTICLE_OPEN)
    if a < 0:
        raise TaggedOutputError(f"no {ARTICLE_OPEN} in the response")
    b = text.find(ARTICLE_CLOSE, a)
    article = text[a + len(ARTICLE_OPEN):b if b >= 0 else len(text)]
    p, q = text.find(PLAN_OPEN), text.find(PLAN_CLOSE)
    plan = text[p + len(PLAN_OPEN):q].strip() if 0 <= p < q <= a else ""
    return plan, clean_article(article)


_FENCE_RE = re.compile(r"^```(?:markdown|md)?\s*\n(?P<body>.*)\n```\s*$", re.S)


def clean_article(text: str) -> str:
    text = text.strip()
    if m := _FENCE_RE.match(text):
        text = m["body"].strip()
    return text + "\n"


MATERIAL_INTRO_JA = (
    "次は、この記事の著者（あなたが代わりに書く人）が手元に残した覚え書きです。"
    "覚え書きにある体験・試したこと・結果は、著者の体験として一人称で書いて構いません。"
)

WEB_RULES_JA = (
    "調べものには、ウェブ検索とウェブページの取得を使って構いません。"
    "ウェブで確かめた事実には、出典のページへの Markdown のリンクを付けてください。"
    "この依頼に書かれていない体験・計測結果・引用は作らないでください（ウェブで見つけた発言は、出典のリンクを付けて引けます）。"
)

HUMANLIKE_TREATMENT_JA = (
    "書き方について。\n"
    "- 書き始める前に、この話題のどこがあなたにとって面白いのか、どの一点を掘り下げたいのかを決めてください。"
    "決めたことは出力しません。\n"
    "- ウェブで具体的なもの（実際に起きた出来事、一次資料、仕様、出典のある発言）を探し、リンクを付けて使ってください。\n"
    "- その視点から書き、選んだ一点を深く掘り下げます。ほかの点は、依頼で挙げた点も含めて、一言触れるだけにするか省きます。\n"
    "- 話を進める順番は、あなたの興味が向かう順にしてください。\n"
    "- 道しるべの文（「以下では〜を見ていきます」）、決め台詞の締め、「A ではなく B」の言い直し、立場や範囲の宣言、"
    "自分の書いたことへの但し書きは書かないでください。\n"
    "- 一般的な助言の節や FAQ の節は作らないでください。"
)

TREATMENTS_JA = {"plain": "", "humanlike": HUMANLIKE_TREATMENT_JA}


def research_generation_prompt(brief: Brief, treatment: str, material: str | None = None) -> str:
    parts = [_request_ja(brief) + REGISTER_REQUEST_JA.get(brief.formality, "")]
    if material:
        parts.append(MATERIAL_INTRO_JA + "\n\n" + material.strip())
    parts.append(WEB_RULES_JA)
    if TREATMENTS_JA[treatment]:
        parts.append(TREATMENTS_JA[treatment])
    parts.append(f"出力の形式: 記事の全文を {ARTICLE_OPEN} と {ARTICLE_CLOSE} のあいだに書いてください。"
                 "記事は Markdown で、タイトルを # の見出しにします。タグの外には何も書かないでください。")
    return "\n\n".join(parts)

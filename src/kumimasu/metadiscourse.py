from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel


if TYPE_CHECKING:
    pass

CATEGORIES: dict[str, str] = {
    "signpost": "道しるべ。先の展開や結論の位置を予告・確認するだけの文（「先に結論を述べます」「以下では〜を見ていきます」"
                "「この点が以下の議論の出発点になります」）",
    "aphorism": "警句的な締め。段落や節を決め台詞でまとめる文（「ウェブアーカイブは証拠そのものではない。証拠を構成する一つの観測記録である。」"
                "「これ以上ないほど分かりやすい結論だ。」）",
    "reframe": "対比による言い直し。「A ではなく B」「〜というより〜」の形で、すでに述べたことを言い換えて強調するだけの文",
    "stance": "立場の表明・持ち帰りの一点（「私の立場をはっきりさせておきます」「覚えておいてほしいのは一つだけです」）",
    "scope": "範囲の宣言（「なお、〜はここでは扱いません」「〜には今回は踏み込みません」「詳しくは〜に譲ります」）",
    "self_hedge": "自分の整理・判断の性格への但し書き（「これは私が整理した分類です」「数字で測ったものではなく、私の見立てです」"
                  "「これは Docker への批判ではありません」）",
    "claim_heading": "主張の見出し。内容を指す名詞句ではなく、主張そのものを文にした見出し（「PDF は JPEG を入れる『外箱』」「信じる対象を一つにしない」）",
}

RULES: dict[str, re.Pattern[str]] = {
    "signpost": re.compile(
        r"先に結論|結論から(言|述|申)|結論を先に|以下では|以下で[^。]{0,20}(見て|説明|解説|紹介)|本記事では|この記事では|"
        r"ここでは[^。]{0,30}(見て|説明し|解説し|紹介し|整理し)|見ていきましょう|見ていきます|順に見て|順番に見て|ここからが本題|"
        r"出発点にな|以下の議論|整理しておき|おさらい|前置きが長く"),
    "scope": re.compile(r"扱いません|扱わない|触れません|触れない|踏み込みません|踏み込まない|割愛|範囲外|対象外とし|"
                        r"詳しくは[^。]{0,40}(譲|参照|ご覧)|別の機会|別記事"),
    "stance": re.compile(r"私の立場|立場をはっきり|はっきりさせておき|強調しておき|断言し|言い切り|声を大にして|主張したい|申し上げておき|"
                         r"覚えておいてほしい|持ち帰って"),
    "self_hedge": re.compile(r"私が(整理|分類|まとめ)した|筆者の(整理|分類|見立て)|私の見立て|私見|測ったものではな|あくまで[^。]{0,30}"
                             r"(目安|一例|私見|個人的)|批判ではありません|否定するものではありません|網羅(的)?ではありません|"
                             r"保証するものではありません"),
    "reframe": re.compile(r"ではなく、|ではなく[^、。]{1,30}(です|だ|ます)。?$|というより|のではありません。?$"),
}
_APHORISM = re.compile(r"そのもの|本質|すべて|全て|にすぎ|に過ぎ|鍵|カギ|答え|結論|のです。$|のだ。$|である。$")
_SPECIFIC = re.compile(r"[0-9A-Za-z`]")
_HEADING_ASSERTIVE = re.compile(r"(ない|だ|である|です|ます|ません|すぎる|べき|！|!|。)$")
_HEADING_TOPIC = re.compile(r"[^とに]は|が")
_HEADING_NOUNISH = re.compile(r"(とは|か|まとめ|はじめに|おわりに|について|かた|方|しくみ|仕組み|\?|？)$")
LEADS = {"list": "箇条書き", "table": "表", "code": "コード"}
_SENT_SPLIT = re.compile(r"(?<=[。！？!?])")
_LIST_MARK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MARKUP = re.compile(r"\*\*|__|`")


class Unit(BaseModel):
    id: str
    kind: str
    text: str
    section: int
    heading: str = ""
    paragraph_end: bool = False
    section_end: bool = False
    leads_into: str = ""


class Hit(BaseModel):
    id: str
    category: str
    text: str
    section: int
    heading: str = ""
    source: str = ""


def section_level(markdown: str) -> int:
    levels = [len(m[1]) for m in (HEADING_RE.match(ln) for ln in code_free_lines(markdown)) if m]
    inner = [lv for lv in levels if lv > 1] if levels and levels[0] == 1 else levels
    return min(inner) if inner else 2


def code_free_lines(markdown: str) -> list[str]:
    out, fence = [], False
    for ln in markdown.splitlines():
        if ln.lstrip().startswith(("```", "~~~")):
            fence = not fence
            out.append("")
            continue
        out.append("" if fence else ln)
    return out


def sentences(text: str) -> list[str]:
    flat = re.sub(r"\s*\n\s*", "", text.strip())
    return [s.strip() for s in _SENT_SPLIT.split(flat) if len(s.strip()) >= 2]


def split_units(markdown: str) -> list[Unit]:
    """Prose sentences and headings with their unit section (-1 before the first section heading). Code, tables and quotes are skipped."""
    level = section_level(markdown)
    units: list[Unit] = []
    section, heading = -1, ""
    block: list[str] = []
    n_m = n_h = 0

    raw = markdown.splitlines()

    def flush(at: int) -> None:
        nonlocal n_m, block
        text = "\n".join(block)
        in_list = bool(block) and block_is_item
        block = []
        sents = sentences(text)
        following = "" if in_list else _next_block(raw, at)
        for i, s in enumerate(sents):
            n_m += 1
            last = i == len(sents) - 1
            units.append(Unit(id=f"M{n_m}", kind="sentence", text=s, section=section, heading=heading,
                              paragraph_end=last, leads_into=following if last else ""))

    block_is_item = False
    for at, ln in enumerate(code_free_lines(markdown)):
        m = HEADING_RE.match(ln)
        if block and (m or not ln.strip() or ln.lstrip().startswith(("|", ">", "<", "![")) or _LIST_MARK.match(ln)):
            flush(at)
        if m:
            if len(m[1]) == 1:
                continue
            if len(m[1]) <= level:
                section, heading = section + 1, m[2]
            n_h += 1
            units.append(Unit(id=f"H{n_h}", kind="heading", text=m[2], section=section, heading=heading))
            continue
        if not ln.strip() or ln.lstrip().startswith(("|", ">", "<", "![")):
            continue
        if _LIST_MARK.match(ln):
            block, block_is_item = [_LIST_MARK.sub("", ln)], True
            continue
        if not block:
            block_is_item = False
        block.append(ln.strip())
    if block:
        flush(len(raw))
    last: dict[int, Unit] = {}
    for u in units:
        if u.kind == "sentence":
            last[u.section] = u
    for u in last.values():
        u.section_end = True
    return units


def _next_block(raw: list[str], at: int) -> str:
    for ln in raw[at:]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith(("```", "~~~")):
            return "code"
        if s.startswith("|"):
            return "table"
        if _LIST_MARK.match(ln):
            return "list"
        return ""
    return ""


def rule_hits(units: list[Unit]) -> list[Hit]:
    out = []
    for u in units:
        cat = None
        if u.kind == "heading":
            plain = _MARKUP.sub("", u.text).strip()
            if len(plain) >= 6 and not _HEADING_NOUNISH.search(plain) \
                    and (_HEADING_ASSERTIVE.search(plain) or _HEADING_TOPIC.search(plain)):
                cat = "claim_heading"
        else:
            cat = next((c for c, rx in RULES.items() if rx.search(u.text)), None)
            if cat is None and (u.paragraph_end or u.section_end) and len(u.text) <= 50 \
                    and not _SPECIFIC.search(u.text) and _APHORISM.search(u.text):
                cat = "aphorism"
        if cat:
            out.append(Hit(id=u.id, category=cat, text=u.text, section=u.section, heading=u.heading, source="rule"))
    return out

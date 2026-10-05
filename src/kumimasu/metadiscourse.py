from __future__ import annotations

import itertools
import re

from pydantic import BaseModel

from .textutil import code_free_lines, sentences

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
_LIST_MARK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MARKUP = re.compile(r"\*\*|__|`")


class Sentence(BaseModel):
    id: str
    kind: str
    text: str
    section: int
    heading: str = ""
    paragraph_end: bool = False
    slot: str = ""
    section_end: bool = False
    leads_into: str = ""


class Hit(BaseModel):
    id: str
    category: str
    text: str
    section: int
    heading: str = ""
    source: str = ""


def section_level(lines: list[str]) -> int:
    levels = [len(m[1]) for m in (HEADING_RE.match(ln) for ln in lines) if m]
    inner = [lv for lv in levels if lv > 1] if levels and levels[0] == 1 else levels
    return min(inner) if inner else 2


_SKIPPED = ("|", ">", "<", "![")


def split_sentences(markdown: str) -> list[Sentence]:
    raw = markdown.split("\n")
    lines = code_free_lines(markdown)
    level = section_level(lines)
    out: list[Sentence] = []
    section, heading = -1, ""
    block: list[str] = []
    in_list = False
    n_sent, n_head = itertools.count(1), itertools.count(1)

    def flush(at: int) -> None:
        sents = sentences("\n".join(block))
        following = "" if in_list else _next_block(raw, at)
        prose = not in_list and len(sents) >= 2
        for i, s in enumerate(sents):
            last = i == len(sents) - 1
            slot = ("first" if i == 0 else "last" if last else "") if prose else ""
            out.append(Sentence(id=f"M{next(n_sent)}", kind="sentence", text=s,
                                section=section, heading=heading, paragraph_end=last, slot=slot,
                                leads_into=following if last else ""))

    for at, ln in enumerate(lines):
        m = HEADING_RE.match(ln)
        if block and (m or not ln.strip() or ln.lstrip().startswith(_SKIPPED) or _LIST_MARK.match(ln)):
            flush(at)
            block = []
        if m:
            if len(m[1]) == 1:
                continue
            if len(m[1]) <= level:
                section, heading = section + 1, m[2]
            out.append(Sentence(id=f"H{next(n_head)}", kind="heading", text=m[2],
                                section=section, heading=heading))
        elif ln.strip() and not ln.lstrip().startswith(_SKIPPED):
            if _LIST_MARK.match(ln):
                block, in_list = [_LIST_MARK.sub("", ln)], True
            else:
                in_list = in_list and bool(block)
                block.append(ln.strip())
    if block:
        flush(len(raw))
    last = {u.section: u for u in out if u.kind == "sentence"}
    for u in last.values():
        u.section_end = True
    return out


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


def rule_hits(units: list[Sentence]) -> list[Hit]:
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

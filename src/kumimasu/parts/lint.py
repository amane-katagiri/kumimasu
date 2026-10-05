from __future__ import annotations

import re
from dataclasses import dataclass

from markdown_it import MarkdownIt

from .model import Part, PartDoc

EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿️]")
DECOR_LEAD = re.compile(r"^\s*([✅❌⭕✔✖⚠💡📌👉※→⇒★☆■□●○◆◇▶►▷]|⭐)")
BOLD = re.compile(r"\*\*[^*\n]+?\*\*|__[^_\n]+?__")
BOLD_LEAD = re.compile(r"^\s*(\*\*[^*\n]+?\*\*|__[^_\n]+?__)\s*([:：]|$|\s*[-–—]\s)")
CODE_SPAN = re.compile(r"(`+)(.+?)\1", re.DOTALL)
_inline = MarkdownIt("commonmark")
_CJK = re.compile(r"[　-ヿ㐀-鿿＀-￯]")


def plain(text: str) -> str:
    out: list[str] = []
    tokens = _inline.parseInline(text)
    for tok in tokens:
        for c in tok.children or []:
            match c.type:
                case "text" | "code_inline":
                    out.append(c.content)
                case "softbreak" | "hardbreak":
                    out.append("\n")
                case "image":
                    out.append(f"[画像: {c.content}]" if c.content else "[画像]")
    joined = "".join(out)
    return re.sub(r"(?<=(.))\n(?=(.))", lambda m: "" if _CJK.match(m[1]) and _CJK.match(m[2]) else " ", joined).strip()


MAX_BOLD_PER_KCHAR = 3.0


@dataclass
class Finding:
    rule: str
    excerpt: str


def _text_parts(doc: PartDoc) -> list[tuple[Part, Part | None]]:
    parents = doc.parents()
    out = []
    for p in doc.parts():
        if p.kind in ("paragraph", "heading", "section") and p.text:
            parent = parents.get(p.id)
            out.append((p, parent if parent is not None and parent.kind == "item" else None))
    return out


def lint(doc: PartDoc) -> list[Finding]:
    findings: list[Finding] = []
    bold = 0
    chars = 0
    for p, item in _text_parts(doc):
        text = CODE_SPAN.sub("", p.text)
        chars += len(plain(p.text))
        bold += len(BOLD.findall(text))
        excerpt = plain(p.text)[:40]
        if item is not None and item.children and item.children[0].id == p.id and BOLD_LEAD.match(text):
            findings.append(Finding("bold-lead-item", excerpt))
        if EMOJI.search(text):
            findings.append(Finding("emoji", "".join(sorted(set(EMOJI.findall(text))))))
        if any(DECOR_LEAD.match(ln) for ln in text.split("\n")):
            findings.append(Finding("decor-symbol", excerpt))
    density = round(1000 * bold / chars, 3) if chars else 0.0
    if density > MAX_BOLD_PER_KCHAR:
        findings.append(Finding("bold-density", f"{density}/1000 chars > {MAX_BOLD_PER_KCHAR}"))
    return findings

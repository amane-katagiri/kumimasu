from __future__ import annotations

import difflib
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from .check import READER_KIND_LABEL, CheckReport, dash_hits, fresh_report
from .files import atomic_write, create_new, dump_yaml
from .generate import DATA_NOTE_JA
from .infounits import info_units
from .keep import KeepStore, text_hash
from .llm import ask_replacements
from .surface import SURFACE_CATEGORIES
from .textutil import (
    apply_edits,
    code_ranges,
    enclosing_sentences,
    locate,
    norm,
    overlaps,
    paragraph_at,
)
from .workdir import WorkDir, check_draft_name, now

if TYPE_CHECKING:
    from .llm import Provider

Decision = Literal["", "keep", "delete", "rewrite"]
DECISION_LABEL = {"": "未決", "keep": "残す", "delete": "削る", "rewrite": "書き直す"}
ITEM_KIND_LABEL = {"meta": "メタ言説", "glue": "つなぎの効用文", "lint": "表記", "drop": "書かないはずの事柄",
                   "fabrication": "材料に無い体験", "number": "出典の無い数値", "link": "開けないリンク",
                   "reader": "読者に不明", "form": "形の提案", "caveat": "保守的な但し書き", "flow": "段落の運び",
                   "user": "自分で足した項目"}


class Rewrite(BaseModel):
    result: str
    source: Literal["llm", "user"] = "llm"
    made_from: str = ""
    note: str = ""
    scope: Literal["span", "sentence"] = "span"


class Item(BaseModel):
    id: str
    kind: str
    category: str = ""
    start: int | None = None
    end: int | None = None
    text: str = ""
    votes: str = ""
    reason: str = ""
    unit: dict | None = None
    decision: Decision = ""
    note: str = ""
    rewrite: Rewrite | None = None
    attempt: int = 0
    stale: bool = False
    note_changed: bool = False
    source: str = ""


class Review(BaseModel):
    draft: str
    items: list[Item] = []
    updated_at: str = ""


class ItemEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = ""
    decision: str | None = None
    note: str | None = None
    result: str | None = None
    regenerate: bool = False
    start: int | None = None
    end: int | None = None


class ReviewEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ItemEdit] = []
    remove: list[str] = []


def _item_id(kind: str, category: str, text: str) -> str:
    return f"{kind}-{hashlib.sha1(f'{kind}|{category}|{norm(text)}'.encode()).hexdigest()[:10]}"


def _why(it: dict) -> str:
    return f"{it['why']}\n直し方: {it['fix']}" if it["fix"] else it["why"]


def items_from_checks(src: str, reports: list[CheckReport], units: dict[str, str]) -> list[Item]:
    out: dict[str, Item] = {}
    by_du = {d.id: d for d in info_units(src)}
    dashes = {norm(x) for x in dash_hits(src)}

    def add(kind: str, category: str, text: str, span: tuple[int, int] | None, **kw) -> None:
        iid = _item_id(kind, category, text)
        if iid not in out:
            out[iid] = Item(id=iid, kind=kind, category=category, text=text, start=span[0] if span else None,
                            end=span[1] if span else None, **kw)

    for rep in reports:
        for c in rep.checks:
            n = c.runs
            for it in c.items:
                match c.id:
                    case "meta":
                        add("meta", it["category"], it["text"], locate(src, it["text"]),
                            votes=f"{it.get('votes', '')}/{n}" if n else "", reason=SURFACE_CATEGORIES.get(it["category"], ""))
                    case "caveat":
                        add("caveat", "", it["text"], locate(src, it["text"]),
                            votes=f"{it.get('votes', '')}/{n}" if n else "", reason=SURFACE_CATEGORIES["caveat"])
                    case "glue":
                        add("glue", "glue", it["text"], locate(src, it["text"]),
                            votes=f"{it.get('votes', '')}/{n}" if n else "", reason=SURFACE_CATEGORIES["glue"])
                    case "flow":
                        add("flow", it["category"], it["text"], locate(src, it["text"]),
                            votes=f"{it.get('votes', '')}/{n}" if n else "", reason=SURFACE_CATEGORIES[it["category"]])
                    case "lint":
                        if it["rule"] == "dash" and norm(it["text"]) not in dashes:
                            continue
                        add("lint", it["rule"], it["text"], locate(src, it["text"]), reason=c.relation)
                    case "drop_absent":
                        add("drop", it.get("status", ""), it["text"], None, reason=c.relation,
                            unit={"id": it["id"], "text": units.get(it["id"], it["text"])})
                    case "fabrication":
                        du = by_du.get(it["unit"])
                        add("fabrication", it.get("source", ""), it["text"],
                            (du.start, du.end) if du else locate(src, it["text"]), reason=c.detail)
                    case "numbers":
                        ctx = locate(src, it["context"][:40])
                        at = src.find(it["number"], ctx[0]) if ctx else src.find(it["number"])
                        add("number", "", it["context"], (at, at + len(it["number"])) if at >= 0 else None,
                            reason=c.relation)
                    case "reader":
                        add("reader", READER_KIND_LABEL[it["kind"]], it["quote"], locate(src, it["quote"]), reason=_why(it),
                            unit={"id": it["unit"], "text": units.get(it["unit"], it["unit_text"])} if it["unit"] else None)
                    case "form":
                        add("form", READER_KIND_LABEL[it["kind"]], it["quote"], locate(src, it["quote"]), reason=_why(it))
                    case "links":
                        if it.get("verdict", "ok") != "ok":
                            at = src.find(it["url"])
                            add("link", it.get("verdict", ""), it["url"], (at, at + len(it["url"])) if at >= 0 else None,
                                reason=c.relation)
    drops = {it.unit["id"]: it for it in out.values() if it.kind == "drop" and it.unit}
    for rep in reports:
        for du_id, ids in sorted(rep.sources.items()):
            if (du := by_du.get(du_id)) is None:
                continue
            for uid in ids:
                if (it := drops.get(uid)) is not None and it.start is None:
                    it.start, it.end = du.start, du.end
    return sorted(out.values(), key=_order)


def load_reports(wd: WorkDir, draft: str) -> list[CheckReport]:
    return [r for r in (fresh_report(wd, draft, surface) for surface in (False, True)) if r is not None]


def review_path(wd: WorkDir, draft: str) -> Path:
    return wd.root / f"review.{draft.removesuffix('.md')}.yaml"


def _order(i: Item) -> tuple[bool, int]:
    return i.start is None, i.start or 0


def load_review(wd: WorkDir, draft: str, src: str | None = None) -> Review:
    src = wd.read(draft) if src is None else src
    path = review_path(wd, draft)
    saved = Review.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))) if path.exists() else Review(draft=draft)
    by_id = {i.id: i for i in saved.items}
    units = {u.id: u.text for u in wd.units()}
    keep = KeepStore(wd).hashes()
    items = []
    for it in items_from_checks(src, load_reports(wd, draft), units):
        old = by_id.get(it.id)
        if old:
            it.decision, it.note, it.rewrite, it.source, it.attempt = old.decision, old.note, old.rewrite, old.source, old.attempt
        elif it.kind in ("meta", "caveat", "glue", "flow") and text_hash(it.text) in keep:
            it.decision = "keep"
        items.append(it)
    items += [i for i in saved.items if i.kind == "user" and i.start is not None and i.end is not None]
    for it in items:
        mark_flags(it, src)
    return Review(draft=draft, items=sorted(items, key=_order), updated_at=saved.updated_at)


@dataclass
class ReviewContext:
    wd: WorkDir
    draft: str
    src: str
    review: Review

    @classmethod
    def load(cls, wd: WorkDir, draft: str) -> ReviewContext:
        src = wd.read(draft)
        return cls(wd, draft, src, load_review(wd, draft, src))

    def needs_apply(self) -> bool:
        meta = applied_path(self.wd, self.draft)
        if not self.wd.is_plain_file(final_name(self.draft)) or not meta.is_file():
            return True
        return json.loads(meta.read_text(encoding="utf-8")).get("fingerprint") != apply_fingerprint(self.src, self.review)

    def needs_rewrite_call(self, regenerate: tuple[str, ...] = ()) -> bool:
        return any(i.decision == "rewrite" and not i.stale and (i.rewrite is None or i.id in regenerate)
                   for i in self.review.items if i.start is not None)


def current_text(it: Item, src: str) -> str:
    return src[it.start:it.end] if it.start is not None and it.end is not None and it.end <= len(src) else ""


def mark_flags(it: Item, src: str) -> None:
    made = it.rewrite.made_from if it.rewrite else (it.text if it.kind == "user" else "")
    it.stale = False
    if made and current_text(it, src) != made:
        at = src.find(made)
        if at >= 0 and src.find(made, at + 1) < 0:
            it.start, it.end = at, at + len(made)
        else:
            it.stale = True
    it.note_changed = bool(it.rewrite) and it.rewrite.note != it.note


def save_decisions(wd: WorkDir, draft: str, body: dict, source: str = "") -> Review:
    edit = ReviewEdit.model_validate(body)
    src = wd.read(draft)
    rev = load_review(wd, draft, src)
    sent = {x.id or f"user-{secrets.token_hex(4)}": x for x in edit.items}
    remove = set(edit.remove)
    known = {i.id for i in rev.items}
    items = []
    for it in rev.items:
        if it.id in remove and it.kind == "user":
            continue
        if it.id in sent:
            update_item(it, sent[it.id], src, source)
        items.append(it)
    for iid, x in sent.items():
        if iid in known:
            continue
        if not iid.startswith("user-"):
            raise ValueError(f"知らない項目です: {iid}")
        a, b = x.start, x.end
        if a is None or b is None or not 0 <= a < b <= len(src):
            raise ValueError(f"{iid}: 位置が下書きの外です")
        it = Item(id=iid, kind="user", start=a, end=b, text=src[a:b])
        update_item(it, x, src, source)
        items.append(it)
    rev = Review(draft=draft, items=sorted(items, key=_order))
    for it in rev.items:
        mark_flags(it, src)
    save_review(wd, rev)
    update_keep(wd, rev)
    return rev


def update_item(it: Item, x: ItemEdit, src: str, source: str = "") -> None:
    before = (it.decision, it.note, it.rewrite)
    decision = it.decision if x.decision is None else x.decision
    if decision not in ("", "keep", "delete", "rewrite"):
        raise ValueError(f"決定は keep, delete, rewrite のどれかにしてください: {decision}")
    it.decision = decision
    if x.note is not None:
        it.note = x.note
    if x.regenerate:
        regenerate_item(it)
    elif x.result is not None and (it.rewrite is None or x.result != it.rewrite.result):
        it.rewrite = Rewrite(result=strip_block_marks(src, it.start, x.result.strip()), source="user",
                             made_from=current_text(it, src), note=it.note,
                             scope=it.rewrite.scope if it.rewrite else "span")
    if source and (it.decision, it.note, it.rewrite) != before:
        it.source = source


def regenerate_item(it: Item) -> None:
    it.rewrite = None
    it.attempt += 1


def update_keep(wd: WorkDir, rev: Review) -> None:
    KeepStore(wd).update({it.text: it.decision == "keep" for it in rev.items if it.kind in ("meta", "caveat", "glue", "flow", "user")})


REWRITE_PROMPT_JA = DATA_NOTE_JA + """

次の各項目は、記事の下書きの中で著者が「書き直す」と決めた文です。各項目の文だけを書き直してください。前後の段落は文脈として付けたもので、直しません。

- 著者のメモがあれば、それに従います。
- 段落・見出し・表・コードの構成は変えません。文の数も、メモが求めない限り増やしません。
- 「直す文」に無い見出しの # や箇条書きの記号は付けず、置き換える文そのものだけを返します。
- 「特に直す箇所」がある項目も、返すのは「直す文」全体を置き換える文です。直す箇所の前後はそのまま残し、文として切れないようにします。
- 情報は足しません。

{{"items": [{{"id": "…", "replacement": "…"}}]}} の形の JSON で、すべての項目に答えてください。

{items}"""


_BLOCK_MARK = re.compile(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)+")


def strip_block_marks(src: str, start: int | None, text: str) -> str:
    if start is None:
        return text
    line_head = src[src.rfind("\n", 0, start) + 1:start]
    return _BLOCK_MARK.sub("", text, count=1).lstrip() if line_head.strip() and _BLOCK_MARK.fullmatch(line_head) else text


def rewrite_span(src: str, it: Item) -> tuple[int, int]:
    if it.rewrite is not None and it.rewrite.scope == "span":
        return it.start, it.end
    return enclosing_sentences(src, it.start, it.end)


def rewrite_prompt(src: str, items: list[Item]) -> str:
    blocks = []
    for it in items:
        a, b = enclosing_sentences(src, it.start, it.end)
        focus = f"特に直す箇所（文の一部）: {src[it.start:it.end]}\n\n" if (a, b) != (it.start, it.end) else ""
        # The attempt number makes a regenerated item's prompt differ, so the cache does not hand back the old reply.
        again = f"（作り直し {it.attempt} 回目）\n\n" if it.attempt else ""
        blocks.append(f"## 項目 {it.id}\n\n{again}直す文:\n{src[a:b]}\n\n{focus}"
                      + (f"著者のメモ: {it.note}\n\n" if it.note else "") + f"文脈（この段落の中の文です）:\n{paragraph_at(src, it.start, it.end)}")
    return REWRITE_PROMPT_JA.format(items="\n\n".join(blocks))


def _unique(hay: str, needle: str) -> int:
    i = hay.find(needle)
    return i if needle and i >= 0 and hay.find(needle, i + 1) < 0 else -1


def final_changes(base: str, final: str, rev: Review) -> list[dict]:
    out = []
    for it in rev.items:
        if it.stale or it.start is None or it.end is None:
            continue
        if it.decision == "rewrite" and it.rewrite:
            at = _unique(final, it.rewrite.result)
            if at >= 0:
                out.append({"id": it.id, "kind": "rewritten", "start": at, "end": at + len(it.rewrite.result), "before": it.text})
        elif it.decision == "delete":
            for n in (40, 20, 10):
                lead = base[max(0, it.start - n):it.start]
                at = _unique(final, lead)
                if at >= 0:
                    out.append({"id": it.id, "kind": "deleted", "start": at + len(lead), "end": at + len(lead), "before": it.text})
                    break
    return out


class ApplyResult(BaseModel):
    out: str
    deleted: int
    rewritten: int
    reused: int = 0
    skipped: list[str] = []
    stale: list[str] = []
    diff: list[list[str]] = []
    calls: int = 0


def line_diff(a: str, b: str) -> list[list[str]]:
    rows = []
    for line in difflib.ndiff(a.splitlines(), b.splitlines()):
        tag = line[:1]
        if tag in (" ", "-", "+"):
            rows.append([tag, line[2:]])
    return rows


_FINAL = re.compile(r"\.final(?=\.|$)")


def is_final(name: str) -> bool:
    return bool(_FINAL.search(name.removesuffix(".md")))


def base_of(name: str) -> str:
    stem = name.removesuffix(".md")
    m = _FINAL.search(stem)
    return (stem[:m.start()] if m else stem) + ".md"


def final_name(base: str) -> str:
    return base.removesuffix(".md") + ".final.md"


def applied_path(wd: WorkDir, base: str) -> Path:
    return wd.root / (final_name(base).removesuffix(".md") + ".json")


def apply_fingerprint(src: str, rev: Review) -> str:
    rows = [[i.id, i.decision, i.start, i.end, i.text, i.rewrite.result if i.decision == "rewrite" and i.rewrite else None]
            for i in rev.items if i.decision in ("delete", "rewrite")]
    return hashlib.sha256(json.dumps([src, rows], ensure_ascii=False).encode()).hexdigest()



def base_drafts(wd: WorkDir) -> list[str]:
    return sorted(p.name for p in wd.root.glob("draft*.md")
                  if not p.name.endswith(".prompt.md") and not is_final(p.name) and wd.is_plain_file(p.name))


def require_base(name: str) -> None:
    check_draft_name(name)
    if is_final(name):
        raise ValueError(f"{name} は反映の出力です。元の下書き {base_of(name)} の決定を直して、そこから反映してください")


def download_name(wd: WorkDir, base: str, stamp: datetime | None = None) -> str:
    return f"{wd.root.resolve().name}-{base.removesuffix('.md')}-{(stamp or datetime.now().astimezone()).strftime('%Y%m%d-%H%M')}.md"


def export_final(wd: WorkDir, base: str, to_dir: Path, stamp: datetime | None = None) -> Path:
    require_base(base)
    src = wd.root / final_name(base)
    if not src.is_file():
        raise ValueError(f"{src.name} がありません。先に反映してください")
    to_dir.mkdir(parents=True, exist_ok=True)
    stem = download_name(wd, base, stamp).removesuffix(".md")
    data = wd.read(src.name).encode()
    dest, n = to_dir / f"{stem}.md", 2
    while True:
        try:
            create_new(dest, data, 0o644)
            return dest
        except FileExistsError:
            dest, n = to_dir / f"{stem}-{n}.md", n + 1


def apply_review(wd: WorkDir, draft: str, provider: Provider | None, regenerate: tuple[str, ...] = (),
                 ctx: ReviewContext | None = None) -> ApplyResult:
    require_base(draft)
    ctx = ctx or ReviewContext.load(wd, draft)
    src, rev = ctx.src, ctx.review
    for it in rev.items:
        if it.id in regenerate:
            regenerate_item(it)
            mark_flags(it, src)
    code = code_ranges(src)
    placed = [i for i in rev.items if i.start is not None and i.end is not None]
    skipped = [i.id for i in placed if i.decision in ("delete", "rewrite") and overlaps(i.start, i.end, code)]
    stale = [i.id for i in rev.items if i.decision in ("delete", "rewrite") and i.stale and i.id not in skipped]
    left_out = {*skipped, *stale}
    dels = [i for i in placed if i.decision == "delete" and i.id not in left_out]
    rews = [i for i in placed if i.decision == "rewrite" and i.id not in left_out]
    fresh = [i for i in rews if i.rewrite is None]
    calls = 0
    if fresh:
        if provider is None:
            raise ValueError("結果の無い書き直す項目があるので、書き直しの provider が要ります")
        got = ask_replacements(provider, rewrite_prompt(src, fresh), [i.id for i in fresh])
        calls = 1
        for it in fresh:
            if it.id in got:
                it.rewrite = Rewrite(result=strip_block_marks(src, it.start, got[it.id]), source="llm",
                                     made_from=current_text(it, src), note=it.note, scope="sentence")
                mark_flags(it, src)
        save_review(wd, rev)
    elif regenerate:
        save_review(wd, rev)
    done = [i for i in rews if i.rewrite is not None]
    edits = [(i.start, i.end, "") for i in dels] + [(*rewrite_span(src, i), i.rewrite.result) for i in done]
    text = apply_edits(src, edits)
    out = final_name(draft)
    wd.write(out, text)
    atomic_write(applied_path(wd, draft), json.dumps({"fingerprint": apply_fingerprint(src, rev)}))
    return ApplyResult(out=out, deleted=len(dels), rewritten=len(done), reused=len(done) - sum(i in fresh for i in done),
                       skipped=skipped + [i.id for i in rews if i.rewrite is None], stale=stale,
                       diff=line_diff(src, text), calls=calls)


def save_review(wd: WorkDir, rev: Review) -> None:
    rev.updated_at = now()
    atomic_write(review_path(wd, rev.draft), dump_yaml(rev.model_dump(exclude={"items": {"__all__": {"stale", "note_changed"}}})))

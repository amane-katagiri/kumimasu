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
from pydantic import BaseModel

from .check import CheckReport, check_stem, dash_hits
from .files import atomic_write, create_new, dump_yaml
from .generate import DATA_NOTE_JA
from .keep import KeepStore, text_hash
from .llm import ask_replacements
from .payload import info_units
from .surface import SURFACE_CATEGORIES
from .textutil import apply_edits, code_ranges, locate, norm, overlaps, paragraph_at
from .workdir import WorkDir, check_draft_name, now

if TYPE_CHECKING:
    from .llm import Provider

Decision = Literal["", "keep", "delete", "rewrite"]


class Rewrite(BaseModel):
    result: str
    source: Literal["llm", "user"] = "llm"
    made_from: str = ""
    note: str = ""


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
    stale: bool = False
    note_changed: bool = False
    source: str = ""


class Review(BaseModel):
    draft: str
    items: list[Item] = []
    updated_at: str = ""


def _item_id(kind: str, category: str, text: str) -> str:
    return f"{kind}-{hashlib.sha1(f'{kind}|{category}|{norm(text)}'.encode()).hexdigest()[:10]}"


def _runs(detail: str) -> int | None:
    m = re.search(r"(\d+) 回の判定", detail)
    return int(m[1]) if m else None


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
            n = _runs(c.detail)
            for it in c.items:
                match c.id:
                    case "meta":
                        add("meta", it["category"], it["text"], locate(src, it["text"]),
                            votes=f"{it.get('votes', '')}/{n}" if n else "", reason=SURFACE_CATEGORIES.get(it["category"], ""))
                    case "glue":
                        add("glue", "glue", it["text"], locate(src, it["text"]),
                            votes=f"{it.get('votes', '')}/{n}" if n else "", reason=SURFACE_CATEGORIES["glue"])
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
    src = wd.root / draft
    out = []
    for surface in (False, True):
        p = wd.root / f"{check_stem(draft, surface)}.json"
        if p.exists() and p.stat().st_mtime >= src.stat().st_mtime:
            out.append(CheckReport.model_validate_json(p.read_text(encoding="utf-8")))
    return out


def review_path(wd: WorkDir, draft: str):
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
            it.decision, it.note, it.rewrite, it.source = old.decision, old.note, old.rewrite, old.source
        elif it.kind in ("meta", "glue") and text_hash(it.text) in keep:
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
    """An item keeps its offsets into the base draft and the sentence its result (or the user's item) was made from.
    If the draft no longer has that sentence at those offsets, re-anchor to its only exact occurrence; if it is missing
    or occurs more than once, the item is stale and its result is not applied."""
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
    """Partial update: items in body["items"] are updated (new user-* ids are created from start/end);
    ids in body["remove"] (user items) are removed; everything else is left as it is."""
    src = wd.read(draft)
    rev = load_review(wd, draft, src)
    sent = {str(x.get("id") or f"user-{secrets.token_hex(4)}"): x for x in body.get("items", [])}
    remove = {str(x) for x in body.get("remove", [])}
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
        a, b = int(x["start"]), int(x["end"])
        if not 0 <= a < b <= len(src):
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


def update_item(it: Item, x: dict, src: str, source: str = "") -> None:
    before = (it.decision, it.note, it.rewrite)
    decision = x.get("decision", it.decision)
    if decision not in ("", "keep", "delete", "rewrite"):
        raise ValueError(f"決定は keep, delete, rewrite のどれかにしてください: {decision}")
    it.decision = decision
    it.note = str(x.get("note", it.note))
    if x.get("regenerate"):
        it.rewrite = None
    else:
        result = x.get("result")
        if result is not None and (it.rewrite is None or str(result) != it.rewrite.result):
            it.rewrite = Rewrite(result=str(result).strip(), source="user", made_from=current_text(it, src), note=it.note)
    if source and (it.decision, it.note, it.rewrite) != before:
        it.source = source


def update_keep(wd: WorkDir, rev: Review) -> None:
    KeepStore(wd).update({it.text: it.decision == "keep" for it in rev.items if it.kind in ("meta", "glue", "user")})


REWRITE_PROMPT_JA = DATA_NOTE_JA + """

次の各項目は、記事の下書きの中で著者が「書き直す」と決めた文です。各項目の文だけを書き直してください。前後の段落は文脈として付けたもので、直しません。

- 著者のメモがあれば、それに従います。
- 段落・見出し・表・コードの構成は変えません。文の数も、メモが求めない限り増やしません。
- 情報は足しません。

{{"items": [{{"id": "…", "replacement": "…"}}]}} の形の JSON で、すべての項目に答えてください。

{items}"""


def rewrite_prompt(src: str, items: list[Item]) -> str:
    blocks = []
    for it in items:
        blocks.append(f"## 項目 {it.id}\n\n直す文:\n{src[it.start:it.end]}\n\n"
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
            it.rewrite = None
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
        got = ask_replacements(provider, rewrite_prompt(src, fresh))
        calls = 1
        for it in fresh:
            if it.id in got:
                it.rewrite = Rewrite(result=got[it.id], source="llm", made_from=current_text(it, src), note=it.note)
                mark_flags(it, src)
        save_review(wd, rev)
    elif regenerate:
        save_review(wd, rev)
    done = [i for i in rews if i.rewrite is not None]
    edits = [(i.start, i.end, "") for i in dels] + [(i.start, i.end, i.rewrite.result) for i in done]
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

from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel

from .llm import extract_json
from .parts.markdown import parse as parse_parts
from .payload import info_units
from .surface import SURFACE_CATEGORIES
from .check import KEEP_FILE, CheckReport, check_stem, dash_hits, load_keep, text_hash
from .workdir import WorkDir, atomic_write, dump_yaml, now

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


def norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def find_span(src: str, text: str, start: int = 0) -> tuple[int, int] | None:
    """Locate text in the source ignoring whitespace and inline markup the sentence splitter keeps or drops."""
    chars = [c for c in norm(text) if c not in "*_"]
    if not chars:
        return None
    gap = r"[\s*_]*"
    m = re.compile(gap.join(re.escape(c) for c in chars)).search(src, start)
    return (m.start(), m.end()) if m else None


def code_ranges(src: str) -> list[tuple[int, int]]:
    return [p.span for p in parse_parts(src).parts() if p.kind == "code" and p.span]


def _overlaps(a: int, b: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a < y and x < b for x, y in ranges)


def _item_id(kind: str, category: str, text: str) -> str:
    return f"{kind}-{hashlib.sha1(f'{kind}|{category}|{norm(text)}'.encode()).hexdigest()[:10]}"


def _runs(detail: str) -> int | None:
    m = re.search(r"(\d+) 回の判定", detail)
    return int(m[1]) if m else None


def items_from_checks(src: str, reports: list[CheckReport], units: dict[str, str]) -> list[Item]:
    """Findings that point at text in the draft, with source offsets (None when they cannot be placed)."""
    out: dict[str, Item] = {}
    dunits = info_units(src)
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
                        add("meta", it["category"], it["text"], find_span(src, it["text"]),
                            votes=f"{it.get('votes', '')}/{n}" if n else "", reason=SURFACE_CATEGORIES.get(it["category"], ""))
                    case "glue":
                        add("glue", "glue", it["text"], find_span(src, it["text"]),
                            votes=f"{it.get('votes', '')}/{n}" if n else "", reason=SURFACE_CATEGORIES["glue"])
                    case "lint":
                        if it["rule"] == "dash" and norm(it["text"]) not in dashes:
                            continue
                        add("lint", it["rule"], it["text"], find_span(src, it["text"]), reason=c.relation)
                    case "drop_absent":
                        add("drop", it.get("status", ""), it["text"], None, reason=c.relation,
                            unit={"id": it["id"], "text": units.get(it["id"], it["text"])})
                    case "fabrication":
                        du = next((d for d in dunits if d.id == it["unit"]), None)
                        add("fabrication", it.get("source", ""), it["text"],
                            (du.start, du.end) if du else find_span(src, it["text"]), reason=c.detail)
                    case "numbers":
                        ctx = find_span(src, it["context"][:40])
                        at = src.find(it["number"], ctx[0]) if ctx else src.find(it["number"])
                        add("number", "", it["context"], (at, at + len(it["number"])) if at >= 0 else None,
                            reason=c.relation)
                    case "links":
                        if it.get("verdict", "ok") != "ok":
                            at = src.find(it["url"])
                            add("link", it.get("verdict", ""), it["url"], (at, at + len(it["url"])) if at >= 0 else None,
                                reason=c.relation)
    by_du = {d.id: d for d in dunits}
    for rep in reports:
        for du_id, ids in sorted(rep.sources.items()):
            du = by_du.get(du_id)
            for it in out.values():
                if it.kind == "drop" and it.unit and it.unit["id"] in ids and it.start is None and du:
                    it.start, it.end = du.start, du.end
    return sorted(out.values(), key=lambda i: (i.start is None, i.start or 0))


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


def load_review(wd: WorkDir, draft: str) -> Review:
    """Items from the current check results, with saved decisions and the user's own items merged in."""
    src = wd.read(draft)
    path = review_path(wd, draft)
    saved = Review.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))) if path.exists() else Review(draft=draft)
    by_id = {i.id: i for i in saved.items}
    units = {u.id: u.text for u in wd.units()}
    keep = load_keep(wd)
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
    return Review(draft=draft, items=sorted(items, key=lambda i: (i.start is None, i.start or 0)), updated_at=saved.updated_at)


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
    rev = load_review(wd, draft)
    src = wd.read(draft)
    sent = {str(x.get("id")): x for x in body.get("items", [])}
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
            raise ValueError(f"unknown item id: {iid}")
        a, b = int(x["start"]), int(x["end"])
        if not 0 <= a < b <= len(src):
            raise ValueError(f"{iid}: offsets outside the draft")
        it = Item(id=iid, kind="user", start=a, end=b, text=src[a:b])
        update_item(it, x, src, source)
        items.append(it)
    rev = Review(draft=draft, items=sorted((Item.model_validate(i.model_dump()) for i in items),
                                           key=lambda i: (i.start is None, i.start or 0)))
    for it in rev.items:
        mark_flags(it, src)
    save_review(wd, rev)
    update_keep(wd, rev)
    return rev


def update_item(it: Item, x: dict, src: str, source: str = "") -> None:
    """Apply one item from the page or the CLI: decision and note; a changed result becomes the user's own (locked)
    result; regenerate clears the result so only this item is rewritten on the next apply."""
    before = (it.decision, it.note, it.rewrite)
    decision = x.get("decision", it.decision)
    if decision not in ("", "keep", "delete", "rewrite"):
        raise ValueError(f"decision must be keep, delete or rewrite: {decision}")
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
    """Sentences marked 残す are remembered by text hash; un-marking one in this review forgets it."""
    p = wd.root / KEEP_FILE
    rows = {r["hash"]: r for r in (yaml.safe_load(p.read_text(encoding="utf-8")) or [])} if p.exists() else {}
    for it in rev.items:
        if it.kind not in ("meta", "glue", "user"):
            continue
        h = text_hash(it.text)
        if it.decision == "keep":
            rows[h] = {"hash": h, "text": it.text}
        else:
            rows.pop(h, None)
    atomic_write(p, dump_yaml(list(rows.values())))


_EMPTY_LINE = re.compile(r"\s*(#{1,6}|[-*+]|\d+[.)]|>)?\s*")


def _cleanup_at(text: str, at: int) -> str:
    """Tidy only the line an edit touched: drop it if nothing but a marker is left, else close the gap."""
    ls = text.rfind("\n", 0, at) + 1
    le = text.find("\n", at)
    le = len(text) if le < 0 else le
    line = text[ls:le]
    if _EMPTY_LINE.fullmatch(line):
        return text[:ls] + text[le + 1:]
    left, right = line[:at - ls], line[at - ls:]
    if left[-1:] in (" ", "\t") and right[:1] in (" ", "\t"):
        right = right.lstrip(" \t")
    if not right:
        left = left.rstrip(" \t")
    if left and _EMPTY_LINE.fullmatch(left) and left.strip():
        right = right.lstrip(" \t")
    return text[:ls] + left + right + text[le:]


def _collapse_blank_lines(text: str) -> str:
    out, fence, blank = [], False, False
    for line in text.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence and not line.strip():
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(line)
    return "\n".join(out).strip("\n") + "\n"


def apply_edits(src: str, edits: list[tuple[int, int, str]]) -> str:
    """Replace spans from the end backwards (overlaps and code blocks are skipped), tidying each touched line."""
    code = code_ranges(src)
    taken: list[tuple[int, int]] = []
    text = src
    for a, b, new in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        if _overlaps(a, b, taken) or _overlaps(a, b, code):
            continue
        taken.append((a, b))
        text = text[:a] + new + text[b:]
        text = _cleanup_at(text, a + len(new) if new else a)
    return _collapse_blank_lines(text)


def delete_spans(src: str, spans: list[tuple[int, int]]) -> str:
    return apply_edits(src, [(a, b, "") for a, b in spans])


REWRITE_PROMPT_JA = """次の各項目は、記事の下書きの中で著者が「書き直す」と決めた文です。各項目の文だけを書き直してください。前後の段落は文脈として付けたもので、直しません。

- 著者のメモがあれば、それに従います。
- 段落・見出し・表・コードの構成は変えません。文の数も、メモが求めない限り増やしません。
- 情報は足しません。

{{"items": [{{"id": "…", "replacement": "…"}}]}} の形の JSON で、すべての項目に答えてください。

{items}"""


def rewrite_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {"items": {
        "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "replacement"],
                                   "properties": {"id": {"type": "string"}, "replacement": {"type": "string"}}}}}}


def _paragraph(src: str, a: int, b: int) -> str:
    start = src.rfind("\n\n", 0, a)
    end = src.find("\n\n", b)
    return src[0 if start < 0 else start + 2:len(src) if end < 0 else end].strip()


def rewrite_prompt(src: str, items: list[Item]) -> str:
    blocks = []
    for it in items:
        blocks.append(f"## 項目 {it.id}\n\n直す文:\n{src[it.start:it.end]}\n\n"
                      + (f"著者のメモ: {it.note}\n\n" if it.note else "") + f"文脈（この段落の中の文です）:\n{_paragraph(src, it.start, it.end)}")
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


def needs_apply(wd: WorkDir, base: str) -> bool:
    meta = applied_path(wd, base)
    if not (wd.root / final_name(base)).is_file() or not meta.is_file():
        return True
    stored = json.loads(meta.read_text(encoding="utf-8")).get("fingerprint")
    return stored != apply_fingerprint(wd.read(base), load_review(wd, base))


def base_drafts(wd: WorkDir) -> list[str]:
    return sorted(p.name for p in wd.root.glob("draft*.md") if not p.name.endswith(".prompt.md") and not is_final(p.name))


def require_base(name: str) -> None:
    if is_final(name):
        raise ValueError(f"{name} は反映の出力です。元の下書き {base_of(name)} の決定を直して、そこから反映してください")


def download_name(wd: WorkDir, base: str, stamp: datetime | None = None) -> str:
    return f"{wd.root.resolve().name}-{base.removesuffix('.md')}-{(stamp or datetime.now()).strftime('%Y%m%d-%H%M')}.md"


def export_final(wd: WorkDir, base: str, to_dir: Path, stamp: datetime | None = None) -> Path:
    """Copy <base>.final.md to <to_dir>/<workdir>-<base stem>-<YYYYmmdd-HHMM>.md, never overwriting."""
    require_base(base)
    src = wd.root / final_name(base)
    if not src.is_file():
        raise ValueError(f"{src.name} がありません。先に反映してください")
    to_dir.mkdir(parents=True, exist_ok=True)
    stem = download_name(wd, base, stamp).removesuffix(".md")
    dest, n = to_dir / f"{stem}.md", 2
    while dest.exists():
        dest, n = to_dir / f"{stem}-{n}.md", n + 1
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def needs_rewrite_call(wd: WorkDir, draft: str, regenerate: tuple[str, ...] = ()) -> bool:
    return any(i.decision == "rewrite" and not i.stale and (i.rewrite is None or i.id in regenerate)
               for i in load_review(wd, draft).items if i.start is not None)


def apply_review(wd: WorkDir, draft: str, provider: Provider | None, regenerate: tuple[str, ...] = ()) -> ApplyResult:
    """Deletions are deterministic. A rewrite item with a stored result reuses it (locked); only items without one
    (or listed in regenerate) go to the provider, in one call. Stale items are left out and reported."""
    require_base(draft)
    src = wd.read(draft)
    rev = load_review(wd, draft)
    for it in rev.items:
        if it.id in regenerate:
            it.rewrite = None
            mark_flags(it, src)
    code = code_ranges(src)
    placed = [i for i in rev.items if i.start is not None and i.end is not None]
    skipped = [i.id for i in placed if i.decision in ("delete", "rewrite") and _overlaps(i.start, i.end, code)]
    stale = [i.id for i in rev.items if i.decision in ("delete", "rewrite") and i.stale and i.id not in skipped]
    dels = [i for i in placed if i.decision == "delete" and i.id not in skipped + stale]
    rews = [i for i in placed if i.decision == "rewrite" and i.id not in skipped + stale]
    fresh = [i for i in rews if i.rewrite is None]
    calls = 0
    if fresh:
        if provider is None:
            raise ValueError("結果の無い書き直す項目があるので、書き直しの provider が要ります")
        raw = provider.complete(rewrite_prompt(src, fresh), json_schema=rewrite_schema())
        calls = 1
        got = {str(x.get("id")): str(x.get("replacement", "")).strip() for x in extract_json(raw).get("items", [])}
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

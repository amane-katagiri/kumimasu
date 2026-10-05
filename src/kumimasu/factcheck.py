from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections.abc import Callable

from pydantic import BaseModel

from .metadiscourse import code_free_lines, sentences

_MD_LINK = re.compile(r"\]\((https?://(?:[^\s()]|\([^\s()]*\))+)(?:\s+\"[^\"]*\")?\)")
_BARE = re.compile(r"(?<![(\[`])https?://[^\s<>)\]`」』、。]+")
_TRAILING = ".,;:!?'\""


def extract_urls(markdown: str) -> list[str]:
    seen: dict[str, None] = {}
    markdown = "\n".join(code_free_lines(markdown))
    for m in _MD_LINK.finditer(markdown):
        seen.setdefault(m[1].rstrip(_TRAILING), None)
    for m in _BARE.finditer(_MD_LINK.sub("", markdown)):
        seen.setdefault(m[0].rstrip(_TRAILING), None)
    return list(seen)


class UrlStatus(BaseModel):
    url: str
    status: int | None = None
    error: str = ""

    @property
    def verdict(self) -> str:
        if self.status is None:
            return "unreachable"
        if self.status < 400:
            return "ok"
        if self.status in (404, 410):
            return "dead"
        return "blocked"


Fetcher = Callable[[str, str, float], int]

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def urllib_fetch(url: str, method: str, timeout: float) -> int:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": _UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def check_url(url: str, fetch: Fetcher = urllib_fetch, timeout: float = 15) -> UrlStatus:
    try:
        status = fetch(url, "HEAD", timeout)
        # Many servers reject or mishandle HEAD; only a GET failure counts against the link.
        if status >= 400:
            status = fetch(url, "GET", timeout)
        return UrlStatus(url=url, status=status)
    except Exception as e:  # noqa: BLE001
        return UrlStatus(url=url, error=f"{type(e).__name__}: {e}"[:200])


_FIRST_PERSON = re.compile(r"(私|わたし|僕|ぼく|筆者|自分|手元)")
_EXPERIENCE = re.compile(
    r"(試し(た|ました|てみ)|てみ(た|ました|ると|たところ)|計測し|測っ(た|て)|測定し|ベンチマーク|確認し(た|ました)|"
    r"実行し(た|ました)|使っ(た|ていま|ている|てい)|作り?ました|作った|書いた|書きました|入れ(た|ました)|"
    r"インストールし|動かし(た|ました)|調べ(た|ました)|気づ(いた|きました)|遭遇|ハマ|困っ)"
)
_DIRECT = re.compile(r"(実際に(試|動か|使|測|計測)|手元で|うちの環境|私の環境|自分の環境)")


def _prose_sentences(markdown: str) -> list[str]:
    return [s for line in code_free_lines(markdown) if not line.lstrip().startswith(("#", "|", ">")) for s in sentences(line)]


def firsthand_hits(markdown: str) -> list[str]:
    return [s for s in _prose_sentences(markdown) if _DIRECT.search(s) or (_FIRST_PERSON.search(s) and _EXPERIENCE.search(s))]

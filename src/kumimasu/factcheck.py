from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urlparse

from pydantic import BaseModel

from .textutil import code_free_lines, sentences

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
    checked: bool = True

    @property
    def verdict(self) -> str:
        if not self.checked:
            return "unchecked"
        if self.status is None:
            return "unreachable"
        if self.status < 400:
            return "ok"
        if self.status in (404, 410):
            return "dead"
        return "blocked"


Fetcher = Callable[[str, str, float], int]

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MAX_URLS = 30
TOTAL_SECONDS = 90.0
URL_TIMEOUT = 15.0
POOL = 4
MAX_REDIRECTS = 5


class RefusedAddress(OSError):
    pass


def _public_socket(host: str, port: int, timeout: float) -> socket.socket:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise RefusedAddress(f"{host} を引けません")
    for *_, addr in infos:
        ip = ipaddress.ip_address(addr[0].split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise RefusedAddress(f"{host} は公開されていないアドレス {addr[0]} を指しています")
    family, kind, proto, _, addr = infos[0]
    sock = socket.socket(family, kind, proto)
    sock.settimeout(timeout)
    try:
        sock.connect(addr)
    except OSError:
        sock.close()
        raise
    return sock


class _PublicHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = _public_socket(self.host, self.port, self.timeout)


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        self.sock = self._context.wrap_socket(_public_socket(self.host, self.port, self.timeout), server_hostname=self.host)


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTPConnection, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=ssl.create_default_context())


class _WebOnlyRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme not in ("http", "https"):
            raise RefusedAddress(f"http(s) 以外への転送です: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _PublicHTTPHandler, _PublicHTTPSHandler,
                                      _WebOnlyRedirects)


def urllib_fetch(url: str, method: str, timeout: float) -> int:
    if urlparse(url).scheme not in ("http", "https"):
        raise RefusedAddress(f"http(s) ではありません: {url}")
    req = urllib.request.Request(url, method=method, headers={"User-Agent": _UA, "Accept": "*/*"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def check_url(url: str, fetch: Fetcher = urllib_fetch, timeout: float = URL_TIMEOUT) -> UrlStatus:
    try:
        status = fetch(url, "HEAD", timeout)
        # Many servers reject or mishandle HEAD; only a GET failure counts against the link.
        if status >= 400:
            status = fetch(url, "GET", timeout)
        return UrlStatus(url=url, status=status)
    except Exception as e:  # noqa: BLE001
        return UrlStatus(url=url, error=f"{type(e).__name__}: {e}"[:200])


def check_urls(urls: list[str], fetch: Fetcher = urllib_fetch, total: float = TOTAL_SECONDS) -> list[UrlStatus]:
    todo = urls[:MAX_URLS]
    out = {u: UrlStatus(url=u, checked=False, error="件数の上限を超えたので確かめていません") for u in urls[MAX_URLS:]}
    pool = ThreadPoolExecutor(POOL)
    futures = {pool.submit(check_url, u, fetch): u for u in todo}
    done, _ = wait(futures, timeout=total)
    pool.shutdown(wait=False, cancel_futures=True)
    for f, u in futures.items():
        out[u] = f.result() if f in done else UrlStatus(url=u, checked=False, error="時間切れで確かめていません")
    return [out[u] for u in urls]


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

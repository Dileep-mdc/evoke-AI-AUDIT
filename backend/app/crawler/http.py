from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from ..config import (
    BROWSER_UA,
    CONCURRENCY,
    CONNECT_TIMEOUT,
    CRAWLER_UA,
    MAX_BODY_BYTES,
    MAX_REDIRECTS,
    REQUEST_TIMEOUT,
    RETRIES,
)
from ..errors import humanize_error
from . import snapshot

# Transient responses worth a short retry instead of an immediate FAIL: the origin/CDN
# (502/503/504) or a shared API quota (429) is momentarily unavailable, not permanently broken.
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
RETRY_BACKOFF_SECONDS = 0.4


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: Optional[int]
    headers: dict
    content: bytes
    text: str
    content_type: str
    elapsed_ms: int
    error: Optional[str] = None
    hops: int = 0
    # True when the audit bot's user-agent was refused (401/403) and this response is the one
    # the site served to a browser instead. See fetch().
    bot_ua_refused: bool = False

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 400 and not self.error

    @property
    def html_hash(self) -> str:
        return hashlib.sha256(self.content[:MAX_BODY_BYTES]).hexdigest()


def normalize_url(url: str) -> str:
    url = (url or "").strip().strip("\"'").replace("\\", "/")
    if not url:
        raise ValueError("URL is required")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are allowed")
    host = (parsed.hostname or "").strip().rstrip(".")
    if not host:
        raise ValueError("URL is missing a hostname")
    if host in {"localhost", "127.0.0.1", "0.0.0.0"} or host.startswith("10.") or host.startswith("192.168."):
        raise ValueError("Local/private hosts are not allowed")
    path = parsed.path or "/"
    if path.strip("/") == "":
        path = "/"
    return f"{parsed.scheme}://{host}{path}"


def origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def same_host(a: str, b: str) -> bool:
    ha = (urlparse(a).hostname or "").lower().removeprefix("www.")
    hb = (urlparse(b).hostname or "").lower().removeprefix("www.")
    return ha == hb and ha != ""


# One pooled client per (event loop, user agent, redirect policy), instead of one per request.
#
# This was the single largest cost in a crawl, and it was being paid on every page. A fresh
# AsyncClient opens a new TCP connection and negotiates TLS from scratch -- typically 100-300ms
# against a remote origin -- then throws the connection away when its context manager exits. At
# the MAX_PAGES ceiling that is thousands of handshakes to fetch thousands of pages from ONE
# host that would have served them all down a handful of kept-alive connections. The `limits=`
# below was dead configuration under that pattern: a pool that never saw a second request.
#
# Keyed by the loop as well as by the headers because an AsyncClient binds to the loop that
# created it, and a client reused from a different loop raises rather than reconnecting -- the
# test suite and any CLI entry point call asyncio.run() more than once per process.
_clients: dict[tuple[object, str, bool], httpx.AsyncClient] = {}


def _timeout() -> httpx.Timeout:
    """Separate connect and read budgets.

    One flat timeout meant a host that black-holes connections held a crawl slot for the whole
    read budget, multiplied by the retry count, before the crawl learned it was dead -- and a
    dead host is the one case where waiting cannot help. Connecting is either quick or never.
    """
    return httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT, pool=CONNECT_TIMEOUT)


def client(user_agent: str = CRAWLER_UA, follow: bool = True) -> httpx.AsyncClient:
    """The shared client for this (loop, user agent, redirect policy). Callers must not close it."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    # The loop OBJECT, not its id: ids are recycled once a loop is garbage collected, and a
    # client handed back for a dead loop that happens to share an id fails on its first
    # request with "Event loop is closed".
    key = (loop, user_agent, follow)
    existing = _clients.get(key)
    if existing is not None and not existing.is_closed:
        return existing
    _forget_closed_loops()
    created = httpx.AsyncClient(
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml,text/plain,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        },
        follow_redirects=follow,
        timeout=_timeout(),
        max_redirects=MAX_REDIRECTS,
        limits=httpx.Limits(
            max_connections=max(CONCURRENCY * 2, 24),
            max_keepalive_connections=max(CONCURRENCY, 12),
            keepalive_expiry=30.0,
        ),
    )
    _clients[key] = created
    return created


def _forget_closed_loops() -> None:
    """Drop clients belonging to loops that have finished, so their entries cannot accumulate
    in a process that runs many loops (the test suite, a CLI). Their connections are already
    gone with the loop; this releases the bookkeeping."""
    for key in [k for k in _clients if getattr(k[0], "is_closed", None) and k[0].is_closed()]:
        _clients.pop(key, None)


async def aclose_clients() -> None:
    """Release every pooled connection. Called on server shutdown; safe to call at any time."""
    for key, existing in list(_clients.items()):
        _clients.pop(key, None)
        try:
            await existing.aclose()
        except Exception:
            pass


async def _read_capped(resp: httpx.Response) -> bytes:
    """The response body, stopping as soon as MAX_BODY_BYTES have arrived.

    The cap used to be applied by slicing `resp.content`, which meant the whole body was
    downloaded first: a 200MB video or disk image linked from a page came over the wire in
    full and was then discarded down to 2MB. Reading the stream abandons the transfer at the
    cap instead.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= MAX_BODY_BYTES:
            break
    return b"".join(chunks)[:MAX_BODY_BYTES]


# A WAF/CDN that refuses the audit bot's own user-agent. Only the default identity falls back:
# a caller that passes a user-agent explicitly (TECH-02's AI-bot probes) is asking what THAT
# agent receives, and must see the refusal.
_BOT_REFUSED_STATUS = {401, 403}


async def fetch(
    url: str,
    *,
    user_agent: str = CRAWLER_UA,
    follow: bool = True,
    method: str = "GET",
    live: bool = False,
) -> FetchResult:
    """GET a URL, retrying transient failures.

    `live=True` always asks the network, even during a scan of a saved site copy: for checks
    whose question is what the live site serves right now (TECH-02).

    When the default audit user-agent is refused, the request is repeated with a browser one.
    Without that, a site whose CDN blocks unknown bots reported robots.txt, llms.txt and the
    sitemap as missing and every internal link as broken -- all of which exist and return 200
    to a browser. Being blocked is itself a finding, and TECH-02 still measures it with the
    AI crawlers' own user-agents; `bot_ua_refused` records that the fallback was used.
    """
    if not live and user_agent in (CRAWLER_UA, BROWSER_UA) and follow and method == "GET":
        # Only the app's own identities: an explicit AI-bot user-agent (TECH-02) is asking what
        # the LIVE site serves that bot, which a saved copy cannot answer.
        snap = snapshot.load(url)
        saved = snap.result(url) if snap else None
        if saved is not None:
            return saved
    res = await _fetch_once(url, user_agent=user_agent, follow=follow, method=method)
    if user_agent == CRAWLER_UA and res.status_code in _BOT_REFUSED_STATUS:
        retry = await _fetch_once(url, user_agent=BROWSER_UA, follow=follow, method=method)
        if retry.ok:
            retry.bot_ua_refused = True
            return retry
    return res


async def _fetch_once(url: str, *, user_agent: str, follow: bool, method: str) -> FetchResult:
    last_error = None
    for attempt in range(RETRIES + 1):
        started = time.perf_counter()
        try:
            c = client(user_agent=user_agent, follow=follow)
            async with c.stream(method, url) as resp:
                if resp.status_code in TRANSIENT_STATUS_CODES and attempt < RETRIES:
                    last_error = f"HTTP {resp.status_code}"
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                content = await _read_capped(resp)
                try:
                    text = content.decode(resp.encoding or "utf-8", errors="replace")
                except Exception:
                    text = content.decode("utf-8", errors="replace")
                return FetchResult(
                    url=url,
                    final_url=str(resp.url),
                    status_code=resp.status_code,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    content=content,
                    text=text,
                    content_type=resp.headers.get("content-type", ""),
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    hops=max(0, len(resp.history)),
                )
        except Exception as exc:
            # Some httpx exceptions (ConnectTimeout, ReadTimeout) stringify to "" with no
            # message, which made humanize_error() match nothing and left last_error empty
            # -- silently hiding *why* the fetch failed. Falling back to the exception's own
            # class name keeps the regex matching working and guarantees a non-empty reason.
            raw = str(exc) or type(exc).__name__
            last_error = humanize_error(raw) or raw
            if attempt < RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
    return FetchResult(
        url=url,
        final_url=url,
        status_code=None,
        headers={},
        content=b"",
        text="",
        content_type="",
        elapsed_ms=0,
        error=last_error,
    )


async def check(url: str) -> FetchResult:
    """Whether a URL resolves, and through how many redirects -- without downloading it.

    For the checks that read only a status code and a hop count (TECH-03's llms.txt links,
    TECH-05's sitemap URLs, TECH-06's internal links). They used full GETs, which on a site
    with 600 KB pages meant gigabytes per scan: thousands of URLs at a few seconds each is
    what made those three parameters time out. HEAD returns the same status and redirects
    with no body. A saved site copy answers first; a server that does not implement HEAD
    (405/501) or does not answer it is asked again with GET.
    """
    snap = snapshot.load(url)
    saved = snap.result(url) if snap else None
    if saved is not None:
        return saved
    res = await fetch(url, method="HEAD")
    if res.status_code is None or res.status_code in (405, 501):
        res = await fetch(url)
    return res


def is_html(result: FetchResult) -> bool:
    ct = (result.content_type or "").lower()
    if "html" in ct or "xml" in ct:
        return True
    snippet = result.text[:200].lower()
    return "<html" in snippet or "<!doctype" in snippet


# File extensions whose URLs cannot carry the HTML the audit reads. A marketing site links to
# plenty of them -- brochure PDFs, case-study decks, logo assets, demo videos -- and the crawl
# fetched every one, decoded up to 2MB of binary into a str, and handed it to BeautifulSoup and
# trafilatura before discarding it as not-HTML. Skipping them by extension costs one string
# comparison and removes hundreds of pointless fetches and parses from a large site.
#
# Deliberately not applied to robots.txt, llms.txt or sitemap URLs: those are fetched by name
# through their own modules, never through link discovery, so they are never filtered here.
_NON_HTML_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".rtf",
    ".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".dmg", ".exe", ".msi", ".apk", ".pkg",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif", ".tiff", ".avif",
    ".mp3", ".mp4", ".m4a", ".m4v", ".avi", ".mov", ".wmv", ".webm", ".ogg", ".wav", ".flv",
    ".css", ".js", ".mjs", ".json", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".map",
    ".csv", ".ics", ".dwg", ".psd", ".ai", ".eps",
)


def looks_like_document(url: str) -> bool:
    """Whether this URL is worth fetching as a page, judged from its path alone.

    The extension only, never the query string: "/report?format=pdf" is a page that renders a
    PDF, while "/brochure.pdf" is the PDF itself.
    """
    path = (urlparse(url).path or "").lower().rstrip("/")
    return not path.endswith(_NON_HTML_EXTENSIONS)


def join_url(base: str, href: str) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    try:
        return urljoin(base, href)
    except Exception:
        return None

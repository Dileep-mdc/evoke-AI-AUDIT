from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from ..config import CRAWLER_UA, MAX_BODY_BYTES, MAX_REDIRECTS, REQUEST_TIMEOUT, RETRIES
from ..errors import humanize_error

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
    redirect_chain: list = field(default_factory=list)
    hops: int = 0

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


def client(user_agent: str = CRAWLER_UA, follow: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,application/xml,text/plain,*/*;q=0.8"},
        follow_redirects=follow,
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        max_redirects=MAX_REDIRECTS,
        limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
    )


async def fetch(
    url: str,
    *,
    user_agent: str = CRAWLER_UA,
    follow: bool = True,
    method: str = "GET",
) -> FetchResult:
    last_error = None
    for attempt in range(RETRIES + 1):
        started = time.perf_counter()
        try:
            async with client(user_agent=user_agent, follow=follow) as c:
                resp = await c.request(method, url)
                if resp.status_code in TRANSIENT_STATUS_CODES and attempt < RETRIES:
                    last_error = f"HTTP {resp.status_code}"
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                content = resp.content[:MAX_BODY_BYTES]
                text = ""
                try:
                    text = content.decode(resp.encoding or "utf-8", errors="replace")
                except Exception:
                    text = content.decode("utf-8", errors="replace")
                chain = [str(h.url) for h in resp.history] + [str(resp.url)]
                return FetchResult(
                    url=url,
                    final_url=str(resp.url),
                    status_code=resp.status_code,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    content=content,
                    text=text,
                    content_type=resp.headers.get("content-type", ""),
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    redirect_chain=chain,
                    hops=max(0, len(resp.history)),
                )
        except Exception as exc:
            last_error = humanize_error(str(exc)) or str(exc)
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


def is_html(result: FetchResult) -> bool:
    ct = (result.content_type or "").lower()
    if "html" in ct or "xml" in ct:
        return True
    snippet = result.text[:200].lower()
    return "<html" in snippet or "<!doctype" in snippet


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

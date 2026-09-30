"""Web search for the off-page checks, through Google's Custom Search JSON API.

google_search() returns result links, titles, snippets and, where the page publishes one, a
date. No results page is scraped, so there is no bot-detection page to mistake for "zero
results".

Any failure (not configured, quota exhausted, key rejected, network) comes back as ok=False with
a plain-English reason, and the calling check reports UNKNOWN rather than scoring a guess.

The API key is sent to Google only. What the audit stores and shows is display_url, an ordinary
google.com link for the same query, so a report never carries the key.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote_plus, urlencode

from ..config import (
    BROWSER_UA,
    GOOGLE_API_KEY,
    GOOGLE_CSE_ID,
    GOOGLE_SEARCH_ENDPOINT,
    GOOGLE_SEARCH_RESULTS,
)
from .http import fetch

PROVIDER = "Google Custom Search"


@dataclass
class SearchResult:
    query: str
    ok: bool
    items: list[dict] = field(default_factory=list)   # {"url", "title", "snippet", "date"}
    total_results: Optional[int] = None
    error: Optional[str] = None
    status: Optional[int] = None
    provider: str = PROVIDER                           # which search service answered

    @property
    def display_url(self) -> str:
        return f"https://www.google.com/search?q={quote_plus(self.query)}"

    @property
    def urls(self) -> list[str]:
        return [i["url"] for i in self.items if i.get("url")]

    def snippets(self, limit: int = 8) -> list[dict]:
        return [{"title": i.get("title", ""), "snippet": i.get("snippet", "")}
                for i in self.items[:limit] if i.get("title") or i.get("snippet")]


def _mask(text: str) -> str:
    """Never let a key reach an error message, the evidence or a log line."""
    if GOOGLE_API_KEY and text:
        text = text.replace(GOOGLE_API_KEY, "***")
    return text


def configured() -> bool:
    return bool(GOOGLE_API_KEY and GOOGLE_CSE_ID)


def _api_error(res) -> str:
    """Google's own error message, when the body carries one."""
    try:
        body = json.loads(res.text or "{}")
        return ((body.get("error") or {}).get("message") or "").strip()
    except Exception:
        return ""


def _failure(res, service: str) -> str:
    detail = _api_error(res)
    if res.status_code == 429:
        reason = f"{service} daily quota is used up (HTTP 429)"
    elif res.status_code == 403:
        reason = f"Google rejected the API key or {service} is not enabled for it (HTTP 403)"
    elif res.status_code == 400:
        reason = f"Google rejected the {service} request (HTTP 400)" + ("; check GOOGLE_CSE_ID" if service == "Google search" else "")
    elif res.status_code:
        reason = f"{service} returned HTTP {res.status_code}"
    else:
        reason = f"{service} could not be reached: {res.error or 'network error'}"
    return _mask(f"{reason}{': ' + detail if detail else ''}")


# ---------------------------------------------------------------------------------------------
# Result dates (for review recency)
# ---------------------------------------------------------------------------------------------
_META_DATE_KEYS = ("article:modified_time", "article:published_time", "og:updated_time",
                   "datemodified", "datepublished", "date", "dc.date", "pubdate")
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_SNIPPET_DATE = re.compile(r"^\s*([A-Z][a-z]{2})[a-z]*\.? (\d{1,2}), (\d{4})")
_SNIPPET_AGO = re.compile(r"^\s*(\d+) (day|week|month|year)s? ago", re.I)
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _item_date(item: dict, today: Optional[date] = None) -> Optional[str]:
    """The page's own date if it publishes one, else the date Google prints before the snippet."""
    today = today or datetime.now(timezone.utc).date()
    for tag in ((item.get("pagemap") or {}).get("metatags") or []):
        for key in _META_DATE_KEYS:
            m = _ISO_DATE.search(str(tag.get(key) or ""))
            if m:
                try:
                    return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
                except ValueError:
                    pass
    snippet = item.get("snippet") or ""
    m = _SNIPPET_DATE.match(snippet)
    if m and m[1].lower() in _MONTHS:
        try:
            return date(int(m[3]), _MONTHS[m[1].lower()], int(m[2])).isoformat()
        except ValueError:
            pass
    m = _SNIPPET_AGO.match(snippet)
    if m:
        n, unit = int(m[1]), m[2].lower()
        days = n * {"day": 1, "week": 7, "month": 30, "year": 365}[unit]
        return (today - timedelta(days=days)).isoformat()
    return None


# ---------------------------------------------------------------------------------------------
# Google search
# ---------------------------------------------------------------------------------------------
async def google_search(query: str, num: int = GOOGLE_SEARCH_RESULTS) -> SearchResult:
    if not configured():
        return SearchResult(query, ok=False, error=(
            "Google search is not configured: set GOOGLE_API_KEY and GOOGLE_CSE_ID in backend/.env"))
    params = urlencode({"key": GOOGLE_API_KEY, "cx": GOOGLE_CSE_ID, "q": query, "num": max(1, min(10, num))})
    res = await fetch(f"{GOOGLE_SEARCH_ENDPOINT}?{params}", user_agent=BROWSER_UA, live=True)
    if res.status_code != 200:
        return SearchResult(query, ok=False, error=_failure(res, "Google search"), status=res.status_code)
    try:
        body = json.loads(res.text or "{}")
    except Exception as exc:
        return SearchResult(query, ok=False, error=_mask(f"Google search returned unreadable data: {exc}"), status=200)
    items = []
    for i in body.get("items") or []:
        if not i.get("link"):
            continue
        items.append({"url": i["link"], "title": i.get("title", ""),
                      "snippet": (i.get("snippet") or "").replace("\n", " "), "date": _item_date(i)})
    try:
        total = int((body.get("searchInformation") or {}).get("totalResults") or 0)
    except (TypeError, ValueError):
        total = None
    return SearchResult(query, ok=True, items=items, total_results=total, status=200)


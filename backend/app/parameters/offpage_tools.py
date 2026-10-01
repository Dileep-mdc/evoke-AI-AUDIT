"""The reusable tool layer the off-page agents share (OFF-page architecture, section 4).

Eleven tools, used by all seven agents in offpage_agents.py. There is no separate
Reddit/Quora/certification/directory/trade-press tool: those sources are found with
web_search() and source-specific query patterns.

    1. web_search()                 Google Custom Search, or OpenAI web search (an OpenAI agent)
    2. wikipedia_search()           Wikipedia search API
    3. wikidata_search()            Wikidata entity search API
    4. crawl_page()                 open a source page
    5. extract_content()            title and visible text of a crawled page
    6. extract_entity()             the Wikidata entry or Wikipedia article that is this company
    7. extract_company_details()    name/location details from each business-profile listing
    8. verify_source()              open a source page and confirm it is about the company
    9. compare_entities()           does a piece of text name any of the given terms
   10. extract_reviews()            the result rows that are on review platforms
   11. extract_dates_and_rankings() newest date and age; best/top list pages and positions

Tools collect and extract; they never score. Every lookup is cached on the scan context,
so two agents asking the same question (OFF-01 and OFF-02 both read one Wikidata search)
make one request.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import quote_plus, urlparse

from ..config import (
    BROWSER_UA,
    ENTITY_SEARCH_LIMIT,
    OFFPAGE_VERIFY_TIMEOUT,
    REFERENCE_LANGUAGE,
    SEARCH_PROVIDER,
    WIKIDATA_API,
    WIKIMEDIA_USER_AGENT,
    WIKIPEDIA_API,
)
from ..crawler.google_search import SearchResult, google_search
from ..crawler.google_search import configured as google_configured
from ..crawler.openai_search import configured as openai_configured
from ..crawler.openai_search import openai_search
from ..crawler.http import fetch
from ..crawler.parse import make_soup

_LIST_TITLE = re.compile(r"\b(best|top|leading)\b", re.I)
_TEXT_LIMIT = 20_000


# ---------------------------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------------------------
def host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")


def on_domain(url: str, domain: str) -> bool:
    h = host(url)
    return h == domain or h.endswith("." + domain)


def domains_present(urls: list[str], domains: tuple[str, ...]) -> dict[str, list[str]]:
    return {d: [u for u in urls if on_domain(u, d)] for d in domains}


def site_or(domains) -> str:
    return "(" + " OR ".join(f"site:{d}" for d in domains) + ")"


# Legal-form words that end a registered company name. Directories and search engines mostly list
# the trading name ("nVent Electric", not "nVent Electric plc"), so an exact search for the full
# registered name can find nothing where the company is plainly present.
_LEGAL_FORMS = {
    "plc", "inc", "incorporated", "ltd", "limited", "llc", "llp", "lp", "corp", "corporation", "co",
    "pvt", "private", "pte", "pty", "gmbh", "ag", "sa", "nv", "bv", "spa", "srl", "sarl", "ab", "oy", "as", "kk",
}


def short_name(company_name: str) -> str:
    """The name without its trailing legal form: "nVent Electric plc" -> "nVent Electric",
    "Evoke Technologies Private Limited" -> "Evoke Technologies". Never strips the last word."""
    words = (company_name or "").replace(",", " ").split()
    while len(words) > 1 and words[-1].lower().replace(".", "") in _LEGAL_FORMS:
        words.pop()
    return " ".join(words)


def name_variants(company_name: str) -> list[str]:
    """The names to search, in order: the full name, then the short name when it differs."""
    short = short_name(company_name)
    return [company_name] if not short or short == company_name else [company_name, short]


def _cache(ctx, name: str) -> Optional[dict]:
    if ctx is None:
        return None
    cache = getattr(ctx, name, None)
    if cache is None:
        try:
            cache = {}
            setattr(ctx, name, cache)
        except Exception:
            return None
    return cache


async def _json(url: str, ctx=None) -> tuple[dict | list | None, dict]:
    """GET a Wikimedia JSON API (Wikidata, Wikipedia), cached per scan, sent under the named
    user-agent Wikimedia asks API clients for."""
    cache = _cache(ctx, "_offpage_json_cache")
    if cache is not None and url in cache:
        return cache[url]
    res = await fetch(url, user_agent=WIKIMEDIA_USER_AGENT)
    meta = {"url": url, "status": res.status_code, "error": res.error}
    if not res.ok:
        out = (None, meta)
    else:
        try:
            out = (json.loads(res.text), meta)
        except Exception as exc:
            meta["error"] = str(exc)
            out = (None, meta)
    if cache is not None and out[0] is not None:
        cache[url] = out
    return out


# ---------------------------------------------------------------------------------------------
# 1-3. Search tools
# ---------------------------------------------------------------------------------------------
def search_provider() -> str:
    """"google" or "openai": SEARCH_PROVIDER when set to one, else Google when it is configured
    and OpenAI web search when only an OpenAI key is."""
    if SEARCH_PROVIDER in ("google", "openai"):
        return SEARCH_PROVIDER
    if google_configured():
        return "google"
    return "openai" if openai_configured() else "none"


async def web_search(query: str) -> SearchResult:
    """One web search. Discovery only: the result pages are the evidence, not the listing."""
    provider = search_provider()
    if provider == "openai":
        return await openai_search(query)
    if provider == "google":
        return await google_search(query)
    return SearchResult(query, ok=False, error=(
        "Web search is not configured: set OPENAI_API_KEY, or GOOGLE_API_KEY and GOOGLE_CSE_ID, in backend/.env"))


async def web_search_company(template: str, company_name: str) -> SearchResult:
    """web_search() for a query naming the company ("{name}" in the template). When the full name
    finds nothing, the search runs once more with the legal form removed. A failed search is
    returned as it is: retrying a different name cannot fix an unavailable service."""
    sr = None
    for name in name_variants(company_name):
        sr = await web_search(template.replace("{name}", name))
        if not sr.ok or sr.items:
            return sr
    return sr


def wikidata_search_url(company_name: str, limit: int = ENTITY_SEARCH_LIMIT) -> str:
    return (f"{WIKIDATA_API}?action=wbsearchentities&search={quote_plus(company_name)}"
            f"&language={REFERENCE_LANGUAGE}&format=json&type=item&limit={limit}")


def wikipedia_search_url(company_name: str) -> str:
    return f"{WIKIPEDIA_API}?action=query&list=search&srsearch={quote_plus(company_name)}&utf8=1&format=json"


async def _by_name(company_name: str, ctx, url_for, hits_of) -> tuple[dict | None, dict]:
    """Search by the full name, then by the short name when the full one has no hits."""
    data, meta = None, {}
    for name in name_variants(company_name):
        data, meta = await _json(url_for(name), ctx)
        if data is None or hits_of(data):
            break
    return data, meta


async def wikidata_search(company_name: str, ctx=None) -> tuple[dict | None, dict]:
    return await _by_name(company_name, ctx, wikidata_search_url, lambda d: d.get("search"))


async def wikipedia_search(company_name: str, ctx=None) -> tuple[dict | None, dict]:
    return await _by_name(company_name, ctx, wikipedia_search_url, lambda d: (d.get("query") or {}).get("search"))


# ---------------------------------------------------------------------------------------------
# 4-5. Crawl and extract
# ---------------------------------------------------------------------------------------------
async def crawl_page(url: str, ctx=None):
    """Open a third-party source page as a browser would. Cached per scan."""
    cache = _cache(ctx, "_offpage_page_cache")
    if cache is not None and url in cache:
        return cache[url]
    res = await fetch(url, user_agent=BROWSER_UA, live=True)
    if cache is not None:
        cache[url] = res
    return res


def _content(html: str) -> dict:
    soup = make_soup(html)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return {"title": title, "text": soup.get_text(" ", strip=True)[:_TEXT_LIMIT]}


async def extract_content(page) -> dict:
    """Title and visible text of a crawled page. Parsed off the event loop."""
    if page is None or not getattr(page, "ok", False) or not getattr(page, "text", ""):
        return {"title": "", "text": ""}
    return await asyncio.to_thread(_content, page.text)


# ---------------------------------------------------------------------------------------------
# 6-7. Entity and company-detail extraction
# ---------------------------------------------------------------------------------------------
def extract_entity(hits: list[dict], company_name: str, field: str = "label") -> Optional[dict]:
    """The search hit that is this company: the first whose name (`field`: a Wikidata "label"
    or a Wikipedia "title") names the company, per names_company(). None when no hit does."""
    return next((h for h in hits or [] if names_company(h.get(field) or "", company_name)), None)


def extract_company_details(items: list[dict], domains: tuple[str, ...], brand_terms: list[str], locations: list[str]) -> dict:
    """Per profile platform found: its result links, and whether its listing names the company
    and one of the site's own office locations."""
    out = {}
    for d in domains:
        rows = [i for i in items if on_domain(i["url"], d)]
        if not rows:
            continue
        blob = " ".join(f"{i['title']} {i['snippet']}" for i in rows)
        out[d] = {"urls": [i["url"] for i in rows][:3],
                  "name_matches": compare_entities(blob, brand_terms),
                  "location_matches": bool(locations) and compare_entities(blob, locations)}
    return out


# ---------------------------------------------------------------------------------------------
# 8-9. Verification and matching
# ---------------------------------------------------------------------------------------------
def _plain(name: str) -> str:
    """Lower-case words only: "nVent-Electric, plc." -> "nvent electric plc"."""
    return " ".join(re.sub(r"[^\w]+", " ", (name or "").lower()).split())


def names_company(candidate: str, company_name: str) -> bool:
    """True when a record's name (a Wikidata label, a Wikipedia title) is this company's.

    The candidate must contain the company's whole name without its legal form, as words:
    "NVent Electric" names "nVent Electric plc", while "Evoke plc" -- a betting company --
    does not name "Evoke Technologies", although both start with "Evoke". Matching on the
    first word alone made every same-first-word company look like a match.
    """
    wanted = _plain(short_name(company_name))
    return bool(wanted) and f" {wanted} " in f" {_plain(candidate)} "


def compare_entities(text: str, terms: list[str]) -> bool:
    """True when the text names any of the terms (case-insensitive)."""
    text = (text or "").lower()
    return any(t and t.lower() in text for t in terms)


async def verify_source(url: str, terms: list[str], ctx=None, require: list[str] | None = None) -> dict:
    """Open a source page and confirm it is about the company.

    result is one of: verified (the page names the company, and one of `require` when given),
    not_about_company (it opened but does not name the company), claim_not_found (it names the
    company but none of `require`), blocked (the site refused a browser: 401/403/429/999),
    unreachable.
    """
    try:
        page = await asyncio.wait_for(crawl_page(url, ctx), timeout=OFFPAGE_VERIFY_TIMEOUT)
    except asyncio.TimeoutError:
        return {"url": url, "result": "unreachable", "status": None, "reason": "timed out"}
    status = getattr(page, "status_code", None)
    if status in (401, 403, 429, 999):
        return {"url": url, "result": "blocked", "status": status}
    if not getattr(page, "ok", False):
        return {"url": url, "result": "unreachable", "status": status, "reason": getattr(page, "error", None)}
    content = await extract_content(page)
    text = f"{content['title']} {content['text']}"
    if not compare_entities(text, terms):
        outcome = "not_about_company"
    elif require and not compare_entities(text, require):
        outcome = "claim_not_found"
    else:
        outcome = "verified"
    return {"url": url, "result": outcome, "status": status, "title": content["title"][:160]}


# ---------------------------------------------------------------------------------------------
# 10-11. Reviews, dates and rankings
# ---------------------------------------------------------------------------------------------
def extract_reviews(items: list[dict], domains: tuple[str, ...]) -> list[dict]:
    """The result rows that sit on one of the review platforms."""
    return [i for i in items if any(on_domain(i["url"], d) for d in domains)]


def extract_dates_and_rankings(items: list[dict], *, own_domain: str = "", today: Optional[date] = None) -> dict:
    """Newest dated item and its age in days; and the best/top/leading list pages among the
    items (not on the company's own site) with their position in the results."""
    today = today or datetime.now(timezone.utc).date()
    dates = sorted((i["date"] for i in items if i.get("date")), reverse=True)
    newest = dates[0] if dates else None
    own = (own_domain or "").lower().removeprefix("www.")
    lists = [{"url": i["url"], "title": i.get("title", ""), "position": n}
             for n, i in enumerate(items, 1)
             if _LIST_TITLE.search(i.get("title") or "") and not (own and on_domain(i["url"], own))]
    return {"dates": dates, "newest_date": newest,
            "age_days": (today - date.fromisoformat(newest)).days if newest else None,
            "list_pages": lists}

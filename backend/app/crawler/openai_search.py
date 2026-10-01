"""Web search for the off-page checks through an OpenAI agent (OpenAI Agents SDK).

One agent with OpenAI's hosted web-search tool runs each query and returns the results as a
structured list -- url, title, snippet, date -- the same shape google_search() returns, so the
off-page agents and their deterministic scoring do not care which provider answered.

The key is OPENAI_API_KEY from backend/.env (config.py loads it into the environment, where the
Agents SDK reads it). Tracing is off: no query leaves this process except the search itself.

A model can misremember a URL. Every result is therefore checked against the sources the
web-search call itself returned, and a URL the search did not return is dropped. Any failure
(no key, quota, rejected key, network, timeout) comes back as ok=False with a plain reason, and
the calling check reports UNKNOWN rather than scoring a guess.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel

from ..config import (
    GOOGLE_SEARCH_RESULTS,
    OPENAI_API_KEY,
    OPENAI_SEARCH_CONCURRENCY,
    OPENAI_SEARCH_MODEL,
    OPENAI_SEARCH_RETRIES,
    OPENAI_SEARCH_TIMEOUT,
)
from .google_search import SearchResult

PROVIDER = "OpenAI web search"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Each query is a full agent run with a web search, and the results it reads count against the
# account's tokens-per-minute limit; a scan's off-page checks fire together, so few run at once.
_semaphore = asyncio.Semaphore(max(1, OPENAI_SEARCH_CONCURRENCY))
_RATE_LIMIT_WAITS = (10.0, 20.0, 40.0)   # seconds, when OpenAI does not say how long to wait
_MAX_WAIT = 60.0

INSTRUCTIONS = (
    "You are a web search engine. Run a web search for the query you are given and return the "
    "results you found, in the order a search engine would list them.\n"
    "- Honour search operators as a search engine does: quoted phrases must appear, site:domain "
    "limits results to that domain, inurl:word needs the word in the URL, and OR means either.\n"
    "- Only return URLs that appeared in your search results. Never invent or guess a URL.\n"
    "- title: the page title. snippet: one or two sentences from the result about the query.\n"
    "- date: the page's own publish or last-update date as YYYY-MM-DD when the result shows one; "
    "otherwise null. Never use today's date or a guess.\n"
    "- Return an empty list when nothing matches."
)


class _Item(BaseModel):
    url: str
    title: str
    snippet: str
    date: Optional[str] = None


class _Results(BaseModel):
    results: list[_Item]


_agent = None


def _search_agent():
    """Built on first use, so the backend starts without the Agents SDK when it is not needed."""
    global _agent
    if _agent is None:
        from agents import Agent, ModelSettings, WebSearchTool

        _agent = Agent(
            name="off-page-web-search",
            model=OPENAI_SEARCH_MODEL,
            instructions=INSTRUCTIONS,
            tools=[WebSearchTool()],
            output_type=_Results,
            # The sources the web-search call really returned, used to drop invented URLs.
            model_settings=ModelSettings(response_include=["web_search_call.action.sources"]),
        )
    return _agent


def configured() -> bool:
    return bool(OPENAI_API_KEY)


def _key(url: str) -> str:
    """A URL compared on host and path: the search adds tracking parameters and trailing slashes."""
    u = urlparse(url)
    return f"{(u.hostname or '').lower().removeprefix('www.')}{u.path.rstrip('/')}"


def _sources(result) -> Optional[set[str]]:
    """The URLs the web-search calls returned, or None when the API did not report them."""
    found, reported = set(), False
    for raw in getattr(result, "raw_responses", None) or []:
        for item in getattr(raw, "output", None) or []:
            if getattr(item, "type", "") != "web_search_call":
                continue
            sources = getattr(getattr(item, "action", None), "sources", None)
            if sources is not None:
                reported = True
                found.update(_key(getattr(s, "url", "") or "") for s in sources)
    return found if reported else None


def _failure(exc: Exception) -> tuple[str, Optional[int]]:
    import openai

    if isinstance(exc, asyncio.TimeoutError):
        return f"OpenAI web search timed out after {OPENAI_SEARCH_TIMEOUT:.0f}s", None
    if isinstance(exc, openai.RateLimitError):
        if _out_of_credit(exc):
            return "The OpenAI account has run out of credit (HTTP 429 insufficient_quota); add credit to resume web search", 429
        return f"OpenAI web search rate limit still reached after {OPENAI_SEARCH_RETRIES} retries (HTTP 429)", 429
    if isinstance(exc, openai.AuthenticationError):
        return "OpenAI rejected the API key (HTTP 401); check OPENAI_API_KEY in backend/.env", 401
    if isinstance(exc, openai.PermissionDeniedError):
        return f"OpenAI refused the request (HTTP 403); check that the project can use {OPENAI_SEARCH_MODEL}", 403
    if isinstance(exc, openai.APIConnectionError):
        return "OpenAI could not be reached: network error", None
    if isinstance(exc, openai.APIStatusError):
        return f"OpenAI web search returned HTTP {exc.status_code}", exc.status_code
    return f"OpenAI web search failed: {type(exc).__name__}", None


def _out_of_credit(exc) -> bool:
    """A 429 for an empty account, which no amount of waiting fixes, rather than a rate limit."""
    return getattr(exc, "code", None) == "insufficient_quota" or "insufficient_quota" in str(exc)


def _wait_for(exc, attempt: int) -> float:
    """How long to wait before retrying a rate-limited search: OpenAI's Retry-After when sent."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    for header, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        try:
            if headers.get(header):
                return min(_MAX_WAIT, max(0.0, float(headers[header]) / scale))
        except (TypeError, ValueError):
            pass
    return _RATE_LIMIT_WAITS[min(attempt, len(_RATE_LIMIT_WAITS) - 1)]


async def _run(query: str, num: int):
    """One search, retried when OpenAI's rate limit refuses it. The wait happens outside the
    semaphore, so a rate-limited search does not hold a slot other searches could use."""
    import openai
    from agents import RunConfig, Runner

    for attempt in range(OPENAI_SEARCH_RETRIES + 1):
        try:
            async with _semaphore:
                return await asyncio.wait_for(
                    Runner.run(_search_agent(), f"Query: {query}\nReturn up to {num} results.",
                               run_config=RunConfig(tracing_disabled=True)),
                    timeout=OPENAI_SEARCH_TIMEOUT)
        except openai.RateLimitError as exc:
            if _out_of_credit(exc) or attempt == OPENAI_SEARCH_RETRIES:
                raise
            await asyncio.sleep(_wait_for(exc, attempt))


async def openai_search(query: str, num: int = GOOGLE_SEARCH_RESULTS) -> SearchResult:
    if not configured():
        return SearchResult(query, ok=False, provider=PROVIDER,
                            error="OpenAI web search is not configured: set OPENAI_API_KEY in backend/.env")
    try:
        result = await _run(query, num)
    except Exception as exc:
        error, status = _failure(exc)
        return SearchResult(query, ok=False, provider=PROVIDER, error=error, status=status)

    returned = _sources(result)
    items, seen = [], set()
    for r in (getattr(result.final_output, "results", None) or []):
        key = _key(r.url)
        if not r.url.startswith(("http://", "https://")) or key in seen:
            continue
        if returned is not None and key not in returned:
            continue  # not a URL the search returned
        seen.add(key)
        items.append({"url": r.url, "title": r.title, "snippet": r.snippet.replace("\n", " "),
                      "date": r.date if r.date and _DATE.match(r.date) else None})
    return SearchResult(query, ok=True, provider=PROVIDER, items=items[:num], total_results=len(items), status=200)

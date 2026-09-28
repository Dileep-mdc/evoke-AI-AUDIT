"""The crawl scheduler: fetch a batch of URLs concurrently, and account for every one.

This replaces a Crawlee BasicCrawler that used to sit between this function and fetch().
Crawlee owns an autoscaled request queue, deduplication and retry accounting -- none of which
this batch needed, because the caller hands over a list that is already deduplicated and
priority-ordered, and fetch() in http.py already does its own retries and returns a reason
instead of raising. What the queue did contribute was measurable: on a 1,095-URL crawl it
handed back 750 results and dropped 345 with neither handler firing, so the code below it had
to re-fetch a third of the site directly to honour its own contract -- and a Configuration, a
storage client and an event manager were built per batch to schedule work that is a semaphore.

So the semaphore is what this is now. The contract is unchanged and is the important part:

    every URL passed in appears exactly once in the mapping returned, keyed by the URL as it
    was REQUESTED rather than the one it ended up at, so a caller can match a result back to
    what it asked for even when the response redirected somewhere else.

A URL that could not be fetched comes back as a FetchResult carrying the reason. Missing must
never mean absent: these mappings are what every parameter's denominator is built from, and a
hole in one silently shrinks the site the audit believes it measured.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from ..config import CONCURRENCY, CRAWLER_UA
from .http import FetchResult, fetch

log = logging.getLogger("crawler")


def failed_result(url: str, error: str) -> FetchResult:
    """A FetchResult standing in for a page that was never successfully fetched."""
    return FetchResult(
        url=url,
        final_url=url,
        status_code=None,
        headers={},
        content=b"",
        text="",
        content_type="",
        elapsed_ms=0,
        error=error[:300] or "the crawl returned no result for this URL",
    )


async def crawl_raw(
    urls: list[str],
    *,
    user_agent: str = CRAWLER_UA,
    concurrency: int = CONCURRENCY,
    on_progress: Optional[Callable[[int], None]] = None,
    deadline: Optional[float] = None,
) -> dict[str, FetchResult]:
    """Fetch every URL once over plain HTTP, keyed by the URL as it was requested.

    `deadline` is a time.monotonic() timestamp. Once it passes, URLs that have not started
    are not fetched; each still gets an entry saying the crawl ran out of time, so a scan on
    a pathological site finishes with an honest, partial crawl instead of running forever.
    """
    if not urls:
        return {}

    results: dict[str, FetchResult] = {}
    total = len(urls)
    done = 0
    last_reported = -1
    semaphore = asyncio.Semaphore(max(1, concurrency))

    def report() -> None:
        # Only on a change of whole percent: the callback writes the scan row to SQLite, and
        # at one write per page a large crawl spent real time reporting that it was crawling.
        nonlocal last_reported
        if on_progress is None:
            return
        pct = min(99, int(done / total * 100))
        if pct != last_reported:
            last_reported = pct
            on_progress(pct)

    async def _one(url: str) -> None:
        nonlocal done
        async with semaphore:
            if deadline is not None and time.monotonic() > deadline:
                results[url] = failed_result(url, "skipped: the crawl reached its time budget")
            else:
                try:
                    results[url] = await fetch(url, user_agent=user_agent)
                except Exception as exc:  # fetch() does not raise, but a result is owed regardless
                    results[url] = failed_result(url, f"{type(exc).__name__}: {exc}")
        done += 1
        report()

    await asyncio.gather(*(_one(u) for u in urls))

    for url in urls:
        # Belt and braces: the mapping is a promise the caller relies on to build its
        # denominators, so it is never returned with a hole in it.
        results.setdefault(url, failed_result(url, "the crawl returned no result for this URL"))

    if on_progress:
        on_progress(100)
    return results

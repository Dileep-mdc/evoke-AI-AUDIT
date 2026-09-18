"""The crawl: Crawlee's request queue over our httpx fetch.

Division of labour, and why it is split this way:

  Crawlee   owns CRAWLING -- the autoscaled request queue, deduplication, retry
            accounting and the concurrency ceiling. It runs in-process: unlike Scrapy it
            installs no Twisted reactor, so it needs no subprocess and can be awaited
            directly from the FastAPI request that started the scan.
  httpx     owns SCRAPING -- the actual HTTP call, via the fetch() in http.py that the
            rest of the audit already depends on.

Crawlee's own HttpCrawler was the obvious first choice and is deliberately not used:

  * Its HttpResponse exposes status, headers and body but no redirect history, and
    TECH-20 scores a page as problematic at 3 or more redirect hops. Going through
    fetch() keeps httpx's exact hop count.
  * Its httpx extra depends on httpx2, a separate distribution from the httpx this
    project pins. Reusing fetch() keeps one HTTP library in the tree instead of two.
  * fetch() already carries the behaviour this audit relies on: the MAX_BODY_BYTES cap,
    transient-status retries, and humanize_error() so a failure reaches the report as a
    sentence rather than a stack-trace fragment.

BasicCrawler is the Crawlee class meant for exactly this -- bring your own transport, let
Crawlee schedule it.
"""
from __future__ import annotations

import tempfile
from datetime import timedelta
from typing import Callable

from crawlee import ConcurrencySettings, Request, service_locator
from crawlee.configuration import Configuration
from crawlee.crawlers import BasicCrawler, BasicCrawlingContext
from crawlee.events import LocalEventManager
from crawlee.storage_clients import MemoryStorageClient

from ..config import CONCURRENCY, CRAWLER_UA, REQUEST_TIMEOUT
from .http import FetchResult, fetch

# fetch() already retries transient statuses and connection errors itself (RETRIES in
# config.py), and returns a FetchResult carrying the reason rather than raising. A second
# retry layer here would multiply the wall-clock cost of a genuinely dead host by the
# product of the two, so Crawlee is told to hand each URL over exactly once.
_CRAWLEE_RETRIES = 0


_configuration: Configuration | None = None


def _configured() -> Configuration:
    """The one Configuration every crawl in this process shares.

    Built once and registered on Crawlee's service locator up front, for two reasons:

      * A scan calls crawl_raw() several times -- the priority batch, then one call per
        discovery round -- and the server runs many scans over its lifetime. Making a
        Configuration per call also made a temp directory per call and never removed any
        of them, so a long-lived server slowly littered the system temp directory.
      * Registering it before anything else touches the service locator is what stops
        Crawlee warning that it had to create an event manager implicitly, which it
        otherwise does on the first storage-client call.

    storage_dir is required but never written to: every crawl uses MemoryStorageClient, so
    the queue and dataset stay in memory and nothing about one batch outlives it.
    """
    global _configuration
    if _configuration is None:
        _configuration = Configuration(storage_dir=tempfile.mkdtemp(prefix="aiaudit_crawlee_"))
        try:
            service_locator.set_configuration(_configuration)
        except Exception:
            # Already set by something else in this process; that configuration is just as
            # usable as this one, and failing a scan over it would be absurd.
            pass
    return _configuration


async def crawl_raw(
    urls: list[str],
    *,
    user_agent: str = CRAWLER_UA,
    concurrency: int = CONCURRENCY,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, FetchResult]:
    """Fetch every URL once over plain HTTP, keyed by the URL as it was requested.

    The key is the requested URL, not the final one, so a caller can match a result back
    to what it asked for even when the response redirected somewhere else.
    """
    if not urls:
        return {}

    results: dict[str, FetchResult] = {}
    done = 0

    # MemoryStorageClient, not the default: Crawlee persists its request queue and dataset
    # to disk otherwise, which would drop a storage/ tree next to whatever directory the
    # server started in and grow it across scans. This crawl is a single batch whose caller
    # holds the results in memory, so there is nothing worth persisting.
    configuration = _configured()
    crawler = BasicCrawler(
        storage_client=MemoryStorageClient(),
        configuration=configuration,
        event_manager=LocalEventManager.from_config(configuration),
        concurrency_settings=ConcurrencySettings(
            max_concurrency=concurrency,
            desired_concurrency=min(concurrency, 10),
        ),
        max_request_retries=_CRAWLEE_RETRIES,
        # Generous: the ceiling that matters is REQUEST_TIMEOUT inside fetch(), applied per
        # attempt. This only needs to be longer than fetch()'s own worst case so Crawlee
        # never cancels a request that is still legitimately in flight.
        request_handler_timeout=timedelta(seconds=REQUEST_TIMEOUT * 3),
        # The audit MUST fetch pages robots.txt disallows. Whether a site blocks AI crawlers
        # is the finding TECH-01 and TECH-02 report, so a crawler that quietly obeyed the
        # rules would have nothing to measure and would under-report site content.
        respect_robots_txt_file=False,
        configure_logging=False,
    )

    @crawler.router.default_handler
    async def _handler(context: BasicCrawlingContext) -> None:
        nonlocal done
        results[context.request.url] = await fetch(context.request.url, user_agent=user_agent)
        done += 1
        if on_progress:
            on_progress(min(99, int(done / len(urls) * 100)))

    @crawler.failed_request_handler
    async def _failed(context: BasicCrawlingContext, error: Exception) -> None:
        # fetch() does not raise, so reaching here means Crawlee itself gave up on the
        # request (handler timeout, cancellation). Record it as a failed FetchResult so the
        # URL is still present in the mapping with a reason, rather than silently absent.
        nonlocal done
        results.setdefault(
            context.request.url,
            FetchResult(
                url=context.request.url,
                final_url=context.request.url,
                status_code=None,
                headers={},
                content=b"",
                text="",
                content_type="",
                elapsed_ms=0,
                error=f"{type(error).__name__}: {error}"[:300] or "crawl request failed",
            ),
        )
        done += 1

    # always_enqueue because the caller's list is already deduplicated and ordered by
    # priority. Crawlee's default unique_key would collapse two entries that differ only
    # by fragment or query order, and the caller expects one result per URL it passed.
    await crawler.run([Request.from_url(u, always_enqueue=True) for u in urls])

    if on_progress:
        on_progress(100)
    return results

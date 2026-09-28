"""Body-text extraction, run in worker processes.

trafilatura is the most expensive step in reading a page -- measured on this site's own HTML,
0.2-0.4s per page against 0.14-0.24s for the BeautifulSoup parse -- and it is pure-Python CPU
work, which is the one kind of work threads cannot help with. Measured on eight real pages:

    sequential            0.56s per page
    eight threads         0.70s per page   (slower: GIL contention plus scheduling overhead)
    extraction in 4 procs 0.18s per page   (warm)

So extraction goes to processes and everything else stays where it is. The BeautifulSoup tree
cannot travel: `Page.soup` is what fifteen parameters read, and shipping a parsed tree between
processes costs more than rebuilding it. What crosses the boundary is a string of HTML in and
a string of text out.

Degradation is total and silent by design. A pool that will not start, a worker that dies, a
machine where spawning is unavailable -- each falls back to extracting in this process, which
is exactly what the crawl did before, and the fallback is permanent for the process rather
than retried per page. Extraction is not optional to the audit, so it may never be the thing
that fails a scan.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from typing import Optional

from ..config import EXTRACT_POOL_MIN_BATCH, EXTRACT_WORKERS

log = logging.getLogger("crawler.extract")

# The one definition of how body text is extracted. Both the in-process path and the worker
# path read it, so a change to extraction cannot apply to only some pages of a crawl.
EXTRACT_KWARGS = {"include_tables": True, "include_comments": False, "favor_recall": True}

_pool: Optional[ProcessPoolExecutor] = None
_pool_disabled = False


def extract_text_sync(html: str) -> Optional[str]:
    """Extract body text here and now. Returns None when trafilatura finds no article."""
    if not html:
        return None
    try:
        import trafilatura

        return trafilatura.extract(html, **EXTRACT_KWARGS)
    except Exception:
        # A malformed document is a fact about the page, not a reason to lose the page: the
        # caller falls back to reading visible text off the soup it already has.
        return None


def _pool_for(batch_size: int) -> Optional[ProcessPoolExecutor]:
    """The shared pool, or None when this batch should just be extracted in-process.

    Workers cost a fresh interpreter each (~1.5s on Windows, where spawning is the only
    option), paid once for the life of the server. That is a good trade across a thousand-page
    crawl and a bad one for a handful of pages, hence the batch floor.
    """
    global _pool, _pool_disabled
    if _pool_disabled or EXTRACT_WORKERS < 1 or batch_size < EXTRACT_POOL_MIN_BATCH:
        return None
    if _pool is None:
        try:
            _pool = ProcessPoolExecutor(max_workers=EXTRACT_WORKERS)
        except Exception as exc:
            _pool_disabled = True
            log.warning("extraction pool unavailable (%s); extracting in-process", exc)
            return None
    return _pool


async def extract_many(documents: list[str]) -> list[Optional[str]]:
    """Extract body text for a batch of HTML documents, in worker processes when it pays.

    Results line up with `documents` by position. An empty document extracts to None without
    reaching a worker, so pages that were never HTML cost nothing here.
    """
    if not documents:
        return []

    indexed = [(i, html) for i, html in enumerate(documents) if html]
    results: list[Optional[str]] = [None] * len(documents)
    pool = _pool_for(len(indexed))
    if pool is None:
        for i, html in indexed:
            results[i] = extract_text_sync(html)
        return results

    loop = asyncio.get_running_loop()
    # Twice the worker count: enough to keep every worker fed while the next result is being
    # collected, without holding the whole crawl's HTML in flight between processes at once.
    semaphore = asyncio.Semaphore(max(1, EXTRACT_WORKERS * 2))
    failed = False

    async def _one(index: int, html: str) -> None:
        global _pool, _pool_disabled
        nonlocal failed
        async with semaphore:
            if failed or _pool_disabled:
                results[index] = extract_text_sync(html)
                return
            try:
                results[index] = await loop.run_in_executor(pool, extract_text_sync, html)
            except Exception as exc:
                # A broken pool cannot be repaired mid-flight, and every page still in the
                # batch would raise the same way, so it is retired once and the rest of this
                # crawl -- and the rest of the process -- extracts in-process.
                failed = True
                _pool_disabled = True
                _pool = None
                log.warning("extraction pool failed (%s); extracting in-process from here", exc)
                results[index] = extract_text_sync(html)

    await asyncio.gather(*(_one(i, html) for i, html in indexed))
    return results


def shutdown_pool() -> None:
    """Stop the worker processes. Called on server shutdown; safe to call at any time."""
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

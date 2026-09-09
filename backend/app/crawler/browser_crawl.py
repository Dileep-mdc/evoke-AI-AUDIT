"""Parent-process side of the Scrapy + Playwright crawl.

Launches scrapy_worker.py as a subprocess (Scrapy's reactor can only start once per
process, so it must never run inside the long-lived FastAPI server) and turns its
JSON-lines output back into FetchResult objects the rest of the crawler already knows
how to use.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Callable

from ..config import BASE_DIR, BROWSER_UA, CONCURRENCY
from .http import FetchResult

SECONDS_PER_PAGE_BUDGET = 6.0
MIN_TIMEOUT_SECONDS = 120.0
# Large sites (hundreds of pages) need real headroom here -- at the old 1200s (20 min) cap,
# a subprocess still mid-fetch got killed and every page it hadn't reported back yet was
# silently dropped, capping audit coverage well short of the full site.
MAX_TIMEOUT_SECONDS = 3600.0


def _row_to_fetch_result(row: dict) -> FetchResult:
    html = row.get("html") or ""
    content = html.encode("utf-8", errors="replace")
    headers = row.get("headers") or {}
    return FetchResult(
        url=row["orig_url"],
        final_url=row.get("final_url") or row["orig_url"],
        status_code=row.get("status_code"),
        headers=headers,
        content=content,
        text=html,
        content_type=headers.get("content-type", ""),
        elapsed_ms=row.get("elapsed_ms") or 0,
        error=row.get("error"),
        hops=row.get("hops") or 0,
    )


async def crawl_pages(
    urls: list[str],
    *,
    user_agent: str = BROWSER_UA,
    concurrency: int = CONCURRENCY,
    timeout_s: float | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, dict[str, FetchResult]]:
    """Fetch every URL twice (raw HTTP + Playwright-rendered) in one subprocess run.

    Returns {orig_url: {"raw": FetchResult, "rendered": FetchResult}}. A URL that never
    reported back (crash, killed on timeout) is simply absent -- callers already tolerate
    a missing/failed page the same way a plain fetch failure was tolerated before.
    """
    if not urls:
        return {}

    if timeout_s is None:
        timeout_s = min(MAX_TIMEOUT_SECONDS, max(MIN_TIMEOUT_SECONDS, len(urls) * SECONDS_PER_PAGE_BUDGET))

    tmp_dir = Path(tempfile.mkdtemp(prefix="aiaudit_scrape_"))
    targets_file = tmp_dir / "targets.json"
    out_file = tmp_dir / "pages.jsonl"
    targets_file.write_text(json.dumps(urls), encoding="utf-8")

    # Invoked as `-m app.crawler.scrapy_worker` (not by file path) so the interpreter's
    # sys.path[0] is the backend/ working directory, not app/crawler/ itself -- running it
    # by path would put app/crawler/ first on sys.path, and its own http.py would shadow
    # the stdlib http package that Scrapy's dependencies import internally.
    cmd = [
        sys.executable, "-m", "app.crawler.scrapy_worker",
        "--targets-file", str(targets_file),
        "--out", str(out_file),
        "--concurrency", str(concurrency),
        "--user-agent", user_agent,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(BASE_DIR),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )

    expected = len(urls) * 2
    stop = asyncio.Event()

    async def watch_progress() -> None:
        last = -1
        while not stop.is_set():
            lines = 0
            try:
                if out_file.exists():
                    with out_file.open(encoding="utf-8") as f:
                        lines = sum(1 for _ in f)
            except Exception:
                lines = last if last >= 0 else 0
            if lines != last and on_progress and expected:
                on_progress(min(99, int(lines / expected * 100)))
                last = lines
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    watcher = asyncio.create_task(watch_progress())
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
    finally:
        stop.set()
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass

    results: dict[str, dict[str, FetchResult]] = {}
    if out_file.exists():
        with out_file.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                bucket = results.setdefault(row["orig_url"], {})
                bucket[row["kind"]] = _row_to_fetch_result(row)

    for tmp in (targets_file, out_file):
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        tmp_dir.rmdir()
    except Exception:
        pass

    return results

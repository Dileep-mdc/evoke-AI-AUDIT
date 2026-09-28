"""The optional second copy of a page: the DOM after JavaScript has run.

Division of labour, continuing the one fetch_many.py describes:

  httpx      owns the RESPONSE -- status, headers, redirect hop count, raw bytes. Everything
             a technical parameter scores is a property of the HTTP exchange, and only the
             real HTTP client knows it. A browser reports the page it ended up at, not the
             three redirects it took to get there.
  Playwright owns the DOM -- what a reader (and an AI crawler that executes JavaScript)
             actually sees, once the framework has mounted and fetched its content.

Neither replaces the other, so this module adds a copy rather than substituting one. A page
that is rendered keeps its httpx FetchResult untouched; only the HTML that content extraction
reads is swapped. See parse_page() in discover.py, which takes both.

Why this is not the default for every page: a headless Chromium costs roughly two orders of
magnitude more time and memory per page than an httpx GET, and MAX_PAGES is 2500. Most
marketing sites are server-rendered, where the rendered DOM and the raw HTML carry the same
words and the browser buys nothing at all. discover.py therefore probes one page first and
only spends the render budget on sites where the probe shows it would change the reading.

Every failure here is soft. Playwright not being installed, a browser that will not launch,
a page that never settles -- each leaves the raw HTML in place and the audit scores what it
scored before. A scan must never fail because the optional half of the crawl was unavailable.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable

from ..config import BROWSER_UA, RENDER_CONCURRENCY, RENDER_TIMEOUT
from . import snapshot

log = logging.getLogger("crawler.render")

# Subresources the audit never reads. Images, fonts and media are the bulk of a page's bytes
# and none of them affect the DOM that content extraction walks, so they are refused at the
# network layer: it is the single biggest speed lever in this module.
#
# Stylesheets are deliberately NOT blocked. _in_chrome() and visible_text() in discover.py
# both make decisions that depend on layout-adjacent structure, and some frameworks defer
# content mounting until their stylesheet resolves.
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

# After the DOM is parsed, how long to keep waiting for the network to go quiet. A separate,
# much shorter budget than RENDER_TIMEOUT because it is optional: analytics beacons, chat
# widgets and polling keep a real marketing site's network busy indefinitely, so this wait
# is expected to time out routinely and its expiry is not an error.
_NETWORK_IDLE_MS = 2500


@dataclass
class RenderResult:
    """One rendered page. `html` is empty whenever `error` is set."""
    url: str
    html: str = ""
    final_url: str = ""
    elapsed_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.html) and not self.error


def render_unavailable() -> str | None:
    """Why rendering cannot run, or None if it can.

    Import-only check, deliberately: launching a browser to find out costs seconds, and this
    is called before a scan decides whether to budget for rendering at all. A browser that
    imports but will not launch is caught later by render_many(), which degrades the same way.
    """
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return "playwright is not installed (pip install playwright && playwright install chromium)"
    return None


async def render_many(
    urls: list[str],
    *,
    user_agent: str = BROWSER_UA,
    concurrency: int = RENDER_CONCURRENCY,
    timeout: float = RENDER_TIMEOUT,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, RenderResult]:
    """Render each URL in a headless browser and return its post-JavaScript HTML.

    Keyed by the requested URL, matching crawl_raw()'s contract, so a caller can line the
    rendered copy up against the raw one without tracking redirects itself. Every requested
    URL appears in the result; ones that could not be rendered carry an `error` and no HTML.

    One browser process is shared by the whole batch and each page gets its own context, so
    pages cannot leak cookies or storage into one another the way shared-context tabs would.
    """
    if not urls:
        return {}

    results: dict[str, RenderResult] = {u: RenderResult(url=u, error="not rendered") for u in urls}
    # A scan reading a saved copy of the site gets the browser's HTML from disk, as saved by
    # snapshot_site.py; only pages the copy lacks would need a live browser.
    live = []
    for u in urls:
        snap = snapshot.load(u)
        html = snap.rendered(u) if snap else None
        if html:
            results[u] = RenderResult(url=u, html=html, final_url=u)
        else:
            live.append(u)
    if not live:
        if on_progress:
            on_progress(100)
        return results
    urls = live
    unavailable = render_unavailable()
    if unavailable:
        for r in results.values():
            r.error = unavailable
        log.info("skipping render pass for %d urls: %s", len(urls), unavailable)
        return results

    from playwright.async_api import async_playwright

    done = 0
    total = len(urls)

    async def _block_subresources(route) -> None:
        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()

    try:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(
                    headless=True,
                    # --disable-dev-shm-usage: Chromium's default shared-memory location is
                    # small in containers, and exhausting it crashes the tab rather than
                    # failing the request, which would lose the page with no error to report.
                    args=["--disable-dev-shm-usage", "--disable-gpu"],
                )
            except Exception as exc:
                reason = f"could not launch a browser: {type(exc).__name__}: {exc}"[:300]
                for r in results.values():
                    r.error = reason
                log.warning("render pass unavailable -- %s", reason)
                return results

            semaphore = asyncio.Semaphore(max(1, concurrency))

            async def _one(url: str) -> None:
                nonlocal done
                started = time.perf_counter()
                async with semaphore:
                    context = None
                    try:
                        context = await browser.new_context(
                            user_agent=user_agent,
                            ignore_https_errors=True,
                        )
                        page = await context.new_page()
                        await page.route("**/*", _block_subresources)
                        response = await page.goto(
                            url, wait_until="domcontentloaded", timeout=timeout * 1000
                        )
                        try:
                            # Best-effort settle for content mounted after DOMContentLoaded.
                            # Expected to expire on sites with persistent network chatter,
                            # which is why its failure is swallowed rather than reported.
                            await page.wait_for_load_state("networkidle", timeout=_NETWORK_IDLE_MS)
                        except Exception:
                            pass
                        html = await page.content()
                        results[url] = RenderResult(
                            url=url,
                            html=html,
                            final_url=(response.url if response else url),
                            elapsed_ms=int((time.perf_counter() - started) * 1000),
                        )
                    except Exception as exc:
                        results[url] = RenderResult(
                            url=url,
                            elapsed_ms=int((time.perf_counter() - started) * 1000),
                            error=f"{type(exc).__name__}: {exc}"[:300] or "render failed",
                        )
                    finally:
                        if context is not None:
                            try:
                                await context.close()
                            except Exception:
                                pass
                done += 1
                if on_progress:
                    on_progress(min(100, int(done / total * 100)))

            await asyncio.gather(*(_one(u) for u in urls))
            try:
                await browser.close()
            except Exception:
                pass
    except Exception as exc:
        # The playwright context manager itself failed (driver missing, host teardown). Pages
        # already rendered keep their HTML; the rest carry the reason.
        reason = f"render pass failed: {type(exc).__name__}: {exc}"[:300]
        for r in results.values():
            if not r.ok and r.error == "not rendered":
                r.error = reason
        log.warning("%s", reason)

    rendered = sum(1 for r in results.values() if r.ok)
    log.info("render pass: %d of %d urls rendered", rendered, total)
    return results

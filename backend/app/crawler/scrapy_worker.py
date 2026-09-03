"""Standalone subprocess entry point that crawls a list of URLs with Scrapy + Playwright.

Run in its own OS process (never imported into the FastAPI app) because Scrapy installs a
Twisted reactor that can only be started once per process -- the server handles many scans
over its lifetime, so Scrapy cannot live inside it. Each invocation crawls one batch of URLs
and exits.

For every target URL this yields TWO records: a "raw" fetch (plain HTTP, no JS) and a
"rendered" fetch (Playwright, JS executed). Downstream code uses the raw copy for anything
about the wire response itself (redirect hops, byte size, HTTP-level HTML weight) and the
rendered copy for anything about what the page actually shows (visible text, headings,
structured data) -- so JS-injected content is no longer invisible to the audit.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

if sys.platform == "win32":
    import asyncio
    # Twisted's asyncio reactor needs the selector event loop; Windows defaults to the
    # Proactor loop, which doesn't implement add_reader/add_writer and breaks the reactor
    # at import time if this isn't set first.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import scrapy
from scrapy.crawler import CrawlerProcess


class PageSpider(scrapy.Spider):
    name = "page_spider"

    def __init__(self, targets_file: str, out_file: str, **kw):
        super().__init__(**kw)
        with open(targets_file, encoding="utf-8") as f:
            self.targets = json.load(f)
        self.out_file = out_file
        self._out = open(out_file, "w", encoding="utf-8")

    async def start(self):
        # Scrapy 2.13+ replaced the old sync start_requests() with this async
        # generator; the base Spider.start_requests() no longer exists at all,
        # so a start_requests() override here would be silently never called.
        for url in self.targets:
            yield scrapy.Request(
                url, callback=self.parse_page, errback=self.on_error,
                meta={"orig_url": url, "kind": "raw", "started": time.perf_counter()},
                dont_filter=True,
            )
            yield scrapy.Request(
                url, callback=self.parse_page, errback=self.on_error,
                meta={
                    "orig_url": url, "kind": "rendered", "started": time.perf_counter(),
                    "playwright": True, "playwright_include_page": False,
                },
                dont_filter=True,
            )

    def parse_page(self, response):
        meta = response.meta
        row = {
            "orig_url": meta["orig_url"],
            "kind": meta["kind"],
            "final_url": response.url,
            "status_code": response.status,
            "headers": {
                (k.decode() if isinstance(k, bytes) else k): (v[0].decode() if isinstance(v[0], bytes) else v[0])
                for k, v in response.headers.items() if v
            },
            "html": response.text,
            "elapsed_ms": int((time.perf_counter() - meta["started"]) * 1000),
            "hops": int(response.request.meta.get("redirect_times") or 0),
            "error": None,
        }
        self._write(row)

    def on_error(self, failure):
        meta = failure.request.meta
        row = {
            "orig_url": meta["orig_url"],
            "kind": meta["kind"],
            "final_url": meta["orig_url"],
            "status_code": None,
            "headers": {},
            "html": "",
            "elapsed_ms": int((time.perf_counter() - meta["started"]) * 1000),
            "hops": 0,
            "error": failure.getErrorMessage()[:300],
        }
        self._write(row)

    def _write(self, row: dict) -> None:
        self._out.write(json.dumps(row) + "\n")
        self._out.flush()

    def closed(self, reason):
        self._out.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets-file", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--user-agent", default="")
    args = parser.parse_args()

    # These must live on the CrawlerProcess's own settings object, not the spider's
    # custom_settings -- CrawlerProcess installs the Twisted reactor (and therefore
    # decides whether scrapy-playwright can use it directly) from these settings at
    # __init__ time, before a spider is even created, so anything set later is too late.
    settings = {
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "DOWNLOAD_HANDLERS": {
            "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
            "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        },
        "PLAYWRIGHT_BROWSER_TYPE": "chromium",
        "PLAYWRIGHT_LAUNCH_OPTIONS": {"headless": True},
        "PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT": 30000,
        "ROBOTSTXT_OBEY": False,
        "REDIRECT_ENABLED": True,
        "RETRY_TIMES": 1,
        "LOG_LEVEL": "ERROR",
        "COOKIES_ENABLED": False,
        "DOWNLOAD_TIMEOUT": 30,
        "AJAXCRAWL_ENABLED": False,
        "CONCURRENT_REQUESTS": args.concurrency,
    }
    if args.user_agent:
        settings["USER_AGENT"] = args.user_agent

    process = CrawlerProcess(settings=settings)
    process.crawl(PageSpider, targets_file=args.targets_file, out_file=args.out)
    process.start()


if __name__ == "__main__":
    main()

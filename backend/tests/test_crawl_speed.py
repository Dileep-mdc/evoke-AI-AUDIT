"""The crawl's cost, and the reasons a crawl used to look like it had hung.

A scan of a real site sat on "crawling 16%" for as long as anyone was willing to watch it.
Three separate things were true at once, and each is pinned down here:

  * The host resolved to four CDN addresses and only ONE of them would complete a TLS
    handshake from the auditing machine. Python connects to whichever address the resolver
    returns first and does not fail over after a handshake stalls, so three pages in four
    spent the full connect budget achieving nothing -- test_resolver_*.
  * Every fetch built a new HTTP client, so every page paid a fresh TCP connection and TLS
    negotiation to a host that would have served the whole crawl over a handful of kept-alive
    connections -- test_the_http_client_is_pooled.
  * Sixteen percent was the number the bar sat on while page fetching, parsing and three
    rounds of link discovery all ran without reporting anything -- test_progress_*.
"""
from __future__ import annotations

import asyncio
import socket
import time

import pytest

from app.crawler import extract, resolver
from app.crawler.discover import _band, parse_page
from app.crawler.http import FetchResult, client, looks_like_document


def _run(coro):
    return asyncio.run(coro)


def _html(body: str) -> FetchResult:
    raw = f"<html><head><title>T</title></head><body>{body}</body></html>".encode()
    return FetchResult(
        url="https://x/p", final_url="https://x/p", status_code=200,
        headers={"content-type": "text/html"}, content=raw, text=raw.decode(),
        content_type="text/html", elapsed_ms=1,
    )


# --- what is worth fetching at all ---------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://x/brochure.pdf", "https://x/logo.PNG", "https://x/deck.pptx",
    "https://x/video.mp4", "https://x/bundle.js", "https://x/styles.css",
])
def test_binary_and_asset_links_are_not_crawled_as_pages(url):
    """These carry no HTML, so fetching one spends a request, up to 2MB of transfer and a
    parse to learn what the extension already said."""
    assert looks_like_document(url) is False


@pytest.mark.parametrize("url", [
    "https://x/", "https://x/services/", "https://x/report?format=pdf",
    "https://x/blog/why-pdf-matters", "https://x/case-study",
])
def test_pages_are_still_crawled(url):
    """Only the path's extension is consulted: a page that SERVES a PDF is still a page, and
    a URL that merely mentions one in a slug is not an asset."""
    assert looks_like_document(url) is True


# --- connection reuse ----------------------------------------------------------------------

def test_the_http_client_is_pooled():
    """One client per user agent per loop, not one per request. A fresh client per page meant
    a TLS handshake per page: the largest single cost in a crawl of any size."""
    async def main():
        first = client(user_agent="a")
        second = client(user_agent="a")
        other = client(user_agent="b")
        assert first is second
        assert other is not first
        assert first.timeout.connect is not None, "connect has its own budget, not the read one"
        await __import__("app.crawler.http", fromlist=["aclose_clients"]).aclose_clients()

    _run(main())


# --- address selection ---------------------------------------------------------------------

def _addrinfo(*addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in addresses]


def _fake_dns(monkeypatch, *addresses):
    """Answer every lookup with these addresses, for the probe and for the installed hook.

    Both are needed: the probe reaches the resolver through socket.getaddrinfo, while the hook
    the module installs calls the reference it captured at import -- capturing it is what stops
    the hook recursing into itself.
    """
    fake = lambda *a, **k: _addrinfo(*addresses)
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    monkeypatch.setattr(resolver, "_original_getaddrinfo", fake)


def test_resolver_pins_the_host_to_addresses_that_answer(monkeypatch):
    """The measured failure: four addresses, one usable. Every other address must be dropped
    for that host, or three pages in four hang until the connect timeout expires."""
    _fake_dns(monkeypatch, "1.1.1.1", "2.2.2.2")

    async def only_the_second(address, host, port, timeout):
        return address == "2.2.2.2"

    monkeypatch.setattr(resolver, "_handshake_ok", only_the_second)
    try:
        summary = _run(resolver.prefer_reachable_addresses("https://site.example/"))
        assert summary == {"host": "site.example", "checked": True, "total": 2, "reachable": 1}
        # Both spellings the resolver is called with: anyio encodes the host to bytes before
        # it reaches socket.getaddrinfo, and a key that misses makes this module a no-op.
        for host in ("site.example", b"site.example", "SITE.EXAMPLE."):
            got = {info[4][0] for info in socket.getaddrinfo(host, 443)}
            assert got == {"2.2.2.2"}, host
    finally:
        resolver.restore()


def test_an_unmeasured_host_resolves_exactly_as_it_would_have(monkeypatch):
    """The hook is process-wide, so it must be invisible to everything it has not measured --
    the OpenAI client included."""
    _fake_dns(monkeypatch, "9.9.9.9", "8.8.8.8")

    async def only_the_first(address, host, port, timeout):
        return address == "9.9.9.9"

    monkeypatch.setattr(resolver, "_handshake_ok", only_the_first)
    try:
        _run(resolver.prefer_reachable_addresses("https://site.example/"))
        elsewhere = {info[4][0] for info in socket.getaddrinfo("api.openai.com", 443)}
        assert elsewhere == {"9.9.9.9", "8.8.8.8"}
    finally:
        resolver.restore()


def test_a_host_where_nothing_answers_is_left_alone(monkeypatch):
    """If no address completes a handshake the problem is not which address to use, and
    filtering the answer down to nothing would turn a reachability problem into a DNS one."""
    _fake_dns(monkeypatch, "1.1.1.1", "2.2.2.2")

    async def nothing_answers(address, host, port, timeout):
        return False

    monkeypatch.setattr(resolver, "_handshake_ok", nothing_answers)
    try:
        summary = _run(resolver.prefer_reachable_addresses("https://site.example/"))
        assert summary["reachable"] == 0
        assert {info[4][0] for info in socket.getaddrinfo("site.example", 443)} == {"1.1.1.1", "2.2.2.2"}
    finally:
        resolver.restore()


# --- extraction ------------------------------------------------------------------------------

def test_extraction_results_line_up_with_their_documents():
    """extract_many returns by position, and a page that was never HTML extracts to None
    without being sent anywhere."""
    docs = ["<html><body><article>" + "word " * 80 + "</article></body></html>", "", "<html></html>"]
    results = _run(extract.extract_many(docs))
    assert len(results) == 3
    assert results[0] and "word" in results[0]
    assert results[1] is None


def test_a_batch_uses_its_supplied_extraction_instead_of_running_its_own(monkeypatch):
    """The batch path extracts in worker processes and hands the text to parse_page. If
    parse_page re-extracted anyway, the pool would be pure overhead."""
    def must_not_run(html):
        raise AssertionError("parse_page re-extracted text the caller had already supplied")

    monkeypatch.setattr("app.crawler.discover.extract_text_sync", must_not_run)
    page = parse_page(_html("<p>ignored body copy</p>"), None, "supplied body text")
    assert page.text == "supplied body text"
    assert page.word_count == 3


def test_a_single_page_caller_still_gets_its_text_extracted():
    """Omitting the argument means "extract it here", which is what every non-batch caller
    (the render probe, the tests, a one-page re-read) relies on."""
    page = parse_page(_html("<article><p>" + "meaningful sentence here. " * 20 + "</p></article>"))
    assert page.word_count > 20


def test_a_broken_extraction_pool_falls_back_instead_of_failing_the_crawl(monkeypatch):
    """Extraction is not optional to the audit, so a pool that cannot start or dies mid-batch
    may cost speed and nothing else."""
    monkeypatch.setattr(extract, "_pool_disabled", False)
    monkeypatch.setattr(extract, "_pool", None)
    monkeypatch.setattr(extract, "EXTRACT_POOL_MIN_BATCH", 1)

    def no_pool(*a, **k):
        raise OSError("no worker processes here")

    monkeypatch.setattr(extract, "ProcessPoolExecutor", no_pool)
    docs = ["<html><body><article>" + "word " * 80 + "</article></body></html>"]
    results = _run(extract.extract_many(docs))
    assert results[0] and "word" in results[0]


# --- progress ----------------------------------------------------------------------------------

def test_progress_bands_cover_their_phase_and_stay_inside_it():
    """Every phase reports 0-100 for itself; this is the only place that decides where that
    lands on the scan's bar. Phases that overrun their band would report a scan is further
    along than it is."""
    seen: list[int] = []
    report = _band(10, 18, seen.append)
    for pct in (0, 50, 100, 140, -5):
        report(pct)
    assert seen == [10, 14, 18, 18, 10]


def test_the_crawl_reports_every_phase_between_the_fetch_and_the_render():
    """The stall this whole file exists for: fetching, parsing and link discovery all used to
    land on a single number. Each now owns a distinct, increasing slice of the bar."""
    import inspect

    from app.crawler import discover

    source = inspect.getsource(discover.crawl_site)
    bands = [line for line in source.splitlines() if "_band(" in line]
    assert len(bands) >= 4, "fetch, parse, discovery and render each report separately"


def test_a_crawl_past_its_deadline_stops_and_says_so():
    """Whatever the site does, the crawl ends -- and the URLs it never reached still carry a
    reason, so a partial crawl is visible as one rather than as a smaller site."""
    from app.crawler.fetch_many import crawl_raw

    urls = ["https://x/1", "https://x/2"]
    results = _run(crawl_raw(urls, deadline=time.monotonic() - 1))
    assert set(results) == set(urls)
    assert all("time budget" in (r.error or "") for r in results.values())

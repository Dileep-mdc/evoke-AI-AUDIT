"""What counts as page content, and which pages count at all.

Three defects sat underneath most of the on-page findings and were reported as findings about
the audited site rather than about this tool:

  * The fallback text extractor stripped nothing, so menus and footers were page copy. That
    inflated word_count, put nav labels in ON-04's "opening", and made ON-03 quote the
    mega-menu as the site's definition of a term.
  * Headings were read from the whole document, so one site's 3,863 "headings" were mostly
    the same handful of menu labels repeated once per page.
  * classify_page decided page type from body text on a first-match-wins scan, and page type
    is the sole basis for "relevant pages" across roughly fifteen parameters.

Plus the crawl contract: crawl_raw returned 750 results for 1,095 requested URLs and the
missing 345 surfaced only as "no response from scraper".
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bs4 import BeautifulSoup

from app.crawler.discover import classify_page, parse_page, visible_text
from app.parameters.common import excluded_page_counts, scorable_pages


def _run(coro):
    return asyncio.run(coro)


def _fetch_result(url="https://x/p", html="", *, ok=True, status=200, error=None):
    return SimpleNamespace(
        url=url, final_url=url, text=html, status_code=status, error=error, ok=ok,
        hops=0, elapsed_ms=10, content=b"", headers={}, content_type="text/html",
    )


# --- the fallback extractor ---------------------------------------------------------------

CHROME_PAGE = """
<html><body>
  <header><a href="/">Acme</a><h2>The Acme Edge</h2></header>
  <nav><ul><li>AI &amp; Automation</li><li>Data &amp; Analytics</li><li>Careers</li></ul>
       <h3>Partners</h3></nav>
  <main><h1>Managed data platform</h1>
        <p>We run the pipeline your finance team reports from.</p></main>
  <footer><h4>Privacy policy</h4><p>Copyright Acme 2026</p></footer>
</body></html>
"""


def test_the_fallback_extractor_drops_page_furniture():
    soup = BeautifulSoup(CHROME_PAGE, "html.parser")

    text = visible_text(soup, CHROME_PAGE)

    assert "pipeline your finance team" in text, "real body copy survives"
    assert "Careers" not in text and "Partners" not in text, "nav text is not page copy"
    assert "Copyright Acme" not in text, "footer text is not page copy"


def test_word_count_no_longer_counts_the_menu():
    """word_count drives ON-21's thin-page test and TECH's depth checks, so nav text landing
    in it made thin pages read as substantial."""
    soup = BeautifulSoup(CHROME_PAGE, "html.parser")

    assert len(visible_text(soup, CHROME_PAGE).split()) < len(soup.get_text(" ", strip=True).split())


def test_headings_exclude_the_mega_menu():
    page = parse_page(_fetch_result(html=CHROME_PAGE))

    texts = [h["text"] for h in page.headings]
    assert "Managed data platform" in texts, "content headings are kept"
    assert "The Acme Edge" not in texts, "a header heading is site furniture"
    assert "Partners" not in texts, "a nav heading is site furniture"
    assert "Privacy policy" not in texts, "a footer heading is site furniture"


def test_the_extractor_does_not_mutate_the_soup_the_crawler_still_needs():
    """Nav links feed the crawl frontier, so stripping them in place to fix extraction would
    break discovery. The clean-up has to happen on a copy."""
    page = parse_page(_fetch_result(html=CHROME_PAGE))

    assert page.soup.find("nav") is not None, "nav survives in the tree"
    assert any(l["href"].endswith("/") for l in page.links), "nav links still reach the frontier"


# --- page classification -------------------------------------------------------------------

def test_body_text_no_longer_decides_page_type():
    """A service page whose opening paragraph mentioned "news" or "about" was filed as an
    article or an about page before the service rule was ever reached."""
    body = "Read the latest news about our company and insights from the blog. " * 4

    assert classify_page("https://x/services/data-platform", "Data platform", body) == "service"
    assert classify_page("https://x/about", "About us", body) == "about"


def test_substring_matching_no_longer_misfiles_pages():
    """"/showcase/" matched "case" and became a case study."""
    assert classify_page("https://x/showcase/gallery", "Showcase", "") != "case_study"
    assert classify_page("https://x/case-studies/acme", "Acme", "") == "case_study"
    # Unhyphenated paths are common too, and a /service... slug after them must not win.
    assert classify_page("https://x/casestudies/service-bidding-platform", "Acme", "") == "case_study"
    assert classify_page("https://x/taxcasestudies/qe/", "QE", "") == "case_study"


def test_the_homepage_is_still_the_homepage():
    assert classify_page("https://x/", "Acme", "services and solutions") == "home"
    assert classify_page("https://x", "Acme", "") == "home"


def test_the_title_is_a_fallback_when_the_path_says_nothing():
    assert classify_page("https://x/p/12345", "Careers at Acme", "") == "utility"
    assert classify_page("https://x/p/12345", "Nothing in particular", "") == "other"


# --- which pages may be scored ---------------------------------------------------------------

def _page(url, *, ok=True, blocked=""):
    return SimpleNamespace(
        url=url, result=_fetch_result(url, ok=ok), blocked_reason=blocked,
        text="copy", word_count=2, page_type="service", headings=[], links=[],
        images=[], schema_blocks=[], dates={}, title="t", soup=None,
        meta_description="", canonical="",
    )


def test_failed_and_bot_blocked_pages_are_excluded_from_scoring():
    """A fetch failure used to DEFLATE ON-21 (a zero-word page is "thin content") and INFLATE
    TECH-07 (filtering the timed-out pages left only the fast ones in the average). A
    Cloudflare interstitial was scored as the site's copy on every on-page parameter."""
    ctx = SimpleNamespace(pages=[
        _page("https://x/good"),
        _page("https://x/dead", ok=False),
        _page("https://x/challenge", blocked="Blocked by the site's bot-detection page."),
    ])

    kept = scorable_pages(ctx)

    assert [p.url for p in kept] == ["https://x/good"]
    assert excluded_page_counts(ctx) == {
        "pages_crawled": 3, "pages_excluded_fetch_failed": 1, "pages_excluded_bot_blocked": 1,
    }


def test_scorable_pages_is_safe_on_an_empty_crawl():
    assert scorable_pages(SimpleNamespace(pages=[])) == []
    assert scorable_pages(SimpleNamespace(pages=None)) == []


# --- the crawl contract ------------------------------------------------------------------

def test_crawl_raw_returns_one_result_per_requested_url(monkeypatch):
    """The mapping this function returns is what every downstream denominator is built from,
    so it may not have holes: one entry per requested URL, keyed by the URL as requested."""
    import app.crawler.fetch_many as fm

    urls = [f"https://x/{i}" for i in range(6)]
    fetched: list[str] = []

    async def fake_fetch(url, *, user_agent=""):
        fetched.append(url)
        return _fetch_result(url)

    monkeypatch.setattr(fm, "fetch", fake_fetch)

    results = _run(fm.crawl_raw(urls))

    assert set(results) == set(urls), "every requested URL has an entry"
    assert sorted(fetched) == sorted(urls), "every requested URL was fetched exactly once"
    assert all(r.ok for r in results.values())


def test_a_url_the_crawler_cannot_fetch_still_gets_an_explicit_reason(monkeypatch):
    """Missing must never mean absent. A URL that cannot be fetched at all comes back as a
    failed FetchResult carrying why, so it lands in the failures file rather than vanishing."""
    import app.crawler.fetch_many as fm

    async def boom(url, *, user_agent=""):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(fm, "fetch", boom)

    results = _run(fm.crawl_raw(["https://x/one"]))

    assert set(results) == {"https://x/one"}
    assert "connection reset" in results["https://x/one"].error


def test_the_crawl_stops_at_its_time_budget_and_says_so(monkeypatch):
    """A site that never stops answering (or never answers at all) must not hang a scan. Past
    the deadline the remaining URLs are not fetched, but each still reports why."""
    import time

    import app.crawler.fetch_many as fm

    async def fake_fetch(url, *, user_agent=""):
        return _fetch_result(url)

    monkeypatch.setattr(fm, "fetch", fake_fetch)

    urls = [f"https://x/{i}" for i in range(4)]
    results = _run(fm.crawl_raw(urls, deadline=time.monotonic() - 1))

    assert set(results) == set(urls)
    assert all("time budget" in (r.error or "") for r in results.values())


def test_service_titles_reduce_to_the_concept_a_page_would_actually_say():
    """Whole SEO titles were searched for verbatim in page copy and almost never found."""
    from app.parameters.common import core_concept
    assert core_concept("Best AI Advisory and Consulting Services for Enterprises | Acme") == "ai advisory and consulting"
    assert core_concept("Top Enterprise AI Agent Solutions for Business") == "enterprise ai agent"
    assert core_concept("Leading Enterprise AI Solutions Provider") == "enterprise ai"
    assert core_concept("Data Engineering Services - Acme") == "data engineering"
    assert core_concept("Microservices QA, streamlined and predictable") == "microservices qa"

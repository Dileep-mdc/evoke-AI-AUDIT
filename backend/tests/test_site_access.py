"""Two crawl behaviours a scan's page count depends on.

1. A site that refuses the audit (tcs.com answers every request with 403) is reported as
   refused, not as a one-page website: the crawl stops early, nothing is opened in a browser,
   the on-site checks say why they could not run, and the scan does not retry them against
   the same refusal.

2. On a client-rendered site, the links that exist only after JavaScript has run are
   followed. Link discovery reads the server's HTML, which on such a site carries no
   navigation; without following the rendered pages' links the crawl stopped at the homepage.

Nothing here touches the network or launches a browser: every fetch, the sitemap and robots
readers, and render_many() are substituted in the discover module.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.crawler import discover
from app.crawler.discover import crawl_site, parse_page, site_access
from app.crawler.http import FetchResult
from app.crawler.render import RenderResult
from app.crawler.robots import RobotsData
from app.parameters import engine

ORIGIN = "https://site.test"


def _result(url: str, status: int = 200, html: str = "") -> FetchResult:
    body = html.encode()
    return FetchResult(
        url=url, final_url=url, status_code=status, headers={}, content=body, text=html,
        content_type="text/html" if html else "", elapsed_ms=5,
    )


def _page(url: str, status: int = 200, html: str = "<html><body><p>hello</p></body></html>"):
    return parse_page(_result(url, status, html))


DENIED_HTML = "<html><head><title>Access Denied</title></head><body>You don't have permission.</body></html>"
SHELL_HTML = '<html><head><title>App</title></head><body><div id="root"></div></body></html>'


def _content(title: str, links: list[str]) -> str:
    anchors = "".join(f'<a href="{href}">{href}</a>' for href in links)
    words = " ".join(f"{title.lower()} detail {i}" for i in range(80))
    return (f"<html><head><title>{title}</title></head><body><div id=\"root\"><nav>{anchors}</nav>"
            f"<main><h1>{title}</h1><p>{words}</p></main></div></body></html>")


def _stub_site(monkeypatch, raw: dict[str, tuple[int, str]], rendered: dict[str, str]) -> list[list[str]]:
    """Serve `raw` over "HTTP" and `rendered` from the "browser". Returns the render calls."""
    def fake_result(url: str) -> FetchResult:
        status, html = raw.get(url, (404, ""))
        return _result(url, status, html)

    async def fake_fetch(url, **_kw):
        return fake_result(url)

    async def fake_crawl_raw(urls, **_kw):
        return {u: fake_result(u) for u in urls}

    async def fake_robots(origin):
        url = origin + "/robots.txt"
        return RobotsData(url=url, result=fake_result(url), raw="", parser=None, sitemap_urls=[])

    async def fake_sitemaps(origin, discovered, prefer=""):
        url = origin + "/sitemap.xml"
        return {"candidates": [url], "sources": [{"url": url, "status": fake_result(url).status_code, "error": None}],
                "urls": [], "errors": [], "count": 0}

    async def fake_reach(_url):
        return {}

    render_calls: list[list[str]] = []

    async def fake_render_many(urls, **_kw):
        render_calls.append(list(urls))
        return {u: (RenderResult(url=u, html=rendered[u], final_url=u) if u in rendered
                    else RenderResult(url=u, error="render failed")) for u in urls}

    monkeypatch.setattr(discover, "fetch", fake_fetch)
    monkeypatch.setattr(discover, "crawl_raw", fake_crawl_raw)
    monkeypatch.setattr(discover, "fetch_robots", fake_robots)
    monkeypatch.setattr(discover, "fetch_sitemaps", fake_sitemaps)
    monkeypatch.setattr(discover, "prefer_reachable_addresses", fake_reach)
    monkeypatch.setattr(discover, "render_many", fake_render_many)
    monkeypatch.setattr(discover, "render_unavailable", lambda: None)
    monkeypatch.setattr(discover, "ENABLE_RENDER", True)
    return render_calls


# --- site_access(): what the report says about a site's response to the audit ------------------

def test_a_site_that_refuses_everything_is_reported_as_refused():
    home = _result(ORIGIN + "/", 403, DENIED_HTML)
    access = site_access(
        "site.test",
        homepage=home,
        robots=_result(ORIGIN + "/robots.txt", 403),
        sitemap={"sources": [{"status": 403}, {"status": 403}]},
        llms=_result(ORIGIN + "/llms.txt", 403),
        pages=[parse_page(home)],
    )
    assert access["blocked"] is True
    assert access["homepage_status"] == 403
    assert access["refused"] == ["the homepage", "robots.txt", "the sitemap", "llms.txt"]
    assert access["pages_readable"] == 0
    assert "HTTP 403" in access["reason"]
    assert "AIVisibilityAuditBot/1.0" in access["reason"]


def test_a_bot_check_homepage_is_reported_as_refused():
    challenge = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
    home = _result(ORIGIN + "/", 200, challenge)
    access = site_access("site.test", homepage=home, robots=None, sitemap={}, llms=None, pages=[parse_page(home)])
    assert access["blocked"] is True
    assert "bot-check page" in access["reason"]


def test_a_site_that_refuses_some_pages_is_not_blocked_but_says_how_many():
    pages = [_page(ORIGIN + "/"), _page(ORIGIN + "/a"), _page(ORIGIN + "/b", 403, DENIED_HTML)]
    access = site_access("site.test", homepage=pages[0].result, robots=None, sitemap={}, llms=None, pages=pages)
    assert access["blocked"] is False
    assert access["reason"] == ""
    assert (access["pages_fetched"], access["pages_readable"], access["pages_refused"]) == (3, 2, 1)


def test_a_site_that_is_down_is_not_called_refused():
    """A 503 or a connection failure is an outage, not the site turning the audit away."""
    home = _result(ORIGIN + "/", 503, "")
    access = site_access("site.test", homepage=home, robots=None, sitemap={}, llms=None, pages=[parse_page(home)])
    assert access["blocked"] is False


# --- crawl_site() on a refused site ----------------------------------------------------------

def test_a_refused_crawl_stops_early_and_opens_no_browser(monkeypatch):
    raw = {
        ORIGIN + "/": (403, DENIED_HTML + '<a href="/about">About</a>'),
        ORIGIN + "/robots.txt": (403, DENIED_HTML),
        ORIGIN + "/sitemap.xml": (403, DENIED_HTML),
        ORIGIN + "/llms.txt": (403, DENIED_HTML),
        ORIGIN + "/about": (403, DENIED_HTML),
    }
    render_calls = _stub_site(monkeypatch, raw, rendered={ORIGIN + "/": _content("Home", [])})
    ctx = asyncio.run(crawl_site(ORIGIN + "/"))
    assert ctx.access["blocked"] is True
    assert render_calls == [], "a refused homepage must not be reopened in a browser"
    assert ctx.render["decision"] == "skipped"
    assert [p.url for p in ctx.pages] == [ORIGIN + "/"], "links on the refusal page are not followed"


def test_on_site_checks_on_a_refused_site_say_why_and_are_not_retried():
    from app.api.scans import _worth_retrying

    home = parse_page(_result(ORIGIN + "/", 403, DENIED_HTML))
    access = {"blocked": True, "homepage_status": 403, "reason": "site.test refused automated access: ..."}
    ctx = SimpleNamespace(pages=[home], origin=ORIGIN, access=access)
    spec = next(s for s in engine.load_registry() if s["section"] == "on_page")
    row = engine._skip(spec, ctx)
    assert row["status"] == "UNKNOWN"
    assert row["error"] == access["reason"]
    assert row["evidence"]["site_refused_access"] is True
    assert _worth_retrying(row) is False


def test_a_crawl_that_read_nothing_is_not_retried_either():
    """The retry re-reads the same crawl, so a check skipped for having no readable page can
    only come back the same way."""
    from app.api.scans import _worth_retrying

    ctx = SimpleNamespace(pages=[], origin=ORIGIN, access={})
    spec = next(s for s in engine.load_registry() if s["section"] == "on_page")
    assert _worth_retrying(engine._skip(spec, ctx)) is False


# --- crawl_site() on a client-rendered site -----------------------------------------------------

def test_links_that_exist_only_after_javascript_are_followed(monkeypatch):
    """Home -> /services -> /contact, where every page's server HTML is an empty shell and the
    links appear only in the browser's copy. All three pages must be crawled and rendered."""
    raw = {
        ORIGIN + "/": (200, SHELL_HTML),
        ORIGIN + "/services": (200, SHELL_HTML),
        ORIGIN + "/contact": (200, SHELL_HTML),
    }
    rendered = {
        ORIGIN + "/": _content("Home", ["/services"]),
        ORIGIN + "/services": _content("Services", ["/contact"]),
        ORIGIN + "/contact": _content("Contact", ["/"]),
    }
    _stub_site(monkeypatch, raw, rendered)
    ctx = asyncio.run(crawl_site(ORIGIN + "/"))
    by_url = {p.url: p for p in ctx.pages}
    assert set(by_url) == {ORIGIN + "/", ORIGIN + "/services", ORIGIN + "/contact"}
    assert all(p.render.get("content_source") == "rendered" for p in ctx.pages)
    assert ctx.render["pages_found_after_render"] == 2
    assert ctx.access["blocked"] is False
    assert ctx.access["pages_readable"] == 3


def test_a_server_rendered_site_renders_one_page_and_finds_links_as_before(monkeypatch):
    """The follow-up must cost nothing where the server's HTML already carries the links."""
    raw = {
        ORIGIN + "/": (200, _content("Home", ["/services"])),
        ORIGIN + "/services": (200, _content("Services", ["/"])),
    }
    rendered = {u: html for u, (_s, html) in raw.items()}
    render_calls = _stub_site(monkeypatch, raw, rendered)
    ctx = asyncio.run(crawl_site(ORIGIN + "/"))
    assert {p.url for p in ctx.pages} == {ORIGIN + "/", ORIGIN + "/services"}
    assert render_calls == [[ORIGIN + "/"]], "only the homepage probe is rendered"
    assert ctx.render["decision"] == "not_needed"
    assert "pages_found_after_render" not in ctx.render


def test_the_render_budget_caps_the_follow_up(monkeypatch):
    """A long chain of client-rendered pages stops when the render budget is spent."""
    chain = [ORIGIN + "/"] + [f"{ORIGIN}/p{i}" for i in range(1, 10)]
    raw = {u: (200, SHELL_HTML) for u in chain}
    rendered = {u: _content(f"Page {i}", [chain[i + 1]] if i + 1 < len(chain) else [])
                for i, u in enumerate(chain)}
    render_calls = _stub_site(monkeypatch, raw, rendered)
    monkeypatch.setattr(discover, "RENDER_BUDGET", 4)
    ctx = asyncio.run(crawl_site(ORIGIN + "/"))
    assert sum(len(c) for c in render_calls) == 4
    assert ctx.render["attempted"] == 4
    assert sum(1 for p in ctx.pages if p.render.get("content_source") == "rendered") == 4


@pytest.fixture(autouse=True)
def _no_extraction_pool(monkeypatch):
    """Extract in-process: these crawls are a handful of pages."""
    import app.crawler.extract as extract
    monkeypatch.setattr(extract, "_pool_for", lambda _n: None)

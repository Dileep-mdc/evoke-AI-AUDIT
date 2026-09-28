"""The hybrid crawl: one HTTP response, optionally two copies of the document.

The split these tests pin down is the whole point of the design, and it is easy to break by
accident in either direction:

  * A rendered page must keep the httpx FetchResult. Every technical parameter that scores the
    HTTP exchange -- redirect hops (TECH-06, TECH-20), user-agent-conditional body size
    (TECH-02), status and timing (TECH-07) -- reads `page.result`, and a browser does not know
    those facts. If rendering ever overwrote them, those checks would start scoring the
    browser's view of the network and nobody would see it happen.
  * Content extraction must read the rendered DOM when there is one. That is the reason the
    browser runs at all: on a client-rendered site the server's HTML is an empty shell and
    every on-page parameter reads zero words from it.

Nothing here launches a browser. render_many() is the seam, and it is substituted, so these
run in CI on a machine with no Chromium and still cover the decisions that matter.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.crawler import discover
from app.crawler.discover import (
    _looks_like_shell,
    _render_comparison,
    _render_candidates,
    _run_render_pass,
    parse_page,
)
from app.crawler.render import RenderResult, render_many


def _run(coro):
    return asyncio.run(coro)


def _fetch_result(url="https://x/p", html="", *, ok=True, status=200, hops=0, content=b"raw-bytes"):
    return SimpleNamespace(
        url=url, final_url=url, text=html, status_code=status, error=None, ok=ok,
        hops=hops, elapsed_ms=10, content=content, headers={}, content_type="text/html",
    )


SHELL_HTML = '<html><head><title>Acme</title></head><body><div id="root"></div></body></html>'
RENDERED_HTML = """
<html><head><title>Acme</title></head><body><div id="root">
  <main>
    <h1>Managed data platforms</h1>
    <h2>What we build</h2>
    <p>We design and operate data platforms for regulated industries, covering ingestion,
       governance, lineage and the reporting layer that sits on top of them.</p>
    <a href="/services">Services</a>
  </main>
</div></body></html>
"""


# --- the split: content from the DOM, HTTP facts from httpx --------------------------------

def test_rendered_page_reads_content_from_the_dom():
    page = parse_page(_fetch_result(html=SHELL_HTML), RENDERED_HTML)
    assert page.render["content_source"] == "rendered"
    assert page.word_count > 20
    assert [h["text"] for h in page.headings] == ["Managed data platforms", "What we build"]


def test_rendered_page_keeps_the_http_response_untouched():
    """The browser knows nothing about redirect hops or the bytes the server actually sent."""
    raw = _fetch_result(html=SHELL_HTML, hops=3, status=200, content=b"x" * 559)
    page = parse_page(raw, RENDERED_HTML)
    assert page.result is raw
    assert page.result.hops == 3
    assert len(page.result.content) == 559


def test_no_rendered_copy_falls_back_to_the_raw_html():
    page = parse_page(_fetch_result(html=RENDERED_HTML))
    assert page.render["content_source"] == "raw"
    assert page.word_count > 20


def test_blank_rendered_html_is_not_treated_as_a_render():
    """An empty string from a failed render must not blank out a page that had content."""
    page = parse_page(_fetch_result(html=RENDERED_HTML), "   ")
    assert page.render["content_source"] == "raw"
    assert page.word_count > 20


# --- the measurement ------------------------------------------------------------------------

def test_comparison_reports_the_gain_against_the_raw_count():
    raw = parse_page(_fetch_result(html="<html><body><p>" + "word " * 100 + "</p></body></html>"))
    rendered = parse_page(_fetch_result(html="<html><body><p>x</p></body></html>"),
                          "<html><body><p>" + "word " * 150 + "</p></body></html>")
    comparison = _render_comparison(raw, rendered)
    assert comparison["gained_words"] == 50
    assert comparison["gain_ratio"] == 0.5


def test_comparison_of_a_pure_shell_reports_a_full_gain_rather_than_dividing_by_zero():
    raw = parse_page(_fetch_result(html=SHELL_HTML))
    rendered = parse_page(_fetch_result(html=SHELL_HTML), RENDERED_HTML)
    comparison = _render_comparison(raw, rendered)
    assert comparison["raw_words"] == 0
    assert comparison["gain_ratio"] == 1.0


# --- choosing which pages get the browser ---------------------------------------------------

def test_an_empty_spa_root_is_a_shell():
    assert _looks_like_shell(parse_page(_fetch_result(html=SHELL_HTML))) is True


def test_a_full_page_is_not_a_shell():
    html = "<html><body><main><p>" + "word " * 400 + "</p></main></body></html>"
    assert _looks_like_shell(parse_page(_fetch_result(html=html))) is False


def test_a_bot_challenge_page_is_left_for_the_block_report_rather_than_rendered():
    """blocked_reason is how a challenged page is kept out of every denominator. Rendering
    past the challenge would erase that signal instead of measuring it."""
    page = parse_page(_fetch_result(html=SHELL_HTML))
    page.blocked_reason = "cloudflare challenge"
    assert _looks_like_shell(page) is False


def test_shells_are_rendered_before_fuller_pages():
    full_html = "<html><body><main><p>" + "word " * 400 + "</p></main></body></html>"
    pages = {
        "https://x/full": parse_page(_fetch_result(url="https://x/full", html=full_html)),
        "https://x/shell": parse_page(_fetch_result(url="https://x/shell", html=SHELL_HTML)),
    }
    order = _render_candidates(pages, list(pages), exclude=set(), budget=5)
    assert order[0] == "https://x/shell"


def test_the_budget_is_a_hard_ceiling():
    pages = {
        f"https://x/{i}": parse_page(_fetch_result(url=f"https://x/{i}", html=SHELL_HTML))
        for i in range(10)
    }
    assert len(_render_candidates(pages, list(pages), exclude=set(), budget=3)) == 3


# --- the probe-then-commit decision ----------------------------------------------------------

def _stub_render(monkeypatch, html_by_url):
    async def _fake(urls, **kwargs):
        return {
            u: (RenderResult(url=u, html=html_by_url[u], final_url=u)
                if html_by_url.get(u) else RenderResult(url=u, error="render failed"))
            for u in urls
        }
    monkeypatch.setattr(discover, "render_many", _fake)


def test_a_server_rendered_site_spends_one_tab_and_stops(monkeypatch):
    """The probe found nothing new, so the browser must not be run on the rest of the site."""
    full_html = "<html><body><main><p>" + "word " * 400 + "</p></main></body></html>"
    pages = {
        "https://x/": parse_page(_fetch_result(url="https://x/", html=full_html)),
        "https://x/a": parse_page(_fetch_result(url="https://x/a", html=full_html)),
    }
    _stub_render(monkeypatch, {"https://x/": full_html, "https://x/a": full_html})
    summary = _run(_run_render_pass("https://x/", pages, list(pages)))
    assert summary["decision"] == "not_needed"
    assert summary["attempted"] == 1
    assert pages["https://x/a"].render["content_source"] == "raw"


def test_a_client_rendered_site_spends_the_budget(monkeypatch):
    pages = {
        "https://x/": parse_page(_fetch_result(url="https://x/", html=SHELL_HTML)),
        "https://x/a": parse_page(_fetch_result(url="https://x/a", html=SHELL_HTML)),
    }
    _stub_render(monkeypatch, {"https://x/": RENDERED_HTML, "https://x/a": RENDERED_HTML})
    summary = _run(_run_render_pass("https://x/", pages, list(pages)))
    assert summary["decision"] == "applied"
    assert summary["rendered_count"] == 2
    assert pages["https://x/a"].render["content_source"] == "rendered"
    assert pages["https://x/a"].word_count > 20


def test_a_render_that_loses_content_keeps_the_servers_own_html(monkeypatch):
    """A consent wall or a mid-mount timeout returns less text than the server sent. The raw
    copy is then the better reading, and replacing it would delete real content."""
    full_html = "<html><body><main><p>" + "word " * 400 + "</p></main></body></html>"
    pages = {
        "https://x/": parse_page(_fetch_result(url="https://x/", html=SHELL_HTML)),
        "https://x/a": parse_page(_fetch_result(url="https://x/a", html=full_html)),
    }
    _stub_render(monkeypatch, {
        "https://x/": RENDERED_HTML,
        "https://x/a": "<html><body><p>Accept cookies to continue.</p></body></html>",
    })
    summary = _run(_run_render_pass("https://x/", pages, list(pages)))
    assert summary["decision"] == "applied"
    assert pages["https://x/a"].render["applied"] is False
    assert pages["https://x/a"].word_count > 300


def test_a_failed_render_leaves_the_page_exactly_as_it_was(monkeypatch):
    pages = {
        "https://x/": parse_page(_fetch_result(url="https://x/", html=SHELL_HTML)),
        "https://x/a": parse_page(_fetch_result(url="https://x/a", html=SHELL_HTML)),
    }
    _stub_render(monkeypatch, {"https://x/": RENDERED_HTML, "https://x/a": ""})
    summary = _run(_run_render_pass("https://x/", pages, list(pages)))
    assert summary["failed_count"] == 1
    assert pages["https://x/a"].render["content_source"] == "raw"
    assert pages["https://x/a"].render["render_error"] == "render failed"


def test_rendering_switched_off_changes_nothing(monkeypatch):
    monkeypatch.setattr(discover, "ENABLE_RENDER", False)
    pages = {"https://x/": parse_page(_fetch_result(url="https://x/", html=SHELL_HTML))}
    summary = _run(_run_render_pass("https://x/", pages, list(pages)))
    assert summary["decision"] == "disabled"
    assert pages["https://x/"].render["content_source"] == "raw"


def test_a_missing_browser_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(discover, "render_unavailable", lambda: "playwright is not installed")
    pages = {"https://x/": parse_page(_fetch_result(url="https://x/", html=SHELL_HTML))}
    summary = _run(_run_render_pass("https://x/", pages, list(pages)))
    assert summary["decision"] == "unavailable"
    assert summary["reason"] == "playwright is not installed"


def test_render_many_returns_one_entry_per_url_when_there_is_no_browser(monkeypatch):
    """Same contract as crawl_raw: every requested URL comes back, carrying a reason."""
    import app.crawler.render as render_module
    monkeypatch.setattr(render_module, "render_unavailable", lambda: "playwright is not installed")
    out = _run(render_many(["https://x/a", "https://x/b"]))
    assert set(out) == {"https://x/a", "https://x/b"}
    assert all(not r.ok and r.error for r in out.values())

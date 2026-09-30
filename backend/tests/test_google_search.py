import asyncio
import json
from types import SimpleNamespace

import pytest

from app.crawler import google_search as gs
from app.parameters import offpage, offpage_agents, offpage_tools


def _run(coro):
    return asyncio.run(coro)


class _Resp(SimpleNamespace):
    pass


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(gs, "GOOGLE_API_KEY", "SECRET-KEY-123")
    monkeypatch.setattr(gs, "GOOGLE_CSE_ID", "cx-1")


def _serve(monkeypatch, status=200, body=None, error=None, seen=None):
    async def fake_fetch(url, **kwargs):
        if seen is not None:
            seen.append((url, kwargs))
        return _Resp(status_code=status, text=json.dumps(body or {}), error=error)
    monkeypatch.setattr(gs, "fetch", fake_fetch)


RESULTS = {
    "searchInformation": {"totalResults": "1230"},
    "items": [
        {"link": "https://www.g2.com/products/acme/reviews", "title": "Acme Reviews | G2", "snippet": "Acme is a data\nplatform"},
        {"link": "https://clutch.co/profile/acme", "title": "Acme | Clutch", "snippet": "Acme, IT services"},
    ],
}


def test_results_are_read_as_structured_items(monkeypatch, configured):
    seen = []
    _serve(monkeypatch, body=RESULTS, seen=seen)
    sr = _run(gs.google_search('"Acme" (site:g2.com OR site:clutch.co)'))
    assert sr.ok and sr.total_results == 1230
    assert sr.urls == ["https://www.g2.com/products/acme/reviews", "https://clutch.co/profile/acme"]
    assert sr.snippets()[0] == {"title": "Acme Reviews | G2", "snippet": "Acme is a data platform"}
    url, kwargs = seen[0]
    assert url.startswith(gs.GOOGLE_SEARCH_ENDPOINT) and "cx=cx-1" in url
    assert kwargs.get("live") is True, "a saved site copy must never answer a Google query"


def test_the_api_key_never_reaches_what_the_audit_stores(monkeypatch, configured):
    _serve(monkeypatch, status=0, error="ConnectError for https://www.googleapis.com/customsearch/v1?key=SECRET-KEY-123&q=x")
    sr = _run(gs.google_search("x"))
    assert not sr.ok and "SECRET-KEY-123" not in sr.error
    assert "SECRET-KEY-123" not in sr.display_url and sr.display_url.startswith("https://www.google.com/search?q=")


def test_not_configured_is_reported_plainly(monkeypatch):
    monkeypatch.setattr(gs, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(gs, "GOOGLE_CSE_ID", "")
    sr = _run(gs.google_search("x"))
    assert not sr.ok and "GOOGLE_API_KEY" in sr.error


@pytest.mark.parametrize("status, phrase", [(429, "quota"), (403, "API key"), (400, "GOOGLE_CSE_ID"), (500, "HTTP 500")])
def test_api_failures_say_why(monkeypatch, configured, status, phrase):
    _serve(monkeypatch, status=status, body={"error": {"message": "detail from google"}})
    sr = _run(gs.google_search("x"))
    assert not sr.ok and phrase in sr.error and "detail from google" in sr.error


@pytest.fixture(autouse=True)
def no_source_pages(monkeypatch):
    """The Evidence & Validation Agent opens source pages; tests never reach the network."""
    async def blocked(url, ctx=None):
        return SimpleNamespace(status_code=403, ok=False, text="", error=None)
    monkeypatch.setattr(offpage_tools, "crawl_page", blocked)


def _ctx(**kw):
    base = dict(company_name="Acme Corp", domain="www.acme.com", pages=[], origin="https://www.acme.com")
    base.update(kw)
    return SimpleNamespace(**base)


def _spec(pid):
    return {"parameter_id": pid, "section": "off_page", "name": pid, "weight": 1.0}


def _patch_search(monkeypatch, items, ok=True, error=None):
    async def fake(query, num=10):
        return gs.SearchResult(query, ok=ok, items=items, error=error, status=200 if ok else 429)
    monkeypatch.setattr(offpage_tools, "google_search", fake)


def test_a_failed_search_is_unknown_not_zero(monkeypatch):
    _patch_search(monkeypatch, [], ok=False, error="Google search daily quota is used up (HTTP 429)")
    row = _run(offpage.off_05(_spec("OFF-05"), _ctx()))
    assert row["status"] == "UNKNOWN" and row["score"] is None
    assert "quota" in row["evidence"]["error"]


def _items(*urls, title="", snippet="", date=None):
    return [{"url": u, "title": title, "snippet": snippet, "date": date} for u in urls]


def test_off05_review_platforms_and_category(monkeypatch):
    monkeypatch.setattr(offpage_agents, "derive_site_categories", lambda ctx, limit=6: ["data platforms"])
    async def no_model(*a, **k):
        return SimpleNamespace(ok=False, parsed=None, error="no model")
    monkeypatch.setattr(offpage_agents, "judge", no_model)
    _patch_search(monkeypatch, _items("https://www.g2.com/products/acme", "https://clutch.co/profile/acme", snippet="Acme, data platforms vendor"))
    row = _run(offpage.off_05(_spec("OFF-05"), _ctx()))
    # 2 of 4 platforms x 70 = 35, + 30 for the category found in the listings
    assert row["score"] == 65.0 and row["evidence"]["platforms_found"] == ["g2.com", "clutch.co"]
    assert row["evidence"]["provider"] == "Google Custom Search"


def test_off03_presence_and_consistency(monkeypatch):
    monkeypatch.setattr(offpage_agents, "derive_site_geographies", lambda ctx: ["hyderabad"])
    _patch_search(monkeypatch, [
        {"url": "https://www.linkedin.com/company/acme", "title": "Acme Corp | LinkedIn", "snippet": "Acme Corp, Hyderabad", "date": None},
        {"url": "https://www.crunchbase.com/organization/acme", "title": "Acme Corp - Crunchbase", "snippet": "IT services", "date": None},
    ])
    row = _run(offpage.off_03(_spec("OFF-03"), _ctx()))
    # 2/4 x 70 = 35, name on both = +15, location on 1 of 2 = +7.5
    assert row["score"] == 57.5


def test_off06_volume_and_recency(monkeypatch):
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=30)).isoformat()
    _patch_search(monkeypatch, _items("https://www.g2.com/a", "https://www.g2.com/b", "https://example.com/c", date=recent))
    row = _run(offpage.off_06(_spec("OFF-06"), _ctx()))
    # 2 review-platform results x 12 = 24, newest within a year = +40
    assert row["score"] == 64 and row["evidence"]["recency_points"] == 40


def test_off07_counts_forums_but_not_the_own_site(monkeypatch):
    _patch_search(monkeypatch, _items("https://www.reddit.com/r/x/1", "https://www.quora.com/q", "https://forum.example.com/t",
                                      "https://community.acme.com/post"))
    row = _run(offpage.off_07(_spec("OFF-07"), _ctx()))
    assert row["evidence"]["mentions_found"] == 3 and row["score"] == 70


def test_off08_generic_best_list_and_named_lists(monkeypatch):
    monkeypatch.setattr(offpage_agents, "derive_site_categories", lambda ctx, limit=1: ["data platforms"])
    calls = []

    async def fake(query, num=10):
        calls.append(query)
        if query.startswith("best "):
            return gs.SearchResult(query, ok=True, items=_items("https://example.com/best", title="Best data platforms 2026", snippet="Top vendors ranked"))
        return gs.SearchResult(query, ok=True, items=_items("https://review.example.com/top-10", title="Top 10 data platforms", snippet="Acme is one"))
    monkeypatch.setattr(offpage_tools, "google_search", fake)
    row = _run(offpage.off_08(_spec("OFF-08"), _ctx()))
    assert row["score"] == 40 and not row["evidence"]["in_generic_results"]
    assert calls[0] == "best data platforms companies"


def test_wikidata_is_fetched_once_for_off01_and_off02(monkeypatch):
    seen = []

    async def fake_fetch(url, **kwargs):
        seen.append(url)
        if "wikidata" in url:
            body = {"search": [{"label": "Acme Corp", "description": "software company"}]}
        else:
            body = {"query": {"search": []}}
        return _Resp(status_code=200, ok=True, text=json.dumps(body), error=None)
    monkeypatch.setattr(offpage_tools, "fetch", fake_fetch)
    ctx = _ctx()
    one = _run(offpage.off_01(_spec("OFF-01"), ctx))
    two = _run(offpage.off_02(_spec("OFF-02"), ctx))
    assert sum(1 for u in seen if "wikidata" in u) == 1
    assert one["score"] == 0      # on Wikidata but not on Wikipedia: both are needed
    assert two["score"] == 100

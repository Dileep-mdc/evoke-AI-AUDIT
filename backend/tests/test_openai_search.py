"""OpenAI web search (an OpenAI Agents SDK agent) as the off-page search provider.

The agent run is stubbed: nothing here reaches OpenAI.
"""
import asyncio
from types import SimpleNamespace

import agents
import pytest

from app.crawler import openai_search as oas
from app.parameters import offpage, offpage_tools


def _run(coro):
    return asyncio.run(coro)


def _result(items, sources):
    """A Runner.run() result: structured output plus the raw web-search call and its sources."""
    call = SimpleNamespace(type="web_search_call",
                           action=SimpleNamespace(sources=None if sources is None else [SimpleNamespace(url=u) for u in sources]))
    return SimpleNamespace(final_output=oas._Results(results=[oas._Item(**i) for i in items]),
                           raw_responses=[SimpleNamespace(output=[call])])


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(oas, "OPENAI_API_KEY", "sk-test")


def _serve(monkeypatch, result=None, raises=None, seen=None):
    async def fake_run(agent, prompt, **kwargs):
        if seen is not None:
            seen.append((agent, prompt, kwargs))
        if raises:
            raise raises
        return result
    monkeypatch.setattr(agents.Runner, "run", fake_run)


def test_results_come_back_in_the_search_result_shape(monkeypatch, key):
    seen = []
    _serve(monkeypatch, _result([
        {"url": "https://www.linkedin.com/company/acme/", "title": "Acme | LinkedIn", "snippet": "Acme\nCorp", "date": "2026-01-05"},
        {"url": "https://www.crunchbase.com/organization/acme", "title": "Acme - Crunchbase", "snippet": "x", "date": "last week"},
    ], ["https://www.linkedin.com/company/acme?utm_source=openai", "https://crunchbase.com/organization/acme/"]), seen=seen)
    sr = _run(oas.openai_search('"Acme" site:linkedin.com'))
    assert sr.ok and sr.provider == "OpenAI web search"
    assert sr.urls == ["https://www.linkedin.com/company/acme/", "https://www.crunchbase.com/organization/acme"]
    assert sr.items[0]["snippet"] == "Acme Corp" and sr.items[0]["date"] == "2026-01-05"
    assert sr.items[1]["date"] is None  # not an ISO date, so not trusted
    assert seen[0][2]["run_config"].tracing_disabled is True
    assert '"Acme" site:linkedin.com' in seen[0][1]


def test_a_url_the_search_did_not_return_is_dropped(monkeypatch, key):
    _serve(monkeypatch, _result([
        {"url": "https://www.g2.com/products/acme", "title": "Acme on G2", "snippet": "x"},
        {"url": "https://www.g2.com/products/invented", "title": "Invented", "snippet": "x"},
    ], ["https://www.g2.com/products/acme"]))
    sr = _run(oas.openai_search("acme g2"))
    assert sr.urls == ["https://www.g2.com/products/acme"]


def test_a_search_that_returned_nothing_yields_no_results(monkeypatch, key):
    _serve(monkeypatch, _result([{"url": "https://example.com/guess", "title": "t", "snippet": "s"}], []))
    assert _run(oas.openai_search("anything")).items == []


def test_failures_are_reported_not_scored(monkeypatch, key):
    _serve(monkeypatch, raises=asyncio.TimeoutError())
    sr = _run(oas.openai_search("q"))
    assert not sr.ok and "timed out" in sr.error


def test_no_key_says_how_to_fix_it(monkeypatch):
    monkeypatch.setattr(oas, "OPENAI_API_KEY", "")
    sr = _run(oas.openai_search("q"))
    assert not sr.ok and "not configured" in sr.error and "OPENAI_API_KEY" in sr.error


@pytest.mark.parametrize("setting, google, openai, expected", [
    ("auto", True, True, "google"),
    ("auto", False, True, "openai"),
    ("auto", False, False, "none"),
    ("openai", True, True, "openai"),
    ("google", False, True, "google"),
])
def test_provider_selection(monkeypatch, setting, google, openai, expected):
    monkeypatch.setattr(offpage_tools, "SEARCH_PROVIDER", setting)
    monkeypatch.setattr(offpage_tools, "google_configured", lambda: google)
    monkeypatch.setattr(offpage_tools, "openai_configured", lambda: openai)
    assert offpage_tools.search_provider() == expected


def test_an_off_page_check_runs_on_openai_search_end_to_end(monkeypatch, key):
    monkeypatch.setattr(offpage_tools, "SEARCH_PROVIDER", "openai")

    async def blocked(url, ctx=None):
        return SimpleNamespace(status_code=403, ok=False, text="", error=None)
    monkeypatch.setattr(offpage_tools, "crawl_page", blocked)
    urls = ["https://www.reddit.com/r/x/1", "https://www.quora.com/q", "https://forum.example.com/t"]
    _serve(monkeypatch, _result([{"url": u, "title": "t", "snippet": "s"} for u in urls], urls))
    ctx = SimpleNamespace(company_name="Acme Corp", domain="www.acme.com", origin="https://www.acme.com", pages=[])
    row = _run(offpage.off_07({"parameter_id": "OFF-07", "section": "off_page", "name": "OFF-07", "weight": 1.0}, ctx))
    assert row["score"] == 70 and row["evidence"]["provider"] == "OpenAI web search"


def test_nothing_configured_is_external_data_required(monkeypatch):
    monkeypatch.setattr(offpage_tools, "SEARCH_PROVIDER", "auto")
    monkeypatch.setattr(offpage_tools, "google_configured", lambda: False)
    monkeypatch.setattr(offpage_tools, "openai_configured", lambda: False)
    ctx = SimpleNamespace(company_name="Acme Corp", domain="www.acme.com", origin="https://www.acme.com", pages=[])
    row = _run(offpage.off_07({"parameter_id": "OFF-07", "section": "off_page", "name": "OFF-07", "weight": 1.0}, ctx))
    assert row["status"] == "UNKNOWN" and row["evidence"]["parameter_status"] == "External Data Required"

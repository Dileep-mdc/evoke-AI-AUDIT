"""The off-page architecture: 7 agents, 11 reusable tools, 9 evaluators, 1 scoring engine.

Pins the agent-to-parameter map and tool layer the architecture document sets out, the data
contract every off-page row carries, and the rule that verification never moves a score.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.crawler import google_search as gs
from app.parameters import offpage, offpage_agents, offpage_tools
from app.parameters.offpage_agents import AGENTS, ORCHESTRATOR

AGENT_MAP = {
    "Knowledge Entity Agent": ("OFF-01", "OFF-02"),
    "Company Profile Agent": ("OFF-03",),
    "Certification Verification Agent": ("OFF-04",),
    "Review Intelligence Agent": ("OFF-05", "OFF-06"),
    "Community Presence Agent": ("OFF-07",),
    "Industry Visibility Agent": ("OFF-08", "OFF-09"),
}
TOOLS = ("web_search", "wikipedia_search", "wikidata_search", "crawl_page", "extract_content", "extract_entity",
         "extract_company_details", "verify_source", "compare_entities", "extract_reviews", "extract_dates_and_rankings")
CONTRACT = ("agent", "source_type", "source_urls", "parameter_status", "verification_result", "calculation_inputs")


def _run(coro):
    return asyncio.run(coro)


def _ctx():
    page = SimpleNamespace(text="We are ISO 27001 certified and a Gold Partner.", page_type="service", title="Data platforms",
                           schema_blocks=[], result=SimpleNamespace(final_url="https://www.acme.com/"))
    return SimpleNamespace(company_name="Acme Corp", domain="www.acme.com", origin="https://www.acme.com", pages=[page])


def _spec(pid):
    return {"parameter_id": pid, "section": "off_page", "name": pid, "weight": 1.0}


def _search(monkeypatch, items):
    async def fake(query, num=10):
        return gs.SearchResult(query, ok=True, items=items, status=200)
    monkeypatch.setattr(offpage_tools, "google_search", fake)


def _pages(monkeypatch, status, text=""):
    async def page(url, ctx=None):
        return SimpleNamespace(status_code=status, ok=200 <= status < 400, text=text, error=None)
    monkeypatch.setattr(offpage_tools, "crawl_page", page)


def test_six_specialists_plus_the_validator_cover_the_nine_parameters():
    assert {a.name: a.parameters for a in AGENTS} == AGENT_MAP
    assert ORCHESTRATOR.validator.name == "Evidence & Validation Agent"
    assert len(AGENTS) + 1 == 7
    assert set(ORCHESTRATOR.agent_for) == set(offpage.HANDLERS)


def test_the_eleven_reusable_tools_exist_and_agents_only_name_those():
    for name in TOOLS:
        assert callable(getattr(offpage_tools, name)), name
    for agent in [*AGENTS, ORCHESTRATOR.validator]:
        assert set(agent.tools) <= set(TOOLS), agent.name


def _row(monkeypatch, status, text):
    _pages(monkeypatch, status, text)
    _search(monkeypatch, [{"url": "https://www.reddit.com/r/x/1", "title": "t", "snippet": "s", "date": None},
                          {"url": "https://www.quora.com/q", "title": "t", "snippet": "s", "date": None}])
    return _run(offpage.off_07(_spec("OFF-07"), _ctx()))


def test_every_row_carries_the_data_contract(monkeypatch):
    row = _row(monkeypatch, 200, "<title>Acme Corp on Reddit</title>")
    for key in CONTRACT:
        assert key in row["evidence"], key
    assert row["evidence"]["agent"] == "Community Presence Agent"
    assert row["evidence"]["calculation_inputs"] == {"mentions_found": 2}
    assert row["evidence"]["parameter_status"] == "Verified"
    assert row["evidence"]["verification_result"]["sources_verified"] == 2


def test_verification_is_evidence_and_never_moves_the_score(monkeypatch):
    verified = _row(monkeypatch, 200, "<title>Acme Corp</title>")
    blocked = _row(monkeypatch, 403, "")
    unrelated = _row(monkeypatch, 200, "<title>Another company</title>")
    assert verified["score"] == blocked["score"] == unrelated["score"] == 40
    assert blocked["evidence"]["parameter_status"] == "Sources Unconfirmed"
    assert {s["result"] for s in blocked["evidence"]["verification_result"]["sources"]} == {"blocked"}
    assert {s["result"] for s in unrelated["evidence"]["verification_result"]["sources"]} == {"not_about_company"}


@pytest.mark.parametrize("error, status, expected", [
    ("Google search is not configured: set GOOGLE_API_KEY and GOOGLE_CSE_ID in backend/.env", None, "External Data Required"),
    ("Google search daily quota is used up (HTTP 429)", 429, "Blocked"),
    ("Google search could not be reached: network error", None, "Unable to Verify"),
])
def test_missing_evidence_is_reported_not_scored(monkeypatch, error, status, expected):
    async def fail(query, num=10):
        return gs.SearchResult(query, ok=False, error=error, status=status)
    monkeypatch.setattr(offpage_tools, "google_search", fail)
    row = _run(offpage.off_06(_spec("OFF-06"), _ctx()))
    assert row["status"] == "UNKNOWN" and row["score"] is None
    assert row["evidence"]["parameter_status"] == expected


def test_certification_claims_are_searched_off_site_without_changing_the_cap(monkeypatch):
    queries = []

    async def fake(query, num=10):
        queries.append(query)
        return gs.SearchResult(query, ok=True, status=200, items=[
            {"url": "https://registry.example.org/acme", "title": "Acme Corp ISO 27001 certificate", "snippet": "", "date": None}])
    monkeypatch.setattr(offpage_tools, "google_search", fake)
    _pages(monkeypatch, 200, "<title>Acme Corp</title> holds ISO 27001")
    row = _run(offpage.off_04(_spec("OFF-04"), _ctx()))
    assert row["score"] == 55
    assert '"iso 27001"' in queries[0] and "-site:acme.com" in queries[0]
    assert row["evidence"]["verification_result"]["sources_verified"] == 1


def test_an_agent_never_scores(monkeypatch):
    """Scores come only from offpage_scoring; the agents module has no score arithmetic."""
    with open(offpage_agents.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert "offpage_scoring" not in source and "score =" not in source


@pytest.mark.parametrize("full, short", [
    ("nVent Electric plc", "nVent Electric"),
    ("Evoke Technologies Private Limited", "Evoke Technologies"),
    ("Acme Pvt. Ltd.", "Acme"),
    ("Siemens AG", "Siemens"),
    ("Apple Inc.", "Apple"),
    ("Acme, Inc.", "Acme"),
    ("Acme Corp", "Acme"),
    ("Deloitte", "Deloitte"),     # nothing to strip
    ("Limited", "Limited"),       # never strips the last word
])
def test_the_legal_form_is_removed_for_the_retry(full, short):
    assert offpage_tools.short_name(full) == short


def test_a_search_with_no_results_is_retried_with_the_short_name(monkeypatch):
    queries = []

    async def fake(query, num=10):
        queries.append(query)
        hit = [{"url": "https://www.reddit.com/r/x/1", "title": "t", "snippet": "s", "date": None}] if "plc" not in query else []
        return gs.SearchResult(query, ok=True, items=hit, status=200)
    monkeypatch.setattr(offpage_tools, "google_search", fake)
    _pages(monkeypatch, 403)
    ctx = _ctx()
    ctx.company_name = "nVent Electric plc"
    row = _run(offpage.off_07(_spec("OFF-07"), ctx))
    assert queries[0].startswith('"nVent Electric plc"') and queries[1].startswith('"nVent Electric"')
    assert row["evidence"]["query"] == queries[1] and row["evidence"]["mentions_found"] == 1


def test_a_failed_search_is_not_retried(monkeypatch):
    queries = []

    async def fail(query, num=10):
        queries.append(query)
        return gs.SearchResult(query, ok=False, error="quota (HTTP 429)", status=429)
    monkeypatch.setattr(offpage_tools, "google_search", fail)
    ctx = _ctx()
    ctx.company_name = "nVent Electric plc"
    _run(offpage.off_07(_spec("OFF-07"), ctx))
    assert len(queries) == 1


def test_wikidata_finds_the_company_under_its_short_name(monkeypatch):
    async def fake_fetch(url, **kwargs):
        if "wikidata" in url:
            body = {"search": []} if "plc" in url else {"search": [{"id": "Q1", "label": "NVent Electric", "description": "electrical company"}]}
        else:
            body = {"query": {"search": [{"title": "NVent Electric"}]}}
        return SimpleNamespace(status_code=200, ok=True, text=__import__("json").dumps(body), error=None)
    monkeypatch.setattr(offpage_tools, "fetch", fake_fetch)
    ctx = _ctx()
    ctx.company_name = "nVent Electric plc"
    one = _run(offpage.off_01(_spec("OFF-01"), ctx))
    two = _run(offpage.off_02(_spec("OFF-02"), ctx))
    assert one["score"] == 100 and two["score"] == 100
    assert "search=nVent+Electric&" in two["checked_url_or_source"]


@pytest.mark.parametrize("candidate, company, same", [
    ("NVent Electric", "nVent Electric plc", True),
    ("Evoke Technologies", "Evoke Technologies Private Limited", True),
    ("Evoke plc", "Evoke Technologies", False),            # a betting company, same first word
    ("Evoke (video game)", "Evoke Technologies", False),
    ("Acme Corp", "Acme Corp", True),
    ("Acmeware", "Acme", False),                           # whole words only
])
def test_a_record_must_name_the_whole_company(candidate, company, same):
    assert offpage_tools.names_company(candidate, company) is same


def test_off01_does_not_take_a_same_first_word_company_for_this_one(monkeypatch):
    async def fake_fetch(url, **kwargs):
        if "wikidata" in url:
            body = {"search": [{"id": "Q274372", "label": "Evoke plc", "description": "betting company"}]}
        else:
            body = {"query": {"search": [{"title": "Evoke plc"}, {"title": "888sport"}]}}
        return SimpleNamespace(status_code=200, ok=True, text=__import__("json").dumps(body), error=None)
    monkeypatch.setattr(offpage_tools, "fetch", fake_fetch)
    ctx = _ctx()
    ctx.company_name = "Evoke Technologies"
    one = _run(offpage.off_01(_spec("OFF-01"), ctx))
    two = _run(offpage.off_02(_spec("OFF-02"), ctx))
    assert one["evidence"]["wikidata"]["present"] is False and one["evidence"]["wikipedia"]["present"] is False
    assert one["score"] == 0 and two["score"] == 0

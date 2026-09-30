"""The OFF-* calculation lines must reproduce the handler's own score.

Each case runs the real handler in offpage.py against stubbed Google / Wikidata /
Wikipedia / model responses, then feeds the evidence it wrote to working_offpage and checks the
arithmetic lands on the same number. A handler changed without its working line fails here.
"""
import asyncio
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.crawler import google_search as gs
from app.parameters import offpage, offpage_agents, offpage_tools
from app.parameters.working_offpage import WORKING, fmt


def _run(coro):
    return asyncio.run(coro)


def _ctx():
    page = SimpleNamespace(text="We are ISO 27001 certified and a Gold Partner.", page_type="service", title="Data platforms",
                           schema_blocks=[], result=SimpleNamespace(final_url="https://www.acme.com/"))
    return SimpleNamespace(company_name="Acme Corp", domain="www.acme.com", origin="https://www.acme.com", pages=[page])


def _spec(pid):
    return {"parameter_id": pid, "section": "off_page", "name": pid, "weight": 1.0}


def _items(*urls, title="Acme Corp", snippet="Acme Corp, data platforms, Hyderabad", date_=None):
    return [{"url": u, "title": title, "snippet": snippet, "date": date_} for u in urls]


RECENT = (date.today() - timedelta(days=40)).isoformat()
OLD = (date.today() - timedelta(days=500)).isoformat()


@pytest.fixture
def stubs(monkeypatch):
    state = SimpleNamespace(items=[], model=None, wikidata=None, wikipedia=None)

    async def fake_search(query, num=10):
        return gs.SearchResult(query, ok=True, items=state.items, status=200)

    async def fake_judge(prompt, **kwargs):
        if state.model is None:
            return SimpleNamespace(ok=False, parsed=None, error="no model")
        return SimpleNamespace(ok=True, parsed=state.model, error=None)

    async def fake_fetch(url, **kwargs):
        body = state.wikidata if "wikidata" in url else state.wikipedia
        if body is None:
            return SimpleNamespace(status_code=503, ok=False, text="", error="unavailable")
        return SimpleNamespace(status_code=200, ok=True, text=json.dumps(body), error=None)

    monkeypatch.setattr(offpage_tools, "google_search", fake_search)
    monkeypatch.setattr(offpage_agents, "judge", fake_judge)
    monkeypatch.setattr(offpage_tools, "fetch", fake_fetch)
    monkeypatch.setattr(offpage_agents, "derive_site_categories", lambda ctx, limit=10: ["data platforms"])
    monkeypatch.setattr(offpage_agents, "derive_site_geographies", lambda ctx, limit=8: ["hyderabad"])
    return state


def _check(pid, row):
    assert row["score"] is not None, f"{pid} did not score"
    ev = {**row["evidence"], "rules_based_score": row["score"]}
    out = WORKING[pid](ev)
    assert out is not None, f"{pid} produced no working line"
    text, recomputed = out
    assert abs(recomputed - row["score"]) <= 0.15, f"{pid}: line says {recomputed}, handler scored {row['score']}: {text}"
    assert text.strip()
    assert not [s for s in ("÷", "×", "→", "≥", "≤", "−", "=") if s in text], text  # plain words, not a formula
    return text


HANDLER = {f"OFF-{i:02d}": getattr(offpage, f"off_{i:02d}") for i in range(1, 10)}


def test_every_off_page_parameter_has_a_working_line():
    assert set(WORKING) == set(HANDLER) == set(offpage.HANDLERS)


ON_WIKIDATA = {"search": [{"id": "Q1", "label": "Acme Corp", "description": "software company"}]}
ON_WIKIPEDIA = {"query": {"search": [{"title": "Acme Corp", "snippet": "x"}]}}


@pytest.mark.parametrize("wikidata, wikipedia, expected", [
    (ON_WIKIDATA, ON_WIKIPEDIA, 100),                                        # on both
    (ON_WIKIDATA, {"query": {"search": [{"title": "Other Co"}]}}, 0),        # Wikipedia missing
    ({"search": [{"label": "Other Co"}]}, ON_WIKIPEDIA, 0),                  # Wikidata missing
    ({"search": []}, {"query": {"search": []}}, 0),                          # on neither
    (None, {"query": {"search": []}}, 0),                                    # one known absence settles it
])
def test_off01_is_yes_or_no_on_both(stubs, wikidata, wikipedia, expected):
    stubs.wikidata, stubs.wikipedia = wikidata, wikipedia
    row = _run(offpage.off_01(_spec("OFF-01"), _ctx()))
    assert row["score"] == expected
    _check("OFF-01", row)


def test_off01_is_unknown_when_the_deciding_source_is_down(stubs):
    stubs.wikidata, stubs.wikipedia = None, ON_WIKIPEDIA
    row = _run(offpage.off_01(_spec("OFF-01"), _ctx()))
    assert row["status"] == "UNKNOWN" and row["evidence"]["parameter_status"] == "Unable to Verify"


@pytest.mark.parametrize("entity", [{"label": "Acme Corp", "description": "software"}, {"label": "Acme Corp", "description": ""}])
def test_off02(stubs, entity):
    stubs.wikidata = {"search": [entity]}
    _check("OFF-02", _run(offpage.off_02(_spec("OFF-02"), _ctx())))


@pytest.mark.parametrize("urls", [
    ("https://www.linkedin.com/company/acme", "https://www.crunchbase.com/organization/acme"),
    (),
    ("https://www.linkedin.com/company/acme", "https://www.crunchbase.com/o/acme", "https://www.bloomberg.com/p/acme", "https://www.zoominfo.com/c/acme"),
])
def test_off03(stubs, urls):
    stubs.items = _items(*urls)
    _check("OFF-03", _run(offpage.off_03(_spec("OFF-03"), _ctx())))


def test_off04(stubs):
    _check("OFF-04", _run(offpage.off_04(_spec("OFF-04"), _ctx())))


@pytest.mark.parametrize("model", [None, {"matches": True}, {"matches": False}])
def test_off05(stubs, model):
    stubs.model = model
    stubs.items = _items("https://www.g2.com/products/acme", "https://clutch.co/profile/acme")
    _check("OFF-05", _run(offpage.off_05(_spec("OFF-05"), _ctx())))


@pytest.mark.parametrize("dated", [RECENT, OLD, None])
def test_off06(stubs, dated):
    stubs.items = _items("https://www.g2.com/a", "https://www.g2.com/b", "https://clutch.co/c", date_=dated)
    _check("OFF-06", _run(offpage.off_06(_spec("OFF-06"), _ctx())))


@pytest.mark.parametrize("n", [0, 2, 4, 7])
def test_off07(stubs, n):
    stubs.items = _items(*[f"https://www.reddit.com/r/x/{i}" for i in range(n)])
    _check("OFF-07", _run(offpage.off_07(_spec("OFF-07"), _ctx())))


@pytest.mark.parametrize("title", ["Best data platforms 2026", "Acme Corp tops the list"])
def test_off08(stubs, title):
    stubs.items = _items("https://review.example.com/best-list", title=title, snippet="ranked")
    _check("OFF-08", _run(offpage.off_08(_spec("OFF-08"), _ctx())))


@pytest.mark.parametrize("model, items", [({"accurate": True}, 2), ({"accurate": False}, 2), (None, 2), (None, 0)])
def test_off09(stubs, model, items):
    stubs.model = model
    stubs.items = _items(*[f"https://www.gartner.com/r/{i}" for i in range(items)])
    _check("OFF-09", _run(offpage.off_09(_spec("OFF-09"), _ctx())))


def test_unknown_rows_have_no_working_line():
    for fn in WORKING.values():
        assert fn({"error": "Google search is not configured", "rules_based_score": None}) is None
        assert fn("not a dict") is None


def test_numbers_are_formatted_for_reading():
    assert fmt(1234.0) == "1,234" and fmt(57.5) == "57.5" and fmt(0) == "0"


def test_off02_scores_zero_when_the_company_has_no_wikidata_entity(stubs):
    stubs.wikidata = {"search": [{"label": "Other Co", "description": "unrelated"}]}
    row = _run(offpage.off_02(_spec("OFF-02"), _ctx()))
    assert row["score"] == 0 and row["status"] == "FAIL"
    assert "Create a Wikidata item" in row["recommendation"]
    _check("OFF-02", row)


def test_off02_is_unknown_only_when_wikidata_is_unreachable(stubs):
    stubs.wikidata = None
    row = _run(offpage.off_02(_spec("OFF-02"), _ctx()))
    assert row["status"] == "UNKNOWN" and row["evidence"]["parameter_status"] == "Unable to Verify"

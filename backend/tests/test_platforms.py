"""The off-page site lists are data (offpage_platforms.json), chosen per scan.

Pins that the default set is unchanged, that an industry or region only ever adds sites, that a
longer list does not lower the score for the same presence, and that the agents search the
lists the scan chose rather than a constant.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.crawler import google_search as gs
from app.parameters import offpage_scoring, offpage_tools, platforms, working_offpage
from app.parameters.offpage_agents import ORCHESTRATOR


def test_the_default_set_is_the_original_four_review_and_four_profile_sites():
    d = platforms.resolve()
    assert d["review"] == ("g2.com", "clutch.co", "gartner.com", "trustradius.com")
    assert d["profile"] == ("linkedin.com", "crunchbase.com", "bloomberg.com", "zoominfo.com")
    assert d["community"] == ("reddit.com", "quora.com")


def test_an_industry_and_region_add_sites_without_removing_any():
    d = platforms.resolve()
    chosen = platforms.resolve("software", "india")
    for kind in platforms.KINDS:
        assert chosen[kind][:len(d[kind])] == d[kind]
    assert "capterra.com" in chosen["review"] and "ambitionbox.com" in chosen["review"]
    assert "tracxn.com" in chosen["profile"]
    assert chosen["label"] == "Global + Software & SaaS + India"


def test_a_site_named_by_two_sets_is_checked_once():
    chosen = platforms.resolve(None, "europe")
    both = platforms.resolve("manufacturing", "europe")
    assert both["profile"].count("kompass.com") == 1
    assert chosen["review"].count("trustpilot.com") == 1


def test_an_unknown_choice_is_refused():
    with pytest.raises(ValueError):
        platforms.resolve("not-an-industry")
    with pytest.raises(ValueError):
        platforms.resolve(None, "atlantis")


def test_every_choice_listed_for_the_dropdowns_resolves():
    c = platforms.choices()
    for o in c["industries"]:
        platforms.resolve(o["key"])
    for o in c["regions"]:
        platforms.resolve(None, o["key"])


def test_a_longer_list_does_not_lower_the_score_for_the_same_presence():
    target = offpage_scoring.presence_target(9)
    assert target == 4
    assert offpage_scoring.off_05(4, target, False) == offpage_scoring.off_05(4, offpage_scoring.presence_target(4), False) == 70
    assert offpage_scoring.off_05(6, target, False) == 70  # capped
    assert offpage_scoring.presence_target(2) == 2


def test_the_working_names_the_sites_actually_checked():
    sites = list(platforms.resolve("software")["review"])
    ev = {"platforms_checked": sites, "platforms_found": ["g2.com", "capterra.com"], "category_match": True,
          "category_method": "llm"}
    text, score = working_offpage.off_05(ev)
    assert score == pytest.approx(2 / 4 * 70 + 30)
    assert f"of the {len(sites)} review sites checked" in text and "any 4 earn full marks" in text


def test_rows_from_before_the_lists_were_configurable_still_read_against_the_default():
    text, score = working_offpage.off_05({"platforms_found": ["g2.com"], "category_match": False})
    assert score == pytest.approx(70 / 4) and "of the 4 review sites checked" in text


def test_the_agents_search_the_sites_the_scan_chose(monkeypatch):
    queries = []

    async def fake(query, num=10):
        queries.append(query)
        return gs.SearchResult(query, ok=True, items=[{"url": "https://www.capterra.com/p/1/acme", "title": "Acme Corp",
                                                       "snippet": "Acme Corp reviews"}], status=200)
    monkeypatch.setattr(offpage_tools, "search_provider", lambda: "google")
    monkeypatch.setattr(offpage_tools, "google_search", fake)
    page = SimpleNamespace(text="Data platforms", page_type="service", title="Data platforms", schema_blocks=[],
                           result=SimpleNamespace(final_url="https://www.acme.com/"))
    ctx = SimpleNamespace(company_name="Acme Corp", domain="www.acme.com", origin="https://www.acme.com", pages=[page],
                          platforms=platforms.resolve("software"))
    f = asyncio.run(ORCHESTRATOR.agents[3].research("OFF-05", ctx))
    assert "site:capterra.com" in queries[0]
    assert "capterra.com" in f.values["platforms_found"]
    assert f.evidence["platform_set"] == "Global + Software & SaaS"

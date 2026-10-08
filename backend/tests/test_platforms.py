"""The off-page site lists come from the scan's industry profile, plus its region's sites.

Pins that the general profile keeps the original lists, that an industry sets its own lists (a
manufacturer is not looked for on G2), that a region only adds, that a longer list does not lower
the score for the same presence, and that the agents search the lists the scan chose.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.crawler import google_search as gs
from app.parameters import offpage_scoring, offpage_tools, platforms, working_offpage
from app.parameters.offpage_agents import ORCHESTRATOR


def test_the_general_profile_keeps_the_original_four_review_and_four_profile_sites():
    d = platforms.resolve()
    assert d["review"] == ("g2.com", "clutch.co", "gartner.com", "trustradius.com")
    assert d["profile"] == ("linkedin.com", "crunchbase.com", "bloomberg.com", "zoominfo.com")
    assert d["community"] == ("reddit.com", "quora.com")
    assert d["industry"] == "general"


def test_an_industry_sets_its_own_lists():
    manufacturing = platforms.resolve("manufacturing")
    assert manufacturing["review"] == ()                      # software review sites do not list manufacturers
    assert "thomasnet.com" in manufacturing["profile"] and "g2.com" not in manufacturing["profile"]
    software = platforms.resolve("software")
    assert "capterra.com" in software["review"] and "g2.com" in software["review"]


def test_a_region_adds_its_sites_after_the_industry_s():
    plain = platforms.resolve("it_services")
    regional = platforms.resolve("it_services", "india")
    for kind in platforms.KINDS:
        assert regional[kind][:len(plain[kind])] == plain[kind]
    assert "ambitionbox.com" in regional["review"] and "tracxn.com" in regional["profile"]
    assert regional["label"] == "IT services & consulting + India"


def test_a_site_named_twice_is_checked_once():
    both = platforms.resolve("manufacturing", "europe")
    assert both["profile"].count("kompass.com") == 1


def test_an_unknown_choice_is_refused():
    with pytest.raises(ValueError):
        platforms.resolve("not-an-industry")
    with pytest.raises(ValueError):
        platforms.resolve(None, "atlantis")


def test_every_listed_choice_resolves():
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
    assert f.evidence["platform_set"] == "Software & SaaS"

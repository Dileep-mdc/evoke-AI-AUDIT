"""Company Identity Agent: the organisation behind any website, named the way the web names it.

Wikidata is faked here; what is pinned is how the site's own signals are read, how an abbreviation
is expanded, how the brand term is chosen, and which Wikidata entity is accepted for a domain.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from app.parameters import identity, offpage, offpage_tools
from app.parameters.common import derive_site_categories, primary_brand


def _home(html="", title="", text="", schema=()):
    return SimpleNamespace(url="https://x/", title=title, text=text, page_type="home", links=[],
                           soup=BeautifulSoup(html or "<html></html>", "html.parser"),
                           schema_blocks=[{"ok": True, "data": s} for s in schema])


def _ctx(home, domain="www.example.org", pages=None):
    return SimpleNamespace(domain=domain, origin=f"https://{domain}", homepage=home, pages=pages or [home],
                           company_name="", brand_terms=[])


@pytest.fixture
def no_wikidata(monkeypatch):
    async def none(url, ctx=None):
        return {"search": [], "entities": {}}, {"url": url}
    monkeypatch.setattr(offpage_tools, "_json", none)


def _resolve(ctx):
    return asyncio.run(identity.resolve(ctx))


# --- reading the site's own signals --------------------------------------------------------------

def test_a_declared_legal_name_wins_over_the_page_title(no_wikidata):
    home = _home(title="Smarter electrical solutions | nVent",
                 schema=[{"@type": "Organization", "legalName": "nVent Electric plc", "name": "nVent"}])
    r = _resolve(_ctx(home, "www.nvent.com"))
    assert r["name"] == "nVent Electric plc" and r["brand"] == "nvent"


def test_the_copyright_line_names_the_organisation(no_wikidata):
    home = _home(html="<footer>© 2026 This site is published by the American Academy of Orthopaedic Surgeons. All rights reserved.</footer>",
                 title="Home")
    r = _resolve(_ctx(home, "www.aaos.org"))
    assert r["name"] == "American Academy of Orthopaedic Surgeons"
    assert r["acronym"] == "AAOS" and r["brand"] == "aaos"       # the domain abbreviates the name


def test_a_name_with_its_abbreviation_appended_is_split(no_wikidata):
    home = _home(html='<meta property="og:site_name" content="American Academy of Orthopaedic Surgeons AAOS">')
    r = _resolve(_ctx(home, "www.aaos.org"))
    assert r["name"] == "American Academy of Orthopaedic Surgeons" and r["acronym"] == "AAOS"


def test_an_abbreviation_only_site_is_expanded_from_its_own_text(no_wikidata):
    home = _home(title="ASCE", text="Welcome. The American Society of Civil Engineers (ASCE) represents members worldwide.")
    r = _resolve(_ctx(home, "www.asce.org"))
    assert r["name"] == "American Society of Civil Engineers" and r["acronym"] == "ASCE"


@pytest.mark.parametrize("name, acronym, stem, brand", [
    ("Evoke Technologies", None, "evoketechnologies", "evoke"),
    ("American Red Cross", None, "redcross", "red cross"),
    ("nVent Electric plc", None, "nvent", "nvent"),
    ("American Academy of Orthopaedic Surgeons", "AAOS", "aaos", "aaos"),
    ("Siemens", None, "siemens", "siemens"),
])
def test_the_brand_term_is_distinctive_never_a_common_word(name, acronym, stem, brand):
    assert identity.brand_term(name, acronym, stem) == brand


def test_primary_brand_uses_the_identity_not_the_first_word():
    ctx = SimpleNamespace(company_name="American Academy of Orthopaedic Surgeons", domain="www.aaos.org",
                          identity={"brand": "aaos"})
    assert primary_brand(ctx) == "aaos"          # was "american"


def test_the_organisation_s_own_name_is_not_a_category():
    home = SimpleNamespace(links=[{"text": "AAOS & Emergency Medical Services (EMS)"}, {"text": "Orthopaedic Education"}])
    ctx = SimpleNamespace(pages=[], homepage=home, brand_terms=["aaos"], identity={"brand": "aaos", "acronym": "AAOS"})
    assert derive_site_categories(ctx) == ["orthopaedic education"]


# --- confirming against Wikidata -----------------------------------------------------------------

def _wikidata(monkeypatch, search, entities):
    async def fake(url, ctx=None):
        if "wbsearchentities" in url:
            return {"search": [{"id": q} for q in search]}, {"url": url}
        return {"entities": entities}, {"url": url}
    monkeypatch.setattr(offpage_tools, "_json", fake)


def _entity(label, sites, aliases=(), enwiki=None):
    return {"labels": {"en": {"value": label}}, "descriptions": {"en": {"value": "organisation"}},
            "aliases": {"en": [{"value": a} for a in aliases]},
            "claims": {"P856": [{"mainsnak": {"datavalue": {"value": s}}} for s in sites]},
            "sitelinks": {"enwiki": {"title": enwiki}} if enwiki else {}}


def test_the_entity_whose_home_page_is_the_site_beats_one_owning_a_section(monkeypatch):
    _wikidata(monkeypatch, ["Q1318737", "Q155026"], {
        "Q1318737": _entity("UPS Airlines", ["https://www.ups.com/aircargo/"]),
        "Q155026": _entity("United Parcel Service", ["https://www.ups.com/"], enwiki="United Parcel Service")})
    r = _resolve(_ctx(_home(title="UPS"), "www.ups.com"))
    assert r["wikidata_id"] == "Q155026" and r["name"] == "United Parcel Service" and r["acronym"] == "UPS"


def test_a_sub_brand_on_a_subdomain_is_not_the_organisation(monkeypatch):
    _wikidata(monkeypatch, ["Q1414523"], {"Q1414523": _entity("Schroff GmbH", ["https://schroff.nvent.com/"])})
    home = _home(schema=[{"@type": "Organization", "legalName": "nVent Electric plc"}])
    r = _resolve(_ctx(home, "www.nvent.com"))
    assert r["name"] == "nVent Electric plc" and r["wikidata_id"] is None and not r["confirmed"]


def test_an_entity_unrelated_by_name_is_not_accepted_on_its_website_alone(monkeypatch):
    _wikidata(monkeypatch, ["Q110575451"], {"Q110575451": _entity("The Line", ["https://www.apple.com/"])})
    home = _home(schema=[{"@type": "Organization", "name": "Apple"}])
    r = _resolve(_ctx(home, "www.apple.com"))
    assert r["wikidata_id"] is None


def test_wikidata_unreachable_still_gives_the_site_s_own_name(monkeypatch):
    async def down(url, ctx=None):
        return None, {"url": url, "error": "unreachable"}
    monkeypatch.setattr(offpage_tools, "_json", down)
    r = _resolve(_ctx(_home(schema=[{"@type": "Corporation", "name": "Evoke Technologies"}]), "www.evoketechnologies.com"))
    assert r["name"] == "Evoke Technologies" and not r["confirmed"]


# --- the knowledge checks use the confirmed entity ------------------------------------------------

def test_a_confirmed_entity_settles_the_knowledge_checks_without_a_name_search(monkeypatch):
    async def nothing_by_name(url, **kwargs):
        body = {"search": []} if "wikidata" in url else {"query": {"search": []}}
        return SimpleNamespace(status_code=200, ok=True, text=json.dumps(body), error=None)
    monkeypatch.setattr(offpage_tools, "fetch", nothing_by_name)
    ctx = SimpleNamespace(company_name="American Academy of Orthopaedic Surgeons", domain="www.aaos.org",
                          origin="https://www.aaos.org", pages=[], brand_terms=["aaos"],
                          identity={"name": "American Academy of Orthopaedic Surgeons", "acronym": "AAOS", "brand": "aaos",
                                    "short_name": "American Academy of Orthopaedic Surgeons", "wikidata_id": "Q4742902",
                                    "wikidata_description": "orthopedic organization",
                                    "wikipedia_title": "American Academy of Orthopaedic Surgeons"})
    spec = lambda pid: {"parameter_id": pid, "section": "off_page", "name": pid, "weight": 1.0}
    one = asyncio.run(offpage.off_01(spec("OFF-01"), ctx))
    two = asyncio.run(offpage.off_02(spec("OFF-02"), ctx))
    assert one["score"] == 100 and one["evidence"]["wikidata"]["matched_by"] == "official website"
    assert two["score"] == 100      # brand carried by the full name, not the word "american"


def test_menu_labels_for_audiences_or_languages_are_not_categories():
    home = SimpleNamespace(links=[{"text": "Patients & Public"}, {"text": "English"}, {"text": "Electrical Enclosures"}])
    ctx = SimpleNamespace(pages=[], homepage=home, brand_terms=["acme"], identity={})
    assert derive_site_categories(ctx) == ["electrical enclosures"]

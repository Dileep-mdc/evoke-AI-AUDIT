"""Report accuracy: N/A instead of scores for nothing, checks read together, industry profiles.

Each test pins one of the problems found in the nVent report: a score for FAQ schema on a site with
no FAQs, passes for "nothing found", robots.txt and the firewall disagreeing without explanation,
crawler blocking buried among minor issues, canonical tags reading as "duplicates solved", content
depth penalised twice, and a manufacturer graded by IT-services rules.
"""
import asyncio
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from app.parameters import crosschecks, engine, industry, offpage_scoring, onpage, technical
from app.parameters.scoring import display_status, status_counts


def _run(coro):
    return asyncio.run(coro)


def _spec(pid, section="technical"):
    return {"parameter_id": pid, "section": section, "name": pid, "weight": 1.0}


def _page(url="https://x/p", *, text="", title="", page_type="other", soup=None, schema=(), images=(), html=""):
    return SimpleNamespace(
        url=url, title=title, text=text, word_count=len(text.split()), page_type=page_type, soup=soup,
        schema_blocks=[{"ok": True, "data": s} for s in schema], images=list(images), headings=[], links=[],
        dates={}, hreflang=[], canonical="", blocked_reason="", render={},
        result=SimpleNamespace(final_url=url, text=html, ok=True, status_code=200, error=None, hops=0, elapsed_ms=10, content=b""),
    )


def _ctx(pages, **extra):
    return SimpleNamespace(origin="https://x", domain="x", pages=pages, homepage=pages[0] if pages else None,
                           company_name="X", brand_terms=["x"], crawl_errors=[], render={}, **extra)


# --- nothing to evaluate is a failure at 0, with the fix -----------------------------------------

def _fails_for_no_data(out):
    assert out["status"] == "FAIL" and out["score"] == 0 and display_status(out) == "FAIL"
    assert out["evidence"]["no_data"] and out["evidence"]["no_data_reason"] and out["recommendation"]
    assert "not_applicable" not in out["evidence"]


def test_faq_schema_validity_fails_without_any_faq():
    out = _run(onpage.on_06(_spec("ON-06", "on_page"), _ctx([_page(text="We make enclosures.")])))
    _fails_for_no_data(out)
    assert "FAQ" in out["recommendation"]


def test_claim_sourcing_fails_without_claims():
    _fails_for_no_data(_run(onpage.on_14(_spec("ON-14", "on_page"), _ctx([_page(text="We make enclosures for every need.")]))))


def test_alt_text_fails_without_images_or_videos():
    _fails_for_no_data(_run(technical.tech_14(_spec("TECH-14"), _ctx([_page(text="plain")]))))


def test_images_alone_carry_the_score_when_there_are_no_videos():
    page = _page(images=[{"alt": "Rack enclosure front view"}, {"alt": ""}])
    out = _run(technical.tech_14(_spec("TECH-14"), _ctx([page])))
    assert out["score"] == 50.0          # was 50 x 0.7 + a free 30 for having no videos


def test_page_type_schema_fails_when_no_page_carries_one():
    _fails_for_no_data(_run(technical.tech_17(_spec("TECH-17"), _ctx([_page(page_type="utility")]))))


def test_a_row_saved_as_n_a_is_upgraded_to_a_no_data_failure():
    from app.parameters.common import upgrade_na_row
    row = {"parameter_id": "ON-06", "section": "on_page", "name": "FAQ schema", "status": "UNKNOWN", "score": None,
           "evidence": {"not_applicable": True, "na_reason": "no FAQ exists on any page", "faq_pages_total": 0}}
    assert upgrade_na_row(row)
    _fails_for_no_data(row)
    assert row["evidence"]["faq_pages_total"] == 0 and status_counts([row]) == {"pass": 0, "partial": 0, "fail": 1, "unknown": 0}
    assert not upgrade_na_row(row)


def test_a_missing_rendered_copy_stays_unmeasured_not_failed():
    """TECH-12's old flag meant the crawl had no browser, not that the site lacks anything."""
    from app.parameters.common import upgrade_na_row
    row = {"parameter_id": "TECH-12", "section": "technical", "name": "x", "status": "UNKNOWN", "score": None,
           "evidence": {"not_applicable": True, "render_decision": "none"}}
    assert upgrade_na_row(row)
    assert row["status"] == "UNKNOWN" and row["evidence"] == {"render_decision": "none", "retry_pointless": True}


# --- one problem, one penalty ----------------------------------------------------------------

def test_text_to_code_no_longer_scores_word_count():
    html = "<html><body>" + "<p>word</p>" * 10 + "</body></html>"
    short = _page(text="word " * 10, html=html)
    out = _run(technical.tech_13(_spec("TECH-13"), _ctx([short])))
    row = out["evidence"]["pages"][0]
    assert "combined" not in row and out["score"] == row["text_to_code_score"]


def test_menu_lists_do_not_count_as_content_lists():
    soup = BeautifulSoup("<nav><ul><li>a</li></ul><ul><li>b</li></ul></nav><main><ul><li>c</li></ul></main>", "html.parser")
    out = _run(technical.tech_12(_spec("TECH-12"), _ctx([_page(soup=soup)])))
    assert out["evidence"]["lists"] == 1


# --- reading the checks together ------------------------------------------------------------

def _row(pid, score, evidence, status="FAIL"):
    return {"parameter_id": pid, "status": status, "score": score, "evidence": evidence, "recommendation": "Fix it."}


def test_robots_allowing_while_the_firewall_blocks_is_explained_and_gates_the_report():
    rows = [
        _row("TECH-01", 100, {"decisions": [{"name": "GPTBot", "decision": "ALLOWED"}, {"name": "ClaudeBot", "decision": "ALLOWED"}]}, "PASS"),
        _row("TECH-02", 50, {"bots": [{"bot": "GPTBot", "blocked": True}, {"bot": "ClaudeBot", "blocked": False}]}),
    ]
    found = crosschecks.reconcile(rows, _ctx([]))
    assert "robots.txt allows these AI crawlers (GPTBot)" in rows[1]["evidence"]["cross_check"]
    # The note is shown beside the fix (workbook explanation, score panel), never inside it.
    assert "robots.txt allows" not in (rows[1]["recommendation"] or "")
    assert "gating" not in found


def test_canonicals_are_not_read_as_duplicates_solved():
    rows = [_row("TECH-20", 100, {}, "PASS"), _row("ON-19", 40, {"duplicate_pairs": 330})]
    crosschecks.reconcile(rows, _ctx([]))
    assert "330 near-duplicate page pairs" in rows[0]["evidence"]["cross_check"]


def test_no_note_when_nothing_disagrees():
    rows = [_row("TECH-01", 100, {"decisions": [{"name": "GPTBot", "decision": "ALLOWED"}]}, "PASS"),
            _row("TECH-02", 100, {"bots": [{"bot": "GPTBot", "blocked": False}]}, "PASS")]
    found = crosschecks.reconcile(rows, _ctx([]))
    assert found == {"cross_checks": []} and all("cross_check" not in r["evidence"] for r in rows)


# --- parts that cannot be measured give their weight to the parts that can -------------------

def test_profile_consistency_without_a_stated_location_uses_the_name_match():
    assert offpage_scoring.off_03(4, 4, 1.0, None) == 100
    assert offpage_scoring.off_03(4, 4, 1.0, 0.0) == 85


def test_review_presence_carries_the_score_without_a_derivable_category():
    assert offpage_scoring.off_05(2, 4, None) == 50
    assert offpage_scoring.off_05(2, 4, False) == 35


# --- industry profiles ------------------------------------------------------------------------

def test_a_manufacturer_is_detected_from_its_own_pages():
    pages = [_page("https://n/", title="Electrical enclosures manufacturer", text="Industrial enclosures. Download the datasheet and CAD. Find a distributor near you."),
             _page("https://n/products/enclosure-a", title="Enclosure A specifications", page_type="service",
                   schema=[{"@type": "Product", "name": "Enclosure A"}]),
             _page("https://n/where-to-buy", title="Where to buy")]
    found = industry.detect(_ctx(pages))
    assert found["key"] == "manufacturing" and found["detected"]


def test_an_unclear_site_stays_on_the_general_profile():
    assert industry.detect(_ctx([_page("https://g/", title="Welcome", text="Hello there.")]))["key"] == "general"


def test_every_check_runs_whatever_the_industry(monkeypatch):
    ran = []

    async def handler(spec, ctx):
        ran.append(spec["parameter_id"])
        return {"parameter_id": spec["parameter_id"], "section": "off_page", "name": "x", "status": "FAIL", "score": 0.0,
                "evidence": {}, "recommendation": "r", "error": None}
    monkeypatch.setitem(engine.HANDLERS, "OFF-05", handler)
    ctx = _ctx([_page()], industry={"key": "manufacturing"})
    out = _run(engine.evaluate(_spec("OFF-05", "off_page"), ctx))
    assert ran == ["OFF-05"] and display_status(out) == "FAIL"


def test_the_content_checklist_follows_the_industry():
    manufacturing = [r["point"] for r in industry.content_rubric(_ctx([], industry={"key": "manufacturing"}))]
    general = [r["point"] for r in industry.content_rubric(_ctx([]))]
    assert any("specification" in p for p in manufacturing) and not any("specification" in p for p in general)


def test_a_manufacturer_s_product_pages_are_expected_to_carry_product_schema():
    page = _page(page_type="service", schema=[{"@type": "Product", "name": "Enclosure"}])
    out = _run(technical.tech_17(_spec("TECH-17"), _ctx([page], industry={"key": "manufacturing"})))
    assert out["score"] == 100 and out["evidence"]["pages"][0]["expected"] == "Product"


@pytest.mark.parametrize("key", list(industry.PROFILES))
def test_every_profile_is_complete(key):
    prof = industry.PROFILES[key]
    assert prof["label"] and len(prof["content_rubric"]) >= 6
    assert set(prof["platforms"]) == {"review", "profile", "community"}
    # Industries skip nothing, with one reviewed exception: OFF-04's CMMI/ISO/partner-tier credentials
    # are IT-services claims, so a clearly non-IT industry marks it N/A, with a reason for the report.
    assert set(prof.get("not_applicable") or {}) <= {"OFF-04"}
    assert all(reason.strip() for reason in (prof.get("not_applicable") or {}).values())
    assert key not in ("general", "it_services", "software") or not prof.get("not_applicable")


def test_a_check_the_industry_skipped_becomes_unmeasured_not_failed():
    """It was never run, so a 0 would be invented: it waits for a re-check instead."""
    from app.parameters.common import upgrade_na_row
    for ev in ({"not_applicable": True, "na_reason": "Healthcare sites cannot name patients", "industry": "Healthcare"},
               {"no_data": True, "no_data_reason": "Healthcare sites cannot name patients", "industry": "Healthcare"}):
        row = {"parameter_id": "ON-13", "section": "on_page", "name": "x", "status": "FAIL", "score": 0.0, "evidence": dict(ev)}
        assert upgrade_na_row(row)
        assert row["status"] == "UNKNOWN" and row["score"] is None and "re-check" in row["evidence"]["summary"]


def test_an_upgraded_reason_states_the_finding_only():
    from app.parameters.common import upgrade_na_row
    row = {"parameter_id": "TECH-21", "section": "technical", "name": "x", "status": "UNKNOWN", "score": None,
           "evidence": {"not_applicable": True, "na_reason": "no hreflang annotations were found, so there is nothing to check"}}
    assert upgrade_na_row(row)
    assert row["evidence"]["no_data_reason"] == "no hreflang annotations were found" and not upgrade_na_row(row)

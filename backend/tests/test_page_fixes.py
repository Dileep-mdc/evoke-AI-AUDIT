"""Fix by page (parameters/page_fixes.py): every page a check found wanting, with its own numbers.

Pinned here: a page is listed by the same rule that scored it, the fix names that page's own
details (its URL in the canonical tag, its H1 count, its word count against the threshold), checks
measured site-wide say so instead of listing nothing, and malformed evidence never breaks a report.
Also that the model's recommendation prompt is given the site and its pages to name.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.llm.scoring import build_prompt
from app.parameters import engine, page_fixes
from app.parameters.page_fixes import BUILDERS, Site, counts_for, fixes_for, site_from_report

SITE = Site(origin="https://www.acme.com", company="Acme Corp")


def _row(pid: str, evidence: dict, status: str = "FAIL") -> dict:
    return {"parameter_id": pid, "status": status, "evidence": evidence}


def test_a_missing_canonical_names_the_tag_with_the_pages_own_url():
    row = _row("TECH-20", {"pages": [
        {"url": "https://www.acme.com/a", "canonical": "", "hops": 0, "ok": False, "notes": ["missing canonical"]},
        {"url": "https://www.acme.com/b", "canonical": "https://www.acme.com/b", "hops": 0, "ok": True, "notes": []},
    ]})
    out = fixes_for(row, SITE)
    assert out["total"] == 1
    assert out["fixes"][0]["target"] == "https://www.acme.com/a"
    assert '<link rel="canonical" href="https://www.acme.com/a">' in out["fixes"][0]["fix"]


def test_heading_problems_keep_their_capitals():
    row = _row("TECH-10", {"pages": [{"url": "https://www.acme.com/a", "h1_count": 2, "skipped_levels": 1, "score": 65}]})
    issue = fixes_for(row, SITE)["fixes"][0]["issue"]
    assert issue.startswith("2 H1 headings") and "H2 straight to H4" in issue


def test_thin_pages_are_listed_against_the_threshold_that_scored_them():
    row = _row("ON-19", {"thin": [{"url": "https://www.acme.com/t", "words": 91}], "near_duplicates": []})
    fix = fixes_for(row, SITE)["fixes"][0]
    assert fix["issue"] == f"Thin page: 91 words (under {page_fixes._THIN_WORDS})"


def test_only_failing_pages_are_listed():
    row = _row("TECH-13", {"pages": [
        {"url": "https://www.acme.com/ok", "text_to_code": 30.0, "words": 900},
        {"url": "https://www.acme.com/heavy", "text_to_code": 4.2, "words": 300},
    ]})
    assert [f["target"] for f in fixes_for(row, SITE)["fixes"]] == ["https://www.acme.com/heavy"]


def test_images_are_grouped_by_the_page_they_sit_on():
    row = _row("TECH-14", {"missing_alt": [
        {"src": "https://www.acme.com/img/a.png", "alt": "", "page": "https://www.acme.com/p"},
        {"src": "https://www.acme.com/img/b.png", "alt": "", "page": "https://www.acme.com/p"},
    ]})
    out = fixes_for(row, SITE)
    assert out["total"] == 1
    assert out["fixes"][0]["issue"] == "2 images without alt text: a.png, b.png"


def test_organization_markup_fix_uses_the_company_and_homepage():
    fix = fixes_for(_row("TECH-16", {"organization": False}), SITE)["fixes"][0]
    assert fix["target"] == "https://www.acme.com/"
    assert '"name": "Acme Corp"' in fix["fix"] and '"url": "https://www.acme.com/"' in fix["fix"]


def test_a_placeholder_byline_is_called_out():
    row = _row("ON-12", {"signals": [{"url": "https://www.acme.com/news", "byline": "{{r.PageAuthors}}"},
                                      {"url": "https://www.acme.com/blog", "byline": "By Jane Smith"}]})
    out = fixes_for(row, SITE)
    assert [f["target"] for f in out["fixes"]] == ["https://www.acme.com/news"]
    assert "template placeholder" in out["fixes"][0]["issue"]


def test_site_wide_checks_say_so():
    out = fixes_for(_row("OFF-05", {}), SITE)
    assert out["scope"] == "site" and out["fixes"] == [] and "whole" in out["note"]


def test_an_unmeasured_check_has_no_page_list():
    out = fixes_for(_row("TECH-20", {"pages": [{"url": "x", "ok": False}]}, status="UNKNOWN"), SITE)
    assert out["total"] == 0 and "not measured" in out["note"]


@pytest.mark.parametrize("pid", sorted(BUILDERS))
def test_every_builder_survives_empty_and_malformed_evidence(pid):
    for evidence in ({}, {"pages": "oops", "thin": None, "headings": [1, 2]}, {"pages": [{"url": None}]}):
        out = fixes_for(_row(pid, evidence), SITE)
        assert out["scope"] == "page"
        assert isinstance(out["fixes"], list)


def test_counts_cover_every_page_level_check():
    rows = [_row(pid, {}) for pid in BUILDERS] + [_row("OFF-01", {})]
    counts = counts_for(rows, SITE)
    assert set(counts) == set(BUILDERS)


def test_the_site_comes_from_the_report():
    site = site_from_report({"domain": "www.acme.com", "identity": {"name": "Acme Corp"}})
    assert (site.origin, site.company, site.home) == ("https://www.acme.com", "Acme Corp", "https://www.acme.com/")


# --- the recommendation the model writes -------------------------------------------------------

def test_the_model_is_told_whose_site_it_is_and_which_pages_need_the_change():
    ctx = SimpleNamespace(company_name="Acme Corp", domain="www.acme.com", origin="https://www.acme.com",
                          industry={"label": "IT services"}, pages=[])
    row = _row("ON-19", {"thin": [{"url": "https://www.acme.com/t", "words": 91}], "near_duplicates": []})
    text = engine.site_context(ctx, row)
    assert "Organisation: Acme Corp (www.acme.com)" in text
    assert "Industry profile: IT services" in text
    assert "Pages or items needing a change for this parameter: 1" in text
    assert "https://www.acme.com/t: Thin page: 91 words" in text


def test_the_prompt_carries_the_site_context_and_the_recommendation_rules():
    prompt = build_prompt(parameter_id="ON-19", name="Thin pages", section="on_page", definition="d",
                          metric="m", curated="{}", site_context="Organisation: Acme Corp")
    assert "<SITE_CONTEXT>\nOrganisation: Acme Corp\n</SITE_CONTEXT>" in prompt
    assert "name up to three real URLs" in prompt
    assert "Never mention parameter IDs" in prompt
    assert "does not suit what this organisation is" in prompt


def test_no_context_means_no_empty_block():
    prompt = build_prompt(parameter_id="X", name="n", section="s", definition="d", metric="m", curated="{}")
    assert "SITE_CONTEXT>" not in prompt.split("Rules:")[0]

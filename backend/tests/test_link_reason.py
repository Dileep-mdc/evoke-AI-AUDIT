"""The workbook column that says which pages a check read, and why not the others.

The column exists because the curated-data column shows a sample and nothing said what it was
a sample of. A reader seeing twelve pages listed against a 1,000-page site cannot tell whether
the other 988 were out of scope, unreachable, or silently dropped -- and for several
parameters the honest answer used to be the last one.
"""
from __future__ import annotations

from app.page_selection import SCOPES, link_reason
from app.parameters.engine import load_registry

REGISTRY = load_registry()


def test_every_registry_parameter_has_a_scope_description():
    """A new parameter must not ship with a blank explanation of what it reads."""
    missing = [p["parameter_id"] for p in REGISTRY if p["parameter_id"] not in SCOPES]
    assert not missing, f"no page-selection scope recorded for: {missing}"


def test_it_answers_both_halves_of_the_question():
    text = link_reason("ON-05", {"service_pages_total": 212, "pages_crawled": 1063})

    assert "WHAT THIS CHECK READS" in text
    assert "WHY THE REST ARE NOT INCLUDED" in text
    assert "212 pages were scored, out of 1,063 pages crawled" in text
    assert "service" in text.lower()


def test_pages_lost_by_the_crawl_are_separated_from_pages_out_of_scope():
    """Two different facts. One is a scoping decision the reader should accept; the other is a
    gap in the evidence they should worry about."""
    text = link_reason("ON-20", {
        "pages_assessed": 900, "pages_crawled": 1000,
        "pages_excluded_fetch_failed": 80, "pages_excluded_bot_blocked": 20,
    })

    assert "EXCLUDED BY THE CRAWL, NOT BY THIS CHECK" in text
    assert "80 could not be fetched" in text
    assert "20 returned a bot-challenge page" in text


def test_a_complete_model_pass_says_so():
    text = link_reason("ON-04", {"pages_assessed": 50, "pages_crawled": 50,
                                 "llm": {"population": 50, "assessed": 50, "complete": True}})
    assert "AI READ: all 50 of them." in text


def test_a_capped_or_failed_model_pass_is_not_reported_as_complete():
    """The cap and the failed batches are the two ways a reading can be partial, and both have
    to reach the reader -- a partial pass presented as whole is the original defect."""
    text = link_reason("ON-18", {
        "pages_compared": 1013,
        "llm": {"population": 2012, "assessed": 520, "complete": False,
                "capped_at": 600, "batches_failed": 2},
    })

    assert "AI READ: 520 of 2,012 candidate pairs." in text
    assert "Capped at 600" in text
    assert "2 batch(es) failed" in text
    assert "all 2,012" not in text


def test_off_page_parameters_do_not_claim_to_have_read_your_pages():
    """They query third-party sources. Quoting the crawl's page counts here would imply this
    check opened pages it never touched -- and engine.evaluate attaches those counts to every
    parameter, so the guard has to be explicit."""
    text = link_reason("OFF-03", {"pages_crawled": 1063, "pages_excluded_fetch_failed": 40})

    assert "No pages of your site" in text
    assert "ON THIS SCAN" not in text
    assert "1,063" not in text
    assert "EXCLUDED BY THE CRAWL" not in text


def test_an_unknown_parameter_still_produces_something_readable():
    text = link_reason("XXX-99", {})
    assert "WHAT THIS CHECK READS" in text and "WHY THE REST" in text

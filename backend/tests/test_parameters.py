"""Regression tests for the frozen parameter logic.

Each test here pins a rule that was previously wrong in a way that silently changed a
customer-facing score, so the behaviour cannot drift back without a failing test.
"""
from __future__ import annotations

import random
import re

import pytest

from app.crawler.parse import make_soup
from app.parameters.common import (
    band,
    content_words,
    near_duplicate_pairs,
    top_term_density,
)
from app.parameters.onpage import density_score


# --- keyword density (ON-10) -----------------------------------------------------------

NORMAL_PROSE = (
    "The consultancy helps enterprises modernise legacy platforms. The team works with "
    "clients across healthcare and finance to deliver cloud migration, data engineering and "
    "quality assurance. Our engineers have shipped production systems for more than a decade, "
    "and the approach begins with a discovery workshop that maps the current estate before any "
    "code is written. Results are measured against agreed outcomes. "
) * 4

STUFFED = (
    "web design company web design services web design agency best web design web design "
    "pricing web design portfolio web design team web design process "
) * 12


def test_content_words_drop_function_words():
    words = content_words("The team works with the clients and their outcomes")
    assert "the" not in words and "and" not in words and "with" not in words
    assert {"team", "works", "clients", "outcomes"} <= set(words)


def test_density_separates_normal_prose_from_stuffing():
    """The whole point of ON-10. Counting function words made "the" the peak term on every
    page, pushing normal copy into the stuffing band, so every site scored the same floor."""
    normal, _ = top_term_density(NORMAL_PROSE)
    stuffed, _ = top_term_density(STUFFED)
    assert density_score(normal) == 100
    assert density_score(stuffed) == 45
    assert normal < stuffed


def test_density_peak_term_is_topical_not_grammatical():
    _, top = top_term_density(NORMAL_PROSE)
    assert top[0][0] not in {"the", "and", "with", "that"}


def test_density_is_none_for_pages_too_short_to_judge():
    assert top_term_density("Only a handful of words here.")[0] is None


# --- near-duplicate detection (ON-20 / ON-21) ------------------------------------------

def _brute_force(documents, threshold):
    tokens = [(u, set(re.findall(r"[a-z0-9]{4,}", t.lower()))) for u, t in documents]
    out = []
    for i, (u1, a) in enumerate(tokens):
        for u2, b in tokens[i + 1:]:
            if not a or not b:
                continue
            similarity = len(a & b) / len(a | b)
            if similarity >= threshold:
                out.append((u1, u2, round(similarity, 2)))
    return sorted(out)


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("threshold", [0.5, 0.7, 0.72, 0.9])
def test_prefix_filter_returns_exactly_what_all_pairs_would(seed, threshold):
    """The prefix filter is an optimisation, not an approximation: it must find every pair
    brute force finds. An earlier document-frequency cap quietly dropped all of them."""
    random.seed(seed)
    vocabulary = [f"word{i:03d}" for i in range(random.choice([12, 40, 300]))]
    docs = [(f"u{i}", " ".join(random.choices(vocabulary, k=random.randint(4, 25)))) for i in range(80)]
    docs += [(f"dup{i}", docs[i][1]) for i in range(4)]              # exact duplicates
    docs += [(f"near{i}", docs[i][1] + " extratoken") for i in range(4)]  # near duplicates
    expected = _brute_force(docs, threshold)
    actual = sorted((p["a"], p["b"], p["similarity"]) for p in near_duplicate_pairs(docs, threshold))
    assert actual == expected


def test_identical_pages_are_found():
    docs = [("/a", "cloud migration for healthcare payers"), ("/b", "cloud migration for healthcare payers")]
    assert len(near_duplicate_pairs(docs, 0.72)) == 1


def test_unrelated_pages_are_not_paired():
    docs = [("/a", "cloud migration healthcare payers"), ("/b", "corporate governance annual report filing")]
    assert near_duplicate_pairs(docs, 0.72) == []


def test_duplicate_scan_stays_well_inside_the_parameter_timeout():
    """At MAX_PAGES the old all-pairs comparison was ~3.1M intersections and timed out,
    turning both duplicate checks UNKNOWN on exactly the sites that needed them."""
    import time

    random.seed(1)
    vocabulary = [f"term{i}" for i in range(4000)]
    docs = [(f"https://x/{i}", " ".join(random.choices(vocabulary, k=60))) for i in range(2500)]
    started = time.perf_counter()
    near_duplicate_pairs(docs, 0.72)
    assert time.perf_counter() - started < 15  # PARTIAL of the 25s PARAMETER_TIMEOUT


# --- banding helper --------------------------------------------------------------------

def test_band_picks_first_threshold_met_and_falls_through_to_floor():
    bands = ((0.9, 100.0), (0.5, 70.0))
    assert band(0.95, bands, 10.0) == 100.0
    assert band(0.5, bands, 10.0) == 70.0
    assert band(0.1, bands, 10.0) == 10.0


# --- handler-level scoring shape -------------------------------------------------------

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from app.parameters.engine import load_registry  # noqa: E402
from app.parameters.onpage import _journey_stage_coverage, _collect_thought_leadership_signals, on_13  # noqa: E402
from app.parameters.technical import _collect_author_signals, tech_09, tech_17, tech_18, tech_19, tech_21  # noqa: E402

SPECS = {p["parameter_id"]: p for p in load_registry()}


def _page(url="https://x/p", *, html="", text="", words=0, hreflang=(), schema=(), page_type="other", title=""):
    return SimpleNamespace(
        url=url,
        result=SimpleNamespace(final_url=url, text=html, status_code=200, error=None, ok=True, hops=0, elapsed_ms=100, content=b""),
        soup=make_soup(html) if html else None,
        text=text,
        word_count=words,
        hreflang=list(hreflang),
        schema_blocks=[{"ok": True, "data": s} for s in schema],
        page_type=page_type,
        headings=[],
        links=[],
        images=[],
        dates={},
        title=title,
        meta_description="",
        canonical="",
        blocked_reason="",
    )


def _ctx(pages):
    return SimpleNamespace(origin="https://x", domain="x", pages=pages, homepage=pages[0] if pages else None,
                           company_name="X", brand_terms=["x"], crawl_errors=[])


def _run(coro):
    return asyncio.run(coro)


def test_tech21_absent_hreflang_is_unknown_not_a_free_hundred():
    """Not applicable must be excluded, not passed. Scoring 100 lifted the Technical pillar
    for every single-market site -- the opposite of 'this check does not apply'."""
    out = _run(tech_21(SPECS["TECH-21"], _ctx([_page()])))
    assert out["status"] == "UNKNOWN" and out["score"] is None


def test_tech21_scores_when_hreflang_is_actually_present():
    pages = [_page(hreflang=[{"lang": "en-us", "href": "https://x/en"}, {"lang": "de-de", "href": "https://x/de"}])]
    out = _run(tech_21(SPECS["TECH-21"], _ctx(pages)))
    assert out["status"] == "PASS" and out["score"] == 100


def test_tech09_is_unknown_without_a_rendered_copy():
    """With no browser available the crawl fetches no rendered copy, so TECH-09 has nothing to
    compare and must report UNKNOWN rather than a meaningless score."""
    page = _page("https://x/a", html="<p>hello</p>", words=100)
    ctx = _ctx([page])
    ctx.render = {"decision": "unavailable", "reason": "playwright is not installed"}
    out = _run(tech_09(SPECS["TECH-09"], ctx))
    assert out["score"] is None and out["status"] == "UNKNOWN"


def test_tech09_scores_100_when_the_probe_shows_the_site_needs_no_rendering():
    """A server-rendered site is the PASS case, not an unmeasured one: the probe compared the
    two copies, found them equivalent, and that single comparison is the finding."""
    ctx = _ctx([_page("https://x/a", html="<p>hello</p>", words=100)])
    ctx.render = {
        "decision": "not_needed",
        "probe_url": "https://x/",
        "reason": "the rendered homepage carried 410 words against 400 in the raw HTML",
        "probe": {"raw_words": 400, "rendered_words": 410, "gained_words": 10, "gain_ratio": 0.025},
    }
    out = _run(tech_09(SPECS["TECH-09"], ctx))
    assert out["score"] == 100


def test_tech09_scores_the_share_of_text_that_only_exists_after_javascript():
    """A shell that renders to 1000 words from 100 hides 90% of the page from a crawler that
    does not execute JavaScript, and must score 10 rather than being reported as healthy."""
    page = _page("https://x/a", html="<div id='root'></div>", words=1000)
    page.render = {
        "content_source": "rendered",
        "applied": True,
        "raw_words": 100,
        "rendered_words": 1000,
        "gained_words": 900,
        "gain_ratio": 9.0,
        "raw_headings": 0,
        "rendered_headings": 12,
    }
    ctx = _ctx([page])
    ctx.render = {"decision": "applied", "budget": 40}
    out = _run(tech_09(SPECS["TECH-09"], ctx))
    assert out["score"] == 10
    assert out["evidence"]["compared_pages"] == 1


def test_tech17_faqpage_does_not_satisfy_every_page_type():
    """FAQPage used to satisfy any expected type, so a blog post with only FAQPage counted
    as correctly marked-up Article content."""
    article = _page(page_type="article", schema=[{"@type": "FAQPage"}])
    out = _run(tech_17(SPECS["TECH-17"], _ctx([article])))
    assert out["score"] == 0


def test_tech17_accepts_real_subtypes():
    article = _page(page_type="article", schema=[{"@type": "BlogPosting"}])
    out = _run(tech_17(SPECS["TECH-17"], _ctx([article])))
    assert out["score"] == 100


def test_tech19_with_no_markup_is_unknown_not_forty():
    """Nothing to validate is not '40% valid'. Absence is already scored by TECH-16/17."""
    out = _run(tech_19(SPECS["TECH-19"], _ctx([_page()])))
    assert out["status"] == "UNKNOWN" and out["score"] is None


# --- event-loop-blocking per-page loops now run via asyncio.to_thread ------------------
#
# TECH-18, ON-13 and ON-18's heuristic pass were measured at 74-101s each on a 1057-page
# scan, with no `await` inside their per-page loop -- meaning every other parameter
# scheduled in the same PARAMETER_CONCURRENCY batch sat frozen behind them too, not just
# these three. Extracted into plain helpers run through asyncio.to_thread(); these tests
# pin that the extraction changed *where* the loop runs, not what it computes.

def test_collect_author_signals_matches_the_original_loop_shape():
    schema_page = _page("https://x/a", schema=[{"@type": "Person", "name": "Jane Roe", "jobTitle": "CTO"}])
    visible_page = _page("https://x/b", html='<div class="byline">By John Doe</div>')
    plain_page = _page("https://x/c")
    signals = _collect_author_signals([schema_page, visible_page, plain_page])
    assert {"url": "https://x/a", "item": {"name": "Jane Roe", "jobTitle": "CTO", "@type": "Person"}} in signals
    assert any(s.get("url") == "https://x/b" and s.get("visible") == "By John Doe" for s in signals)
    assert len(signals) == 2, "the page with no author signal must not appear"


def test_tech18_runs_the_extracted_helper_through_a_thread_and_scores_the_same():
    pages = [_page("https://x/a", schema=[{"@type": "Person", "name": "Jane Roe", "jobTitle": "CTO"}])]
    out = _run(tech_18(SPECS["TECH-18"], _ctx(pages)))
    assert out["score"] == 95, "a jobTitle-carrying author signal must still score 95 after the move to a thread"


def test_collect_thought_leadership_signals_matches_the_original_loop_shape():
    visible_page = _page("https://x/a", html='<div class="author">Jane Roe</div>')
    schema_page = _page("https://x/b", schema=[{"@type": "Person", "name": "John Doe"}])
    plain_page = _page("https://x/c")
    signals = _collect_thought_leadership_signals([visible_page, schema_page, plain_page])
    assert {"url": "https://x/a", "visible_author": True, "byline": "Jane Roe"} in signals
    assert {"url": "https://x/b", "schema": "John Doe", "byline": "John Doe"} in signals
    assert len(signals) == 2


def test_on13_runs_the_extracted_helper_through_a_thread_and_scores_the_same():
    pages = [_page("https://x/a", html='<div class="author">Jane Roe</div>')]
    out = _run(on_13(SPECS["ON-13"], _ctx(pages)))
    assert out["score"] == 80, "a real author signal must still score 80 after the move to a thread"


def test_journey_stage_coverage_matches_the_original_loop_shape():
    journey = {"awareness": ("guide",), "decision": ("contact",)}
    pages = [
        _page("https://x/a", title="A Guide to Widgets", text="guide"),
        _page("https://x/b", title="Contact Us", text="contact"),
    ]
    covered = _journey_stage_coverage(pages, journey)
    assert covered["awareness"] == ["https://x/a"]
    assert covered["decision"] == ["https://x/b"]
    assert "comparison" not in covered, "a stage with no matching page must not appear at all"

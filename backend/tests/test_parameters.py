"""Regression tests for the frozen parameter logic.

Each test here pins a rule that was previously wrong in a way that silently changed a
customer-facing score, so the behaviour cannot drift back without a failing test.
"""
from __future__ import annotations

import random
import re

import pytest

from app.parameters.common import (
    band,
    content_words,
    near_duplicate_pairs,
    top_term_density,
)
from app.parameters.onpage import density_score


# --- keyword density (ON-09 / ON-10) ---------------------------------------------------

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
from app.parameters.onpage import on_16  # noqa: E402
from app.parameters.technical import tech_09, tech_17, tech_19, tech_21  # noqa: E402

SPECS = {p["parameter_id"]: p for p in load_registry()}


def _page(url="https://x/p", *, html="", text="", words=0, hreflang=(), schema=(), page_type="other"):
    return SimpleNamespace(
        url=url,
        result=SimpleNamespace(final_url=url, text=html, status_code=200, error=None, ok=True, hops=0, elapsed_ms=100, content=b""),
        soup=None,
        text=text,
        word_count=words,
        hreflang=list(hreflang),
        schema_blocks=[{"ok": True, "data": s} for s in schema],
        page_type=page_type,
        headings=[],
        links=[],
        images=[],
        dates={},
        title="",
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


def test_tech09_caps_each_page_so_one_page_cannot_mask_another():
    """Raw HTML often holds more words than rendered text, so an uncapped page exceeds 100
    and, in the mean, hides a genuinely JS-only page."""
    rich = _page("https://x/a", html="<p>" + " ".join(["word"] * 900) + "</p>", words=100)   # ratio ~900%
    js_only = _page("https://x/b", html="<div></div>", words=100)                            # ratio ~0%
    out = _run(tech_09(SPECS["TECH-09"], _ctx([rich, js_only])))
    assert out["score"] <= 55, "a 900% page must not average away a 0% page"


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


def test_on16_cannot_exceed_its_own_denominator():
    """Reviewer signals were counted site-wide but divided by article/service pages only,
    so bylines elsewhere pushed the ratio past 100% before it was clamped."""
    pages = [
        _page("https://x/a", text="Reviewed by Dr Jane Roe", page_type="article"),
        _page("https://x/b", text="Reviewed by Dr Jane Roe", page_type="about"),
        _page("https://x/c", text="no reviewer here", page_type="service"),
    ]
    out = _run(on_16(SPECS["ON-16"], _ctx(pages)))
    assert out["score"] == 50, "1 of the 2 article/service pages has a reviewer"


def test_on16_is_unknown_when_no_page_makes_an_expertise_claim():
    out = _run(on_16(SPECS["ON-16"], _ctx([_page(page_type="utility")])))
    assert out["status"] == "UNKNOWN"

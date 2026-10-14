"""On-Page and Off-Page calculation fixes from the October 2026 formula audit.

Each test pins a case where the score did not follow the stated formula: a domain name deciding
which pages are service pages, substring matches crediting brands and places that were never
named, model replies dropped over punctuation, repeated sentences counted once per page, and
review checks failing a manufacturer for review sites its industry does not use.
"""
import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from app.llm.batch import Coverage
from app.parameters import offpage, offpage_tools, onpage, platforms, working_offpage, working_onpage


def _run(coro):
    return asyncio.run(coro)


def _spec(pid, section="on_page"):
    return {"parameter_id": pid, "section": section, "name": pid, "weight": 1.0}


def _page(url, *, text="", title="", page_type="other", html="", headings=(), links=()):
    return SimpleNamespace(
        url=url, title=title, text=text, word_count=len(text.split()), page_type=page_type,
        soup=BeautifulSoup(html, "html.parser") if html else None, schema_blocks=[], images=[],
        headings=list(headings), links=list(links), dates={}, hreflang=[], canonical="", blocked_reason="", render={},
        result=SimpleNamespace(final_url=url, text=html, ok=True, status_code=200, error=None, hops=0, elapsed_ms=10, content=b""),
    )


def _ctx(pages, **extra):
    values = dict(origin="https://acmesolutions.com", domain="acmesolutions.com", pages=pages,
                  homepage=pages[0] if pages else None, company_name="Acme", brand_terms=["acme", "acmesolutions.com"],
                  crawl_errors=[], render={})
    values.update(extra)
    return SimpleNamespace(**values)


def _judge(monkeypatch, verdicts):
    async def fake(items, prompt_builder, **kwargs):
        return verdicts, Coverage(population=len(items), submitted=len(items), assessed=len(verdicts), batches=1)
    monkeypatch.setattr(onpage, "judge_all", fake)


# --- matching on whole words -----------------------------------------------------------------

@pytest.mark.parametrize("text, terms, expected", [
    ("Top 10 survey firms", ["ey"], False),          # "ey" inside "survey"
    ("EY tops the list of advisers", ["ey"], True),
    ("Pumps for business customers", ["us"], False),  # "us" inside "business"
    ("Offices in Dallas, Texas", ["dallas"], True),
    ("Acme Pumps on LinkedIn", ["acme pumps"], True),
])
def test_compare_entities_matches_whole_words_only(text, terms, expected):
    assert offpage_tools.compare_entities(text, terms) is expected


# --- OFF-05 / OFF-06: no review sites for the industry is N/A, not a 0 ------------------------

@pytest.mark.parametrize("pid, handler", [("OFF-05", offpage.off_05), ("OFF-06", offpage.off_06)])
def test_review_checks_do_not_apply_when_the_industry_lists_no_review_sites(monkeypatch, pid, handler):
    async def no_search(query, num=10):
        raise AssertionError("a check with no sites to look for must not search")
    monkeypatch.setattr(offpage_tools, "google_search", no_search)
    lists = platforms.resolve("manufacturing")
    assert not lists["review"]
    ctx = SimpleNamespace(company_name="Acme Pumps Inc", domain="acmepumps.com", origin="https://acmepumps.com",
                          pages=[], platforms=lists)
    row = _run(handler(_spec(pid, "off_page"), ctx))
    assert row["status"] == "N/A" and row["score"] is None
    assert "review" in row["evidence"]["does_not_apply"]


# --- OFF-06: dates that are not real, or are in the future, are ignored ----------------------

def test_review_dates_skip_impossible_and_future_dates():
    items = [{"url": "https://g2.com/a", "date": "2025-02-30"},   # no such day
             {"url": "https://g2.com/b", "date": "2030-01-01"},   # in the future
             {"url": "https://g2.com/c", "date": "2026-06-01"}]
    out = offpage_tools.extract_dates_and_rankings(items, today=date(2026, 10, 14))
    assert out["newest_date"] == "2026-06-01"
    assert out["age_days"] == (date(2026, 10, 14) - date(2026, 6, 1)).days


# --- OFF-08 / OFF-05 working text ------------------------------------------------------------

def test_off08_working_quotes_the_query_actually_run():
    text, score = working_offpage.off_08({"in_generic_results": True, "topic": "orthopedic organization",
                                          "query": "best orthopedic organization"})
    assert score == 100 and "'best orthopedic organization'" in text and "companies" not in text


def test_an_empty_site_list_is_not_replaced_by_the_default_one():
    assert working_offpage._checked({"platforms_checked": []}, "review") == ()


# --- the URL path, not the domain, decides page kind -----------------------------------------

def test_on05_domain_name_does_not_make_every_page_a_service_page():
    pages = [_page("https://acmesolutions.com/", page_type="home", text="Welcome"),
             _page("https://acmesolutions.com/careers", text="Join us")]
    row = _run(onpage.on_05(_spec("ON-05"), _ctx(pages)))
    assert row["evidence"]["service_pages_total"] == 1   # the homepage stands in; careers is not a service page


def test_on16_lists_only_the_stages_it_found_and_not_from_the_domain():
    pages = [_page("https://acmesolutions.com/contact", title="Contact", text="Contact us today")]
    row = _run(onpage.on_16(_spec("ON-16"), _ctx(pages)))
    assert row["evidence"]["covered"] == ["decision"]
    assert row["score"] == pytest.approx(100 / 6, abs=0.1)


# --- ON-20: the brand must be in the copy, and places are whole words ------------------------

def test_on20_brand_is_not_read_from_the_url_and_usa_is_not_in_thousand(monkeypatch):
    monkeypatch.setattr(onpage, "derive_site_categories", lambda ctx, **kw: ["payroll"])
    monkeypatch.setattr(onpage, "derive_site_geographies", lambda ctx, **kw: [])
    pages = [_page("https://acmesolutions.com/payroll", text="Our payroll software handles a thousand stores.")]
    row = _run(onpage.on_20(_spec("ON-20"), _ctx(pages)))
    assert row["evidence"]["pages"][0] == {"url": pages[0].url, "brand": False, "category": True, "geo": False}
    assert row["score"] == 0


# --- ON-14: each claim once, and "sourced" means sourced --------------------------------------

def test_on14_counts_a_repeated_footer_claim_once():
    footer = "Trusted by 500+ enterprises across every region we serve today."
    pages = [_page(f"https://acmesolutions.com/p{i}", text=footer) for i in range(20)]
    pages.append(_page("https://acmesolutions.com/report",
                       text="According to Gartner, 40% of firms will move their payroll to the cloud."))
    row = _run(onpage.on_14(_spec("ON-14"), _ctx(pages)))
    assert row["evidence"]["claims_total"] == 2
    assert row["score"] == 50


def test_on14_short_link_text_and_word_fragments_do_not_count_as_a_source():
    page = _page("https://acmesolutions.com/a", text="Our digital team outsourced nothing and cut costs by 40% this year.",
                 links=[{"text": "IT", "href": "/it"}])
    row = _run(onpage.on_14(_spec("ON-14"), _ctx([page])))
    assert row["score"] == 0


# --- ON-10: an unreadable reply is skipped, not fatal ----------------------------------------

@pytest.mark.parametrize("value, expected", [
    (5, 5), (12, 8), (-1, 0), (["problem", "approach"], 2), ("5/8", 5), ("none", None), (None, None), (True, None),
])
def test_on10_reads_the_points_covered_in_any_shape(value, expected):
    assert onpage._points_covered(value, 8) == expected


# --- ON-12: an empty author element names nobody, and a person counts once per page ---------

def test_on12_empty_author_element_is_not_a_signal():
    page = _page("https://acmesolutions.com/blog", html='<div class="author"></div><p>Body</p>', text="Body")
    row = _run(onpage.on_12(_spec("ON-12"), _ctx([page])))
    assert row["evidence"]["author_signals_total"] == 0 and row["score"] == 30


def test_on12_visible_byline_and_markup_for_one_person_count_once():
    page = _page("https://acmesolutions.com/blog", html='<span class="byline">Jane Doe</span>', text="Body")
    page.schema_blocks = [{"ok": True, "data": {"@type": "Article", "author": {"@type": "Person", "name": "Jane Doe",
                                                                              "jobTitle": "CTO"}}}]
    signals = onpage._collect_thought_leadership_signals([page])
    assert len(signals) == 1 and signals[0]["byline"] == "Jane Doe - CTO"


# --- ON-01 / ON-07: model replies are matched back tolerantly --------------------------------

def _headings(*texts):
    return [{"level": 2, "text": t} for t in texts]


def test_on01_reply_without_the_question_mark_still_counts(monkeypatch):
    page = _page("https://acmesolutions.com/services/payroll", page_type="service",
                 headings=_headings("How much does payroll outsourcing cost?", "Our payroll delivery team"))
    _judge(monkeypatch, [{"heading": "how much does payroll outsourcing cost", "buyer_question": True},
                         {"heading": "Our payroll delivery team", "buyer_question": False}])
    row = _run(onpage.on_01(_spec("ON-01"), _ctx([page])))
    assert row["evidence"]["method"] == "llm"
    assert row["evidence"]["question_headings"] == 1 and row["evidence"]["question_share"] == 0.5


def test_on01_replies_matching_nothing_keep_the_keyword_reading(monkeypatch):
    page = _page("https://acmesolutions.com/services/payroll", page_type="service",
                 headings=_headings("How much does payroll outsourcing cost?", "Our payroll delivery team"))
    _judge(monkeypatch, [{"heading": "Heading 1: something else", "buyer_question": True}])
    row = _run(onpage.on_01(_spec("ON-01"), _ctx([page])))
    assert row["evidence"]["method"] == "heuristic" and "llm_unavailable" in row["evidence"]
    assert row["evidence"]["question_headings"] == 1   # the pattern count, consistent with the score


def test_on07_trailing_slash_in_the_reply_does_not_fail_the_check(monkeypatch):
    page = _page("https://acmesolutions.com/pricing", title="Pricing", text="Plans and pricing",
                 html="<table><tr><td>Plan</td></tr></table>")
    _judge(monkeypatch, [{"url": "https://acmesolutions.com/pricing/", "evaluation_intent": True}])
    row = _run(onpage.on_07(_spec("ON-07"), _ctx([page])))
    assert row["status"] == "PASS" and row["score"] == 100


# --- working text: a no-data FAIL reads 0, and ON-04 averages what the score averages --------

@pytest.mark.parametrize("fn", ["on_01", "on_02", "on_03", "on_07", "on_14"])
def test_working_for_a_no_data_row_says_0(fn):
    ev = {"no_data": True, "no_data_reason": "nothing to rate", "eligible_headings": 0, "answers": [],
          "concepts_found_on_site": 0, "eligible_pages_total": 0, "claims": 0}
    assert getattr(working_onpage, fn)(ev) == ("Nothing to rate, so it scores 0.", 0.0)


def test_on04_working_leaves_out_pages_without_an_opening():
    ev = {"method": "heuristic", "pages": [{"url": "a", "opening": "Acme runs payroll for 400 firms.", "score": 85},
                                           {"url": "b", "opening": "", "score": 35}]}
    assert working_onpage.on_04(ev)[1] == 85

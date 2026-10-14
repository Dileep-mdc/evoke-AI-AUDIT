"""Related checks must agree with each other, and a check that does not apply must say so.

From a review of the AAOS audit (a US, English-only medical association):
  * TECH-21 hreflang failed at 0 on a single-market site, where no hreflang is the correct setup.
  * ON-06 FAQ schema validity scored 50 on a site with no FAQ markup at all.
  * TECH-19 "markup validates" scored 25 next to TECH-16/17 at 0, without saying what it validated.
  * TECH-18 author markup scored 80 on styled bylines while ON-12 found no named person in them.
  * OFF-04 scored CMMI/ISO certification claims for a medical association.
"""
import asyncio
from types import SimpleNamespace

from app.parameters import crosschecks, offpage
from app.parameters.engine import load_registry
from app.parameters.onpage import on_06
from app.parameters.scoring import category_score, prioritize
from app.parameters.technical import tech_18, tech_19

from tests.test_parameters import _ctx, _page

SPECS = {p["parameter_id"]: p for p in load_registry()}


def _run(coro):
    return asyncio.run(coro)


def _faq_page(url, *, heading=True, schema=None, text=""):
    page = _page(url, text=text, words=400, schema=[schema] if schema else (), page_type="service")
    page.headings = [{"level": 2, "text": "Frequently asked questions"}] if heading else []
    return page


FAQ = {"@type": "FAQPage", "mainEntity": [
    {"@type": "Question", "name": "What is an enclosure?", "acceptedAnswer": {"@type": "Answer", "text": "A box."}}]}


# --- ON-06 -------------------------------------------------------------------------------

def test_on06_visible_faqs_without_markup_score_zero():
    pages = [_faq_page(f"https://x/s{i}") for i in range(3)]
    out = _run(on_06(SPECS["ON-06"], _ctx(pages)))
    assert out["score"] == 0 and out["status"] == "FAIL"
    assert "FAQPage structured data" in out["recommendation"]


def test_on06_markup_matching_the_page_scores_full():
    page = _faq_page("https://x/s", schema=FAQ, text="Frequently asked questions. What is an enclosure? A box.")
    out = _run(on_06(SPECS["ON-06"], _ctx([page])))
    assert out["score"] == 100 and out["evidence"]["with_consistent_answers"] == 1


def test_on06_markup_the_page_does_not_show_scores_half():
    page = _faq_page("https://x/s", heading=False, schema=FAQ, text="Product overview only.")
    out = _run(on_06(SPECS["ON-06"], _ctx([page])))
    assert out["score"] == 50 and out["evidence"]["with_valid_schema"] == 1


def test_on06_does_not_take_a_footer_faq_link_for_an_faq():
    page = _faq_page("https://x/s", heading=False, text="Home About FAQ Contact")
    out = _run(on_06(SPECS["ON-06"], _ctx([page])))
    assert out["evidence"].get("faq_pages_total") == 0  # nothing that looks like an FAQ section


# --- TECH-18 -------------------------------------------------------------------------------

def test_tech18_styled_bylines_are_not_author_markup():
    page = _page("https://x/a", html='<div class="byline">By the editorial team</div>')
    out = _run(tech_18(SPECS["TECH-18"], _ctx([page])))
    assert out["score"] == 0 and out["status"] == "FAIL"


def test_tech18_an_organization_author_is_not_a_person():
    page = _page("https://x/a", schema=[{"@type": "Article", "author": {"@type": "Organization", "name": "AAOS"}}])
    assert _run(tech_18(SPECS["TECH-18"], _ctx([page])))["score"] == 0


def test_tech18_person_markup_still_scores():
    page = _page("https://x/a", schema=[{"@type": "Article", "author": {"@type": "Person", "name": "Jane Roe", "jobTitle": "MD"}}])
    assert _run(tech_18(SPECS["TECH-18"], _ctx([page])))["score"] == 95


# --- TECH-19 -------------------------------------------------------------------------------

def test_tech19_says_what_markup_it_validated():
    page = _page("https://x/a", schema=[{"@type": "BreadcrumbList"}, {"@type": "WebSite"}])
    page.schema_blocks.append({"ok": False, "error": "Expecting ',' delimiter"})
    out = _run(tech_19(SPECS["TECH-19"], _ctx([page])))
    ev = out["evidence"]
    assert ev["types_found"] == ["BreadcrumbList", "WebSite"]
    assert [b["valid"] for b in ev["validated_blocks"]] == [True, True, False]


def test_tech19_is_explained_next_to_missing_organization_and_page_type_markup():
    rows = [{"parameter_id": "TECH-19", "score": 25.0, "status": "FAIL", "recommendation": "Fix invalid JSON-LD.",
             "evidence": {"blocks": 4, "types_found": ["WebSite"]}},
            {"parameter_id": "TECH-16", "score": 0.0, "status": "FAIL", "evidence": {}},
            {"parameter_id": "TECH-17", "score": 0.0, "status": "FAIL", "evidence": {}}]
    crosschecks.reconcile(rows)
    note = rows[0]["evidence"]["cross_check"]
    assert "WebSite" in note and "TECH-16" in note and "TECH-17" in note


def test_the_tech19_note_names_only_the_check_that_is_missing():
    rows = [{"parameter_id": "TECH-19", "score": 100.0, "status": "PASS", "recommendation": None,
             "evidence": {"blocks": 1, "types_found": ["Organization", "ProductGroup"]}},
            {"parameter_id": "TECH-16", "score": 100.0, "status": "PASS", "evidence": {}},
            {"parameter_id": "TECH-17", "score": 0.0, "status": "FAIL", "evidence": {}}]
    crosschecks.reconcile(rows)
    note = rows[0]["evidence"]["cross_check"]
    assert "TECH-17" in note and "TECH-16" not in note and "that check scores 0" in note


def test_tech18_and_on12_read_the_same_nested_authors(monkeypatch):
    """F5 puts its authors inside each BlogPosting; TECH-18 saw 1,439 and ON-12 saw none."""
    from app.parameters.onpage import _collect_thought_leadership_signals
    from app.parameters.technical import _collect_author_signals
    post = {"@type": "BlogPosting", "author": {"@type": "Person", "name": "Jane Roe", "jobTitle": "Engineer"}}
    page = _page("https://x/blog/a", schema=[post], words=400)
    assert [a["item"]["name"] for a in _collect_author_signals([page])] == ["Jane Roe"]
    assert [s["byline"] for s in _collect_thought_leadership_signals([page])] == ["Jane Roe - Engineer"]


def test_tech18_and_on12_are_reconciled():
    rows = [{"parameter_id": "TECH-18", "score": 80.0, "status": "PARTIAL", "evidence": {}},
            {"parameter_id": "ON-12", "score": 30.0, "status": "FAIL", "evidence": {"llm": {"named_people": 0}}}]
    crosschecks.reconcile(rows)
    assert "ON-12" in rows[0]["evidence"]["cross_check"]


# --- OFF-04 -------------------------------------------------------------------------------

def _off_ctx(industry_key):
    page = SimpleNamespace(text="We are ISO 27001 certified and work with CMMI.", page_type="other", title="",
                           schema_blocks=[], result=SimpleNamespace(final_url="https://x/"))
    return SimpleNamespace(company_name="X", domain="x", origin="https://x", pages=[page],
                           industry={"key": industry_key})


def test_off04_is_not_applicable_outside_it(monkeypatch):
    for key in ("healthcare", "manufacturing", "retail", "finance"):
        out = _run(offpage.off_04(SPECS["OFF-04"], _off_ctx(key)))
        assert out["status"] == "N/A" and out["score"] is None, key
        assert "IT-services credentials" in out["evidence"]["summary"]


def test_off04_still_runs_for_it_and_for_an_unclear_industry(monkeypatch):
    async def blocked(url, ctx=None):
        return SimpleNamespace(status_code=403, ok=False, text="", error=None)
    from app.crawler import google_search as gs
    from app.parameters import offpage_tools

    async def no_results(query, num=10):
        return gs.SearchResult(query, ok=True, items=[], status=200)
    monkeypatch.setattr(offpage_tools, "crawl_page", blocked)
    monkeypatch.setattr(offpage_tools, "google_search", no_results)
    for key in ("it_services", "software", "general"):
        out = _run(offpage.off_04(SPECS["OFF-04"], _off_ctx(key)))
        assert out["status"] != "N/A" and out["score"] == 55, key


# --- N/A is left out, not failed ------------------------------------------------------------------

def test_an_na_row_is_left_out_of_the_pillar_and_the_issues():
    rows = [{"parameter_id": "TECH-01", "section": "technical", "status": "PASS", "score": 100.0, "weight": 1.0},
            {"parameter_id": "TECH-21", "section": "technical", "status": "N/A", "score": None, "weight": 1.0}]
    assert category_score(rows, "technical") == 100.0
    assert not [i for i in prioritize(rows) if i["parameter_id"] == "TECH-21"]

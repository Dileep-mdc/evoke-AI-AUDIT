"""Every on-page parameter scores from the whole population, and says what it was.

The audit used to hand the model a capped sample -- 12 pages, 24 excerpts, 20 pairs -- and
then report the resulting ratio as the site's score, with nothing in the evidence recording
what the sample was drawn from. On one real 750-page scan that meant ON-04 scored the site
from its first twelve URLs in crawl order, ON-20 scored 1,345 duplicate pairs after checking
20 of them, and seven parameters sent the grader a numerator with no denominator at all.

These tests pin the two properties that fixes it: the classifier sees every candidate, and
the evidence always carries the population next to the sample.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import app.llm.batch as batch
import app.llm.client as llm_client
import app.llm.scoring as llm_scoring
from app.llm.client import LLMResult
from app.parameters.engine import apply_judgement, load_registry
from app.parameters.onpage import (
    _definition_windows,
    on_01,
    on_04,
    on_05,
    on_08,
    on_17,
    on_20,
    on_22,
)

SPECS = {p["parameter_id"]: p for p in load_registry()}


def _run(coro):
    return asyncio.run(coro)


def _page(url="https://x/p", *, text="", words=0, title="", headings=(), page_type="other", soup=None):
    return SimpleNamespace(
        url=url,
        result=SimpleNamespace(final_url=url, text="", status_code=200, error=None, ok=True, hops=0, elapsed_ms=10, content=b""),
        soup=soup,
        text=text,
        word_count=words or len(text.split()),
        hreflang=[],
        schema_blocks=[],
        page_type=page_type,
        headings=[dict(h) for h in headings],
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
                           company_name="X", brand_terms=["acme"], crawl_errors=[])


@pytest.fixture
def batched_model(monkeypatch):
    """Stub the classifier and record every batch it was given.

    Returns a list that ends up holding one entry per model call, so a test can assert on
    how the work was split as well as on how much of it happened.
    """
    def install(reply_for):
        calls: list[str] = []

        async def fake_judge(prompt, *, system="", json_mode=True, **kwargs):
            calls.append(prompt)
            payload = reply_for(prompt)
            return LLMResult(ok=True, text=json.dumps(payload), parsed=payload, elapsed_ms=1)

        monkeypatch.setattr(batch, "judge", fake_judge)
        monkeypatch.setattr(llm_client, "ENABLE_LLM_SCORING", True)
        return calls
    return install


def _echo(key, value=True):
    """A reply that answers every item in the batch, so coverage comes out complete."""
    def build(prompt: str):
        # The prompt embeds the batch as a JSON array; count its entries to answer each one.
        start = prompt.index("[", prompt.index("JSON array"))
        depth, end = 0, start
        for i, ch in enumerate(prompt[start:], start):
            depth += (ch == "[") - (ch == "]")
            if depth == 0:
                end = i
                break
        items = json.loads(prompt[start:end + 1])
        return {"results": [{**item, key: value} for item in items]}
    return build


# --- the batching primitive -------------------------------------------------------------

def test_judge_all_covers_every_item_across_batches(batched_model):
    calls = batched_model(_echo("useful"))
    items = [{"url": f"https://x/{i}", "items": ["a", "b"]} for i in range(95)]

    verdicts, coverage = _run(batch.judge_all(items, lambda b: f"JSON array:\n{json.dumps(b)}", batch_size=40))

    assert len(calls) == 3, "95 items at 40 per batch should be three calls"
    assert coverage.population == 95
    assert coverage.assessed == 95
    assert coverage.complete
    assert len(verdicts) == 95


def test_judge_all_reports_an_incomplete_pass_rather_than_hiding_it(monkeypatch):
    """A failed batch has to be visible. Silently returning the verdicts that did come back
    would report a ratio measured over an unknown fraction of the site as if it were whole."""
    async def flaky(prompt, *, system="", json_mode=True, **kwargs):
        if "https://x/0" in prompt:
            return LLMResult(ok=False, error="rate limited")
        return LLMResult(ok=True, parsed={"results": [{"ok": True}]}, elapsed_ms=1)

    monkeypatch.setattr(batch, "judge", flaky)
    items = [{"url": f"https://x/{i}"} for i in range(80)]

    _, coverage = _run(batch.judge_all(items, lambda b: json.dumps(b), batch_size=40))

    assert coverage.batches_failed == 1
    assert not coverage.complete
    assert coverage.error == "rate limited"
    assert coverage.as_evidence()["complete"] is False


def test_judge_all_with_no_items_is_not_a_complete_pass():
    """Nothing judged is not the same as everything judged, and `complete` must not say it is."""
    _, coverage = _run(batch.judge_all([], lambda b: json.dumps(b)))
    assert not coverage.complete and coverage.population == 0


# --- handlers now read the whole site ----------------------------------------------------

def test_on04_reads_every_page_not_the_first_twelve(batched_model):
    """ON-04 used to open with `for page in ctx.pages[:12]`, which is not a sample of the
    site -- it is whatever the crawler happened to fetch first."""
    calls = batched_model(_echo("leads_with_substance"))
    pages = [_page(f"https://x/{i}", text="We build custom acme software for regulated insurers and banks. " * 4)
             for i in range(50)]

    out = _run(on_04(SPECS["ON-04"], _ctx(pages)))

    assert out["evidence"]["pages_assessed"] == 50
    assert out["evidence"]["llm"]["population"] == 50
    assert out["evidence"]["llm"]["assessed"] == 50
    assert out["evidence"]["llm"]["complete"] is True
    assert len(calls) == 2, "50 openings should batch into two calls, not be truncated to one sample"


def test_on20_confirms_every_candidate_pair(batched_model):
    """The widest gap found in the audit: 1,345 title-overlap pairs, 20 shown to the model,
    18 of those 20 rejected -- and all 1,325 unseen pairs still counted against the site."""
    calls = batched_model(_echo("competing", value=False))  # the model rejects every pair
    # Titles overlapping enough to be candidates, but not identical (identical titles are
    # filtered out before the model sees them).
    pages = [_page(f"https://x/{i}", title=f"enterprise application development services consulting team {i}",
                   text="t") for i in range(12)]

    out = _run(on_20(SPECS["ON-20"], _ctx(pages)))

    ev = out["evidence"]
    assert ev["llm"]["candidate_pairs"] == 66, "every pair of the 12 pages is a candidate"
    assert ev["llm"]["complete"] is True
    assert ev["llm"]["assessed"] == 66, "and every candidate was judged, not the first twenty"
    assert len(calls) == 2, "66 pairs at 40 per batch is two calls"
    assert ev["llm"]["confirmed_competing"] == 0
    # Nothing was confirmed, so nothing counts against the site and it scores clean.
    assert ev["pages_affected"] == 0
    assert out["score"] == 100


def test_on01_keeps_the_count_and_the_share_consistent(batched_model):
    """The evidence used to carry the pattern count beside the model's share -- one real scan
    shipped `question_headings: 385` next to `question_share: 0.0`, and the grader wrote
    "only 0 of them are classified as buyer questions" from it."""
    batched_model(_echo("buyer_question", value=False))
    headings = [{"level": 2, "text": f"How do we handle case {i}?"} for i in range(20)]
    pages = [_page("https://x/s", page_type="service", headings=headings, text="t")]

    out = _run(on_01(SPECS["ON-01"], _ctx(pages)))

    ev = out["evidence"]
    assert ev["method"] == "llm"
    assert ev["question_headings_by_pattern"] == 20, "the pattern count is kept, separately"
    assert ev["question_headings"] == 0, "the headline count follows the model, like the share"
    assert ev["question_share"] == 0.0
    assert ev["question_headings"] / ev["eligible_headings"] == ev["question_share"]


def test_on01_judges_each_distinct_heading_once_but_weights_by_occurrence(batched_model):
    """A site-wide heading is one editorial decision, not N of them.

    Judging every occurrence meant asking the model about "The Evoke Edge" ~2,000 times, at
    ~2,000x the cost, and weighting that one heading as 2,000 data points in the share. The
    share still has to describe the site's headings, so occurrences are counted -- but each
    distinct heading is only sent once.
    """
    calls = batched_model(_echo("buyer_question", value=False))
    menu = {"level": 2, "text": "The Acme Edge platform"}          # repeated site-wide
    unique = lambda i: {"level": 2, "text": f"How long does rollout {i} take?"}
    pages = [_page(f"https://x/s{i}", page_type="service", text="t", headings=[menu, unique(i)])
             for i in range(60)]

    out = _run(on_01(SPECS["ON-01"], _ctx(pages)))

    ev = out["evidence"]
    assert ev["eligible_headings"] == 120, "occurrences are still counted"
    assert ev["distinct_headings"] == 61, "but only 61 distinct strings exist"
    assert ev["llm"]["population"] == 61, "and only those were sent to the model"
    assert ev["llm"]["heading_occurrences_counted"] == 120, "weighted back up to the real total"
    assert len(calls) == 2, "61 distinct headings, not 120 occurrences, decides the call count"


def test_no_handler_imposes_its_own_ceiling_on_what_gets_scored():
    """The rule this change exists to enforce, checked against the source itself.

    Handlers used to carry a dozen numbers each deciding how much of a site was looked at --
    `rows[:16]`, `eligible[:12]`, `urls[:15]`, a 600-pair ceiling. Every one was chosen rather
    than derived, and together they were the real answer to "how much of my site did you
    audit". Batch SIZE and per-request budgets still exist, but they live in config and bound
    a request, not a measurement.
    """
    import re
    from pathlib import Path

    handlers = Path(__file__).resolve().parents[1] / "app" / "parameters"
    offenders = []
    for name in ("onpage.py", "technical.py"):
        for num, line in enumerate(( handlers / name).read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#")[0]
            # A slice on a collection that feeds evidence or a model call. Character slices on
            # a single string (excerpt[:400]) are a different thing and are not matched here.
            for match in re.finditer(r"\b(rows|pairs|urls|links|items|pages|eligible|candidates|samples|signals|patterns|thin|dups|authors|tags|mismatches|images|major|verdicts)\[:\s*\d+\s*\]", code):
                offenders.append(f"{name}:{num}: {match.group(0)}")
    assert not offenders, "handler-level ceilings on what gets scored:\n  " + "\n  ".join(offenders)


def test_on20_judges_every_candidate_pair_with_no_ceiling(batched_model):
    """ON-20's candidates grow faster than the page count, which is why it carried a ceiling.
    The ceiling is gone: batches are packed by size, so a large pair set costs more requests
    rather than losing pairs."""
    calls = batched_model(_echo("competing", value=False))
    pages = [_page(f"https://x/{i}", title=f"enterprise application development services team {i}", text="t")
             for i in range(12)]

    out = _run(on_20(SPECS["ON-20"], _ctx(pages)))

    llm = out["evidence"]["llm"]
    assert llm["candidate_pairs"] == 66
    assert llm["population"] == 66
    assert llm["assessed"] == 66, "every pair judged, none dropped to a ceiling"
    assert llm["complete"] is True
    assert "capped_at" not in llm
    assert len(calls) >= 2


# --- evidence always carries the denominator ---------------------------------------------

def test_on05_reports_the_service_page_total_not_just_a_sample():
    """Seven parameters used to send a numerator with no denominator. ON-05 showed sixteen
    rows and the grader read sixteen as the whole population."""
    pages = [_page(f"https://x/services/{i}", page_type="service", text="t") for i in range(40)]

    out = _run(on_05(SPECS["ON-05"], _ctx(pages)))

    ev = out["evidence"]
    assert ev["service_pages_total"] == 40
    assert ev["service_pages_with_faq"] == 0
    # Every page is recorded, not the first sixteen. What reaches one prompt is bounded by a
    # character budget in llm/scoring.py, and what reaches a spreadsheet cell by Excel's own
    # limit -- neither is a handler deciding how much of the site to describe.
    assert len(ev["pages"]) == 40, "evidence holds every page it scored"


def test_on17_reports_what_the_dated_pages_are_out_of():
    """`dated_pages: 333` alone gave the grader no way to state the share."""
    pages = [_page(f"https://x/{i}", text="t") for i in range(20)]
    for page in pages[:5]:
        page.dates = {"published": "2026-01-01", "modified": None}

    out = _run(on_17(SPECS["ON-17"], _ctx(pages)))

    ev = out["evidence"]
    assert ev["dated_pages"] == 5
    assert ev["pages_assessed"] == 20
    assert ev["undated_pages"] == 15
    assert ev["dated_share"] == 0.25


def test_every_on_page_parameter_reports_what_it_measured_over():
    """The requirement, enforced across all twenty rather than parameter by parameter.

    A ratio without its denominator is the defect this whole change exists to remove: the
    grader receives a sample, has no way to tell it from the population, and describes the
    sample as the site. Every on-page handler must therefore put at least one explicit total
    in its evidence.
    """
    from bs4 import BeautifulSoup
    from app.parameters.onpage import HANDLERS

    html = "<div><ul><li>Scope the work</li><li>Build it</li><li>Deliver the step</li></ul><table><tr><td>a</td></tr></table></div>"
    pages = []
    for i in range(6):
        page = _page(
            f"https://x/services/{i}",
            title=f"acme data analytics services for insurers {i}",
            text=("Acme is a data analytics consultancy in the United States. "
                  "Our process delivers 40% faster reporting for clients according to a 2026 report. "
                  "What does implementation involve? " * 3),
            page_type="service",
            soup=BeautifulSoup(html, "html.parser"),
            headings=[{"level": 2, "text": "What does implementation involve?"},
                      {"level": 2, "text": "Frequently asked questions"}],
        )
        page.dates = {"published": "2026-01-01", "modified": None}
        pages.append(page)
    ctx = _ctx(pages)

    # Any key naming a population. Handlers are free to choose the wording that fits the
    # thing they counted; what they are not free to do is report only a numerator.
    markers = ("_total", "population", "pages_assessed", "pages_crawled", "pages_compared",
               "eligible_headings", "major_pages", "claims", "lists", "dated_pages",
               "question_headings", "concepts_derived", "pages_per_stage", "author_signals_total")

    missing = []
    for pid, handler in HANDLERS.items():
        out = _run(handler(SPECS[pid], ctx))
        evidence = out.get("evidence") or {}
        if out.get("status") == "UNKNOWN" and out.get("score") is None:
            continue  # a check that does not apply reports why, not a denominator
        if not any(marker in key for key in evidence for marker in markers):
            missing.append((pid, sorted(evidence)))
    assert not missing, f"these on-page parameters report no denominator: {missing}"


def test_scoring_waits_in_its_own_lane_not_behind_the_classification_fan_out(monkeypatch):
    """The regression that made a full-coverage scan look like a scan with no model at all.

    Classification fans out -- one handler issues up to ninety batched calls at once --
    while scoring is one call per parameter and is the one that produces the reported number.
    Sharing a semaphore meant the scoring call queued behind every outstanding classification
    call: a measured run graded 25 of 60 parameters, with 24 hitting JUDGEMENT_TIMEOUT, and
    every timed-out parameter silently fell back to its rules-based score.
    """
    lanes: list[str] = []

    async def fake_judge(prompt, *, system="", json_mode=True, lane="classify"):
        lanes.append(lane)
        return LLMResult(ok=True, parsed={"score": 50, "explanation": "e"}, elapsed_ms=1)

    monkeypatch.setattr(llm_scoring, "judge", fake_judge)
    spec = next(p for p in load_registry() if p["parameter_id"] == "TECH-01")
    row = {
        "parameter_id": "TECH-01", "section": spec["section"], "name": spec["name"],
        "status": "PARTIAL", "score": 70.0, "confidence": 0.8,
        "evidence": {"summary": "s"}, "recommendation": "r",
    }

    _run(apply_judgement(spec, row))

    assert lanes == ["score"], "the grading call must not share the classification budget"


def test_the_two_lanes_are_actually_different_semaphores():
    """A `lane` argument that resolved to the same semaphore would pass the test above and
    fix nothing."""
    assert llm_client._lane_semaphore("score") is not llm_client._lane_semaphore("classify")


# --- the extraction bugs that produced findings about the extractor ----------------------

def test_on03_prefers_a_prose_definition_over_the_navigation_menu():
    """`blob.find(concept)` took the first occurrence, which on a site whose menu lists every
    service is the menu. One real scan quoted "27001, hipaa, gdpr and soc 2 standards. ai &
    automation application development data & analytics..." as the site's definition."""
    nav = "iso 27001 hipaa gdpr soc 2 data analytics enterprise solutions quality assurance "
    prose = "Data analytics is the practice of turning raw operational records into decisions. "
    windows = _definition_windows(nav + prose * 3, "data analytics")

    assert windows, "the concept was found"
    assert "is the practice of" in windows[0], f"nav text won again: {windows[0]!r}"


def test_on08_does_not_count_a_list_of_empty_items():
    """An icon row extracts as ["", "", "", "", "", ""]. It was counted as a six-item list and
    then graded "not useful" -- a finding about the extractor, reported against the site."""
    from bs4 import BeautifulSoup

    html = "<div><ul>" + "<li></li>" * 6 + "</ul><ul><li>Scope the work</li><li>Build it</li><li>Deliver the step</li></ul></div>"
    page = _page("https://x/", soup=BeautifulSoup(html, "html.parser"), text="t")

    out = _run(on_08(SPECS["ON-08"], _ctx([page])))

    ev = out["evidence"]
    assert ev["lists_total"] == 1, "only the real list counts"
    assert ev["empty_or_markup_only_lists"] == 1, "and the empty one is reported, not scored"


def test_on22_sees_the_brand_in_the_title_not_only_the_body():
    """The audited company's own homepage came back `brand: false` because the extractor had
    dropped the header -- and the parameter scored 0 on that."""
    pages = [_page("https://x/", title="Acme — data analytics in the United States", text="body copy with nothing in it")]
    ctx = _ctx(pages)

    out = _run(on_22(SPECS["ON-22"], ctx))

    assert out["evidence"]["pages_naming_brand"] == 1, "the brand is in the title"


# --- the engine no longer lets the model narrate a check that never ran ------------------

def test_a_check_that_never_ran_gets_no_model_written_explanation(monkeypatch):
    """ON-13 timed out before inspecting a page, and the model -- shown only "did not complete
    within 60s" -- wrote "There are no visible author signals or bylines to assess" into the
    client report. That is a finding about a check that did not happen."""
    called = []

    async def fake_judge(prompt, *, system="", json_mode=True, **kwargs):
        called.append(prompt)
        return LLMResult(ok=True, parsed={"score": 30, "explanation": "No author signals were found."}, elapsed_ms=1)

    monkeypatch.setattr(llm_scoring, "judge", fake_judge)
    spec = SPECS["ON-13"]
    row = {
        "parameter_id": "ON-13", "section": spec["section"], "name": spec["name"],
        "status": "UNKNOWN", "score": None, "confidence": 0,
        "evidence": {"summary": "This check did not complete within 900s and was skipped."},
        "recommendation": "Re-run this check.",
    }

    out = _run(apply_judgement(spec, row))

    assert called == [], "the model must not be asked to describe a check that did not run"
    assert out["status"] == "UNKNOWN" and out["score"] is None
    assert "explanation" not in out["evidence"]
    assert out["evidence"]["summary"] == "This check did not complete within 900s and was skipped."
    assert out["evidence"]["scoring_method"] == "deterministic"


def test_a_score_with_no_explanation_is_not_accepted_as_a_judgement(monkeypatch):
    """An empty explanation used to be stored and still labelled "AI model", putting an
    unjustified number into the report looking exactly like a justified one."""
    async def fake_judge(prompt, *, system="", json_mode=True, **kwargs):
        return LLMResult(ok=True, parsed={"score": 95, "explanation": "", "recommendation": None}, elapsed_ms=1)

    monkeypatch.setattr(llm_scoring, "judge", fake_judge)
    spec = next(p for p in load_registry() if p["parameter_id"] == "TECH-01")
    row = {
        "parameter_id": "TECH-01", "section": spec["section"], "name": spec["name"],
        "status": "PARTIAL", "score": 70.0, "confidence": 0.8,
        "evidence": {"summary": "rules-based reading"}, "recommendation": "Allow the AI crawlers.",
    }

    out = _run(apply_judgement(spec, row))

    assert out["evidence"]["scoring_method"] == "deterministic"
    assert out["score"] == 70.0, "the rules-based score stands in rather than the unexplained 95"
    assert "no explanation" in out["evidence"]["llm_error"]


def test_the_models_reasoning_is_kept_beside_its_explanation(monkeypatch):
    """The explanation is written to agree with the score. The working it showed first is what
    an auditor re-reads when the number looks wrong, so it is stored rather than discarded."""
    async def fake_judge(prompt, *, system="", json_mode=True, **kwargs):
        return LLMResult(ok=True, parsed={
            "evidence_observed": "12 of 40 service pages carry an FAQ.",
            "reasoning": "12/40 is 30%, which is below the metric's 60% band.",
            "score": 30, "explanation": "Most service pages have no FAQ.", "recommendation": "Add FAQs.",
        }, elapsed_ms=1)

    monkeypatch.setattr(llm_scoring, "judge", fake_judge)
    spec = next(p for p in load_registry() if p["parameter_id"] == "TECH-01")
    row = {
        "parameter_id": "TECH-01", "section": spec["section"], "name": spec["name"],
        "status": "PARTIAL", "score": 70.0, "confidence": 0.8,
        "evidence": {"summary": "rules-based reading"}, "recommendation": "Allow the AI crawlers.",
    }

    out = _run(apply_judgement(spec, row))

    assert out["score"] == 30.0
    assert "12 of 40" in out["evidence"]["model_reasoning"]
    assert "below the metric's 60% band" in out["evidence"]["model_reasoning"]
    assert out["evidence"]["explanation"] == "Most service pages have no FAQ."

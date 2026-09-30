"""The universal model-scoring pass, and the workbook it feeds.

Two things have to hold for every one of the sixty parameters: the model decides the score,
the explanation and the recommendation; and when the model cannot be reached, the scan still
produces a complete report from the rules-based scores rather than sixty UNKNOWNs.
"""
from __future__ import annotations

import asyncio
import io
import json

import pytest
from openpyxl import load_workbook

from app.llm import scoring as llm_scoring
from app.llm.client import LLMResult
from app.parameters.engine import apply_judgement, load_registry
from app.scan_output import COLUMNS, build_excel

REGISTRY = load_registry()


def _run(coro):
    return asyncio.run(coro)


def _row(spec, *, score=70.0, status="PARTIAL"):
    return {
        "parameter_id": spec["parameter_id"],
        "section": spec["section"],
        "name": spec["name"],
        "status": status,
        "score": score,
        "max_score": 100,
        "weight": 1.0,
        "confidence": 0.85,
        "checked_url_or_source": "https://example.com",
        "evidence": {"summary": "rules-based summary", "pages": [{"url": "https://example.com"}]},
        "recommendation": "rules-based recommendation",
        "error": None,
        "duration_ms": 5,
        "evaluated_at": "2026-09-17T00:00:00+00:00",
    }


@pytest.fixture
def model_returns(monkeypatch):
    """Stub the model with a canned JSON reply, and record the prompts it was sent."""
    def install(payload: dict):
        seen: list[str] = []

        async def fake_judge(prompt, *, system="", json_mode=True, **kwargs):
            seen.append(prompt)
            return LLMResult(ok=True, text=json.dumps(payload), parsed=payload, elapsed_ms=7)

        monkeypatch.setattr(llm_scoring, "judge", fake_judge)
        return seen
    return install


def test_the_model_explains_and_advises_but_the_rules_based_score_stands(model_returns):
    """The final score is the Score Logic applied to the measured values, so anyone can
    reproduce it. The model's own number is kept for reference and never replaces it."""
    model_returns({"score": 42, "explanation": "Only two of ten pages qualify.", "recommendation": "Add FAQs."})
    spec = REGISTRY[0]

    out = _run(apply_judgement(spec, _row(spec)))

    assert out["score"] == 70.0
    assert out["status"] == "PARTIAL"
    assert out["recommendation"] == "Add FAQs."
    assert out["evidence"]["explanation"] == "Only two of ten pages qualify."
    assert out["evidence"]["summary"] == "Only two of ten pages qualify."
    assert out["evidence"]["scoring_method"] == "rules_based"
    assert out["evidence"]["rules_based_score"] == 70.0
    assert out["evidence"]["model_score"] == 42.0


def test_the_prompt_carries_the_definition_the_metric_and_the_curated_data(model_returns):
    """The model grades against the parameter's own frozen definition and metric, not a
    generic instruction -- otherwise sixty different checks would be graded identically."""
    seen = model_returns({"score": 90, "explanation": "ok", "recommendation": None})
    spec = next(p for p in REGISTRY if p["parameter_id"] == "TECH-01")

    _run(apply_judgement(spec, _row(spec)))

    prompt = seen[0]
    assert "TECH-01" in prompt
    assert "WHAT THIS PARAMETER MEANS" in prompt and "HOW THE METRIC IS DEFINED" in prompt
    assert "<CURATED_DATA>" in prompt and "</CURATED_DATA>" in prompt
    assert "GPTBot" in prompt, "the frozen metric for TECH-01 did not reach the prompt"


def test_the_prompt_does_not_anchor_the_model_to_the_rules_based_score(model_returns):
    """The rules-based number must not reach the grader.

    It used to, as `REFERENCE: The rules-based pass scored this 70.0 out of 100` plus a rule
    saying to keep it unless plainly contradicted. Measured on one real scan, 18 of the 20
    on-page parameters then returned the rules score to the decimal -- the model was
    ratifying, not grading. The score is still computed and still used as the fallback when
    the model is unavailable; it just no longer travels in the prompt.
    """
    seen = model_returns({"score": 90, "explanation": "ok", "recommendation": None})
    spec = next(p for p in REGISTRY if p["parameter_id"] == "TECH-01")

    _run(apply_judgement(spec, _row(spec)))

    prompt = seen[0]
    assert "REFERENCE" not in prompt
    assert "rules-based pass scored" not in prompt
    assert "70.0" not in prompt, "the rules-based score leaked into the prompt"


def test_the_prompt_does_not_disclose_the_pass_thresholds(model_returns):
    """Telling a grader where the pass line sits invites scoring to the line. Status is
    derived from the number in Python afterwards, so the model never needed the bands."""
    seen = model_returns({"score": 90, "explanation": "ok", "recommendation": None})
    spec = next(p for p in REGISTRY if p["parameter_id"] == "TECH-01")

    _run(apply_judgement(spec, _row(spec)))

    assert "SCORING BANDS" not in seen[0]
    assert "is a pass" not in seen[0]


def test_the_model_is_asked_to_reason_before_it_scores(model_returns):
    """Chat models emit JSON keys in the order requested, so a score written before the
    explanation is a number the explanation then has to justify."""
    seen = model_returns({"score": 90, "explanation": "ok", "recommendation": None})
    spec = next(p for p in REGISTRY if p["parameter_id"] == "TECH-01")

    _run(apply_judgement(spec, _row(spec)))

    shape = seen[0][seen[0].index("Return JSON"):]
    assert shape.index("reasoning") < shape.index('"score"')
    assert shape.index("evidence_observed") < shape.index('"score"')


def test_every_registry_parameter_can_be_judged(model_returns):
    """All sixty, not a sample: a parameter missing from frozen_spec.json, or one whose
    evidence will not serialise, would otherwise only surface on a live scan."""
    model_returns({"score": 88, "explanation": "fine", "recommendation": "tweak"})
    for spec in REGISTRY:
        out = _run(apply_judgement(spec, _row(spec)))
        assert out["evidence"]["scoring_method"] == "rules_based", spec["parameter_id"]
        assert out["evidence"]["model_score"] == 88.0, spec["parameter_id"]
        assert out["score"] == 70.0, spec["parameter_id"]


def test_insufficient_data_stays_unknown_rather_than_being_invented(monkeypatch):
    async def fake_judge(prompt, *, system="", json_mode=True, **kwargs):
        return LLMResult(ok=True, text="{}", parsed={"score": None, "explanation": "No pages were reachable."}, elapsed_ms=3)

    monkeypatch.setattr(llm_scoring, "judge", fake_judge)
    spec = REGISTRY[0]

    out = _run(apply_judgement(spec, _row(spec, score=None, status="UNKNOWN")))

    assert out["score"] is None
    assert out["status"] == "UNKNOWN"
    assert out["confidence"] == 0.0


def test_the_rules_based_score_stands_in_when_the_model_is_unavailable(monkeypatch):
    """conftest disables scoring for the whole suite, so this is the path every other test
    in this repo already runs through -- it must leave a complete, honest row."""
    async def unavailable(prompt, *, system="", json_mode=True, **kwargs):
        return LLMResult(ok=False, error="LLM scoring is disabled (ENABLE_LLM_SCORING=false)")

    monkeypatch.setattr(llm_scoring, "judge", unavailable)
    spec = REGISTRY[0]

    out = _run(apply_judgement(spec, _row(spec)))

    assert out["score"] == 70.0, "the rules-based score must survive"
    assert out["status"] == "PARTIAL"
    assert out["evidence"]["scoring_method"] == "deterministic"
    assert "disabled" in out["evidence"]["llm_error"]
    assert out["evidence"]["curated_input"], "the curated data is recorded either way"


def test_a_failing_model_never_fails_the_parameter(monkeypatch):
    async def explode(prompt, *, system="", json_mode=True, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(llm_scoring, "judge", explode)
    spec = REGISTRY[0]

    out = _run(apply_judgement(spec, _row(spec)))

    assert out["score"] == 70.0
    assert out["evidence"]["scoring_method"] == "deterministic"


def test_curated_data_is_capped_so_one_parameter_cannot_dominate_the_prompt():
    huge = {"pages": [{"url": f"https://example.com/{i}", "text": "x" * 2000} for i in range(500)]}
    curated = llm_scoring.curate(huge)
    assert len(curated) <= llm_scoring.MAX_EVIDENCE_CHARS + 32
    assert "omitted" in curated or "truncated" in curated


# --- the workbook -----------------------------------------------------------------------

def _report():
    params = []
    for spec in REGISTRY[:6]:
        params.append({
            **_row(spec),
            "evidence": {
                "summary": "Two of ten service pages carry an FAQ block.",
                "explanation": "Two of ten service pages carry an FAQ block.",
                "curated_input": '{"pages_with_faq": 2, "pages_checked": 10}',
                "scoring_method": "llm",
                "rules_based_score": 70.0,
            },
        })
    return {
        "domain": "example.com",
        "input_url": "https://example.com",
        "scan_id": "abc-123",
        "generated_at": "2026-09-17T00:00:00+00:00",
        "overall_score": 71.4,
        "overall_label": "Fair",
        "category_scores": {"technical": 71.4, "on_page": None, "off_page": None},
        "category_labels": {"technical": "Fair", "on_page": "Unknown", "off_page": "Unknown"},
        "coverage": {"known": 6, "unknown": 0},
        "parameters": params,
    }


def test_the_workbook_has_a_sheet_and_a_row_per_parameter_with_the_pipeline_columns():
    wb = load_workbook(io.BytesIO(build_excel(_report(), REGISTRY)))

    assert "How to Read This Report" in wb.sheetnames and "Summary" in wb.sheetnames
    ws = wb["Technical"]
    header_row = next(
        r for r in range(1, 6)
        if ws.cell(row=r, column=1).value == "Parameter ID"
    )
    headers = [ws.cell(row=header_row, column=c).value for c in range(1, len(COLUMNS) + 1)]
    # The order the audit actually runs in: what it is, what it means, how it is scored, which
    # pages it read and why not the rest, the data that was graded, the verdict, then what to
    # do about it.
    assert headers[:11] == [
        "Parameter ID",
        "Parameter",
        "What This Parameter Means",
        "How the Metric Is Calculated",
        "What Was Found on Your Site (the exact data scored)",
        "Which Pages Were Used, and Why the Others Were Not",
        "Score (%)",
        "Status",
        "Scored By",
        "Explanation",
        "Recommendation",
    ]

    first = {h: ws.cell(row=header_row + 1, column=c + 1).value for c, h in enumerate(headers)}
    assert first["Parameter ID"] == REGISTRY[0]["parameter_id"]
    assert first["What This Parameter Means"].strip(), "definition column is empty"
    assert first["How the Metric Is Calculated"].strip(), "metric column is empty"
    # The same payload the model graded, re-presented as lines a client can read rather
    # than the raw JSON the model was handed.
    assert first["What Was Found on Your Site (the exact data scored)"] == (
        "Pages with FAQ: 2\nPages checked: 10"
    )
    assert first["Scored By"] == "Rules-based"
    assert first["Explanation"] == "Two of ten service pages carry an FAQ block."


def _recommendation_cell(report):
    wb = load_workbook(io.BytesIO(build_excel(report, REGISTRY)))
    ws = wb["Technical"]
    header_row = next(r for r in range(1, 6) if ws.cell(row=r, column=1).value == "Parameter ID")
    headers = [ws.cell(row=header_row, column=c).value for c in range(1, len(COLUMNS) + 1)]
    return ws.cell(row=header_row + 1, column=headers.index("Recommendation") + 1).value


def test_a_passing_parameter_says_so_rather_than_leaving_the_cell_blank():
    report = _report()
    report["parameters"][0].update(status="PASS", score=100.0, recommendation=None)
    assert _recommendation_cell(report) == "No change required."


def test_a_failing_parameter_with_no_recommendation_does_not_claim_it_is_fine():
    """The cell has to stay honest when neither the handler nor the model produced advice."""
    report = _report()
    report["parameters"][0].update(status="FAIL", score=20.0, recommendation=None)
    assert _recommendation_cell(report) == "No specific recommendation was generated - see the explanation."


def test_each_website_gets_its_own_workbook():
    """Two audits must not bleed into one another: the domain on every sheet, and the rows
    themselves, come from that scan's own report."""
    first = build_excel(_report(), REGISTRY)
    other = _report()
    other["domain"] = "another-site.co"
    other["scan_id"] = "def-456"
    second = build_excel(other, REGISTRY)

    assert first != second
    summary = load_workbook(io.BytesIO(second))["Summary"]
    values = [summary.cell(row=r, column=2).value for r in range(1, 12)]
    assert "another-site.co" in values and "def-456" in values


# --- refusing to invent audit data ------------------------------------------------------

def test_a_check_that_never_ran_cannot_be_given_a_score(model_returns):
    """A handler that timed out or crashed produces an UNKNOWN row whose curated data is an
    error message. The model must not turn that into a number: a scored row counts toward
    the pillar average, so inventing one here silently fabricates audit evidence."""
    model_returns({"score": 95, "explanation": "Looks fine.", "recommendation": None})
    spec = REGISTRY[0]
    crashed = _row(spec, score=None, status="UNKNOWN")
    crashed["error"] = "Timed out after 60s"
    crashed["evidence"] = {"summary": "This check did not complete within 60s and was skipped."}

    out = _run(apply_judgement(spec, crashed))

    assert out["score"] is None, "a check that never ran was given a score"
    assert out["status"] == "UNKNOWN"


def test_a_failing_row_without_advice_does_not_claim_no_change_is_required(model_returns):
    """A FAIL that reaches the workbook with no recommendation -- neither the rules-based
    pass nor the model wrote one -- must not read as if nothing needs doing."""
    model_returns({"score": 30, "explanation": "Most pages lack titles.", "recommendation": None})
    spec = REGISTRY[0]
    failing = _row(spec, score=30.0, status="FAIL")
    failing["recommendation"] = None

    out = _run(apply_judgement(spec, failing))
    assert out["status"] == "FAIL"

    report = _report()
    report["parameters"] = [{**out, "section": "technical"}]
    wb = load_workbook(io.BytesIO(build_excel(report, REGISTRY)))
    ws = wb["Technical"]
    hr = next(r for r in range(1, 6) if ws.cell(row=r, column=1).value == "Parameter ID")
    headers = [ws.cell(row=hr, column=c).value for c in range(1, len(COLUMNS) + 1)]
    cell = ws.cell(row=hr + 1, column=headers.index("Recommendation") + 1).value
    assert cell != "No change required.", "a failing parameter claims no change is required"


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Rejected outright -- no model score is recorded at all.
        (float("nan"), None),   # min(100.0, nan) is 100.0, so clamping alone scores this 100
        (True, None),           # float(True) is 1.0, so clamping alone scores this 1
        ([1], None),
        ({"a": 1}, None),
        ("not a number", None),
        # Infinity is malformed output, not a perfect site, so it is rejected rather than
        # clamped -- clamping it would hand a parameter 100 on the strength of a bug.
        (float("inf"), None),
        # Accepted: clamped, or read out of the string models sometimes quote.
        (-20, 0.0),
        ("85%", 85.0),
        (42, 42.0),
    ],
)
def test_a_malformed_score_never_reaches_the_report(model_returns, raw, expected):
    """Whatever the model puts in `score` has to survive round(), JSON serialisation into
    SQLite, and the workbook -- and must never become a number the evidence does not support."""
    model_returns({"score": raw, "explanation": "x", "recommendation": "y"})
    spec = REGISTRY[0]

    out = _run(apply_judgement(spec, _row(spec)))

    assert out["evidence"].get("model_score") == expected, f"{raw!r} became {out['evidence'].get('model_score')!r}"
    assert out["score"] == 70.0, "the model's number, malformed or not, never replaces the rules-based score"
    json.dumps(out["score"])       # must not emit NaN/Infinity, which are invalid JSON
    json.dumps(out["evidence"])


def test_judging_does_not_mutate_the_handlers_own_evidence(model_returns):
    model_returns({"score": 50, "explanation": "e", "recommendation": "r"})
    spec = REGISTRY[0]
    row = _row(spec)
    original = row["evidence"]
    snapshot = dict(original)

    _run(apply_judgement(spec, row))

    assert original == snapshot, "the handler's evidence dict was mutated in place"


def test_unserialisable_evidence_still_produces_curated_data():
    class Opaque:
        def __repr__(self):
            return "<opaque>"

    curated = llm_scoring.curate({"obj": Opaque(), "when": object()})
    assert curated and isinstance(curated, str)
    json.dumps(curated)


def test_control_characters_in_scraped_text_do_not_kill_the_workbook():
    """Excel rejects C0 control characters and openpyxl raises rather than dropping them.
    The curated-data column carries raw scraped page content, so one stray vertical tab in
    one page title anywhere on the site used to fail build_excel -- which runs at the very
    end of a scan, after the whole crawl and every model call had already succeeded."""
    report = _report()
    report["domain"] = "ex\x00ample.com"
    report["parameters"][0]["evidence"] = {
        "explanation": "Title contains \x0b a vertical tab.",
        "curated_input": '{"title": "Bell \x07 and null \x00 and form feed \x0c"}',
        "scoring_method": "llm",
    }
    report["parameters"][0]["recommendation"] = "Strip \x1f control characters."

    data = build_excel(report, REGISTRY)  # must not raise

    ws = load_workbook(io.BytesIO(data))["Technical"]
    hr = next(r for r in range(1, 6) if ws.cell(row=r, column=1).value == "Parameter ID")
    headers = [ws.cell(row=hr, column=c).value for c in range(1, len(COLUMNS) + 1)]
    curated = ws.cell(row=hr + 1, column=headers.index("What Was Found on Your Site (the exact data scored)") + 1).value
    assert "Bell" in curated and "form feed" in curated, "content was lost, not just sanitised"
    assert not any(ord(ch) < 32 and ch not in "\t\n\r" for ch in curated)


def test_a_very_long_cell_is_truncated_rather_than_making_the_file_unopenable():
    report = _report()
    report["parameters"][0]["evidence"] = {
        "explanation": "x" * 60_000,
        "curated_input": "y" * 60_000,
        "scoring_method": "llm",
    }
    ws = load_workbook(io.BytesIO(build_excel(report, REGISTRY)))["Technical"]
    hr = next(r for r in range(1, 6) if ws.cell(row=r, column=1).value == "Parameter ID")
    for c in range(1, len(COLUMNS) + 1):
        value = ws.cell(row=hr + 1, column=c).value
        if isinstance(value, str):
            assert len(value) <= 32_767, "cell exceeds the hard Excel limit"


# --- the model must not be able to delete a finding -------------------------------------

def test_a_model_null_cannot_erase_a_score_the_rules_pass_produced(model_returns):
    """The prompt invites a null when the data is thin. Honouring it on a row the rules pass
    scored would make the parameter UNKNOWN -- and UNKNOWN rows are dropped from the pillar
    denominators and from the issues list, so the finding would vanish instead of counting
    against the site. The bias runs one way: thin evidence belongs to low-scoring rows."""
    model_returns({"score": None, "explanation": "Not enough to go on.", "recommendation": None})
    spec = REGISTRY[0]

    out = _run(apply_judgement(spec, _row(spec, score=30.0, status="FAIL")))

    assert out["score"] == 30.0, "a real finding was deleted by a model null"
    assert out["status"] == "FAIL"
    assert out["evidence"]["scoring_method"] == "deterministic"
    assert out["evidence"]["llm_error"]


def test_a_rejected_score_is_not_labelled_as_an_ai_grade(model_returns):
    """The workbook prints 'Scored By' and the summary counts 'Graded by AI Model', so a row
    that quietly fell back to its rules score must not be counted among them."""
    model_returns({"score": "not a number", "explanation": "x", "recommendation": "y"})
    spec = REGISTRY[0]

    out = _run(apply_judgement(spec, _row(spec)))

    assert out["score"] == 70.0
    assert out["evidence"]["scoring_method"] == "deterministic"
    assert "unusable" in out["evidence"]["llm_error"]


def test_the_model_cannot_raise_a_failing_parameter_to_pass(model_returns):
    """A model that calls a failing check perfect must not erase the finding: the rules-based
    FAIL stands, and so does the remediation the rules-based pass wrote for it."""
    model_returns({"score": 100, "explanation": "All good.", "recommendation": None})
    spec = REGISTRY[0]
    failing = _row(spec, score=20.0, status="FAIL")
    failing["recommendation"] = "Fix your robots.txt"

    out = _run(apply_judgement(spec, failing))

    assert out["score"] == 20.0
    assert out["status"] == "FAIL"
    assert out["recommendation"] == "Fix your robots.txt"


def test_curated_data_survives_a_judgement_that_times_out(monkeypatch):
    """The curated-data column is the point of the exercise; a slow model must not blank it."""
    async def hang(prompt, *, system="", json_mode=True, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(llm_scoring, "judge", hang)
    monkeypatch.setattr("app.parameters.engine.JUDGEMENT_TIMEOUT", 0.05)
    spec = REGISTRY[0]

    out = _run(apply_judgement(spec, _row(spec)))

    assert out["evidence"]["curated_input"], "curated data was lost on timeout"
    assert out["evidence"]["scoring_method"] == "deterministic"
    assert out["score"] == 70.0


def test_the_rules_based_summary_is_kept_for_cross_checking(model_returns):
    model_returns({"score": 42, "explanation": "model prose", "recommendation": "r"})
    spec = REGISTRY[0]

    out = _run(apply_judgement(spec, _row(spec)))

    assert out["evidence"]["summary"] == "model prose"
    assert out["evidence"]["rules_based_summary"] == "rules-based summary"


def test_a_wide_dict_is_trimmed_rather_than_cut_mid_object():
    """A handler keying evidence by URL yields one entry per crawled page. Cutting that at
    the character cap would hand the model, and the workbook, JSON that never closes."""
    wide = {"pages": {f"https://example.com/{i}": {"words": i} for i in range(2000)}}
    curated = llm_scoring.curate(wide)
    json.loads(curated)  # must still be valid JSON
    assert "keys omitted" in curated

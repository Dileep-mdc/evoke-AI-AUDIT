"""The ON-* calculation lines must reproduce the rules-based score they describe."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.parameters.renumbering import OLD_TO_NEW
from app.parameters.working_onpage import WORKING

EVIDENCE = Path(
    r"C:\Users\DMUPPA~1\AppData\Local\Temp\claude\C--Users-dmuppaneni-OneDrive---Evoke-Technologies-Private-Limited-AI-Audit"
    r"\c6229845-0912-4f0b-93e7-120d1702c1f5\scratchpad\evidence.json"
)

ON_IDS = {"ON-01", "ON-02", "ON-03", "ON-04", "ON-05", "ON-06", "ON-07", "ON-08", "ON-09", "ON-10",
          "ON-11", "ON-12", "ON-13", "ON-14", "ON-15", "ON-16", "ON-17", "ON-18", "ON-19", "ON-20"}

# The sentences are for readers, not a formula: no maths symbols.
MATHS_SYMBOLS = ("÷", "×", "→", "≥", "≤", "−", "=")

# Rows whose evidence genuinely lacks an input the calculation needs.
MAY_BE_NONE = {("evoke", "ON-13")}


def test_every_on_parameter_has_a_formatter():
    assert set(WORKING) == ON_IDS


@pytest.fixture(scope="module")
def export():
    if not EVIDENCE.exists():
        pytest.skip("evidence export not available")
    return json.loads(EVIDENCE.read_text(encoding="utf-8"))


def test_recomputed_scores_match_the_stored_rules_scores(export):
    checked = 0
    for scan, rows in export.items():
        for old_pid, row in rows.items():
            pid = OLD_TO_NEW.get(old_pid, old_pid)  # the export predates the renumbering
            if pid not in WORKING:
                continue
            ev = row.get("evidence") or {}
            stored = ev.get("rules_based_score")
            out = WORKING[pid](ev)
            if stored is None:
                assert out is None, (scan, pid)
                continue
            if out is None:
                assert (scan, pid) in MAY_BE_NONE, f"{scan} {pid}: no working line"
                continue
            text, score = out
            assert text and "LLM" not in text, (scan, pid, text)
            assert not [s for s in MATHS_SYMBOLS if s in text], (scan, pid, text)
            assert abs(score - stored) <= 0.15, f"{scan} {pid}: {score} vs {stored} -- {text}"
            checked += 1
    assert checked >= 30


def test_ai_ratio_with_partial_coverage_says_so():
    ev = {"method": "llm", "rules_based_score": 72.3,
          "llm": {"leads_with_substance": 211, "population": 972, "assessed": 292, "complete": False,
                  "batches_failed": 17, "error": "rate limited: 429"}}
    text, score = WORKING["ON-04"](ev)
    assert round(score, 1) == 72.3
    assert "211 of 292" in text and "out of 972; the rest could not be checked" in text
    assert text.endswith("so it scores 72.3.")


def test_band_line_and_thousands_separators():
    ev = {"pages_affected": 104, "pages_compared": 2500, "pairs_found": 50, "method": "heuristic"}
    text, score = WORKING["ON-18"](ev)
    assert score == 90.0  # clean 1 - 104/2500 = 95.8% -> band >=95% = 90
    assert "2,500" in text and "95.8% of pages are clean" in text and "95% or more earns 90" in text


def test_score_formatting_drops_trailing_zero():
    text, score = WORKING["ON-15"]({"dated_pages": 0, "pages_assessed": 10})
    assert score == 50.0 and text.endswith("so it scores 50.")


def test_unknown_and_malformed_rows_return_none():
    assert WORKING["ON-05"]({"rules_based_score": None, "note": "no service pages"}) is None
    assert WORKING["ON-09"]({"average_max_density": "bad"}) is None
    for fn in WORKING.values():
        assert fn(None) is None  # never raises
        fn({})  # never raises on empty evidence


def test_on13_bonus_count_missing_returns_none():
    ev = {"method": "llm", "llm": {"named_client": 5, "quantified_outcome": 3, "deployment_detail": 2,
                                   "assessed": 10, "complete": True}}
    assert WORKING["ON-13"](ev) is None

"""The TECH-* calculation lines must reproduce the rules-based score they describe."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.parameters.working_technical import WORKING

EVIDENCE_EXPORT = Path(
    r"C:\Users\DMUPPA~1\AppData\Local\Temp\claude\C--Users-dmuppaneni-OneDrive---Evoke-Technologies-Private-Limited-AI-Audit"
    r"\c6229845-0912-4f0b-93e7-120d1702c1f5\scratchpad\evidence.json"
)

TECH_IDS = [f"TECH-{i:02d}" for i in range(1, 23)]

# The sentences are for readers, not a formula: no maths symbols.
MATHS_SYMBOLS = ("÷", "×", "→", "≥", "≤", "−", "=")

# The export predates the TECH-08 fixed-width fix and the TECH-16 merged-Organization fix;
# these are Evoke's evidence and score as the corrected handlers produce them.
_EVOKE_PATCHES = {
    "TECH-08": ({"fixed_width_layout": False}, 100.0),
    "TECH-16": ({"fields": ["name", "url", "logo", "sameAs", "contact"]}, 100.0),
}


def _cases():
    if not EVIDENCE_EXPORT.exists():
        return []
    data = json.loads(EVIDENCE_EXPORT.read_text(encoding="utf-8"))
    cases = []
    for scan, rows in data.items():
        for pid in TECH_IDS:
            row = rows.get(pid)
            if not row:
                continue
            ev = copy.deepcopy(row.get("evidence") or {})
            expected = ev.get("rules_based_score")
            if scan == "evoke" and pid in _EVOKE_PATCHES:
                patch, expected = _EVOKE_PATCHES[pid]
                ev.update(patch)
                ev["rules_based_score"] = expected
            cases.append(pytest.param(pid, ev, expected, id=f"{scan}-{pid}"))
    return cases


def test_every_tech_id_has_a_formatter():
    assert sorted(WORKING) == TECH_IDS


@pytest.mark.parametrize("pid,evidence,expected", _cases() or [pytest.param(None, None, None, id="no-export")])
def test_working_reproduces_the_stored_rules_score(pid, evidence, expected):
    if pid is None:
        pytest.skip("evidence export not available")
    out = WORKING[pid](evidence)
    if expected is None:
        assert out is None
        return
    assert out is not None, f"{pid}: formatter returned None"
    text, score = out
    assert isinstance(text, str) and text and "\n" not in text
    assert not [s for s in MATHS_SYMBOLS if s in text], f"{pid}: {text}"
    assert abs(score - expected) <= 0.15, f"{pid}: {text} -> {score}, stored {expected}"


def test_llms_txt_arithmetic():
    ev = {"non_empty": True, "links_found": 4, "freshness_signal": False,
          "sampled_links": [{"url": "a", "ok": True}] * 3 + [{"url": "b", "ok": False}]}
    text, score = WORKING["TECH-03"](ev)
    assert score == 75.0
    assert "3 of 4" in text and text.endswith("so it scores 75.")


def test_latency_and_weight_bands():
    text, score = WORKING["TECH-07"]({"avg_latency_ms": 2034, "avg_page_bytes": 491924})
    assert score == 52.0
    assert "2,034 ms" in text and "491 KB" in text and "slow, 40 points" in text


def test_viewport_cap_inferred_for_old_rows_and_read_from_new_ones():
    old = {"https": True, "viewport": True, "rules_based_score": 80.0}
    assert WORKING["TECH-08"](old)[1] == 80.0
    new = {"https": True, "viewport": True, "fixed_width_layout": False, "rules_based_score": 100.0}
    assert WORKING["TECH-08"](new)[1] == 100.0
    assert WORKING["TECH-08"]({"https": True, "viewport": False, "fixed_width_layout": True})[1] == 50.0


def test_organization_fields():
    assert WORKING["TECH-16"]({"organization": True, "fields": ["name", "logo", "sameAs"]})[1] == 60.0
    assert WORKING["TECH-16"]({"organization": False, "fields": []})[1] == 0.0


def test_unknown_or_malformed_evidence_returns_none():
    assert WORKING["TECH-09"]({"not_applicable": True, "rules_based_score": None}) is None
    assert WORKING["TECH-21"]({"hreflang": [], "rules_based_score": None}) is None
    for fn in WORKING.values():
        assert fn({}) is None or isinstance(fn({}), tuple)
        assert fn(None) is None
        fn({"pages": "garbage", "decisions": 3, "bots": {"x": 1}, "samples": [None]})  # must not raise


def test_number_formatting_drops_trailing_zero():
    text, score = WORKING["TECH-19"]({"blocks": 4, "valid": 4})
    assert text.endswith("so it scores 100.") and score == 100.0

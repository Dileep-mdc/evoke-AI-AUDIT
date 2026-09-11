"""The freeze guarantee.

These tests are what make the parameter logic "frozen": the registry, the per-scan formula
text and the reference workbook all claim things about the code, and each claim is checked
here against the code itself. Changing a handler without updating its frozen description
fails the build rather than silently shipping a document that is no longer true.

Regenerate everything with:  python tools/build_parameter_workbook.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PARAMS = Path(__file__).resolve().parent.parent / "app" / "parameters"
REGISTRY = json.loads((PARAMS / "registry.json").read_text(encoding="utf-8"))
FORMULAS = json.loads((PARAMS / "formulas.json").read_text(encoding="utf-8"))
FROZEN = json.loads((PARAMS / "frozen_spec.json").read_text(encoding="utf-8"))
IDS = [p["parameter_id"] for p in REGISTRY]


def handlers_calling_the_model() -> set[str]:
    """Parameter IDs whose handler really contains a judge() call, read from source."""
    using = set()
    for module in ("technical", "onpage", "offpage"):
        source = (PARAMS / f"{module}.py").read_text(encoding="utf-8")
        for match in re.finditer(r"\nasync def (\w+)\(spec, ctx\):(.*?)(?=\nasync def |\nHANDLERS)", source, re.S):
            if "judge(" in match.group(2):
                using.add(match.group(1).upper().replace("_", "-"))
    return using


def test_registry_ai_required_matches_the_code():
    """16 parameters used to declare ai_required "Yes" while calling no model at all, so
    every report built from the registry overstated how much of the audit was AI-assisted."""
    calling = handlers_calling_the_model()
    wrong = {
        p["parameter_id"]: (p["ai_required"], p["parameter_id"] in calling)
        for p in REGISTRY
        if (p["ai_required"] == "Yes") != (p["parameter_id"] in calling)
    }
    assert not wrong, f"registry ai_required disagrees with the handlers: {wrong}"


def test_automation_level_matches_ai_required():
    for p in REGISTRY:
        expected = "LLM-assisted (heuristic fallback)" if p["ai_required"] == "Yes" else "Deterministic (rule-based)"
        assert p["automation_level"] == expected, p["parameter_id"]


def test_every_parameter_has_a_scoring_formula():
    """formulas.json feeds the Scoring Formula column of every per-scan workbook. It covered
    41 of 62, so a third of the rows silently fell back to a one-line summary."""
    missing = [i for i in IDS if not FORMULAS.get(i, "").strip()]
    assert not missing, f"no scoring formula recorded for: {missing}"
    assert set(FORMULAS) == set(IDS)


def test_frozen_spec_covers_every_parameter_with_both_columns():
    frozen = FROZEN["parameters"]
    assert set(frozen) == set(IDS)
    for pid, entry in frozen.items():
        assert entry["metric"].strip(), f"{pid} has no metric description"
        assert entry["llm_rationale"].strip(), f"{pid} has no LLM rationale"


def test_frozen_spec_llm_flags_match_the_code():
    calling = handlers_calling_the_model()
    wrong = {
        pid: (entry["uses_llm"], pid in calling)
        for pid, entry in FROZEN["parameters"].items()
        if entry["uses_llm"] != (pid in calling)
    }
    assert not wrong, f"frozen spec disagrees with the handlers: {wrong}"


def test_exactly_the_expected_twelve_parameters_use_a_model():
    """Pins the AI surface area. Adding or removing a model call is a deliberate decision
    that must update this list and the parameter workbook alongside it."""
    assert handlers_calling_the_model() == {
        "TECH-11",
        "ON-03", "ON-04", "ON-07", "ON-08", "ON-09", "ON-11", "ON-18",
        "OFF-02", "OFF-09", "OFF-15", "OFF-18",
    }


def test_every_llm_parameter_still_scores_without_a_model():
    """A scan must complete with no API key. Every LLM-assisted handler therefore keeps a
    deterministic fallback, which shows up in source as a heuristic score computed before
    judge() is awaited -- except OFF-15, whose entire question is "what would an assistant
    cite", and which correctly returns UNKNOWN instead of inventing a number."""
    for module in ("technical", "onpage", "offpage"):
        source = (PARAMS / f"{module}.py").read_text(encoding="utf-8")
        for match in re.finditer(r"\nasync def (\w+)\(spec, ctx\):(.*?)(?=\nasync def |\nHANDLERS)", source, re.S):
            pid = match.group(1).upper().replace("_", "-")
            body = match.group(2)
            if "judge(" not in body or pid == "OFF-15":
                continue
            assert "llm_unavailable" in body, f"{pid} does not record why the model was unavailable"
            assert body.index("score") < body.index("judge("), f"{pid} has no pre-model fallback score"

"""The freeze guarantee.

These tests are what make the parameter logic "frozen": the registry, the per-scan formula
text and the reference workbook all claim things about the code, and each claim is checked
here against the code itself. Changing a handler without updating its frozen description
fails the build rather than silently shipping a document that is no longer true.

registry.json, formulas.json and frozen_spec.json are edited by hand alongside the handler.
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


def handler_bodies() -> list[tuple[str, str]]:
    """(parameter id, handler source body) for every registered handler.

    The id is read from each module's HANDLERS table rather than derived from the function
    name: a sub-numbered parameter (ON-5.1, before the renumbering) cannot be spelled as an
    identifier, so the two stopped lining up.
    """
    out = []
    for module in ("technical", "onpage", "offpage"):
        source = (PARAMS / f"{module}.py").read_text(encoding="utf-8")
        table = re.search(r"\nHANDLERS = \{(.*?)\n\}", source, re.S).group(1)
        ids = {func: pid for pid, func in re.findall(r'"([^"]+)":\s*(\w+)', table)}
        for match in re.finditer(r"\nasync def (\w+)\(spec, ctx\):(.*?)(?=\nasync def |\nHANDLERS)", source, re.S):
            pid = ids.get(match.group(1))
            if pid:
                out.append((pid, match.group(2)))
    # Off-page research, including every model pass, lives in the agents; the handler in
    # offpage.py only maps the findings to calculation inputs. The agent method is its body.
    agents = (PARAMS / "offpage_agents.py").read_text(encoding="utf-8")
    research = {f"OFF-{num}": body for num, body in re.findall(
        r"\n    async def research_off_(\d+)\(self, ctx\) -> Findings:(.*?)(?=\n    (?:async )?def |\nclass |\n# -)", agents, re.S)}
    return [(pid, research.get(pid, body)) for pid, body in out]


# The three ways a handler can run its own classification pass. judge() is the single call;
# judge_all()/judge_one() are the full-coverage batched forms that replaced the capped
# samples. Matching only "judge(" silently stopped finding the on-page handlers the moment
# they moved to batching, which would have let the frozen-spec flags drift unnoticed.
MODEL_CALLS = ("judge(", "judge_all(", "judge_one(")


def handlers_calling_the_model() -> set[str]:
    """Parameter IDs whose handler really runs its own model pass, read from source."""
    return {pid for pid, body in handler_bodies() if any(call in body for call in MODEL_CALLS)}


def test_every_parameter_is_model_scored():
    """Every parameter's score comes from the model, so every parameter says so.

    The claim is made sixty times here and implemented once, in the engine: run_one() hands
    each handler's curated data to score_parameter() regardless of which handler produced
    it. A parameter cannot opt out, which is the whole point of scoring there rather than
    inside sixty separate handlers.
    """
    not_declared = [p["parameter_id"] for p in REGISTRY if p["ai_required"] != "Yes"]
    assert not not_declared, f"registry does not declare these as model-scored: {not_declared}"
    not_frozen = [pid for pid, e in FROZEN["parameters"].items() if not e["uses_llm"]]
    assert not not_frozen, f"frozen spec does not declare these as model-scored: {not_frozen}"


def test_the_engine_really_routes_every_parameter_through_the_model():
    """The sixty claims above are all false the moment this one function stops doing it."""
    engine = (PARAMS / "engine.py").read_text(encoding="utf-8")
    assert "score_parameter(" in engine, "engine.py no longer asks the model to score anything"
    assert "apply_judgement" in engine
    assert "await apply_judgement(spec, await evaluate(spec, ctx))" in engine, \
        "run_one no longer applies the model judgement to every evaluated parameter"


def test_a_scan_still_completes_with_no_model_available():
    """ENABLE_LLM_SCORING defaults to false and a key may be missing, so the rules-based
    score has to survive as the fallback -- otherwise every parameter of every scan would
    come back UNKNOWN out of the box."""
    engine = (PARAMS / "engine.py").read_text(encoding="utf-8")
    assert "deterministic" in engine, "engine.py records no rules-based fallback method"
    assert "rules_based_score" in engine, "engine.py does not preserve the rules-based score"
    scoring = (PARAMS.parent / "llm" / "scoring.py").read_text(encoding="utf-8")
    assert 'method="deterministic"' in scoring, "scoring.py has no fallback when the model fails"


def test_automation_level_matches_ai_required():
    for p in REGISTRY:
        assert p["automation_level"] == "LLM-scored (rules-based fallback)", p["parameter_id"]


def test_every_parameter_has_a_scoring_formula():
    """formulas.json feeds the Scoring Formula column of every per-scan workbook. It covered
    41 of them, so a third of the rows silently fell back to a one-line summary."""
    missing = [i for i in IDS if not FORMULAS.get(i, "").strip()]
    assert not missing, f"no scoring formula recorded for: {missing}"
    assert set(FORMULAS) == set(IDS)


def test_frozen_spec_covers_every_parameter_with_every_column():
    """The four columns of docs/Parameter-Logic.xlsx are built from these fields, so a
    parameter added to the engine without its document copy fails here rather than shipping
    as a blank cell."""
    frozen = FROZEN["parameters"]
    assert set(frozen) == set(IDS)
    for pid, entry in frozen.items():
        assert entry["definition"].strip(), f"{pid} has no definition"
        assert entry["metric_brief"].strip(), f"{pid} has no brief metric explanation"
        assert entry["metric"].strip(), f"{pid} has no metric description"
        assert entry["llm_rationale"].strip(), f"{pid} has no LLM rationale"


def test_the_brief_explanation_is_actually_brief():
    """The brief column is the one a reader skims. The exact formula stays in `metric` and
    reaches clients through each scan's own metrics workbook, so this one stays short."""
    too_long = {
        pid: len(entry["metric_brief"])
        for pid, entry in FROZEN["parameters"].items()
        if len(entry["metric_brief"]) > 480
    }
    assert not too_long, f"brief metric explanation runs long for: {too_long}"


def test_dedicated_llm_pass_flags_match_the_code():
    """Separate from scoring: sixteen handlers run their own classification pass before
    they score, and that flag still has to match what the source actually does."""
    calling = handlers_calling_the_model()
    wrong = {
        pid: (entry["dedicated_llm_pass"], pid in calling)
        for pid, entry in FROZEN["parameters"].items()
        if entry["dedicated_llm_pass"] != (pid in calling)
    }
    assert not wrong, f"frozen spec disagrees with the handlers: {wrong}"


def test_exactly_the_expected_sixteen_parameters_run_a_dedicated_model_pass():
    """Pins the extra AI surface area on top of universal scoring. Every entry here is a
    second model call per scan, so the list growing is something to notice, not wave
    through."""
    assert handlers_calling_the_model() == {
        "TECH-11",
        "ON-01", "ON-03", "ON-04", "ON-07", "ON-08", "ON-10", "ON-11", "ON-12", "ON-13",
        "ON-14", "ON-16", "ON-17", "ON-18",
        "OFF-05", "OFF-09",
    }


def test_every_llm_parameter_still_scores_without_a_model():
    """A scan must complete with no API key. Every LLM-assisted handler therefore keeps a
    deterministic fallback, which shows up in source as a heuristic score computed before
    judge() is awaited. An off-page agent does not score, so its fallback is the rules-based
    reading (`rules_*`) that the evaluator falls back on."""
    for pid, body in handler_bodies():
        if not any(call in body for call in MODEL_CALLS):
            continue
        assert "llm_unavailable" in body, f"{pid} does not record why the model was unavailable"
        first_call = min(body.index(call) for call in MODEL_CALLS if call in body)
        marker = "rules_" if pid.startswith("OFF-") else "score"
        assert marker in body and body.index(marker) < first_call, f"{pid} has no pre-model fallback"

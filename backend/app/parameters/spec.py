"""The frozen parameter spec, loaded for the code that needs it while a scan runs.

frozen_spec.json is where each parameter's plain-language definition and its exact scoring
metric live. Until now nothing read it at runtime -- it existed for the tests and the docs
workbook -- and the audit itself carried no description of what it was measuring.

Both halves of the scoring pipeline need it now. The model is asked to grade a parameter
against the definition and metric written here, and the report workbook prints those same
two fields beside the score. Reading both from one file is what keeps the prompt, the
spreadsheet and the frozen documentation from drifting into three different descriptions of
the same check.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parent / "frozen_spec.json"


@lru_cache(maxsize=1)
def parameter_specs() -> dict[str, dict]:
    """Every parameter's frozen entry, keyed by parameter id."""
    try:
        data = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    params = data.get("parameters")
    return params if isinstance(params, dict) else {}


def spec_for(parameter_id: str, registry_entry: dict | None = None) -> dict:
    """One parameter's definition and metric, falling back to the registry's own wording.

    A parameter missing from frozen_spec.json still has to be gradable, so the registry's
    check_logic stands in for the metric rather than the model being handed a blank rubric.
    """
    entry = parameter_specs().get(parameter_id)
    if entry:
        return entry
    reg = registry_entry or {}
    return {
        "name": reg.get("name", parameter_id),
        "section": reg.get("section", ""),
        "weight": reg.get("weight", 1.0),
        "pass_threshold": reg.get("pass_threshold", 90),
        "partial_threshold": reg.get("partial_threshold", 60),
        "definition": reg.get("check_logic", ""),
        "metric_brief": reg.get("check_logic", ""),
        "metric": reg.get("check_logic", ""),
    }

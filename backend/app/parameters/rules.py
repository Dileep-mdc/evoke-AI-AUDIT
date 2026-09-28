"""The audit's scoring constants, loaded from scoring_rules.json.

Why this exists: the numbers that decide a score used to be literals sitting in whichever
handler happened to use them -- `word_count < 180`, `near_duplicate_pairs(excerpts, 0.72)`,
a four-tuple of band thresholds defined forty lines above the function that read it. That has
two costs. Nobody can review the audit's methodology without reading three modules, and the
numbers can drift from the metric text in frozen_spec.json that claims to describe them.

They cannot be removed. A scoring engine has to encode its scoring, and `(0.035, 100)` IS the
keyword-stuffing rule. What they can be is declared once, in a file a non-Python reader can
audit, with the reasoning attached.

`SCORING_RULES_PATH` points this at a different file, so a deployment can retune thresholds
without a code change and without a fork.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

RULES_PATH = Path(os.getenv("SCORING_RULES_PATH", Path(__file__).with_name("scoring_rules.json")))


def _load() -> dict:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


_RULES = _load()


def band(name: str) -> tuple[tuple[tuple[float, float], ...], float]:
    """A named band table as (bands, floor), in the shape common.band() expects.

    Returned as tuples rather than the lists JSON gives back, because these are constants and
    a caller that mutated one would silently retune every later parameter in the same scan.
    """
    entry = _RULES["bands"][name]
    return tuple(tuple(pair) for pair in entry["bands"]), entry["floor"]


def threshold(name: str, key: str = "value") -> Any:
    """One named threshold. `key` selects a bound on the ranged ones (min/max)."""
    return _RULES["thresholds"][name][key]


def all_rules() -> dict:
    """The whole document, for the test that checks nothing has been left undeclared."""
    return _RULES

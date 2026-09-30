"""One-line, human-readable working for each parameter's rules-based score.

Each section module maps a parameter id to a function that reads the stored evidence and
returns (text, recomputed score). The text is shown only when the recomputed score agrees
with the score being reported, so the pop-up can never display arithmetic that does not
produce the number beside it; anything else falls back to the generic description.
"""
from __future__ import annotations

from typing import Callable, Optional

from .working_offpage import WORKING as _OFF
from .working_onpage import WORKING as _ON
from .working_technical import WORKING as _TECH

WORKING: dict[str, Callable[[dict], Optional[tuple[str, float]]]] = {**_TECH, **_ON, **_OFF}

TOLERANCE = 0.15


def working_for(parameter_id: str, evidence: dict | None, score: float | None) -> Optional[str]:
    if score is None or not isinstance(evidence, dict):
        return None
    fn = WORKING.get(parameter_id)
    if fn is None:
        return None
    try:
        out = fn(evidence)
    except Exception:
        return None
    if not out:
        return None
    text, recomputed = out
    try:
        if abs(float(recomputed) - float(score)) > TOLERANCE:
            return None
    except (TypeError, ValueError):
        return None
    return text or None

"""Model-scored judgement for every audit parameter.

The handlers in app/parameters/ own the first half of the pipeline: pull the pages that
matter to one parameter and reduce them to a small, parameter-specific evidence dict -- the
curated data. This module owns the second half. It hands that curated data, together with
the parameter's own definition and metric from frozen_spec.json, to the model and takes back
the three things the report actually shows: a score, an explanation, and a recommendation.

Routing this through one place rather than through each handler is what makes "every
parameter is model-scored" a property of the engine instead of a promise repeated sixty
times. A handler cannot forget to ask for a judgement, because it never asks: the engine
does, for whatever the handler returned.

The rules-based score is still computed, and still travels with the request, for two
reasons. The model grades against it rather than against a blank page, which keeps a demo
reproducible instead of letting sixty independent judgement calls drift. And when the model
is unavailable -- no key, scoring disabled, rate limited -- it is what the report falls back
to, labelled so nobody mistakes a fallback for a judgement.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Optional

from .client import judge

SCORING_SYSTEM = (
    "You are a strict, evidence-bound grader for a website AI-visibility audit. "
    "You are given one audit parameter: its definition, the metric that defines how it is "
    "scored, and the curated data extracted from the audited site for that parameter. "
    "Grade only from the curated data provided -- never from assumptions about the brand, "
    "and never from knowledge outside the data. "
    "Reply with ONLY a single JSON object matching the requested shape -- no prose, no markdown fences."
)

# The curated data is what the model grades, so it has to survive the trip intact enough to
# be gradable. These caps exist because a handful of parameters gather per-page rows for
# every page of a large site, and the raw dict would otherwise dominate the prompt (and the
# bill) without making the judgement any better.
MAX_EVIDENCE_CHARS = 6000
MAX_LIST_ITEMS = 12
MAX_DICT_KEYS = 40
MAX_STRING_CHARS = 600


def _trim(value: Any) -> Any:
    """Shrink one evidence value to something worth sending, keeping its shape recognisable."""
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING_CHARS else value[:MAX_STRING_CHARS] + " ...[truncated]"
    if isinstance(value, list):
        kept = [_trim(v) for v in value[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            kept.append(f"...[{len(value) - MAX_LIST_ITEMS} more of {len(value)} total omitted]")
        return kept
    if isinstance(value, dict):
        # Breadth is capped as well as depth. A handler is free to key its evidence by URL,
        # which on a large site is one entry per crawled page; without this the payload would
        # be cut mid-object by the character cap below and the model -- and the workbook
        # column -- would be handed JSON that does not close.
        items = list(value.items())
        kept = {str(k): _trim(v) for k, v in items[:MAX_DICT_KEYS]}
        if len(items) > MAX_DICT_KEYS:
            kept["..."] = f"[{len(items) - MAX_DICT_KEYS} more of {len(items)} keys omitted]"
        return kept
    return value


def curate(evidence: dict | None) -> str:
    """The evidence dict as the compact JSON the model is asked to grade.

    This is the "curated data" step of the pipeline made literal: whatever the handler
    gathered, reduced to a bounded payload. The same string is written to the report, so the
    workbook shows exactly what the model saw rather than a prettier summary of it.
    """
    if not evidence:
        return "{}"
    try:
        text = json.dumps(_trim(evidence), ensure_ascii=False, default=str)
    except Exception:
        text = str(evidence)[:MAX_EVIDENCE_CHARS]
    if len(text) > MAX_EVIDENCE_CHARS:
        text = text[:MAX_EVIDENCE_CHARS] + " ...[truncated]"
    return text


def build_prompt(
    *,
    parameter_id: str,
    name: str,
    section: str,
    definition: str,
    metric: str,
    pass_threshold: float,
    partial_threshold: float,
    curated: str,
    reference_score: Optional[float],
    reference_unknown: bool,
) -> str:
    reference = (
        "The rules-based pass could not evaluate this parameter (insufficient data)."
        if reference_unknown or reference_score is None
        else f"The rules-based pass scored this {reference_score:.1f} out of 100."
    )
    return f"""Audit parameter {parameter_id} -- {name} (section: {section}).

WHAT THIS PARAMETER MEANS:
{definition}

HOW THE METRIC IS DEFINED:
{metric}

SCORING BANDS: {pass_threshold} and above is a pass; {partial_threshold} to {pass_threshold} is partial; below {partial_threshold} is a fail.

CURATED DATA EXTRACTED FROM THE AUDITED SITE:
{curated}

REFERENCE: {reference}

Apply the metric above to the curated data and return your judgement.
Rules:
- Follow the stated metric. Treat the reference score as the rules-based reading of the same data: keep it unless the curated data plainly contradicts it, and say so in the explanation when you depart from it.
- Ground every claim in the curated data. Quote concrete figures, URLs or counts from it.
- If the curated data is empty or too thin to judge, return null for score and say what is missing.
- The explanation is 2-3 sentences, written for a client reading an audit report: what was found, and why it scores where it does.
- The recommendation is ONE specific, actionable fix for this site. Return null when the parameter already passes and no action is needed.

Return JSON: {{"score": number 0-100 or null, "explanation": string, "recommendation": string or null}}"""


def coerce_score(raw: Any, fallback: Optional[float]) -> tuple[Optional[float], bool]:
    """The model's `score` field as (score, accepted).

    `accepted` is False when the value was unusable and the fallback is standing in, so the
    caller can stop labelling the row as model-graded. Silently substituting the rules-based
    number and still calling it an AI grade is the exact mislabelling the method field exists
    to prevent.

    Clamping alone is not enough, because the values worth rejecting survive it. Python reads
    a JSON `true` as 1.0, so a boolean would land in the report as a score of 1; and
    min(100.0, nan) is 100.0, because every comparison against NaN is False, so a NaN would
    clamp to a perfect score rather than to nothing. Both have to be caught by type and by
    finiteness before the value is clamped, not after.

    An explicit null is accepted as a null: whether that should erase an existing score is a
    policy question, and it is decided in engine.apply_judgement, not here.
    """
    if raw is None:
        return None, True
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        # Models do sometimes quote the number ("85", "85%"), which is worth honouring;
        # anything else -- a list, an object, prose -- is not a grade.
        try:
            raw = float(str(raw).strip().rstrip("%"))
        except (TypeError, ValueError):
            return fallback, False
    value = float(raw)
    if not math.isfinite(value):
        return fallback, False
    return max(0.0, min(100.0, value)), True


@dataclass
class Judgement:
    """What the model decided, or why the deterministic score stands in for it."""

    score: Optional[float]
    explanation: str
    recommendation: Optional[str]
    unknown: bool
    method: str  # "llm" | "deterministic"
    curated: str
    error: Optional[str] = None
    elapsed_ms: int = 0


async def score_parameter(
    *,
    parameter_id: str,
    spec: dict,
    evidence: dict | None,
    reference_score: Optional[float],
    reference_unknown: bool,
    reference_recommendation: Optional[str] = None,
) -> Judgement:
    """Grade one parameter from its curated data, falling back to the rules-based score.

    `spec` is the parameter's frozen_spec.json entry -- the definition and metric it carries
    are what the model is graded against, so the report, the workbook and the prompt all
    describe the parameter in the same words.
    """
    curated = curate(evidence)
    prompt = build_prompt(
        parameter_id=parameter_id,
        name=spec.get("name", parameter_id),
        section=spec.get("section", ""),
        definition=spec.get("definition", ""),
        metric=spec.get("metric") or spec.get("metric_brief", ""),
        pass_threshold=spec.get("pass_threshold", 90),
        partial_threshold=spec.get("partial_threshold", 60),
        curated=curated,
        reference_score=reference_score,
        reference_unknown=reference_unknown,
    )

    res = await judge(prompt, system=SCORING_SYSTEM)
    parsed = res.parsed if (res.ok and isinstance(res.parsed, dict)) else None

    if parsed is None:
        # No judgement available. The rules-based score is what the report shows, and the
        # method field is what stops it being read as one.
        return Judgement(
            score=reference_score,
            explanation="",
            recommendation=reference_recommendation,
            unknown=reference_unknown or reference_score is None,
            method="deterministic",
            curated=curated,
            error=res.error or "no judgement returned",
            elapsed_ms=res.elapsed_ms,
        )

    raw_score = parsed.get("score")
    score, accepted = coerce_score(raw_score, reference_score)
    explanation = str(parsed.get("explanation") or "").strip()
    recommendation = parsed.get("recommendation")
    recommendation = str(recommendation).strip() if recommendation else None

    return Judgement(
        score=score,
        explanation=explanation,
        recommendation=recommendation or reference_recommendation,
        unknown=score is None,
        method="llm" if accepted else "deterministic",
        curated=curated,
        error=None if accepted else f"the model returned an unusable score ({raw_score!r})",
        elapsed_ms=res.elapsed_ms,
    )

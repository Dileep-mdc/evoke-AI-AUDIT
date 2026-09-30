"""Running every parameter, and having the model grade every one of them.

Each parameter is evaluated in two passes.

  1. The handler in technical.py / onpage.py / offpage.py pulls the pages that matter to
     that one parameter and reduces them to a small evidence dict -- the curated data --
     along with a rules-based score.
  2. That curated data, plus the parameter's own definition and metric from
     frozen_spec.json, goes to the model, which returns the score, the explanation and the
     recommendation the report actually shows.

The second pass lives here rather than inside each handler on purpose. Sixty handlers that
each remember to ask for a judgement is sixty chances to forget one; a scan where the model
graded fifty-nine parameters and quietly scored the sixtieth by hand looks identical in the
report. Driving it from run_one() makes "every parameter is model-scored" true by
construction -- a handler cannot opt out, because it is never asked to opt in.

The rules-based score survives the merge as evidence["rules_based_score"], and is what the
report falls back to when the model is unavailable (scoring disabled, no key, rate limited).
evidence["scoring_method"] says which of the two produced the number, so a fallback is never
mistaken for a judgement.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Awaitable, Callable

from ..config import JUDGEMENT_TIMEOUT, PARAMETER_CONCURRENCY, PARAMETER_TIMEOUT, REGISTRY_PATH
from ..errors import humanize_error
from ..llm.scoring import curate, score_parameter
from .citations import build_citations
from .common import excluded_page_counts, status_from_score
from .offpage import HANDLERS as OFF
from .onpage import HANDLERS as ON
from .spec import spec_for
from .technical import HANDLERS as TECH

HANDLERS: dict[str, Callable] = {**TECH, **ON, **OFF}


def load_registry() -> list[dict]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


async def evaluate(spec: dict, ctx) -> dict:
    """The rules-based pass: run the handler and return its row, whatever happens to it."""
    handler = HANDLERS.get(spec["parameter_id"])
    now = datetime.now(timezone.utc).isoformat()
    if handler is None:
        return {
            **{k: spec.get(k) for k in ("parameter_id", "section", "name", "weight", "max_score")},
            "status": "UNKNOWN",
            "score": None,
            "confidence": 0,
            "checked_url_or_source": None,
            "evidence": {"reason": "No handler registered"},
            "recommendation": "Implement this parameter handler.",
            "error": "Handler missing",
            "duration_ms": 0,
            "evaluated_at": now,
        }
    try:
        out = await asyncio.wait_for(handler(spec, ctx), timeout=PARAMETER_TIMEOUT)
        # Attached centrally rather than by sixty handlers: how big the crawl was, and how
        # much of it was unusable. Every parameter's numbers have to be read against these --
        # "12 pages scored" means something different out of 30 than out of 1,100 -- and the
        # workbook's "Which Pages Were Used" column is assembled from them. setdefault, so a
        # handler that already recorded its own figure keeps it.
        out["evidence"] = out.get("evidence") or {}
        for key, value in excluded_page_counts(ctx).items():
            if value:
                out["evidence"].setdefault(key, value)
        out["evaluated_at"] = now
        return out
    except asyncio.TimeoutError:
        return {
            "parameter_id": spec["parameter_id"],
            "section": spec["section"],
            "name": spec["name"],
            "status": "UNKNOWN",
            "score": None,
            "max_score": spec.get("max_score", 100),
            "weight": spec.get("weight", 1),
            "confidence": 0,
            "checked_url_or_source": getattr(ctx, "origin", None),
            "evidence": {"summary": f"This check did not complete within {PARAMETER_TIMEOUT:.0f}s and was skipped."},
            "recommendation": "Run the audit again later; the source may be slow or unavailable.",
            "error": f"Timed out after {PARAMETER_TIMEOUT:.0f}s",
            "duration_ms": int(PARAMETER_TIMEOUT * 1000),
            "evaluated_at": now,
        }
    except Exception as exc:
        return {
            "parameter_id": spec["parameter_id"],
            "section": spec["section"],
            "name": spec["name"],
            "status": "UNKNOWN",
            "score": None,
            "max_score": spec.get("max_score", 100),
            "weight": spec.get("weight", 1),
            "confidence": 0,
            "checked_url_or_source": getattr(ctx, "origin", None),
            "evidence": {
                "summary": humanize_error(str(exc)) or "This check could not be completed.",
                "exception": humanize_error(str(exc)),
            },
            "recommendation": "Run the audit again; this check raised an unexpected error.",
            "error": humanize_error(str(exc)) or str(exc),
            "duration_ms": 0,
            "evaluated_at": now,
        }


async def apply_judgement(spec: dict, row: dict) -> dict:
    """Have the model score, explain and advise on one already-evaluated parameter.

    Returns the same row, with the model's verdict merged in. Never raises: a parameter
    whose judgement fails keeps its rules-based score and says so.
    """
    parameter_id = row.get("parameter_id") or spec.get("parameter_id")
    frozen = spec_for(parameter_id, spec)
    evidence = dict(row.get("evidence") or {})
    rules_score = row.get("score")
    evidence["rules_based_score"] = rules_score
    # A parameter the rules-based pass could not evaluate -- the source was unreachable, the
    # handler timed out or raised, there is no handler at all -- has nothing for the model to
    # grade but an error message. The model is still asked, because its explanation of what is
    # missing is worth having, but it is not allowed to turn that into a number: a scored row
    # joins the pillar average, so inventing one here would fabricate audit evidence for a
    # check that never actually ran.
    unscorable = rules_score is None or row.get("status") == "UNKNOWN"
    # ...and it is not asked at all. It used to be, on the argument that its account of what
    # was missing was worth having. In practice it invented one: ON-12 timed out before
    # inspecting a single page, and the model -- shown only the words "did not complete
    # within 60s" -- wrote "There are no visible author signals or bylines to assess" into
    # the client report. That is a finding about a check that never ran. The deterministic
    # summary already says what happened, truthfully, so it is what stands.
    if unscorable:
        evidence["scoring_method"] = "deterministic"
        evidence["llm_skipped"] = "not sent for judgement: the rules-based pass could not evaluate this parameter"
        row["evidence"] = evidence
        row["score"] = None
        row["status"] = "UNKNOWN"
        row["confidence"] = 0.0
        return row
    # Recorded before the call, not after, so a judgement that times out or raises still
    # shows the reader what was going to be graded -- the one column this whole pass exists
    # to produce. The rules-based summary is kept for the same reason: once the model's
    # explanation replaces evidence["summary"] there is otherwise no way to see what the
    # deterministic pass concluded, which is exactly what a fallback row invites you to check.
    evidence["curated_input"] = curate(row.get("evidence") or {})
    if evidence.get("summary"):
        evidence["rules_based_summary"] = evidence["summary"]

    try:
        judgement = await asyncio.wait_for(
            score_parameter(
                parameter_id=parameter_id,
                spec=frozen,
                evidence=row.get("evidence") or {},
                reference_score=rules_score,
                reference_unknown=row.get("status") == "UNKNOWN",
                reference_recommendation=row.get("recommendation"),
            ),
            timeout=JUDGEMENT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        evidence["scoring_method"] = "deterministic"
        evidence["llm_error"] = f"Model scoring timed out after {JUDGEMENT_TIMEOUT:.0f}s"
        row["evidence"] = evidence
        return row
    except Exception as exc:
        evidence["scoring_method"] = "deterministic"
        evidence["llm_error"] = humanize_error(str(exc)) or str(exc)
        row["evidence"] = evidence
        return row

    evidence["curated_input"] = judgement.curated
    evidence["scoring_method"] = judgement.method
    if judgement.error:
        evidence["llm_error"] = judgement.error
    if judgement.explanation:
        evidence["explanation"] = judgement.explanation
        evidence["summary"] = judgement.explanation
    if judgement.reasoning:
        # Kept beside the explanation, not merged into it: the explanation is what the client
        # reads, the reasoning is what an auditor re-reads when the number looks wrong.
        evidence["model_reasoning"] = judgement.reasoning

    if judgement.score is None:
        # The model declined to grade data the rules-based pass could score. A null must
        # never turn a scored row UNKNOWN: that drops it from the denominators and the
        # issues list, and the thinnest evidence belongs to the lowest scores, so honouring
        # nulls would quietly raise the overall score.
        evidence["scoring_method"] = "deterministic"
        evidence["llm_error"] = "the model returned no score for data the rules-based pass could grade"
    elif judgement.method == "llm":
        # Only a score the model actually gave; a malformed one falls back to the reference.
        evidence["model_score"] = round(float(judgement.score), 1)
        evidence["scoring_method"] = "rules_based"
    # The final score is the rules-based one: the Score Logic applied to the measured values,
    # which anyone can reproduce from the evidence. The model's review is kept beside it
    # (explanation, reasoning, model_score) but it does not move the number -- when it did,
    # scores shifted by up to 45 points with working that contradicted the stated rule.
    score = rules_score

    unknown = score is None
    status = "UNKNOWN" if unknown else status_from_score(
        score, spec.get("pass_threshold", 90), spec.get("partial_threshold", 60)
    )

    row["evidence"] = evidence
    row["score"] = None if unknown else round(float(score), 1)
    row["status"] = status
    # result() drops the recommendation from a passing row, and that invariant holds here too.
    row["recommendation"] = None if status == "PASS" else (judgement.recommendation or row.get("recommendation"))
    if unknown:
        row["confidence"] = 0.0
    return row


async def run_one(spec: dict, ctx) -> dict:
    row = await apply_judgement(spec, await evaluate(spec, ctx))
    # Every score carries its sources -- whichever pass, rules or model, produced the number.
    row["evidence"] = {**(row.get("evidence") or {}), "citations": build_citations(row)}
    return row


async def run_all_parameters(ctx, specs: list[dict], on_each: Callable[[dict], Awaitable[None]] | None = None) -> list[dict]:
    sem = asyncio.Semaphore(PARAMETER_CONCURRENCY)
    results: list[dict] = [None] * len(specs)  # type: ignore[list-item]

    async def worker(index: int, spec: dict) -> None:
        async with sem:
            row = await run_one(spec, ctx)
        results[index] = row
        if on_each:
            await on_each(row)

    await asyncio.gather(*(worker(i, spec) for i, spec in enumerate(specs)))
    return results

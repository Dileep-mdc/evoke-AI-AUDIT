from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Awaitable, Callable

from ..config import PARAMETER_CONCURRENCY, PARAMETER_TIMEOUT, REGISTRY_PATH
from ..errors import humanize_error
from .offpage import HANDLERS as OFF
from .onpage import HANDLERS as ON
from .technical import HANDLERS as TECH

HANDLERS: dict[str, Callable] = {**TECH, **ON, **OFF}


def load_registry() -> list[dict]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


async def run_one(spec: dict, ctx) -> dict:
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
            "recommendation": "Re-run this check; the source may be slow or unavailable.",
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
            "recommendation": "Re-run this check; the evaluator raised an unexpected error.",
            "error": humanize_error(str(exc)) or str(exc),
            "duration_ms": 0,
            "evaluated_at": now,
        }


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

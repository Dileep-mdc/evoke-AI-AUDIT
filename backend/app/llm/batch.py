"""Putting every candidate in front of the model, not a sample of them.

A handler's classification pass used to send one capped list -- 24 pages, 40 headings, 20
pairs -- and then score the whole site from whatever came back. Two things went wrong with
that, and they compounded:

  * The cap was invisible downstream. Evidence recorded `assessed: 40` and nothing recorded
    that 40 was drawn from 3,863, so both the grading model and the reader took the sample
    for the population.
  * The sample was taken off the top or by a fixed stride, so which items got judged was an
    artefact of crawl order rather than of the site.

`judge_all` replaces that with a full pass: split the candidates into batches, run them
concurrently, and merge. It returns the verdicts together with a Coverage record that says
exactly how much of the population was actually judged -- so a handler can no longer report a
ratio without also reporting what it was measured over, and a partial pass (a batch that
failed, or LLM_MAX_ITEMS biting) is visible instead of silent.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from ..config import LLM_BATCH_CHARS, LLM_BATCH_SIZE, LLM_FULL_COVERAGE, LLM_MAX_ITEMS
from .client import judge
from .prompts import SYSTEM


@dataclass
class Coverage:
    """How much of the candidate population the model actually saw.

    This travels into the evidence dict so the grading model and the workbook both get the
    denominator, not just the numerator. `complete` is the honest headline: it is only True
    when every candidate was judged and every batch came back.
    """

    population: int = 0
    submitted: int = 0
    assessed: int = 0
    batches: int = 0
    batches_failed: int = 0
    capped: bool = False
    error: Optional[str] = None
    _errors: list[str] = field(default_factory=list, repr=False)

    @property
    def complete(self) -> bool:
        return (
            self.population > 0
            and self.assessed >= self.population
            and self.batches_failed == 0
        )

    def as_evidence(self) -> dict:
        """The subset worth writing into evidence, in the shape handlers report it."""
        out: dict[str, Any] = {
            "population": self.population,
            "assessed": self.assessed,
            "complete": self.complete,
        }
        if self.capped:
            out["capped_at"] = self.submitted
        if self.batches_failed:
            out["batches_failed"] = self.batches_failed
        if self.error:
            out["error"] = self.error
        return out


def _chunk(items: Sequence[Any], size: int) -> list[list[Any]]:
    """Split into batches by SIZE IN CHARACTERS as well as by count.

    Count alone stopped working once handlers began sending whole pages instead of 600-char
    excerpts: forty full pages is a request no model will accept, and the fix must not be to
    go back to truncating the pages. So a batch closes when either bound is reached, and an
    item too large for an empty batch travels alone rather than being dropped or cut.

    The effect is that batch size adapts to the content -- forty short rows share a request,
    one long page gets its own -- and nothing is excluded either way.
    """
    size = max(1, size)
    batches: list[list[Any]] = []
    current: list[Any] = []
    used = 0
    for item in items:
        cost = len(json.dumps(item, ensure_ascii=False, default=str))
        if current and (len(current) >= size or used + cost > LLM_BATCH_CHARS):
            batches.append(current)
            current, used = [], 0
        current.append(item)
        used += cost
    if current:
        batches.append(current)
    return batches


async def judge_all(
    items: Sequence[Any],
    prompt_builder: Callable[[list], str],
    *,
    result_key: str = "results",
    batch_size: int = LLM_BATCH_SIZE,
    system: str = SYSTEM,
    full_coverage: Optional[bool] = None,
    max_items: Optional[int] = None,
) -> tuple[list[dict], Coverage]:
    """Classify every item in `items`, in concurrent batches, and merge the verdicts.

    `prompt_builder` is one of the existing per-parameter prompt functions -- it is called
    once per batch with that batch's items, so the prompts themselves do not change and the
    model still sees the same instruction it always did, just over all of the data.

    Returns `(verdicts, coverage)`. Verdicts from batches that failed are simply absent;
    `coverage.batches_failed` is what says so. A caller that needs a ratio should divide by
    `len(verdicts)` and report `coverage` alongside it, never divide by the population it
    did not actually judge.
    """
    population = len(items)
    coverage = Coverage(population=population)
    if not items:
        return [], coverage

    use_full = LLM_FULL_COVERAGE if full_coverage is None else full_coverage
    ceiling = LLM_MAX_ITEMS if max_items is None else max_items

    submitted = list(items)
    if not use_full:
        # Sampling mode still goes through here so the evidence shape is identical either
        # way -- the denominator is reported whether or not the whole population was judged.
        submitted = submitted[:batch_size]
    if ceiling and len(submitted) > ceiling:
        submitted = submitted[:ceiling]
    coverage.capped = len(submitted) < population
    coverage.submitted = len(submitted)

    batches = _chunk(submitted, batch_size)
    coverage.batches = len(batches)

    async def run(batch: list) -> list[dict]:
        res = await judge(prompt_builder(batch), system=system)
        if not res.ok:
            raise RuntimeError(res.error or "classification call failed")
        rows = (res.parsed or {}).get(result_key)
        if not isinstance(rows, list):
            raise RuntimeError(f"reply had no {result_key!r} list")
        return [r for r in rows if isinstance(r, dict)]

    settled = await asyncio.gather(*(run(b) for b in batches), return_exceptions=True)

    verdicts: list[dict] = []
    for outcome in settled:
        if isinstance(outcome, BaseException):
            coverage.batches_failed += 1
            coverage._errors.append(str(outcome))
            continue
        verdicts.extend(outcome)

    coverage.assessed = len(verdicts)
    if coverage._errors:
        # One message, not one per batch: the reader needs to know a gap exists and why,
        # not to read the same rate-limit sentence forty times.
        coverage.error = coverage._errors[0]
    return verdicts, coverage


async def judge_one(
    items: Sequence[Any],
    prompt_builder: Callable[[list], str],
    *,
    system: str = SYSTEM,
) -> tuple[Optional[dict], Coverage]:
    """The whole-site variant: one verdict object about the population, not one per item.

    ON-18 asks a single question ("which journey stages does this site cover?") over many
    pages, so batching it means merging booleans rather than concatenating rows. Every batch
    still sees real pages, and a stage counts as covered if any batch found it.
    """
    population = len(items)
    coverage = Coverage(population=population)
    if not items:
        return None, coverage

    submitted = list(items)
    if not LLM_FULL_COVERAGE:
        submitted = submitted[:LLM_BATCH_SIZE]
    if LLM_MAX_ITEMS and len(submitted) > LLM_MAX_ITEMS:
        submitted = submitted[:LLM_MAX_ITEMS]
    coverage.capped = len(submitted) < population
    coverage.submitted = len(submitted)

    batches = _chunk(submitted, LLM_BATCH_SIZE)
    coverage.batches = len(batches)

    async def run(batch: list) -> dict:
        res = await judge(prompt_builder(batch), system=system)
        if not res.ok or not isinstance(res.parsed, dict):
            raise RuntimeError(res.error or "classification call failed")
        return res.parsed

    settled = await asyncio.gather(*(run(b) for b in batches), return_exceptions=True)

    merged: dict[str, Any] = {}
    judged_items = 0
    for outcome, sent in zip(settled, batches):
        if isinstance(outcome, BaseException):
            coverage.batches_failed += 1
            coverage._errors.append(str(outcome))
            continue
        # Counted per batch rather than as "all of them if any batch worked": a whole-site
        # verdict assembled from half the batches has seen half the pages, and `complete`
        # has to be able to say so.
        judged_items += len(sent)
        for key, value in outcome.items():
            if isinstance(value, dict):
                bucket = merged.setdefault(key, {})
                if isinstance(bucket, dict):
                    for sub, flag in value.items():
                        bucket[sub] = bool(bucket.get(sub)) or bool(flag)
            elif isinstance(value, bool):
                merged[key] = bool(merged.get(key)) or value
            else:
                merged.setdefault(key, value)

    coverage.assessed = judged_items
    if coverage._errors:
        coverage.error = coverage._errors[0]
    return (merged or None), coverage

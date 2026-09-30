"""The deterministic scoring engine for the off-page parameters.

Agents research and verify; they never decide a percentage. Each evaluator in offpage.py maps
an agent's findings to the exact calculation inputs below, and these functions apply the
frozen calculation (frozen_spec.json / formulas.json) to them. Pure functions: the same
inputs always give the same score, so a stored `calculation_inputs` dict reproduces its row.
"""
from __future__ import annotations

from typing import Optional

from . import rules
from .common import band


def off_01(on_wikidata: Optional[bool], on_wikipedia: Optional[bool]) -> float:
    """Yes/no: 100 when the company is on both Wikidata and Wikipedia, 0 when either is missing."""
    return 100.0 if on_wikidata and on_wikipedia else 0.0


def off_02(entity_found: bool, has_description: bool, brand_in_label: bool) -> float:
    """0 with no matching entity; else 50, + 30 with a description, + 20 with the brand in its label."""
    if not entity_found:
        return 0.0
    return min(100, 50 + (30 if has_description else 0) + (20 if brand_in_label else 0))


def off_03(platforms_found: int, platforms_total: int, name_share: float, location_share: float) -> float:
    return platforms_found / platforms_total * 70 + name_share * 15 + location_share * 15


def off_04(claim_kinds_found: int) -> float:
    """55 when a claim is found, 40 when none is. Capped at 55: the claims are unverified."""
    return 55.0 if claim_kinds_found else 40.0


def off_05(platforms_found: int, platforms_total: int, category_match: bool) -> float:
    return platforms_found / platforms_total * 70 + (30 if category_match else 0)


def off_06_volume(review_results: int) -> int:
    return min(60, review_results * 12)


def off_06_recency(newest_review_age_days: Optional[int]) -> int:
    if newest_review_age_days is not None and newest_review_age_days <= rules.threshold("review_recent_days"):
        return 40
    if newest_review_age_days is not None and newest_review_age_days <= rules.threshold("review_stale_days"):
        return 20
    return 0


def off_07(mentions_found: int) -> float:
    return band(mentions_found, *rules.band("forum_mentions"))


def off_08(in_generic_results: bool, list_pages: int) -> float:
    return 100.0 if in_generic_results else band(list_pages, *rules.band("best_list_pages"))


def off_09(coverage_results: int, accurate: Optional[bool]) -> float:
    """20 with no coverage; with coverage 80 or 40 on the model's accuracy verdict, else 55."""
    if not coverage_results:
        return 20.0
    if accurate is None:
        return 55.0
    return 80.0 if accurate else 40.0

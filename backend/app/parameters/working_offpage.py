"""One plain-English sentence per OFF-* parameter, rebuilt from a row's stored evidence.

Each function takes the evidence dict a parameter row carries (parameter_results.evidence)
and returns (text, recomputed_score): what the scan found, with the numbers it actually
measured, and how that becomes the score, plus the score that working produces. The
arithmetic mirrors the handlers in offpage.py exactly, so the recomputed score equals
evidence["rules_based_score"]; working.py shows the sentence only when it does. The words
avoid maths symbols so anyone can follow them.

Returns None when the row was not measured (UNKNOWN) or the evidence lacks an input the
calculation needs. Never raises: a malformed row yields None rather than an error.
"""
from __future__ import annotations

import functools
from typing import Callable, Optional

from . import rules
from .offpage_agents import PROFILE_DOMAINS, REVIEW_DOMAINS

Result = Optional[tuple[str, float]]

_PROFILE_DOMAINS = PROFILE_DOMAINS
_REVIEW_DOMAINS = REVIEW_DOMAINS


def fmt(value: float) -> str:
    """1 decimal place, thousands separators, trailing ".0" dropped: 1234.0 -> "1,234"."""
    text = f"{float(value):,.1f}"
    return text[:-2] if text.endswith(".0") else text


def _safe(fn: Callable[[dict], Result]) -> Callable[[dict], Result]:
    @functools.wraps(fn)
    def wrapper(evidence: dict) -> Result:
        try:
            if not isinstance(evidence, dict):
                return None
            # Present and None means the rules-based pass could not evaluate this row.
            if "rules_based_score" in evidence and evidence["rules_based_score"] is None:
                return None
            out = fn(evidence)
            if out is None:
                return None
            text, score = out
            return str(text), float(score)
        except Exception:
            return None
    return wrapper


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _band(value: float, name: str) -> float:
    bands, floor = rules.band(name)
    for minimum, score in bands:
        if value >= minimum:
            return score
    return floor


def _found(ev: dict, domains: tuple[str, ...]) -> Optional[list[str]]:
    found = ev.get("platforms_found")
    if isinstance(found, list):
        return [d for d in domains if d in found]
    matches = ev.get("matches")
    if isinstance(matches, dict):
        return [d for d in domains if matches.get(d)]
    return None


def _so(score: float) -> str:
    return f"so it scores {fmt(score)}."


def _and(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


@_safe
def off_01(ev: dict) -> Result:
    wd = ev.get("wikidata") or {}
    wp = ev.get("wikipedia") or {}
    on_wd, on_wp = wd.get("present"), wp.get("present")
    if on_wd is None and on_wp is None:
        return None
    parts = []
    for name, present, entry in (("Wikidata", on_wd, (wd.get("entry") or {}).get("label")), ("Wikipedia", on_wp, wp.get("article"))):
        if present:
            parts.append(f"the company is on {name}" + (f" ('{entry}')" if entry else ""))
        elif present is False:
            parts.append(f"the company is not on {name}")
        else:
            parts.append(f"{name} could not be checked")
    second = parts[1].removeprefix("the company is ") if parts[0].startswith("the company is ") else parts[1]
    found = f"{parts[0][0].upper()}{parts[0][1:]} and {second}"
    if on_wd and on_wp:
        return f"{found}, so it scores 100.", 100.0
    return f"{found}. Both are needed, so it scores 0.", 0.0


@_safe
def off_02(ev: dict) -> Result:
    entity = ev.get("entity")
    if entity is None and "entity" in ev:
        return "No Wikidata entry matches the company, so there is nothing for a Knowledge Panel to be built from and it scores 0.", 0.0
    if not isinstance(entity, dict):
        return None
    desc = 30 if entity.get("description") else 0
    if "brand_in_label" not in ev:
        return None
    brand = 20 if ev["brand_in_label"] else 0
    total = min(100, 50 + desc + brand)
    desc_text = "30 more for having a description" if desc else "nothing for a description (it has none)"
    brand_text = "20 more for carrying the brand name" if brand else "nothing for the brand name (it is not in the entry's name)"
    return (f"The Wikidata entry '{entity.get('label', '')}' earns 50 points, {desc_text} and {brand_text},"
            f" {_so(total)}"), total


@_safe
def off_03(ev: dict) -> Result:
    found = _found(ev, _PROFILE_DOMAINS)
    if found is None:
        return None
    name = _num(ev.get("name_consistent_share")) or 0.0
    loc = _num(ev.get("location_consistent_share")) or 0.0
    presence = len(found) / 4 * 70
    score = presence + name * 15 + loc * 15
    if not found:
        return f"None of the 4 business profiles ({_and(list(_PROFILE_DOMAINS))}) were found, {_so(score)}", score
    return (f"{len(found)} of the 4 business profiles were found ({_and(found)}) for {fmt(presence)} of 70 points."
            f" The company name matches the website on {fmt(name * 100)}% of them ({fmt(name * 15)} of 15 points) and"
            f" the location on {fmt(loc * 100)}% ({fmt(loc * 15)} of 15 points), {_so(score)}"), score


@_safe
def off_04(ev: dict) -> Result:
    claims = ev.get("claims")
    if not isinstance(claims, list):
        return None
    if not claims:
        return "No certification claims were found on the site, so it scores 40.", 40.0
    return (f"The site claims {len(claims)} kinds of certification ({_and([str(c) for c in claims])}). They could not"
            f" be verified with the issuer, so the score is held at 55."), 55.0


@_safe
def off_05(ev: dict) -> Result:
    found = _found(ev, _REVIEW_DOMAINS)
    if found is None or "category_match" not in ev:
        return None
    presence = len(found) / 4 * 70
    cat = 30 if ev["category_match"] else 0
    how = "checked by the AI" if ev.get("category_method") == "llm" else "checked by keywords"
    score = presence + cat
    listed = (f"{len(found)} of the 4 review sites list the company ({_and(found)})" if found
              else f"None of the 4 review sites ({_and(list(_REVIEW_DOMAINS))}) list the company")
    cat_text = (f"the listings use the right category (30 points, {how})" if cat
                else f"the listings do not match the company's category (0 of 30 points, {how})")
    return f"{listed} for {fmt(presence)} of 70 points, and {cat_text}, {_so(score)}", score


@_safe
def off_06(ev: dict) -> Result:
    count = _num(ev.get("review_results"))
    if count is None:
        return None
    volume = min(60, int(count) * 12)
    age = _num(ev.get("newest_review_age_days"))
    if age is not None and age <= rules.threshold("review_recent_days"):
        recency, why = 40, f"the newest review is {int(age):,} days old, within a year"
    elif age is not None and age <= rules.threshold("review_stale_days"):
        recency, why = 20, f"the newest review is {int(age):,} days old, within two years"
    elif age is None:
        recency, why = 0, "no dated review was found in the last two years"
    else:
        recency, why = 0, f"the newest review is {int(age):,} days old, older than two years"
    score = volume + recency
    return (f"{int(count)} review results were found, worth {volume} of 60 points (12 each), and {why}"
            f" ({recency} of 40 points), {_so(score)}"), score


@_safe
def off_07(ev: dict) -> Result:
    count = _num(ev.get("mentions_found"))
    if count is None:
        return None
    score = _band(count, "forum_mentions")
    m = ev.get("matches") or {}
    detail = _and([f"{len(v)} on {k}" for k, v in m.items() if isinstance(v, list) and v])
    detail = f" ({detail})" if detail else ""
    if not count:
        return f"No community discussions mention the company (6 or more earns full marks), {_so(score)}", score
    n = int(count)
    found = f"{n} community {'discussion mentions' if n == 1 else 'discussions mention'} the company{detail}"
    if count >= 6:
        return f"{found}. 6 or more earns full marks, {_so(score)}", score
    group = "3 to 5" if count >= 3 else "1 to 2"
    return f"{found}. That is in the '{group}' group (6 or more earns full marks), {_so(score)}", score


@_safe
def off_08(ev: dict) -> Result:
    if "in_generic_results" not in ev:
        return None
    topic = ev.get("topic") or "the category"
    if ev["in_generic_results"]:
        return f"The company appears in the search results for 'best {topic} companies', so it scores 100.", 100.0
    pages = ev.get("list_pages_naming_company") or []
    score = _band(len(pages), "best_list_pages")
    named = (f"{len(pages)} 'best' or 'top' list {'page names' if len(pages) == 1 else 'pages name'} it" if pages
             else "no 'best' or 'top' list names it")
    return f"The company is not in the search results for 'best {topic} companies', and {named}, {_so(score)}", score


@_safe
def off_09(ev: dict) -> Result:
    snippets = ev.get("snippets")
    if not isinstance(snippets, list):
        return None
    if not snippets:
        return "No analyst or trade-press coverage was found, so it scores 20.", 20.0
    if ev.get("method") == "llm":
        accurate = bool((ev.get("llm") or {}).get("accurate"))
        score = 80.0 if accurate else 40.0
        verdict = "describes the company accurately" if accurate else "does not describe the company accurately"
        return f"{_coverage_found(len(snippets))} and the AI judged that it {verdict}, {_so(score)}", score
    return f"{_coverage_found(len(snippets))}, but the AI could not check its accuracy, so it scores 55.", 55.0


def _coverage_found(n: int) -> str:
    return f"{n} {'piece' if n == 1 else 'pieces'} of analyst or press coverage {'was' if n == 1 else 'were'} found"

WORKING: dict[str, Callable[[dict], Result]] = {
    "OFF-01": off_01, "OFF-02": off_02, "OFF-03": off_03, "OFF-04": off_04, "OFF-05": off_05,
    "OFF-06": off_06, "OFF-07": off_07, "OFF-08": off_08, "OFF-09": off_09,
}

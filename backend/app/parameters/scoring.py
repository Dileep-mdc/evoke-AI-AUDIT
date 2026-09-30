from __future__ import annotations

from ..config import WEIGHTS
from .formula_simple import logic_for
from .working import working_for

# Stamped on every report so a report saved under an older method can be recognised and
# recalculated (see api/scans.py::_current_report).
SCORING_METHOD = "rules_fixed_points_v8"  # v8: ids renumbered 1..N; v7: OFF-01 yes/no; v6: OFF-08 retired


def scored_rows(results: list[dict]) -> list[dict]:
    """The rows that actually carry a score, matching what category_score() averages."""
    return [r for r in results if r["status"] != "UNKNOWN" and r.get("score") is not None]


def final_from_rules(row: dict) -> dict:
    """The row as the report scores it: the rules-based score is the final score.

    Scans saved before this rule stored the model's score as the final one, with the
    rules-based score kept in evidence["rules_based_score"]. Reading them through here puts
    every scan -- old or new -- on the same footing as the calculation workbook: the Score
    Logic applied to the measured values, and nothing else. The model's number is kept
    beside it as evidence["model_score"] for reference.
    """
    evidence = row.get("evidence")
    if not isinstance(evidence, dict) or "rules_based_score" not in evidence:
        return row
    rules = evidence["rules_based_score"]
    if row.get("status") == "UNKNOWN" or rules is None:
        return row
    out = dict(row)
    out["evidence"] = ev = dict(evidence)
    if row.get("score") is not None and "model_score" not in ev:
        ev["model_score"] = row["score"]
    out["score"] = round(float(rules), 1)
    out["status"] = status_from_score(out["score"], row.get("pass_threshold", 90), row.get("partial_threshold", 60))
    if out["status"] == "PASS":
        out["recommendation"] = None
    return out


def status_from_score(score: float, pass_at: float = 90, partial_at: float = 60) -> str:
    if score >= pass_at:
        return "PASS"
    if score >= partial_at:
        return "PARTIAL"
    return "FAIL"


def coefficients(results: list[dict]) -> dict[str, float]:
    """Each parameter's fixed share, in points, of the 100 available points.

    A section's weight is split over every parameter in it, measured or not (Technical
    35 / 22, On-Page 40 / 20, Off-Page 25 / 10 with the shipped registry), so a parameter
    is worth the same on every scan and two sites are always compared on one scale. An
    UNKNOWN parameter keeps its share here; it is simply left out of the points that
    could be measured (see measurable_points()), so it neither earns nor costs anything.
    """
    section_weight: dict[str, float] = {}
    for r in results:
        section_weight[r["section"]] = section_weight.get(r["section"], 0.0) + float(r.get("weight") or 1)
    return {
        r["parameter_id"]: float(r.get("weight") or 1) / section_weight[r["section"]] * WEIGHTS[r["section"]] * 100
        for r in results
        if r["section"] in WEIGHTS
    }


def contributions(results: list[dict]) -> list[dict]:
    """Exact point attribution per measured parameter: its ceiling, what it earned, what it lost."""
    coeffs = coefficients(results)
    out = []
    for r in scored_rows(results):
        coefficient = coeffs[r["parameter_id"]]
        earned = coefficient * float(r["score"]) / 100
        out.append({
            "parameter_id": r["parameter_id"],
            "section": r["section"],
            "coefficient": coefficient,
            "points_earned": earned,
            "points_lost": coefficient - earned,
        })
    return out


def measurable_points(results: list[dict], section: str | None = None) -> float:
    """The points that could be measured: the fixed shares of every scored parameter."""
    return sum(c["coefficient"] for c in contributions(results) if section is None or c["section"] == section)


def scrape_confidence(page) -> dict:
    """A 0-100 confidence score for how far a crawled page's scraped data can be trusted.

    Derived only from signals already captured during the crawl (HTTP status, bot-block
    detection, extracted text volume, title presence) -- no extra fetch is made. This is
    confidence in the scrape, not a judgment of the content.

    Lives here with the other scoring functions rather than in the export layer, so the
    number the report shows is produced by the same module that produces every other score.
    """
    fetch = page.result
    status = fetch.status_code if fetch else None
    error = fetch.error if fetch else "No response received"
    if status is None or error:
        return {"score": 0, "label": "Failed", "reasons": [error or "No response received"]}

    score = 100
    reasons: list[str] = []
    if not (200 <= status < 300):
        score -= 35
        reasons.append(f"non-2xx HTTP status ({status})")
    if page.blocked_reason:
        score -= 50
        reasons.append(page.blocked_reason)
    if page.word_count < 40:
        score -= 30
        reasons.append("very little visible text was extracted (page may be JS-only or blocked)")
    elif page.word_count < 150:
        score -= 10
        reasons.append("only a small amount of visible text was extracted")
    if not page.title:
        score -= 5
        reasons.append("no <title> was found")
    score = max(0, min(100, score))
    label = "High" if score >= 75 else "Medium" if score >= 40 else "Low"
    if not reasons:
        reasons.append("clean 2xx response with substantial extracted text")
    return {"score": score, "label": label, "reasons": reasons}


def category_score(results: list[dict], section: str) -> float | None:
    rows = [r for r in results if r["section"] == section and r["status"] != "UNKNOWN" and r.get("score") is not None]
    if not rows:
        return None
    num = sum(float(r["score"]) * float(r.get("weight") or 1) for r in rows)
    den = sum(100.0 * float(r.get("weight") or 1) for r in rows)
    return round(num / den * 100, 1) if den else None


def overall_score(results: list[dict]) -> float | None:
    """Points earned / points measurable x 100, over every measured parameter.

    Computed from the unrounded points, so the headline always equals what the
    per-parameter points add up to.
    """
    measurable = measurable_points(results)
    if not measurable:
        return None
    earned = sum(c["points_earned"] for c in contributions(results))
    return round(earned / measurable * 100, 1)


def status_counts(results: list[dict]) -> dict:
    counts = {"pass": 0, "partial": 0, "fail": 0, "unknown": 0}
    for r in results:
        key = (r.get("status") or "UNKNOWN").lower()
        if key in counts:
            counts[key] += 1
    return counts


def label_for_score(score: float | None) -> str:
    if score is None:
        return "Unknown"
    if score >= 90:
        return "Excellent"
    if score >= 75:
        return "Good"
    if score >= 60:
        return "Fair"
    if score >= 40:
        return "Poor"
    return "Critical"


# Severity bands are fractions of the mean parameter coefficient rather than fixed point
# values, so the bands follow the registry's section sizes and weights if those change.
HIGH_IMPACT_SHARE = 0.8
MEDIUM_IMPACT_SHARE = 0.4


def prioritize(results: list[dict]) -> list[dict]:
    """Rank the fixable findings by the exact number of points each is costing."""
    results = [final_from_rules(r) for r in results]
    scored = contributions(results)
    if not scored:
        return []
    points_lost = {c["parameter_id"]: c["points_lost"] for c in scored}
    # Banded on every parameter's fixed share, not just the measured ones, so a check's
    # severity does not move when unrelated checks happen to be unmeasurable on this scan.
    fixed = coefficients(results)
    mean_coefficient = sum(fixed.values()) / len(fixed)
    high = mean_coefficient * HIGH_IMPACT_SHARE
    medium = mean_coefficient * MEDIUM_IMPACT_SHARE

    issues = []
    for r in results:
        if r["status"] in {"UNKNOWN", "PASS"}:
            continue
        impact = points_lost.get(r["parameter_id"])
        if impact is None:
            continue
        severity = (
            "High Impact" if impact >= high
            else "Medium Impact" if impact >= medium
            else "Low Impact"
        )
        issues.append({
            "issue_id": f"ISSUE-{r['parameter_id']}",
            "parameter_id": r["parameter_id"],
            "severity": severity,
            "category": {
                "technical": "Technical",
                "on_page": "Content",
                "off_page": "Reputation",
            }.get(r["section"], r["section"]),
            "title": r["name"],
            "score_impact": -round(impact, 3),
            "effort": "Medium",
            "recommendation": r.get("recommendation") or "Improve this parameter using the stored evidence.",
        })
    issues.sort(key=lambda x: x["score_impact"])
    return issues


def build_report(scan: dict, results: list[dict], issues: list[dict]) -> dict:
    results = [final_from_rules(r) for r in results]
    tech = category_score(results, "technical")
    onpage = category_score(results, "on_page")
    offpage = category_score(results, "off_page")
    overall = overall_score(results)
    counts = status_counts(results)
    known = sum(counts[k] for k in ("pass", "partial", "fail"))
    coeffs = coefficients(results)
    points = {c["parameter_id"]: c for c in contributions(results)}
    earned_by_section: dict[str, float] = {}
    for c in points.values():
        earned_by_section[c["section"]] = earned_by_section.get(c["section"], 0.0) + c["points_earned"]
    parameters = []
    for r in results:
        c = points.get(r["parameter_id"])
        parameters.append({
            **r,
            "working": working_for(r["parameter_id"], r.get("evidence"), r.get("score")),
            "logic": logic_for(r["parameter_id"]),
            "max_points": round(coeffs.get(r["parameter_id"], 0.0), 3),
            "points_earned": round(c["points_earned"], 3) if c else None,
            "points_lost": round(c["points_lost"], 3) if c else None,
        })
    measurable_total = measurable_points(results)
    return {
        "scan_id": scan["id"],
        "domain": scan["domain"],
        "input_url": scan["input_url"],
        "generated_at": scan.get("completed_at") or scan.get("started_at"),
        "started_at": scan.get("started_at"),
        "completed_at": scan.get("completed_at"),
        "crawler_version": scan.get("crawler_version"),
        "scoring_method": SCORING_METHOD,
        "overall_score": overall,
        "overall_label": label_for_score(overall),
        "category_scores": {
            "technical": tech,
            "on_page": onpage,
            "off_page": offpage,
        },
        "category_labels": {
            "technical": label_for_score(tech),
            "on_page": label_for_score(onpage),
            "off_page": label_for_score(offpage),
        },
        "weights": WEIGHTS,
        # Points earned per pillar, out of that pillar's fixed share of 100.
        "category_contributions": {
            section: round(earned_by_section.get(section, 0.0), 3) for section in WEIGHTS
        },
        "category_points_measurable": {
            section: round(measurable_points(results, section), 3) for section in WEIGHTS
        },
        "points": {
            "available": 100.0,
            "measurable": round(measurable_total, 3),
            "earned": round(sum(earned_by_section.values()), 3),
        },
        "status_counts": counts,
        "coverage": {"scorable_parameters": len(results), "known": known, "unknown": counts["unknown"]},
        "parameters": parameters,
        "top_issues": issues[:8],
        "issues": issues,
    }

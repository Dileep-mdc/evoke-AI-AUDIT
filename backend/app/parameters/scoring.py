from __future__ import annotations

from ..config import WEIGHTS


def scored_rows(results: list[dict]) -> list[dict]:
    """The rows that actually carry a score, matching what category_score() averages."""
    return [r for r in results if r["status"] != "UNKNOWN" and r.get("score") is not None]


def coefficients(results: list[dict]) -> dict[str, float]:
    """Each parameter's exact share, in points, of the 100-point overall score.

    Both denominators below count only what was measured, because that is what the
    scoring functions themselves divide by: category_score() sums the weight of scored
    rows, and overall_score() sums the weight of sections that produced a score. So an
    UNKNOWN parameter does not score zero -- it hands its share to its scored siblings,
    and a section that returns nothing at all hands its share to the other sections.
    Ignoring that is how a per-parameter breakdown ends up not summing to the score it
    claims to explain.
    """
    scored = scored_rows(results)
    section_weight: dict[str, float] = {}
    for r in scored:
        section_weight[r["section"]] = section_weight.get(r["section"], 0.0) + float(r.get("weight") or 1)
    live_weight = sum(WEIGHTS[s] for s in section_weight)
    if not live_weight:
        return {}
    return {
        r["parameter_id"]: (float(r.get("weight") or 1) / section_weight[r["section"]])
        * (WEIGHTS[r["section"]] / live_weight)
        * 100
        for r in scored
    }


def contributions(results: list[dict]) -> list[dict]:
    """Exact point attribution per parameter: its ceiling, what it earned, what it forfeits.

    Because both scoring steps are weighted averages -- linear in the parameter scores --
    these are exact, not estimates. points_earned summed over every row reproduces the
    overall score up to the two round(..., 1) calls applied on the way out.
    """
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


def overall_score(tech: float | None, onpage: float | None, offpage: float | None) -> float | None:
    parts = []
    weights = []
    mapping = {"technical": tech, "on_page": onpage, "off_page": offpage}
    for key, val in mapping.items():
        if val is not None:
            parts.append(val * WEIGHTS[key])
            weights.append(WEIGHTS[key])
    if not weights:
        return None
    return round(sum(parts) / sum(weights), 1)


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
# values. Unmeasured checks inflate every surviving coefficient (see coefficients()), so
# banding on absolute points would promote a whole report to "High Impact" just because a
# data source was unavailable -- the cost really is higher, but the priority order isn't.
HIGH_IMPACT_SHARE = 0.8
MEDIUM_IMPACT_SHARE = 0.4


def prioritize(results: list[dict]) -> list[dict]:
    """Rank the fixable findings by the exact number of overall-score points each is costing."""
    scored = contributions(results)
    if not scored:
        return []
    points_lost = {c["parameter_id"]: c["points_lost"] for c in scored}
    mean_coefficient = sum(c["coefficient"] for c in scored) / len(scored)
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
    tech = category_score(results, "technical")
    onpage = category_score(results, "on_page")
    offpage = category_score(results, "off_page")
    overall = overall_score(tech, onpage, offpage)
    counts = status_counts(results)
    known = sum(counts[k] for k in ("pass", "partial", "fail"))
    # Each pillar's actual points out of 100, summed from the per-parameter attribution so
    # the dashboard displays this rather than multiplying score by weight itself -- that
    # shortcut is wrong whenever a pillar goes unscored and the weights renormalise.
    earned_by_section: dict[str, float] = {}
    for c in contributions(results):
        earned_by_section[c["section"]] = earned_by_section.get(c["section"], 0.0) + c["points_earned"]
    return {
        "scan_id": scan["id"],
        "domain": scan["domain"],
        "input_url": scan["input_url"],
        "generated_at": scan.get("completed_at") or scan.get("started_at"),
        "started_at": scan.get("started_at"),
        "completed_at": scan.get("completed_at"),
        "crawler_version": scan.get("crawler_version"),
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
        "category_contributions": {
            section: round(earned_by_section.get(section, 0.0), 3) for section in WEIGHTS
        },
        "status_counts": counts,
        "coverage": {"scorable_parameters": len(results), "known": known, "unknown": counts["unknown"]},
        "parameters": results,
        "top_issues": issues[:8],
        "issues": issues,
    }

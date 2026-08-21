from __future__ import annotations

from ..config import WEIGHTS


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


def prioritize(results: list[dict]) -> list[dict]:
    issues = []
    for r in results:
        if r["status"] in {"UNKNOWN", "PASS"}:
            continue
        score = float(r.get("score") or 0)
        gap = max(0, 90 - score)
        sev = 1.4 if r.get("weight", 1) >= 1.5 else 1.0
        impact = round(gap * sev * 0.12, 1)
        severity = "High Impact" if impact >= 5 or r.get("weight", 1) >= 1.5 else ("Medium Impact" if impact >= 2.5 else "Low Impact")
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
            "score_impact": -impact,
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
        "status_counts": counts,
        "coverage": {"scorable_parameters": 62, "known": known, "unknown": counts["unknown"]},
        "parameters": results,
        "top_issues": issues[:8],
        "issues": issues,
    }

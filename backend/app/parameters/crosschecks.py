"""Reading the checks together, after they have all run.

Each check is scored on its own, and some pairs then read as contradictions to a client: robots.txt
"allows AI crawlers" (TECH-01 = 100) while the firewall "blocks AI crawlers" (TECH-02 = 40);
canonical tags are "fine" (TECH-20 = 100) while 330 near-duplicate pages remain (ON-19). Both
halves are true. This module says so on the rows involved, and lifts one finding above the rest:
AI crawlers being unable to reach the site at all, which gates every other improvement.

Notes never change a score. They are written into the row's evidence (`cross_check`) and in front
of its recommendation, so the report, the workbook and the score panel all carry them.
"""
from __future__ import annotations

from typing import Optional

GATING_PARAMETERS = ("TECH-01", "TECH-02")


def _evidence(row: Optional[dict]) -> dict:
    return (row or {}).get("evidence") or {}


def _note(row: dict, text: str) -> None:
    row["evidence"] = {**_evidence(row), "cross_check": text}
    rec = row.get("recommendation")
    row["recommendation"] = f"{text} {rec}" if rec else (text if row.get("status") != "PASS" else rec)


def _robots_verdicts(row: Optional[dict]) -> tuple[list[str], list[str]]:
    decisions = _evidence(row).get("decisions") or []
    allowed = [d["name"] for d in decisions if d.get("decision") == "ALLOWED"]
    blocked = [d["name"] for d in decisions if d.get("decision") == "BLOCKED"]
    return allowed, blocked


def _cdn_blocked(row: Optional[dict]) -> list[str]:
    return [b["bot"] for b in _evidence(row).get("bots") or [] if b.get("blocked")]


def reconcile(results: list[dict], ctx=None) -> dict:
    """Annotate contradictory-looking rows in place; return the report-level findings."""
    by = {r["parameter_id"]: r for r in results}
    findings: dict = {"gating": None, "cross_checks": []}

    # TECH-01 vs TECH-02: the site's policy says yes, its security layer says no.
    allowed, robots_blocked = _robots_verdicts(by.get("TECH-01"))
    cdn_blocked = _cdn_blocked(by.get("TECH-02"))
    allowed_but_blocked = [b for b in cdn_blocked if b in allowed]
    if allowed_but_blocked:
        names = ", ".join(allowed_but_blocked)
        _note(by["TECH-02"], f"robots.txt allows these AI crawlers ({names}), but the CDN or firewall still blocks them, "
                             "so they cannot reach the pages. The site's stated policy and its security setup disagree.")
        _note(by["TECH-01"], f"robots.txt allows the AI crawlers, but the CDN or firewall blocks {len(allowed_but_blocked)} "
                             "of them anyway (see TECH-02), so allowing them here is not enough on its own.")
        findings["cross_checks"].append({"parameters": ["TECH-01", "TECH-02"], "bots": allowed_but_blocked})

    # Gating: if AI crawlers cannot get in, nothing else on the site reaches an AI assistant.
    blocked = sorted(set(robots_blocked) | set(cdn_blocked))
    if blocked:
        how = " and ".join(filter(None, [
            "robots.txt" if robots_blocked else "",
            "the CDN or firewall" if cdn_blocked else "",
        ]))
        detail = (f"{len(blocked)} AI crawler{'s are' if len(blocked) != 1 else ' is'} blocked by {how} ({', '.join(blocked)}). "
                  "Until they can reach the site, AI assistants cannot read the pages this audit scored, so fix this "
                  "first: improvements elsewhere stay invisible to them.")
        crawl_blocked = sum(1 for p in getattr(ctx, "pages", None) or []
                            if getattr(getattr(p, "result", None), "ok", False) and getattr(p, "blocked_reason", ""))
        if crawl_blocked:
            detail += (f" The site also refused this audit's own crawler on {crawl_blocked} page"
                       f"{'s' if crawl_blocked != 1 else ''}, so results for those pages may be incomplete.")
        findings["gating"] = {"title": "Gating issue: AI crawler access is blocked", "detail": detail,
                              "blocked_bots": blocked, "blocked_by_robots": robots_blocked,
                              "blocked_by_cdn": cdn_blocked, "parameters": list(GATING_PARAMETERS),
                              "pages_refused_to_audit_crawler": crawl_blocked}

    # TECH-20 vs ON-19 / TECH-22: canonicals tell search engines which copy to index; they do not
    # make duplicate pages or titles go away.
    canonical = by.get("TECH-20")
    dup_pairs = _evidence(by.get("ON-19")).get("duplicate_pairs") or 0
    unique_titles = _evidence(by.get("TECH-22")).get("unique_title_ratio")
    dup_titles = unique_titles is not None and unique_titles < 1
    if canonical and canonical.get("score") is not None and canonical["score"] >= 60 and (dup_pairs or dup_titles):
        parts = [f"{dup_pairs} near-duplicate page pair{'s' if dup_pairs != 1 else ''} (ON-19)" if dup_pairs else "",
                 "duplicate page titles (TECH-22)" if dup_titles else ""]
        _note(canonical, "Canonical tags are set, which tells search engines which copy of a page to index. "
                         f"But {' and '.join(p for p in parts if p)} remain: AI assistants read whichever copy they "
                         "reach, so those pages still need to be merged or made distinct.")
        findings["cross_checks"].append({"parameters": ["TECH-20", "ON-19", "TECH-22"], "duplicate_pairs": dup_pairs})
    return findings


def content_analysis(ctx) -> dict:
    """Whether the content checks read what a browser shows or only the server's raw HTML."""
    pages = [p for p in getattr(ctx, "pages", None) or [] if getattr(getattr(p, "result", None), "ok", False)]
    rendered = sum(1 for p in pages if (getattr(p, "render", None) or {}).get("content_source") == "rendered")
    render = getattr(ctx, "render", None) or {}
    if rendered:
        summary = (f"Rendered browser content for {rendered} of {len(pages)} pages (where JavaScript changes the page); "
                   f"raw HTML for the rest.")
    else:
        summary = "Raw HTML for all pages" + (f" ({render.get('reason')})" if render.get("reason") else ".")
    return {"rendered_pages": rendered, "raw_pages": len(pages) - rendered, "pages": len(pages),
            "render_decision": render.get("decision"), "summary": summary}


def gating_first(issues: list[dict], gating: Optional[dict]) -> list[dict]:
    """The gating issues lead the list, whatever points they cost: they block everything else."""
    if not gating:
        return issues
    lead = [dict(i, gating=True) for i in issues if i["parameter_id"] in GATING_PARAMETERS]
    return lead + [i for i in issues if i["parameter_id"] not in GATING_PARAMETERS]

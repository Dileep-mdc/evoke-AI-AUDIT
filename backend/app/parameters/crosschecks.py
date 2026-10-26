"""Reading the checks together, after they have all run.

Each check is scored on its own, and some pairs then read as contradictions to a client: robots.txt
"allows AI crawlers" (TECH-01 = 100) while the firewall "blocks AI crawlers" (TECH-02 = 40);
canonical tags are "fine" (TECH-20 = 100) while 330 near-duplicate pages remain (ON-19). Both
halves are true. This module says so on the rows involved.

Notes never change a score. They are written into the row's evidence (`cross_check`), which the
workbook's explanation column and the score panel both show. They are kept out of the
recommendation: a fix says what to change, and a note in front of it made TECH-19's "fix" an
explanation of how TECH-16, 17 and 18 relate.
"""
from __future__ import annotations

from typing import Optional


def _evidence(row: Optional[dict]) -> dict:
    return (row or {}).get("evidence") or {}


def _note(row: dict, text: str) -> None:
    row["evidence"] = {**_evidence(row), "cross_check": text}


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
    findings: dict = {"cross_checks": []}

    # TECH-01 vs TECH-02: the site's policy says yes, its security layer says no.
    allowed, _ = _robots_verdicts(by.get("TECH-01"))
    cdn_blocked = _cdn_blocked(by.get("TECH-02"))
    allowed_but_blocked = [b for b in cdn_blocked if b in allowed]
    if allowed_but_blocked:
        names = ", ".join(allowed_but_blocked)
        _note(by["TECH-02"], f"robots.txt allows these AI crawlers ({names}), but the CDN or firewall still blocks them, "
                             "so they cannot reach the pages. The site's stated policy and its security setup disagree.")
        _note(by["TECH-01"], f"robots.txt allows the AI crawlers, but the CDN or firewall blocks {len(allowed_but_blocked)} "
                             "of them anyway (see TECH-02), so allowing them here is not enough on its own.")
        findings["cross_checks"].append({"parameters": ["TECH-01", "TECH-02"], "bots": allowed_but_blocked})

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

    # TECH-19 vs TECH-16 / TECH-17: "markup validates" next to "no Organization / page-type markup"
    # reads as a contradiction unless the report says what was validated.
    validates = by.get("TECH-19")
    missing = [pid for pid in ("TECH-16", "TECH-17", "TECH-18") if (by.get(pid) or {}).get("score") == 0]
    if validates and validates.get("score") is not None and missing:
        ev = _evidence(validates)
        types = ev.get("types_found") or []
        what = f"the {ev.get('blocks')} JSON-LD block{'s' if ev.get('blocks') != 1 else ''} the site has"
        if types:
            what += f" ({', '.join(types)})"
        kinds = {"TECH-16": "the company (Organization) markup on the homepage that TECH-16 looks for",
                 "TECH-17": "the page-type markup (Article, Service, Product ...) on each page that TECH-17 looks for",
                 "TECH-18": "the author (Person) markup that TECH-18 looks for"}
        listed = [kinds[m] for m in missing]
        joined = listed[0] if len(listed) == 1 else f"{', '.join(listed[:-1])} or {listed[-1]}"
        _note(validates, f"This checks whether existing markup is well-formed: it validated {what}. It does not include "
                         f"{joined}, which is why {'that check scores' if len(missing) == 1 else 'those score'} 0: "
                         "valid markup and missing markup are separate findings.")
        findings["cross_checks"].append({"parameters": ["TECH-19", *missing], "types": types})

    # TECH-18 vs ON-12: author markup (machine-readable) and named people in the visible bylines.
    authors, bylines = by.get("TECH-18"), by.get("ON-12")
    named = (_evidence(bylines).get("llm") or {}).get("named_people")
    if authors and authors.get("score") is not None and isinstance(named, int):
        if authors["score"] > 0 and named == 0:
            _note(authors, "Authors are marked up in structured data, but the AI found that none of the visible bylines "
                           "name a real person (see ON-12): the markup should name the real, credentialed author.")
            findings["cross_checks"].append({"parameters": ["TECH-18", "ON-12"], "named_people": named})
        elif authors["score"] == 0 and named > 0:
            _note(authors, f"{named} visible byline{'s name' if named != 1 else ' names'} a real person (see ON-12), "
                           "but none is marked up as a Person, so AI assistants cannot read who wrote the page.")
            findings["cross_checks"].append({"parameters": ["TECH-18", "ON-12"], "named_people": named})
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

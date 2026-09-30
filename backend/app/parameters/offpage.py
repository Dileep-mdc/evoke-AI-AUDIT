"""Off-page parameters: how the wider web describes the company.

The nine parameter evaluation modules of the off-page architecture:

    Website URL -> orchestrator -> specialist agent -> tools -> evidence -> validation
                -> evaluator (here) -> deterministic scoring engine -> score -> report

Each evaluator asks the orchestrator (offpage_agents.py) for its parameter's findings, maps
them to the exact calculation inputs the frozen logic takes, has the scoring engine
(offpage_scoring.py) apply the formula, and returns the row. The row's evidence carries the
data contract: the collected evidence and extracted values, source_type, source_urls,
verification_result, calculation_inputs and parameter_status; parameter_id, score and the
evaluation timestamp are on the row itself.

A source that cannot be reached makes the check UNKNOWN -- it is never scored as zero, because
an unavailable API says nothing about the company.
"""
from __future__ import annotations

from . import offpage_scoring as scoring
from .common import ms_since, result, timed
from .offpage_agents import ORCHESTRATOR, PROFILE_DOMAINS, REVIEW_DOMAINS, Findings

RETRY_SEARCH = "Retry once web search is available."


def _contract(f: Findings, inputs: dict) -> dict:
    return {"agent": f.agent, "source_type": f.source_type, "source_urls": f.source_urls[:10],
            "parameter_status": f.status, "verification_result": f.verification, "calculation_inputs": inputs}


def _row(spec, t, f: Findings, score: float, evidence: dict, inputs: dict, rec, confidence: float):
    return result(spec, score=score, evidence={**evidence, **_contract(f, inputs)}, recommendation=rec,
                  checked=f.checked, duration_ms=ms_since(t), confidence=confidence)


def _unknown(spec, t, f: Findings, rec: str = RETRY_SEARCH):
    return result(spec, score=None, unknown=True, evidence={**f.evidence, **_contract(f, {})}, recommendation=rec,
                  checked=f.checked, error=f.error, duration_ms=ms_since(t))


# ---------------------------------------------------------------------------------------------
# OFF-01  Wikidata/Wikipedia entry present
# ---------------------------------------------------------------------------------------------
async def off_01(spec, ctx):
    t = timed()
    f = await ORCHESTRATOR.research("OFF-01", ctx)
    if not f.available:
        return _unknown(spec, t, f, "Retry once Wikidata and Wikipedia are reachable.")
    inputs = {"on_wikidata": f.values["on_wikidata"], "on_wikipedia": f.values["on_wikipedia"]}
    score = scoring.off_01(**inputs)
    missing = [step for step, key in (("create a Wikidata item for the company", "on_wikidata"),
                                      ("earn an independent Wikipedia article about it", "on_wikipedia"))
               if not inputs[key]]
    steps = " and ".join(missing)
    rec = None if not missing else f"{steps[:1].upper()}{steps[1:]}; both are needed for this check to pass."
    return _row(spec, t, f, score, f.evidence, inputs, rec, confidence=0.85)


# ---------------------------------------------------------------------------------------------
# OFF-02  Google Knowledge Panel presence and consistency
# ---------------------------------------------------------------------------------------------
async def off_02(spec, ctx):
    t = timed()
    f = await ORCHESTRATOR.research("OFF-02", ctx)
    if not f.available:
        return _unknown(spec, t, f, "Retry once Wikidata is reachable.")
    inputs = {"entity_found": f.values["entity_found"], "has_description": f.values["has_description"],
              "brand_in_label": f.values["brand_in_label"]}
    score = scoring.off_02(**inputs)
    if not inputs["entity_found"]:
        rec = "Create a Wikidata item for the company (name, description, website, social profiles) so a Knowledge Panel can be built."
    else:
        rec = "Keep the identity fields a Knowledge Panel draws on (name, description, website) accurate and consistent." if score < 90 else None
    return _row(spec, t, f, score, f.evidence, inputs, rec, confidence=0.4)


# ---------------------------------------------------------------------------------------------
# OFF-03  Company profiles complete, with details consistent across own and third-party sites
# ---------------------------------------------------------------------------------------------
async def off_03(spec, ctx):
    t = timed()
    f = await ORCHESTRATOR.research("OFF-03", ctx)
    if not f.available:
        return _unknown(spec, t, f)
    found, profiles = f.values["platforms_found"], f.values["profiles"]
    name_share = sum(1 for v in profiles.values() if v["name_matches"]) / len(found) if found else 0.0
    loc_share = sum(1 for v in profiles.values() if v["location_matches"]) / len(found) if found else 0.0
    inputs = {"platforms_found": len(found), "platforms_total": len(PROFILE_DOMAINS),
              "name_share": name_share, "location_share": loc_share}
    score = scoring.off_03(**inputs)
    evidence = {**f.evidence, "name_consistent_share": round(name_share, 3), "location_consistent_share": round(loc_share, 3)}
    missing = [d for d in PROFILE_DOMAINS if d not in found]
    rec = None if score >= 90 else ("Create or claim company profiles on: " + ", ".join(missing) + ". " if missing else "") + "Use the same company name and office locations on every profile as on the website."
    return _row(spec, t, f, score, evidence, inputs, rec, confidence=0.5)


# ---------------------------------------------------------------------------------------------
# OFF-04  Certifications verifiable off-site (CMMI, ISO, partner tiers)
# ---------------------------------------------------------------------------------------------
async def off_04(spec, ctx):
    t = timed()
    f = await ORCHESTRATOR.research("OFF-04", ctx)
    inputs = {"claim_kinds_found": len(f.values["claims"])}
    score = scoring.off_04(**inputs)
    if not inputs["claim_kinds_found"]:
        return _row(spec, t, f, score, f.evidence, inputs, "Publish verifiable certifications and link to the issuer's record.", confidence=0.85)
    return _row(spec, t, f, score, f.evidence, inputs, "Verify each certification on the issuer or partner directory and link the record from the site.", confidence=0.4)


# ---------------------------------------------------------------------------------------------
# OFF-05  Review platform completeness and category placement
# ---------------------------------------------------------------------------------------------
async def off_05(spec, ctx):
    t = timed()
    f = await ORCHESTRATOR.research("OFF-05", ctx)
    if not f.available:
        return _unknown(spec, t, f)
    found = f.values["platforms_found"]
    llm_match = f.values["llm_category_match"]
    category_match = f.values["rules_category_match"] if llm_match is None else llm_match
    inputs = {"platforms_found": len(found), "platforms_total": len(REVIEW_DOMAINS), "category_match": category_match}
    score = scoring.off_05(**inputs)
    evidence = {**f.evidence, "category_match": category_match}
    missing = [d for d in REVIEW_DOMAINS if d not in found]
    rec = None if score >= 90 else " ".join(filter(None, [
        f"Claim and complete a profile on: {', '.join(missing)}." if missing else "",
        "" if category_match else "List the company under the category it uses on its own site.",
    ]))
    return _row(spec, t, f, score, evidence, inputs, rec, confidence=0.5)


# ---------------------------------------------------------------------------------------------
# OFF-06  Review volume and recency
# ---------------------------------------------------------------------------------------------
async def off_06(spec, ctx):
    t = timed()
    f = await ORCHESTRATOR.research("OFF-06", ctx)
    if not f.available:
        return _unknown(spec, t, f)
    inputs = {"review_results": f.values["review_results"], "newest_review_age_days": f.values["newest_review_age_days"]}
    volume = scoring.off_06_volume(inputs["review_results"])
    recency = scoring.off_06_recency(inputs["newest_review_age_days"])
    score = volume + recency
    evidence = {**f.evidence, "volume_points": volume, "recency_points": recency}
    rec = None if score >= 90 else "Ask customers for fresh reviews on G2, Clutch, Gartner Peer Insights and TrustRadius, and keep them coming steadily."
    return _row(spec, t, f, score, evidence, inputs, rec, confidence=0.4)


# ---------------------------------------------------------------------------------------------
# OFF-07  Reddit, Quora and industry forum presence
# ---------------------------------------------------------------------------------------------
async def off_07(spec, ctx):
    t = timed()
    f = await ORCHESTRATOR.research("OFF-07", ctx)
    if not f.available:
        return _unknown(spec, t, f)
    inputs = {"mentions_found": f.values["mentions_found"]}
    score = scoring.off_07(**inputs)
    rec = None if score >= 90 else "Take part genuinely in Reddit, Quora and industry-forum discussions where buyers ask about this category."
    return _row(spec, t, f, score, f.evidence, inputs, rec, confidence=0.45)


# ---------------------------------------------------------------------------------------------
# OFF-08  Presence on best/top service lists in the category
# ---------------------------------------------------------------------------------------------
async def off_08(spec, ctx):
    t = timed()
    f = await ORCHESTRATOR.research("OFF-08", ctx)
    if not f.available:
        return _unknown(spec, t, f)
    inputs = {"in_generic_results": f.values["in_generic_results"], "list_pages": f.values["list_pages"]}
    score = scoring.off_08(**inputs)
    rec = None if score >= 90 else f"Earn a place on independent 'best {f.evidence['topic']}' lists (analyst round-ups, directories, trade press)."
    return _row(spec, t, f, score, f.evidence, inputs, rec, confidence=0.45)


# ---------------------------------------------------------------------------------------------
# OFF-09  Analyst, directory and trade-press coverage and description accuracy
# ---------------------------------------------------------------------------------------------
async def off_09(spec, ctx):
    t = timed()
    f = await ORCHESTRATOR.research("OFF-09", ctx)
    if not f.available:
        return _unknown(spec, t, f)
    inputs = {"coverage_results": f.values["coverage_results"], "accurate": f.values["llm_accurate"]}
    score = scoring.off_09(**inputs)
    if not inputs["coverage_results"]:
        return _row(spec, t, f, score, f.evidence, inputs, "Pursue analyst, directory and trade-press coverage.", confidence=0.3)
    rec = "Pursue additional analyst/trade-press coverage and correct any inaccurate descriptions found." if score < 90 else None
    return _row(spec, t, f, score, f.evidence, inputs, rec, confidence=0.55 if inputs["accurate"] is not None else 0.35)


HANDLERS = {
    "OFF-01": off_01, "OFF-02": off_02, "OFF-03": off_03, "OFF-04": off_04, "OFF-05": off_05,
    "OFF-06": off_06, "OFF-07": off_07, "OFF-08": off_08, "OFF-09": off_09,
}

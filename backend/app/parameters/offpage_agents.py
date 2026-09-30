"""The off-page agents and their orchestrator (OFF-page architecture, sections 2 and 6).

    Website URL -> OffPageOrchestrator -> specialist agent -> tools -> evidence
                -> Evidence & Validation Agent -> evaluator (offpage.py) -> scoring engine

Seven agents cover the nine parameters:

    Knowledge Entity Agent            OFF-01, OFF-02   Wikipedia/Wikidata and entity research
    Company Profile Agent             OFF-03           profile completeness and consistency
    Certification Verification Agent  OFF-04           certification and partner-claim checks
    Review Intelligence Agent         OFF-05, OFF-06   review platforms, category, volume, recency
    Community Presence Agent          OFF-07           Reddit, Quora and forum research
    Industry Visibility Agent         OFF-08, OFF-09   best/top lists, analyst and trade press
    Evidence & Validation Agent       all but OFF-01   opens the sources, confirms they are
                                                       about the company, sets the status

An agent researches and verifies; it does not score. It returns Findings: the evidence it
collected and the structured values the evaluator maps to calculation inputs. Where the model
is asked to verify something (OFF-05, OFF-09) a rules-based reading (`rules_*`) is taken
first, so the check still has an input when no model is available. OFF-01 is a yes/no
presence check and fetches nothing beyond the two searches.

When the evidence a check needs cannot be had, the agent says why in `status` --
External Data Required, Blocked or Unable to Verify -- and the row is reported UNKNOWN rather
than given an invented score.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote, urlparse

from ..config import OFFPAGE_VERIFY_SOURCES, WIKIDATA_API, WIKIPEDIA_API
from ..crawler.google_search import SearchResult
from ..llm.client import judge
from ..llm.prompts import SYSTEM, analyst_prompt, category_prompt
from . import offpage_tools as tools
from .common import derive_site_categories, derive_site_geographies, primary_brand

REVIEW_DOMAINS = ("g2.com", "clutch.co", "gartner.com", "trustradius.com")
PROFILE_DOMAINS = ("linkedin.com", "crunchbase.com", "bloomberg.com", "zoominfo.com")

# parameter_status values (data contract). The first three mean the evidence was not available.
EXTERNAL_DATA_REQUIRED = "External Data Required"
BLOCKED = "Blocked"
UNABLE_TO_VERIFY = "Unable to Verify"
VERIFIED = "Verified"
SOURCES_UNCONFIRMED = "Sources Unconfirmed"
NO_SOURCES = "No Sources Found"
UNAVAILABLE = (EXTERNAL_DATA_REQUIRED, BLOCKED, UNABLE_TO_VERIFY)


@dataclass
class Findings:
    """What an agent hands back: the data contract, less the score the engine adds."""
    parameter_id: str
    agent: str
    source_type: str
    checked: str                                   # the source the check read
    evidence: dict = field(default_factory=dict)   # collected evidence, in the report's key layout
    values: dict = field(default_factory=dict)     # extracted values the evaluator maps to inputs
    source_urls: list[str] = field(default_factory=list)
    status: str = NO_SOURCES
    error: Optional[str] = None
    verification: dict = field(default_factory=dict)
    require: list[str] = field(default_factory=list)   # a source must also name one of these
    verify: bool = True                                # False: the validator opens no source pages

    @property
    def available(self) -> bool:
        """False when the evidence the calculation needs could not be collected."""
        return self.error is None


def _search_failed(pid: str, agent: str, source_type: str, sr: SearchResult) -> Findings:
    """A search that returned no usable results is not a finding about the company."""
    if sr.error and "not configured" in sr.error:
        status = EXTERNAL_DATA_REQUIRED
    elif sr.status in (403, 429):
        status = BLOCKED
    else:
        status = UNABLE_TO_VERIFY
    return Findings(pid, agent, source_type, checked=sr.display_url, status=status, error=sr.error,
                    evidence={"provider": sr.provider, "query": sr.query, "error": sr.error})


def _wikidata_page(entity: Optional[dict]) -> list[str]:
    return [f"https://{urlparse(WIKIDATA_API).netloc}/wiki/{entity['id']}"] if entity and entity.get("id") else []


def _wikipedia_page(title: str) -> str:
    return f"https://{urlparse(WIKIPEDIA_API).netloc}/wiki/{quote(title.replace(' ', '_'))}"


class Agent:
    name = ""
    parameters: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()

    async def research(self, parameter_id: str, ctx) -> Findings:
        return await getattr(self, "research_" + parameter_id.lower().replace("-", "_"))(ctx)


# ---------------------------------------------------------------------------------------------
# Knowledge Entity Agent -- OFF-01, OFF-02
# ---------------------------------------------------------------------------------------------
class KnowledgeEntityAgent(Agent):
    name = "Knowledge Entity Agent"
    parameters = ("OFF-01", "OFF-02")
    tools = ("wikidata_search", "wikipedia_search", "extract_entity", "compare_entities")

    async def research_off_01(self, ctx) -> Findings:
        """Presence only: is the company on Wikidata, and is it on Wikipedia? Each is answered
        from the search result names alone -- no page or entity data is fetched, and no model
        is asked. An entry counts when its name carries the brand term."""
        wd_data, wd_meta = await tools.wikidata_search(ctx.company_name, ctx)
        wp_data, wp_meta = await tools.wikipedia_search(ctx.company_name, ctx)
        f = Findings("OFF-01", self.name, "Wikidata and Wikipedia", checked=wp_meta["url"], verify=False)
        brand = primary_brand(ctx)
        f.evidence = {"provider": "Wikidata + Wikipedia", "brand": brand}

        if wd_data is not None:
            hits = wd_data.get("search") or []
            entry = next((h for h in hits if tools.compare_entities(h.get("label") or "", [brand])), None)
            f.evidence["wikidata"] = {"present": entry is not None, "entry": entry,
                                      "results": [h.get("label") for h in hits[:5]]}
            f.values["on_wikidata"] = entry is not None
            f.source_urls += _wikidata_page(entry)
        else:
            f.evidence["wikidata"] = {"error": wd_meta.get("error") or f"HTTP {wd_meta.get('status')}"}
            f.values["on_wikidata"] = None

        if wp_data is not None:
            hits = ((wp_data.get("query") or {}).get("search") or [])
            article = next((h.get("title") for h in hits if tools.compare_entities(h.get("title") or "", [brand])), None)
            f.evidence["wikipedia"] = {"present": article is not None, "article": article,
                                       "results": [h.get("title") for h in hits[:5]]}
            f.values["on_wikipedia"] = article is not None
            if article:
                f.source_urls.append(_wikipedia_page(article))
        else:
            f.evidence["wikipedia"] = {"error": wp_meta.get("error") or f"HTTP {wp_meta.get('status')}"}
            f.values["on_wikipedia"] = None

        # Both are needed, so one confirmed absence settles it even if the other source is down.
        answers = (f.values["on_wikidata"], f.values["on_wikipedia"])
        if False not in answers and None in answers:
            f.status = UNABLE_TO_VERIFY
            f.error = ("Wikidata and Wikipedia are both unavailable" if answers == (None, None)
                       else f"{'Wikidata' if answers[0] is None else 'Wikipedia'} is unavailable")
        else:
            f.status = VERIFIED
        return f

    async def research_off_02(self, ctx) -> Findings:
        data, meta = await tools.wikidata_search(ctx.company_name, ctx)
        f = Findings("OFF-02", self.name, "Wikidata (Knowledge Panel proxy)", checked=meta["url"])
        if data is None:
            f.status, f.error = UNABLE_TO_VERIFY, meta.get("error") or "Wikidata unavailable"
            f.evidence = {"provider": "Wikidata (Knowledge Panel proxy)", **meta}
            return f
        hits = data.get("search") or []
        brand = primary_brand(ctx)
        match = tools.extract_entity(hits, brand, org_words=False, fallback_first=False)
        f.values["entity_found"] = match is not None
        if not match:
            # Wikidata answered and has no entity for the company: that is a finding, not a gap.
            f.status = VERIFIED
            f.values.update(has_description=False, brand_in_label=False)
            f.evidence = {"provider": "Wikidata (Knowledge Panel proxy)", "entity": None, "hits": hits[:3],
                          "note": "No Wikidata entity matches the company, so there is no record for a Knowledge Panel to be built from."}
            return f
        brand_in_label = brand in (match.get("label") or "").lower()
        f.values.update(has_description=bool(match.get("description")), brand_in_label=brand_in_label)
        f.evidence = {"provider": "Wikidata (Knowledge Panel proxy)", "entity": match, "brand_in_label": brand_in_label,
                      "note": "Approximated via Wikidata; the live Google Knowledge Panel is not read directly."}
        f.source_urls = _wikidata_page(match)
        return f


# ---------------------------------------------------------------------------------------------
# Company Profile Agent -- OFF-03
# ---------------------------------------------------------------------------------------------
class CompanyProfileAgent(Agent):
    name = "Company Profile Agent"
    parameters = ("OFF-03",)
    tools = ("web_search", "crawl_page", "extract_company_details", "compare_entities")

    async def research_off_03(self, ctx) -> Findings:
        sr = await tools.web_search_company('"{name}" ' + tools.site_or(PROFILE_DOMAINS), ctx.company_name)
        if not sr.ok:
            return _search_failed("OFF-03", self.name, "Business profiles", sr)
        hits = tools.domains_present(sr.urls, PROFILE_DOMAINS)
        found = [d for d, v in hits.items() if v]
        locations = [g.lower() for g in derive_site_geographies(ctx)]
        profiles = tools.extract_company_details(sr.items, tuple(found), [ctx.company_name.lower(), primary_brand(ctx)], locations)
        f = Findings("OFF-03", self.name, "Business profiles", checked=sr.display_url)
        f.values = {"platforms_found": found, "profiles": profiles}
        f.evidence = {"provider": sr.provider, "query": sr.query, "platforms_found": found, "matches": hits,
                      "profiles": profiles, "site_locations": locations,
                      "note": "Consistency is read from each profile's search listing (title and snippet), not from the full profile page."}
        f.source_urls = [p["urls"][0] for p in profiles.values() if p["urls"]]
        return f


# ---------------------------------------------------------------------------------------------
# Certification Verification Agent -- OFF-04
# ---------------------------------------------------------------------------------------------
_CLAIMS = (
    ("Certified/Accredited", r"\b(certifi\w*|accredit\w*)\b"),
    ("Compliance", r"\bcompliance\b|\bcompliant\b"),
    ("Licensed", r"\blicens(e|ed|ing|ure)\b"),
    ("ISO standard", r"\biso\s?\d{3,6}\b"),
    ("CMMI", r"\bcmmi\b"),
    ("Certified partner/vendor tier", r"\b(certified|authorized|accredited|premier|gold|platinum|elite)\s+partner\b"),
)
# The claims specific enough to look up off-site; a bare "certified" or "compliance" is not.
_SEARCHABLE = ("ISO standard", "CMMI", "Certified partner/vendor tier")


class CertificationVerificationAgent(Agent):
    name = "Certification Verification Agent"
    parameters = ("OFF-04",)
    tools = ("web_search", "verify_source", "compare_entities")

    async def research_off_04(self, ctx) -> Findings:
        blob = " ".join(p.text for p in ctx.pages)
        claims, named = [], []
        for label, pat in _CLAIMS:
            found = [m.group(0) for m in re.finditer(pat, blob, re.I)]
            if found:
                claims.append(label)
                if label in _SEARCHABLE:
                    named += found
        f = Findings("OFF-04", self.name, "Company website and off-site certification sources", checked=ctx.origin)
        f.values["claims"] = claims
        f.evidence = {"claims": claims}
        if not claims:
            return f
        f.evidence["note"] = ("The claims are searched for off-site and the pages found are recorded as verification "
                              "evidence; the score stays capped at 55 because no issuer registry is queried directly.")
        # The exact certificates the site names ("ISO 27001", "CMMI", "gold partner"), up to five.
        terms = list(dict.fromkeys(re.sub(r"\s+", " ", n).strip().lower() for n in named))[:5] or ["certified", "accredited"]
        own = (ctx.domain or "").lower().removeprefix("www.")
        query = '"{name}" (' + " OR ".join(f'"{t}"' for t in terms) + ")" + (f" -site:{own}" if own else "")
        sr = await tools.web_search_company(query, ctx.company_name)
        f.evidence["off_site_query"] = sr.query
        if not sr.ok:
            f.status = _search_failed("OFF-04", self.name, f.source_type, sr).status
            f.verification = {"search_error": sr.error}
            return f
        f.source_urls = sr.urls
        f.require = terms
        return f


# ---------------------------------------------------------------------------------------------
# Review Intelligence Agent -- OFF-05, OFF-06
# ---------------------------------------------------------------------------------------------
class ReviewIntelligenceAgent(Agent):
    name = "Review Intelligence Agent"
    parameters = ("OFF-05", "OFF-06")
    tools = ("web_search", "extract_reviews", "extract_dates_and_rankings", "compare_entities")

    async def research_off_05(self, ctx) -> Findings:
        sr = await tools.web_search_company('"{name}" ' + tools.site_or(REVIEW_DOMAINS), ctx.company_name)
        if not sr.ok:
            return _search_failed("OFF-05", self.name, "Review platforms", sr)
        hits = tools.domains_present(sr.urls, REVIEW_DOMAINS)
        found = [d for d, v in hits.items() if v]
        rows = tools.extract_reviews(sr.items, tuple(found))
        listings = [{"title": i["title"], "snippet": i["snippet"]} for i in rows]
        categories = derive_site_categories(ctx, limit=6)
        blob = " ".join(f"{s['title']} {s['snippet']}" for s in listings)
        # Rules-based reading first, so the check still has an input with no model available.
        rules_category_match = bool(listings and categories and tools.compare_entities(blob, categories))
        f = Findings("OFF-05", self.name, "Review platforms", checked=sr.display_url)
        f.evidence = {"provider": sr.provider, "query": sr.query, "platforms_found": found, "matches": hits,
                      "snippets": listings[:8], "site_categories": categories, "category_method": "keyword"}
        llm_category_match = None
        if listings and categories:
            llm_res = await judge(category_prompt(ctx.company_name, categories, listings[:8]), system=SYSTEM)
            if llm_res.ok and llm_res.parsed and "matches" in llm_res.parsed:
                llm_category_match = bool(llm_res.parsed.get("matches"))
                f.evidence["category_method"] = "llm"
                f.evidence["llm"] = llm_res.parsed
            elif not llm_res.ok:
                f.evidence["llm_unavailable"] = llm_res.error
        f.values = {"platforms_found": found, "rules_category_match": rules_category_match,
                    "llm_category_match": llm_category_match}
        f.source_urls = [v[0] for v in hits.values() if v]
        return f

    async def research_off_06(self, ctx) -> Findings:
        sr = await tools.web_search_company('"{name}" reviews ' + tools.site_or(REVIEW_DOMAINS), ctx.company_name)
        if not sr.ok:
            return _search_failed("OFF-06", self.name, "Review platforms", sr)
        rows = tools.extract_reviews(sr.items, REVIEW_DOMAINS)
        dated = tools.extract_dates_and_rankings(rows)
        f = Findings("OFF-06", self.name, "Review platforms", checked=sr.display_url)
        f.values = {"review_results": len(rows), "newest_review_age_days": dated["age_days"]}
        f.evidence = {"provider": sr.provider, "query": sr.query, "review_results": len(rows),
                      "review_urls": [i["url"] for i in rows][:10], "newest_review_date": dated["newest_date"],
                      "newest_review_age_days": dated["age_days"], "dated_results": len(dated["dates"]),
                      "note": "Volume is the number of review-platform results; recency comes from the date each result page publishes or the search result shows. Review counts on each platform are not read."}
        f.source_urls = [i["url"] for i in rows]
        return f


# ---------------------------------------------------------------------------------------------
# Community Presence Agent -- OFF-07
# ---------------------------------------------------------------------------------------------
class CommunityPresenceAgent(Agent):
    name = "Community Presence Agent"
    parameters = ("OFF-07",)
    tools = ("web_search",)

    async def research_off_07(self, ctx) -> Findings:
        sr = await tools.web_search_company('"{name}" (site:reddit.com OR site:quora.com OR inurl:forum OR inurl:community)', ctx.company_name)
        if not sr.ok:
            return _search_failed("OFF-07", self.name, "Reddit, Quora and forums", sr)
        own = (ctx.domain or "").lower().removeprefix("www.")
        found: dict[str, list[str]] = {"reddit.com": [], "quora.com": [], "forums": []}
        for u in sr.urls:
            if own and tools.on_domain(u, own):
                continue
            if tools.on_domain(u, "reddit.com"):
                found["reddit.com"].append(u)
            elif tools.on_domain(u, "quora.com"):
                found["quora.com"].append(u)
            elif "forum" in u.lower() or "community" in u.lower():
                found["forums"].append(u)
        count = sum(len(v) for v in found.values())
        f = Findings("OFF-07", self.name, "Reddit, Quora and forums", checked=sr.display_url)
        f.values = {"mentions_found": count}
        f.evidence = {"provider": sr.provider, "query": sr.query, "mentions_found": count, "matches": found,
                      "note": "Presence signal only; whether each thread is substantive is not individually judged."}
        f.source_urls = [u for v in found.values() for u in v]
        return f


# ---------------------------------------------------------------------------------------------
# Industry Visibility Agent -- OFF-08, OFF-09
# ---------------------------------------------------------------------------------------------
class IndustryVisibilityAgent(Agent):
    name = "Industry Visibility Agent"
    parameters = ("OFF-08", "OFF-09")
    tools = ("web_search", "extract_dates_and_rankings", "compare_entities")

    async def research_off_08(self, ctx) -> Findings:
        categories = derive_site_categories(ctx, limit=1)
        topic = categories[0] if categories else "companies"
        brand = primary_brand(ctx)
        generic = await tools.web_search(f"best {topic} companies")
        if not generic.ok:
            return _search_failed("OFF-08", self.name, "Best/top list pages", generic)
        in_generic = [i["url"] for i in generic.items if brand and tools.compare_entities(f"{i['title']} {i['snippet']} {i['url']}", [brand])]
        named = await tools.web_search_company(f'"{{name}}" (best OR top OR leading) {topic}', ctx.company_name)
        if not named.ok:
            return _search_failed("OFF-08", self.name, "Best/top list pages", named)
        ranked = tools.extract_dates_and_rankings(named.items, own_domain=ctx.domain or "")["list_pages"]
        list_pages = [r["url"] for r in ranked]
        f = Findings("OFF-08", self.name, "Best/top list pages", checked=generic.display_url)
        f.values = {"in_generic_results": bool(in_generic), "list_pages": len(list_pages)}
        f.evidence = {"provider": generic.provider, "topic": topic, "query": generic.query, "list_query": named.query,
                      "in_generic_results": bool(in_generic), "generic_result_urls": in_generic[:5],
                      "list_pages_naming_company": list_pages[:8], "list_page_positions": ranked[:8],
                      "brand_mentioned": bool(in_generic or list_pages)}
        f.source_urls = in_generic + list_pages
        return f

    async def research_off_09(self, ctx) -> Findings:
        sr = await tools.web_search_company('"{name}" (gartner OR forrester OR idc OR "analyst report" OR "industry report")', ctx.company_name)
        if not sr.ok:
            return _search_failed("OFF-09", self.name, "Analyst, directory and trade press", sr)
        snippets = sr.snippets()
        # Rules-based reading first, so the check still has an input with no model available.
        rules_coverage_found = bool(snippets)
        f = Findings("OFF-09", self.name, "Analyst, directory and trade press", checked=sr.display_url)
        f.values = {"coverage_results": len(snippets), "llm_accurate": None}
        f.evidence = {"provider": sr.provider, "query": sr.query, "snippets": snippets}
        f.source_urls = sr.urls
        if not rules_coverage_found:
            return f
        f.evidence["method"] = "heuristic"
        llm_res = await judge(analyst_prompt(ctx.company_name, snippets), system=SYSTEM)
        if llm_res.ok and llm_res.parsed and "accurate" in llm_res.parsed:
            f.values["llm_accurate"] = bool(llm_res.parsed.get("accurate"))
            f.evidence["method"] = "llm"
            f.evidence["llm"] = llm_res.parsed
        elif not llm_res.ok:
            f.evidence["llm_unavailable"] = llm_res.error
        return f


# ---------------------------------------------------------------------------------------------
# Evidence & Validation Agent -- cross-parameter
# ---------------------------------------------------------------------------------------------
class EvidenceValidationAgent:
    """Opens each check's top source pages and confirms they are about the company.

    Search results are for discovery; the source page itself is the evidence. The result is
    recorded as `verification_result` and summarised in `parameter_status`. It never changes
    a score: a site that refuses a browser (LinkedIn, G2) says nothing about the company.
    """
    name = "Evidence & Validation Agent"
    tools = ("crawl_page", "extract_content", "verify_source")

    async def validate(self, f: Findings, ctx) -> Findings:
        if f.status in UNAVAILABLE or not f.verify:
            return f
        urls = list(dict.fromkeys(f.source_urls))[:OFFPAGE_VERIFY_SOURCES]
        if not urls:
            f.status = NO_SOURCES
            f.verification = {**f.verification, "sources_checked": 0, "sources_verified": 0, "sources": []}
            return f
        terms = [t for t in (ctx.company_name, primary_brand(ctx)) if t]
        results = await asyncio.gather(*(tools.verify_source(u, terms, ctx, require=f.require) for u in urls))
        verified = sum(1 for r in results if r["result"] == "verified")
        f.status = VERIFIED if verified else SOURCES_UNCONFIRMED
        f.verification = {**f.verification, "sources_checked": len(results), "sources_verified": verified,
                          "sources": list(results)}
        return f


# ---------------------------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------------------------
class OffPageOrchestrator:
    """Maps each parameter to its specialist agent, then has the validator check the evidence."""

    def __init__(self, agents: list[Agent], validator: EvidenceValidationAgent):
        self.agents = agents
        self.validator = validator
        self.agent_for = {pid: a for a in agents for pid in a.parameters}

    async def research(self, parameter_id: str, ctx) -> Findings:
        findings = await self.agent_for[parameter_id].research(parameter_id, ctx)
        return await self.validator.validate(findings, ctx)


AGENTS: list[Agent] = [
    KnowledgeEntityAgent(),
    CompanyProfileAgent(),
    CertificationVerificationAgent(),
    ReviewIntelligenceAgent(),
    CommunityPresenceAgent(),
    IndustryVisibilityAgent(),
]
ORCHESTRATOR = OffPageOrchestrator(AGENTS, EvidenceValidationAgent())

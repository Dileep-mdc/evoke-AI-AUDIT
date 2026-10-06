"""What this server can and cannot measure, said before a scan starts rather than after.

A scan with no web-search provider used to run to completion and quietly report most of the
Off-Page pillar as unmeasured, which reads as a finished audit with a lower score. GET
/api/status lets the dashboard say so up front: which providers are configured, whether
Wikidata and Wikipedia can be reached from here, how Core Web Vitals will be measured, what AI
calls cost on recent scans, and a list of plain-English warnings built from all of it.
"""
from __future__ import annotations

import time

import httpx
from fastapi import APIRouter

from ..config import (
    CRUX_API_KEY,
    ENABLE_LLM_SCORING,
    LLM_MODEL,
    LLM_PRICE_INPUT_PER_M,
    LLM_PRICE_OUTPUT_PER_M,
    LLM_SCAN_BUDGET_USD,
    OPENAI_API_KEY,
    OPENAI_SEARCH_PRICE_PER_CALL,
    WIKIDATA_API,
    WIKIMEDIA_USER_AGENT,
)
from ..crawler.render import render_unavailable
from ..llm import usage
from ..parameters.offpage_agents import AGENTS
from ..parameters.offpage_tools import search_provider
from ..parameters.platforms import choices
from .scans import SECTION_TOTALS, usage_history

router = APIRouter(prefix="/api")

# The reachability probe is cached: the home screen asks on every visit, and a network that
# cannot reach Wikidata does not change from one second to the next.
_PROBE_TTL = 300.0
_probe: tuple[float, dict] | None = None


async def _wikimedia_reachable() -> dict:
    global _probe
    if _probe and time.monotonic() - _probe[0] < _PROBE_TTL:
        return _probe[1]
    try:
        async with httpx.AsyncClient(timeout=6, headers={"User-Agent": WIKIMEDIA_USER_AGENT}) as client:
            res = await client.get(WIKIDATA_API, params={"action": "query", "meta": "siteinfo", "format": "json"})
        out = {"reachable": res.status_code == 200, "error": None if res.status_code == 200 else f"HTTP {res.status_code}"}
    except httpx.HTTPError as exc:
        out = {"reachable": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    _probe = (time.monotonic(), out)
    return out


@router.get("/status")
async def status():
    provider = search_provider()
    search_checks = sorted(pid for agent in AGENTS if agent.needs_web_search for pid in agent.parameters)
    llm_ready = bool(ENABLE_LLM_SCORING and OPENAI_API_KEY)
    wikimedia = await _wikimedia_reachable()
    browser = render_unavailable()
    vitals_source = "field" if CRUX_API_KEY else ("lab" if not browser else "proxy")
    typical = usage.estimate(100, usage_history())
    off_total = SECTION_TOTALS["off_page"]

    warnings = []
    if provider == "none":
        warnings.append({
            "level": "warning", "area": "off_page",
            "title": "Off-Page checks need a web-search key",
            "text": (f"{len(search_checks)} of the {off_total} Off-Page checks search the web, and no search provider is "
                     "configured. Set OPENAI_API_KEY, or GOOGLE_API_KEY and GOOGLE_CSE_ID, in backend/.env. Until then "
                     "those checks are reported as not measured and the overall score is partial."),
        })
    if not wikimedia["reachable"]:
        warnings.append({
            "level": "warning", "area": "off_page",
            "title": "Wikidata and Wikipedia are unreachable from this server",
            "text": (f"OFF-01 and OFF-02 will be reported as not measured ({wikimedia['error']}). Check the network, "
                     "proxy or firewall settings of the machine running the backend."),
        })
    if not llm_ready:
        warnings.append({
            "level": "info", "area": "scoring",
            "title": "AI review is off",
            "text": ("Judgement checks use their rules-based fallback. Set ENABLE_LLM_SCORING=true and OPENAI_API_KEY "
                     "in backend/.env to turn it on."),
        })
    if vitals_source != "field":
        warnings.append({
            "level": "info", "area": "technical",
            "title": "Page speed is measured in a lab test" if vitals_source == "lab" else "Page speed is estimated",
            "text": ("Set CRUX_API_KEY (a Google Cloud key with the Chrome UX Report API enabled) to use Google's real-user "
                     "Core Web Vitals for TECH-07." + ("" if vitals_source == "lab" else
                     " No browser is available either (pip install playwright && playwright install chromium), so it "
                     "falls back to response time and page size.")),
        })

    return {
        "llm": {"ready": llm_ready, "enabled": ENABLE_LLM_SCORING, "key_set": bool(OPENAI_API_KEY), "model": LLM_MODEL},
        "search": {"provider": provider, "ready": provider != "none", "checks": search_checks},
        "wikimedia": wikimedia,
        "vitals": {"source": vitals_source, "field_key_set": bool(CRUX_API_KEY), "browser_ready": not browser},
        "cost": {
            "budget_usd": LLM_SCAN_BUDGET_USD or None,
            "price_input_per_m": LLM_PRICE_INPUT_PER_M,
            "price_output_per_m": LLM_PRICE_OUTPUT_PER_M,
            "search_fee_per_call": OPENAI_SEARCH_PRICE_PER_CALL,
            "typical_per_100_pages": typical,
        },
        "platforms": choices(),
        "warnings": warnings,
    }

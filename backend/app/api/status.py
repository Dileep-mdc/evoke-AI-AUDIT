"""What this server is configured to do: AI review, web search, page-speed source and AI cost.

The dashboard reads it for the typical AI cost of a scan; the rest describes the setup for anyone
checking a deployment (GET /api/status).
"""
from __future__ import annotations

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
)
from ..crawler.render import render_unavailable
from ..llm import usage
from ..parameters.offpage_agents import AGENTS
from ..parameters.offpage_tools import search_provider
from ..parameters.platforms import choices
from .scans import usage_history

router = APIRouter(prefix="/api")


@router.get("/status")
async def status():
    provider = search_provider()
    browser = render_unavailable()
    return {
        "llm": {"ready": bool(ENABLE_LLM_SCORING and OPENAI_API_KEY), "enabled": ENABLE_LLM_SCORING,
                "key_set": bool(OPENAI_API_KEY), "model": LLM_MODEL},
        "search": {"provider": provider, "ready": provider != "none",
                   "checks": sorted(pid for agent in AGENTS if agent.needs_web_search for pid in agent.parameters)},
        "vitals": {"source": "field" if CRUX_API_KEY else ("lab" if not browser else "proxy"),
                   "field_key_set": bool(CRUX_API_KEY), "browser_ready": not browser},
        "cost": {
            "budget_usd": LLM_SCAN_BUDGET_USD or None,
            "price_input_per_m": LLM_PRICE_INPUT_PER_M,
            "price_output_per_m": LLM_PRICE_OUTPUT_PER_M,
            "search_fee_per_call": OPENAI_SEARCH_PRICE_PER_CALL,
            "typical_per_100_pages": usage.estimate(100, usage_history()),
        },
        "platforms": choices(),
    }

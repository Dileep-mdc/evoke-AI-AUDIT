"""Industry profiles (industry_profiles.json): the audit adapts to what the site's industry needs.

The checks were written for IT-services and software companies -- G2 and Clutch reviews, service
FAQs, named-client deployment stories -- so a manufacturer or a hospital was graded against rules
that do not apply to it and scored low for reasons a buyer in its industry would never care about.
A profile now decides, per scan: which review/profile/community sites the off-page checks look
for, what a complete key page covers
(ON-10) and which structured-data types its key pages should carry (TECH-17).

The industry is detected from the site's own pages unless the scan request names one, and the
report says which profile was used and why, so a reader can see and question the choice.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

PROFILES_PATH = Path(os.getenv("INDUSTRY_PROFILES_PATH", Path(__file__).with_name("industry_profiles.json")))
DEFAULT = "general"


def _load() -> dict:
    return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))


_DOC = _load()
PROFILES: dict = _DOC["profiles"]


def profile(key: Optional[str]) -> dict:
    return PROFILES.get(key or DEFAULT) or PROFILES[DEFAULT]


def choices() -> list[dict]:
    return [{"key": k, "label": v.get("label", k)} for k, v in PROFILES.items()]


def validate(key: Optional[str]) -> Optional[str]:
    """The key, if it names a profile; raises ValueError otherwise. None means detect."""
    if key and key not in PROFILES:
        raise ValueError(f"Unknown industry: {key}")
    return key or None


def _signals(ctx) -> dict[str, dict]:
    """How strongly the site's own pages point at each industry: keyword hits on the homepage and
    in page titles, URL paths that look like the industry's, and structured-data types it uses."""
    home = (getattr(ctx.homepage, "text", "") or "").lower() if getattr(ctx, "homepage", None) else ""
    titles = " ".join((p.title or "") for p in ctx.pages).lower()
    paths = " ".join((p.url or "").lower() for p in ctx.pages)
    types = set()
    for p in ctx.pages:
        for block in p.schema_blocks or []:
            data = block.get("data") if isinstance(block, dict) else None
            for t in re.findall(r'"@type"\s*:\s*"([^"]+)"', json.dumps(data if data is not None else block)):
                types.add(t.lower())
    out = {}
    for key, prof in PROFILES.items():
        rules = prof.get("detect")
        if not rules:
            continue
        words = [w for w in rules.get("keywords", []) if re.search(rf"\b{re.escape(w)}\b", home + " " + titles)]
        urls = [h for h in rules.get("url_hints", []) if h in paths]
        schema = [s for s in rules.get("schema_types", []) if s in types]
        # Schema types are deliberate declarations, so they count double; URL hints count once each.
        out[key] = {"keywords": words, "url_hints": urls, "schema_types": schema,
                    "score": len(words) + len(urls) + 2 * len(schema)}
    return out


def detect(ctx) -> dict:
    """The industry the site's pages indicate: {"key", "label", "detected", "signals"}."""
    sig = _signals(ctx)
    rules = _DOC.get("detection", {})
    ranked = sorted(sig.items(), key=lambda kv: -kv[1]["score"])
    key = DEFAULT
    if ranked and ranked[0][1]["score"] >= rules.get("min_signals", 4):
        runner_up = ranked[1][1]["score"] if len(ranked) > 1 else 0
        if ranked[0][1]["score"] >= rules.get("margin", 1.5) * max(1, runner_up):
            key = ranked[0][0]
    return {"key": key, "label": profile(key)["label"], "detected": True,
            "signals": {k: v for k, v in sig.items() if v["score"]}}


def not_applicable(ctx, parameter_id: str) -> Optional[str]:
    """Why a check does not apply to this site's industry, or None when it does."""
    return (profile((getattr(ctx, "industry", None) or {}).get("key")).get("not_applicable") or {}).get(parameter_id)


def chosen(key: str) -> dict:
    return {"key": key, "label": profile(key)["label"], "detected": False, "signals": {}}


def for_ctx(ctx) -> dict:
    """The profile this scan uses."""
    return profile((getattr(ctx, "industry", None) or {}).get("key"))




def content_rubric(ctx) -> list[dict]:
    return for_ctx(ctx).get("content_rubric") or PROFILES[DEFAULT]["content_rubric"]


def page_schema(ctx) -> dict:
    return {**PROFILES[DEFAULT].get("page_schema", {}), **for_ctx(ctx).get("page_schema", {})}

"""The third-party sites the off-page checks look for the company on, from offpage_platforms.json.

The lists used to be constants in offpage_agents.py -- G2, Clutch, Gartner and TrustRadius for
reviews; LinkedIn, Crunchbase, Bloomberg and ZoomInfo for profiles; Reddit and Quora for
community -- which suits a US B2B software vendor and nobody else. A manufacturer is listed on
Thomasnet, an Indian services firm on AmbitionBox, a UK company on Trustpilot and Companies
House. Now they are data: a default set every scan checks, plus optional industry and region
sets a scan can add.

A scan's choice travels on the crawl context (`ctx.platforms`), so every agent of that scan
reads the same lists, and each row records the list it was checked against -- the plain-English
working and the "N of M sites" wording are rebuilt from that record, not from today's file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

PLATFORMS_PATH = Path(os.getenv("OFFPAGE_PLATFORMS_PATH", Path(__file__).with_name("offpage_platforms.json")))
KINDS = ("review", "profile", "community")


def _load() -> dict:
    return json.loads(PLATFORMS_PATH.read_text(encoding="utf-8"))


_DOC = _load()


def choices() -> dict:
    """The industries and regions a scan can pick, for the scan screen's dropdowns."""
    def listing(group: str) -> list[dict]:
        return [{"key": k, "label": v.get("label", k)} for k, v in (_DOC.get(group) or {}).items()]
    return {"industries": listing("industries"), "regions": listing("regions")}


def resolve(industry: Optional[str] = None, region: Optional[str] = None) -> dict:
    """The active lists for a scan: the default set plus the chosen industry and region sets.

    Order is kept (default first) and duplicates dropped, so a site named by two sets is
    checked once. Raises ValueError for an industry or region the file does not define.
    """
    sets = [_DOC["default"]]
    labels = []
    for group, key in (("industries", industry), ("regions", region)):
        if not key:
            continue
        entry = (_DOC.get(group) or {}).get(key)
        if entry is None:
            raise ValueError(f"Unknown {'industry' if group == 'industries' else 'region'}: {key}")
        sets.append(entry)
        labels.append(entry.get("label", key))
    active = {kind: tuple(dict.fromkeys(d.lower() for s in sets for d in s.get(kind, []))) for kind in KINDS}
    return {**active, "industry": industry or None, "region": region or None,
            "label": " + ".join(["Global"] + labels) if labels else _DOC["default"].get("label", "Global (default)")}


def platforms_for(ctx) -> dict:
    """The lists this scan uses: the ones chosen for it, or the default set."""
    chosen = getattr(ctx, "platforms", None)
    return chosen if chosen else resolve()


DEFAULT = resolve()

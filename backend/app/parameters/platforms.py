"""The third-party sites the off-page checks look for the company on.

The sites come from the scan's industry profile (industry_profiles.json): a manufacturer is
looked for on Thomasnet and GlobalSpec, a software company on G2 and Capterra, a hospital on
Healthgrades. A region (offpage_platforms.json) adds its local sites -- Trustpilot and Companies
House for the UK, AmbitionBox and Tracxn for India.

A scan's lists travel on the crawl context (`ctx.platforms`), so every agent of that scan reads
the same ones, and each row records the list it was checked against -- the plain-English working
and the "N of M sites" wording are rebuilt from that record, not from today's files.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from . import industry

PLATFORMS_PATH = Path(os.getenv("OFFPAGE_PLATFORMS_PATH", Path(__file__).with_name("offpage_platforms.json")))
KINDS = ("review", "profile", "community")


def _load() -> dict:
    return json.loads(PLATFORMS_PATH.read_text(encoding="utf-8"))


_DOC = _load()


def choices() -> dict:
    """The industries and regions a scan can name."""
    regions = [{"key": k, "label": v.get("label", k)} for k, v in (_DOC.get("regions") or {}).items()]
    return {"industries": industry.choices(), "regions": regions}


def resolve(industry_key: Optional[str] = None, region: Optional[str] = None) -> dict:
    """The active lists for a scan: the industry profile's sites plus the region's.

    Order is kept (industry first) and duplicates dropped. Raises ValueError for an industry or
    region the files do not define.
    """
    industry.validate(industry_key)
    sets = [industry.profile(industry_key).get("platforms", {})]
    labels = [industry.profile(industry_key)["label"]]
    if region:
        entry = (_DOC.get("regions") or {}).get(region)
        if entry is None:
            raise ValueError(f"Unknown region: {region}")
        sets.append(entry)
        labels.append(entry.get("label", region))
    active = {kind: tuple(dict.fromkeys(d.lower() for s in sets for d in s.get(kind, []))) for kind in KINDS}
    return {**active, "industry": industry_key or industry.DEFAULT, "region": region or None, "label": " + ".join(labels)}


def platforms_for(ctx) -> dict:
    """The lists this scan uses: the ones resolved for it, or the general profile's."""
    chosen = getattr(ctx, "platforms", None)
    return chosen if chosen else resolve()


DEFAULT = resolve()

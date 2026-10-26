"""Rebuild a finished audit's crawl from what it saved, so single checks can be re-run without
reading the website again.

Every audit writes {site}-scraped-content.json -- each page's derived fields -- and the page's own
HTML (and its browser-rendered copy) under {site}-raw/. That is everything the on-site checks read
from a page, so the pages are restored from it exactly. The three small site-level documents
(robots.txt, the sitemap and llms.txt) are not in it and are fetched again: three requests.

The files are per site, so they belong to that site's MOST RECENT audit. Restoring for an older
audit of the same site would score it against a different crawl, so that is refused.
"""
from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path
from typing import Optional

from ..scan_output import OUTPUT_DIR, _slug
from .discover import Page, SiteContext, urljoin_safe
from .http import FetchResult, fetch, origin_of
from .parse import make_soup
from .robots import fetch_robots
from .sitemap import fetch_sitemaps


class NotRestorable(Exception):
    """The saved crawl is missing, or belongs to a different audit of the same site."""


def _read_gz(base: Path, relative: Optional[str]) -> str:
    if not relative:
        return ""
    path = base / relative
    try:
        return gzip.decompress(path.read_bytes()).decode("utf-8", "replace")
    except OSError:
        return ""


def _page(base: Path, rec: dict) -> Page:
    raw = _read_gz(base, rec.get("raw_html_file"))
    rendered = _read_gz(base, rec.get("rendered_html_file"))
    result = FetchResult(url=rec["url"], final_url=rec.get("final_url") or rec["url"], status_code=rec.get("status_code"),
                         headers={}, content=raw.encode("utf-8"), text=raw, content_type=rec.get("content_type") or "",
                         elapsed_ms=0, error=rec.get("fetch_error"))
    document = rendered or raw
    return Page(
        url=rec["url"], result=result, soup=make_soup(document) if document else None,
        title=rec.get("title") or "", canonical=rec.get("canonical") or "", word_count=rec.get("word_count") or 0,
        text=rec.get("text") or "", headings=rec.get("headings") or [], links=rec.get("links") or [],
        images=rec.get("images") or [], schema_blocks=rec.get("schema_blocks") or [],
        meta_description=rec.get("meta_description") or "", hreflang=rec.get("hreflang") or [],
        dates=rec.get("dates") or {}, page_type=rec.get("page_type") or "other",
        blocked_reason=rec.get("blocked_reason") or "", rendered_html=rendered, render=rec.get("render") or {},
    )


def _load(domain: str, scan_id: str) -> tuple[dict, list[Page]]:
    index = OUTPUT_DIR / f"{_slug(domain)}-scraped-content.json"
    if not index.exists():
        raise NotRestorable("this audit's saved website data is no longer on disk")
    with index.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    if payload.get("scan_id") != scan_id:
        raise NotRestorable("a newer audit of this site has replaced its saved website data; re-run the audit instead")
    pages = [_page(index.parent, rec) for rec in payload.get("pages") or []]
    return payload, pages


async def restore(scan_id: str, domain: str, report: dict) -> SiteContext:
    """The crawl context of a finished audit, rebuilt from disk plus three small re-fetches."""
    payload, pages = await asyncio.to_thread(_load, domain, scan_id)
    origin = payload.get("origin") or origin_of(payload.get("normalized_url") or f"https://{domain}/")
    robots, llms = await asyncio.gather(fetch_robots(origin), fetch(urljoin_safe(origin, "/llms.txt")))
    sitemap = await fetch_sitemaps(origin, robots.sitemap_urls)
    normalized = payload.get("normalized_url") or origin + "/"
    homepage = next((p for p in pages if p.url in (normalized, origin + "/")), pages[0] if pages else None)
    ctx = SiteContext(
        input_url=payload.get("input_url") or normalized, normalized_url=normalized, origin=origin,
        domain=payload.get("domain") or domain, robots=robots, sitemap=sitemap, homepage=homepage, pages=pages,
        llms=llms, company_name=payload.get("company_name") or "", brand_terms=payload.get("brand_terms") or [],
        crawl_errors=payload.get("crawl_errors") or [], render=payload.get("render") or {},
        snapshot=payload.get("snapshot") or {}, access=payload.get("access") or {},
    )
    # What the audit established about the site is carried over, not worked out again.
    ctx.identity = report.get("identity") or {}
    ctx.industry = report.get("industry") or {}
    ctx.platforms = report.get("platforms") or {}
    if ctx.identity.get("name"):
        ctx.company_name = ctx.identity["name"]
    return ctx

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import urljoin

from ..config import MAX_SITEMAP_URLS
from .http import fetch, same_host


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1].lower()


async def fetch_sitemaps(origin: str, discovered: list[str]) -> dict:
    candidates = list(dict.fromkeys(discovered + [
        urljoin(origin.rstrip("/") + "/", "sitemap.xml"),
        urljoin(origin.rstrip("/") + "/", "sitemap_index.xml"),
        urljoin(origin.rstrip("/") + "/", "sitemap-index.xml"),
    ]))
    cap = MAX_SITEMAP_URLS
    urls: list[str] = []
    sources: list[dict] = []
    errors: list[str] = []
    seen_maps: set[str] = set()

    async def walk(loc: str, depth: int = 0) -> None:
        if loc in seen_maps or depth > 2 or len(urls) >= cap:
            return
        seen_maps.add(loc)
        result = await fetch(loc)
        sources.append({"url": loc, "status": result.status_code, "error": result.error})
        if not result.ok or not result.text.strip():
            if result.error:
                errors.append(f"{loc}: {result.error}")
            elif result.status_code:
                errors.append(f"{loc}: HTTP {result.status_code}")
            return
        try:
            root = ET.fromstring(result.text.encode("utf-8") if isinstance(result.text, str) else result.text)
        except Exception as exc:
            errors.append(f"{loc}: invalid XML ({exc})")
            return
        for el in root.iter():
            if len(urls) >= cap:
                return
            if _local(el.tag) == "sitemap":
                child = None
                for c in list(el):
                    if _local(c.tag) == "loc" and c.text:
                        child = c.text.strip()
                if child:
                    await walk(child, depth + 1)
            elif _local(el.tag) == "url":
                page = None
                for c in list(el):
                    if _local(c.tag) == "loc" and c.text:
                        page = c.text.strip()
                if page and same_host(page, origin):
                    urls.append(page)

    for loc in candidates:
        await walk(loc)
        if urls:
            break

    # unique preserve order
    uniq = list(dict.fromkeys(urls))
    return {
        "candidates": candidates,
        "sources": sources,
        "urls": uniq,
        "errors": errors,
        "count": len(uniq),
    }

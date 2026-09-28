from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
import re
from urllib.parse import urljoin, urlparse

from ..config import MAX_SITEMAP_URLS
from .http import fetch, same_host

GZIP_MAGIC = bytes([0x1F, 0x8B])
LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1].lower()


def locale_of(url: str) -> str:
    """The locale segment a URL's path opens with ("/en-in/..." -> "en-in"), or ""."""
    first = urlparse(url).path.strip("/").split("/")[0].lower()
    return first if LOCALE_RE.match(first) else ""


async def fetch_sitemaps(origin: str, discovered: list[str], prefer: str = "") -> dict:
    """Every page URL the site's sitemaps list, up to MAX_SITEMAP_URLS.

    `prefer` is a locale ("en-in"). A multi-language site publishes one sitemap per locale
    under a single index, and the cap used to fill with whichever locale the index happened to
    list first -- Czech, on one real site -- so the pages actually audited were never found in
    the sitemap at all. Child sitemaps for the preferred locale are now walked first.
    """
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
        if not result.ok or not result.content.strip():
            if result.error:
                errors.append(f"{loc}: {result.error}")
            elif result.status_code:
                errors.append(f"{loc}: HTTP {result.status_code}")
            return
        body = result.content
        if body[:2] == GZIP_MAGIC:
            # sitemap.xml.gz, which the protocol explicitly allows. Served as a file rather than
            # with Content-Encoding, so nothing upstream has unpacked it; parsing the raw bytes
            # reported every such sitemap as "invalid XML" and scored the site as having none.
            try:
                body = gzip.decompress(body)
            except OSError as exc:
                errors.append(f"{loc}: unreadable gzip ({exc})")
                return
        try:
            root = ET.fromstring(body)
        except Exception as exc:
            errors.append(f"{loc}: invalid XML ({exc})")
            return
        children: list[str] = []
        for el in root.iter():
            if len(urls) >= cap:
                return
            if _local(el.tag) == "sitemap":
                child = None
                for c in list(el):
                    if _local(c.tag) == "loc" and c.text:
                        child = c.text.strip()
                if child:
                    children.append(child)
            elif _local(el.tag) == "url":
                page = None
                for c in list(el):
                    if _local(c.tag) == "loc" and c.text:
                        page = c.text.strip()
                if page and same_host(page, origin):
                    urls.append(page)
        # Stable sort: preferred-locale sitemaps first, everything else in the index's order.
        for child in sorted(children, key=lambda c: not (prefer and prefer in c.lower())):
            await walk(child, depth + 1)

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

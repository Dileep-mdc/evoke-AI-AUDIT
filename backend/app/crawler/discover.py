from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urldefrag, urlparse

import trafilatura
from bs4 import BeautifulSoup

from ..config import BROWSER_UA, LINK_SAMPLE, MAX_PAGES
from .browser_crawl import crawl_pages
from .http import FetchResult, fetch, is_html, join_url, normalize_url, origin_of, same_host
from .robots import RobotsData, fetch_robots
from .sitemap import fetch_sitemaps


@dataclass
class Page:
    url: str
    result: FetchResult
    soup: Optional[BeautifulSoup]
    title: str = ""
    canonical: str = ""
    word_count: int = 0
    text: str = ""
    headings: list = field(default_factory=list)
    links: list = field(default_factory=list)
    images: list = field(default_factory=list)
    schema_blocks: list = field(default_factory=list)
    meta_description: str = ""
    hreflang: list = field(default_factory=list)
    dates: dict = field(default_factory=dict)
    page_type: str = "other"
    blocked_reason: str = ""


def visible_text(soup: BeautifulSoup) -> str:
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def extract_jsonld(soup: BeautifulSoup) -> list:
    blocks = []
    import json
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            blocks.append({"ok": True, "data": data, "raw_hash": hashlib.sha256(raw.encode()).hexdigest()})
        except Exception as exc:
            blocks.append({"ok": False, "error": str(exc), "raw_preview": raw[:240]})
    return blocks


# Titles/snippets used by common bot-management vendors (PerimeterX, Akamai, Cloudflare,
# DataDome...) for their JS/CAPTCHA challenge pages. These come back as normal HTTP 200
# responses, so without this check a blocked page silently looks like real -- just very
# thin -- page content instead of a failed scrape.
_BOT_BLOCK_SIGNATURES = (
    "robot or human",
    "verify you are a human",
    "verify you are human",
    "are you a human",
    "just a moment",
    "attention required",
    "access denied",
    "please enable javascript and cookies",
    "checking your browser",
    "pardon our interruption",
    "request unsuccessful",
    "unusual traffic",
)


def detect_bot_block(title: str, text: str) -> str:
    """Return a short reason if this looks like a bot-detection challenge page, else ''."""
    blob = f"{title} {text[:400]}".lower()
    for sig in _BOT_BLOCK_SIGNATURES:
        if sig in blob:
            return f"Blocked by the site's bot-detection page (matched \"{sig}\")."
    return ""


def link_text_confidence(a) -> tuple[str, int]:
    """Best-available accessible text for a link, with a 0-100 confidence score for how
    much that text can be trusted to represent the link (visible text beats aria-label/
    title, which beat an image's alt text, which beats nothing -- an icon-only link with
    no accessible name at all)."""
    visible = a.get_text(" ", strip=True)
    if visible:
        return visible[:160], 100
    aria = (a.get("aria-label") or "").strip()
    if aria:
        return aria[:160], 80
    title_attr = (a.get("title") or "").strip()
    if title_attr:
        return title_attr[:160], 65
    img = a.find("img")
    alt = (img.get("alt") or "").strip() if img else ""
    if alt:
        return alt[:160], 50
    return "", 0


def classify_page(url: str, title: str, text: str) -> str:
    blob = f"{url} {title} {text[:800]}".lower()
    if any(k in blob for k in ("/blog", "article", "insights", "news")):
        return "article"
    if any(k in blob for k in ("case-stud", "success-stor", "portfolio")):
        return "case_study"
    if any(k in blob for k in ("about", "company", "who-we-are")):
        return "about"
    if any(k in blob for k in ("career", "job", "contact")):
        return "utility"
    if any(k in blob for k in ("service", "solution", "product", "offering", "capabilit", "pricing")):
        return "service"
    if urlparse(url).path in {"", "/"}:
        return "home"
    return "other"


def parse_page(raw: FetchResult, rendered: FetchResult | None = None) -> Page:
    """Build a Page from a raw (pre-JS) fetch and its Playwright-rendered counterpart.

    Structural/content extraction (title, headings, links, schema, visible text) reads
    the rendered HTML, so markup or copy that only appears after JavaScript runs is no
    longer invisible to the audit. `page.result` stays the raw fetch, since HTTP-layer
    facts (redirect hops, final URL, byte weight) are unaffected by rendering and several
    checks specifically compare the raw wire response against the rendered content.
    """
    rendered = rendered or raw
    soup = None
    html_source = ""
    if is_html(rendered) and rendered.text:
        html_source = rendered.text
        soup = BeautifulSoup(html_source, "html.parser")
    elif is_html(raw) and raw.text:
        html_source = raw.text
        soup = BeautifulSoup(html_source, "html.parser")
    page = Page(url=raw.url, result=raw, soup=soup)
    if not soup:
        return page
    base_url = rendered.final_url or raw.final_url
    title_el = soup.find("title")
    page.title = title_el.get_text(strip=True) if title_el else ""
    canon = soup.find("link", rel=lambda v: v and "canonical" in v)
    page.canonical = (canon.get("href") or "").strip() if canon else ""
    meta = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    page.meta_description = (meta.get("content") or "").strip() if meta else ""
    extracted = None
    try:
        extracted = trafilatura.extract(html_source, include_tables=True, include_comments=False, favor_recall=True)
    except Exception:
        extracted = None
    page.text = re.sub(r"\s+", " ", extracted).strip() if extracted else visible_text(soup)
    page.word_count = len(page.text.split())
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            page.headings.append({"level": level, "text": h.get_text(" ", strip=True)})
    for a in soup.find_all("a", href=True):
        abs_url = join_url(base_url, a["href"])
        if abs_url:
            text, text_confidence = link_text_confidence(a)
            page.links.append({"href": abs_url, "text": text, "text_confidence": text_confidence})
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        page.images.append({"src": join_url(base_url, src) or src, "alt": (img.get("alt") or "").strip()})
    page.schema_blocks = extract_jsonld(soup)
    for alt in soup.find_all("link", rel=lambda v: v and "alternate" in v):
        if alt.get("hreflang"):
            page.hreflang.append({"lang": alt.get("hreflang"), "href": alt.get("href")})
    pub = soup.find("meta", attrs={"property": "article:published_time"}) or soup.find("time")
    mod = soup.find("meta", attrs={"property": "article:modified_time"})
    page.dates = {
        "published": (pub.get("datetime") or pub.get("content") or pub.get_text(strip=True)) if pub else None,
        "modified": (mod.get("content") if mod else None),
    }
    page.page_type = classify_page(base_url, page.title, page.text)
    page.blocked_reason = detect_bot_block(page.title, page.text)
    return page


@dataclass
class SiteContext:
    input_url: str
    normalized_url: str
    origin: str
    domain: str
    robots: RobotsData
    sitemap: dict
    homepage: Optional[Page]
    pages: list[Page]
    llms: FetchResult
    company_name: str = ""
    brand_terms: list = field(default_factory=list)
    crawl_errors: list = field(default_factory=list)


def _priority_urls(origin: str, sitemap_urls: list[str], homepage_links: list[str]) -> list[str]:
    seeds = [origin.rstrip("/") + "/"]
    preferred = []
    for u in sitemap_urls + homepage_links:
        path = urlparse(u).path.lower()
        if any(k in path for k in ("service", "solution", "product", "about", "case", "insight", "blog", "pricing", "feature")):
            preferred.append(u)
    rest = [u for u in sitemap_urls if u not in preferred]
    ordered = list(dict.fromkeys(seeds + preferred + homepage_links + rest))
    return ordered[:MAX_PAGES]


async def crawl_site(input_url: str, on_progress: Callable[[int], None] | None = None) -> SiteContext:
    def bump(pct: int) -> None:
        if on_progress:
            on_progress(pct)

    normalized = normalize_url(input_url)
    origin = origin_of(normalized)
    domain = urlparse(origin).netloc.lower()
    bump(2)

    # Lightweight, non-rendered probe of the homepage purely to discover its internal
    # links for seeding crawl priority. The homepage Page used everywhere else below
    # comes from the main rendered batch, so it gets the same JS-rendered fidelity as
    # every other page instead of this quick unrendered peek.
    #
    # Some sites only listen on "www." (or, less often, only on the bare domain) and
    # simply refuse -- or never resolve -- a connection on the other. A hard connection
    # failure here (no HTTP response at all, as opposed to a 403/404 the server actually
    # sent) is retried once on the alternate host before the rest of the crawl -- robots.txt,
    # sitemap, every page fetch -- commits to a host that may be entirely unreachable.
    seed_probe = await fetch(normalized, user_agent=BROWSER_UA)
    if seed_probe.status_code is None and seed_probe.error:
        alt_url = _alt_host_url(normalized)
        if alt_url:
            alt_probe = await fetch(alt_url, user_agent=BROWSER_UA)
            if alt_probe.status_code is not None:
                normalized, seed_probe = alt_url, alt_probe
                origin = origin_of(normalized)
                domain = urlparse(origin).netloc.lower()
    bump(3)
    robots = await fetch_robots(origin)
    bump(5)
    sitemap = await fetch_sitemaps(origin, robots.sitemap_urls)
    bump(8)

    home_links: list[str] = []
    if is_html(seed_probe) and seed_probe.text:
        probe_soup = BeautifulSoup(seed_probe.text, "html.parser")
        for a in probe_soup.find_all("a", href=True):
            abs_url = join_url(seed_probe.final_url, a["href"])
            if abs_url and same_host(abs_url, origin):
                home_links.append(urldefrag(abs_url)[0])
    llms = await fetch(urljoin_safe(origin, "/llms.txt"))
    bump(10)

    targets = _priority_urls(origin, sitemap.get("urls", []), home_links)
    errors: list[str] = []

    def on_scrape_progress(pct: int) -> None:
        # The scrape phase owns the 10-16% band of overall scan progress.
        bump(10 + int(pct / 100 * 6))

    fetched = await crawl_pages(targets, on_progress=on_scrape_progress)
    bump(16)

    pages_by_target: dict[str, Page] = {}
    all_targets: list[str] = list(targets)

    async def _parse_batch(url_list: list[str], fetched_pairs: dict) -> None:
        for u in url_list:
            pair = fetched_pairs.get(u)
            if not pair or "raw" not in pair:
                errors.append(f"{u}: no response from scraper")
                continue
            raw = pair["raw"]
            rendered = pair.get("rendered") or raw
            if raw.error:
                errors.append(f"{u}: {raw.error}")
            pages_by_target[u] = await asyncio.to_thread(parse_page, raw, rendered)

    await _parse_batch(targets, fetched)

    # The sitemap can be missing, stale, or blocked outright, which would otherwise cap
    # the crawl at whatever handful of links happen to be on the homepage. Keep following
    # same-host links actually found on the pages fetched so far -- in batches, across a
    # few rounds -- so site coverage doesn't depend on the sitemap succeeding.
    visited = set(all_targets)
    for _ in range(6):
        if len(pages_by_target) >= MAX_PAGES:
            break
        discovered: list[str] = []
        for p in pages_by_target.values():
            for link in p.links:
                href = urldefrag(link["href"])[0]
                if href and href not in visited and same_host(href, origin):
                    visited.add(href)
                    discovered.append(href)
        if not discovered:
            break
        discovered = discovered[: MAX_PAGES - len(pages_by_target)]
        all_targets.extend(discovered)
        batch_fetched = await crawl_pages(discovered)
        await _parse_batch(discovered, batch_fetched)
    bump(18)

    homepage = (
        pages_by_target.get(normalized)
        or pages_by_target.get(origin.rstrip("/") + "/")
        or (pages_by_target.get(targets[0]) if targets else None)
    )

    # pages_by_target is keyed by the originally-requested URL (a stable, deterministic
    # list) rather than fetch-completion order, so parameters that take a prefix of
    # ctx.pages (e.g. "first 8 pages") select the same pages on every scan of an
    # unchanged site rather than "whichever pages responded fastest".
    ordered = [homepage] if homepage else []
    seen = {homepage.result.final_url} if homepage else set()
    for u in all_targets:
        p = pages_by_target.get(u)
        if p is None or not p.result.final_url or p.result.final_url in seen:
            continue
        seen.add(p.result.final_url)
        ordered.append(p)

    host = domain.removeprefix("www.")
    stem = host.split(".")[0].replace("-", " ").strip()
    name = stem.title() or host
    if homepage and homepage.soup:
        og = homepage.soup.find("meta", attrs={"property": "og:site_name"})
        if og and og.get("content"):
            name = og["content"].strip()
        elif homepage.title:
            name = homepage.title.split("|")[0].split("-")[0].strip() or name
    brand_terms = list(dict.fromkeys([t for t in (name.lower(), stem.lower(), host.lower()) if t]))

    return SiteContext(
        input_url=input_url,
        normalized_url=normalized,
        origin=origin,
        domain=domain,
        robots=robots,
        sitemap=sitemap,
        homepage=homepage,
        pages=ordered,
        llms=llms,
        company_name=name,
        brand_terms=brand_terms,
        crawl_errors=errors,
    )


def _alt_host_url(url: str) -> str | None:
    """The same URL with 'www.' added or removed, for the one-time apex/www fallback probe."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return None
    alt_host = host[4:] if host.startswith("www.") else f"www.{host}"
    netloc = alt_host if not parsed.port else f"{alt_host}:{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def urljoin_safe(origin: str, path: str) -> str:
    from urllib.parse import urljoin
    return urljoin(origin.rstrip("/") + "/", path.lstrip("/"))


def sample_internal_links(ctx: SiteContext, limit: int = LINK_SAMPLE) -> list[str]:
    found = []
    for page in ctx.pages:
        for link in page.links:
            href = urldefrag(link["href"])[0]
            if same_host(href, ctx.origin) and href not in found:
                found.append(href)
            if len(found) >= limit:
                return found
    return found

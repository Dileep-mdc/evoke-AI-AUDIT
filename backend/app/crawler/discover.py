from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urldefrag, urlparse

from bs4 import BeautifulSoup

from ..config import BROWSER_UA, CONCURRENCY, LINK_SAMPLE, MAX_PAGES
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
    if any(k in blob for k in ("service", "solution", "capability", "engineering", "staffing", "digital")):
        return "service"
    if urlparse(url).path in {"", "/"}:
        return "home"
    return "other"


def parse_page(result: FetchResult) -> Page:
    soup = None
    if is_html(result) and result.text:
        soup = BeautifulSoup(result.text, "html.parser")
    page = Page(url=result.url, result=result, soup=soup)
    if not soup:
        return page
    title_el = soup.find("title")
    page.title = title_el.get_text(strip=True) if title_el else ""
    canon = soup.find("link", rel=lambda v: v and "canonical" in v)
    page.canonical = (canon.get("href") or "").strip() if canon else ""
    meta = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    page.meta_description = (meta.get("content") or "").strip() if meta else ""
    page.text = visible_text(soup)
    page.word_count = len(page.text.split())
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            page.headings.append({"level": level, "text": h.get_text(" ", strip=True)})
    for a in soup.find_all("a", href=True):
        abs_url = join_url(result.final_url, a["href"])
        if abs_url:
            page.links.append({"href": abs_url, "text": a.get_text(" ", strip=True)[:160]})
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        page.images.append({"src": join_url(result.final_url, src) or src, "alt": (img.get("alt") or "").strip()})
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
    page.page_type = classify_page(result.final_url, page.title, page.text)
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
        if any(k in path for k in ("service", "solution", "about", "case", "insight", "blog", "digital", "engineering", "staff")):
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
    bump(3)
    robots = await fetch_robots(origin)
    bump(5)
    sitemap = await fetch_sitemaps(origin, robots.sitemap_urls)
    bump(8)
    home_result = await fetch(normalized, user_agent=BROWSER_UA)
    homepage = await asyncio.to_thread(parse_page, home_result)
    llms = await fetch(urljoin_safe(origin, "/llms.txt"))
    bump(10)

    home_links = []
    if homepage:
        for link in homepage.links:
            if same_host(link["href"], origin):
                home_links.append(urldefrag(link["href"])[0])

    targets = _priority_urls(origin, sitemap.get("urls", []), home_links)
    sem = asyncio.Semaphore(CONCURRENCY)
    pages: list[Page] = []
    errors: list[str] = []

    async def grab(url: str) -> None:
        async with sem:
            try:
                result = await fetch(url, user_agent=BROWSER_UA)
                pages.append(await asyncio.to_thread(parse_page, result))
            except Exception as exc:
                errors.append(f"{url}: {exc}")

    await asyncio.gather(*(grab(u) for u in targets))
    bump(16)
    # keep homepage first
    pages_by_url = {p.result.final_url: p for p in pages if p.result.final_url}
    if homepage:
        pages_by_url[homepage.result.final_url] = homepage
    ordered = [homepage] if homepage else []
    for p in pages:
        if homepage and p.result.final_url == homepage.result.final_url:
            continue
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

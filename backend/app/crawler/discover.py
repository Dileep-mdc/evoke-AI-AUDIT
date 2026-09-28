from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urldefrag, urlparse

from bs4 import BeautifulSoup

from ..config import (
    BROWSER_UA,
    CRAWL_TIME_BUDGET,
    ENABLE_RENDER,
    MAX_DISCOVERY_ROUNDS,
    MAX_PAGES,
    PARSE_WORKERS,
    RENDER_BUDGET,
    RENDER_DELTA_THRESHOLD,
    RENDER_SHELL_WORDS,
)
from ..parameters.common import flatten_schema
from .extract import extract_many, extract_text_sync
from .fetch_many import crawl_raw
from .http import (
    FetchResult,
    fetch,
    is_html,
    join_url,
    looks_like_document,
    normalize_url,
    origin_of,
    same_host,
)
from .parse import make_soup
from .render import render_many, render_unavailable
from .resolver import prefer_reachable_addresses
from . import snapshot
from .robots import RobotsData, fetch_robots
from .sitemap import fetch_sitemaps, locale_of

log = logging.getLogger("crawler")


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
    # The post-JavaScript DOM, kept only when this page was actually rendered. `result.text`
    # above always holds the server's raw HTML regardless, so holding this beside it means
    # BOTH copies of a rendered page survive the crawl and can be written out verbatim --
    # which is the only way a reader of the saved output can check the extracted text against
    # the document it was extracted from, rather than taking the extraction on trust.
    rendered_html: str = ""
    # How this Page's CONTENT was obtained, and what rendering changed. `result` above is
    # always the httpx response regardless -- see parse_page(). Keys: content_source
    # ("raw" | "rendered" | "none"), and on a rendered page raw_words/rendered_words/
    # gained_words/gain_ratio, which is the raw-vs-rendered comparison TECH-09 scores.
    render: dict = field(default_factory=dict)


# Page furniture: repeated on every page, authored once, and not what any content parameter
# is asking about. trafilatura strips this itself; the fallback below did not, so on the
# pages where trafilatura returns nothing -- short homepages and service landing pages, i.e.
# exactly the pages that matter most -- the menu, footer and cookie banner were being counted
# as page copy. That inflated word_count (so thin pages read as substantial), put nav labels
# in ON-04's "opening", and made ON-03 quote the mega-menu as the site's definition of a term.
_CHROME_TAGS = ("nav", "header", "footer", "aside", "script", "style", "noscript", "template", "form", "svg")


def visible_text(soup: BeautifulSoup, html_source: str = "") -> str:
    """Body copy with page furniture removed.

    Works on a throwaway re-parse rather than mutating `soup`, because the caller still needs
    the full tree afterwards -- nav links feed the crawl frontier and TECH's link sampling, so
    decomposing them in place would break discovery to fix extraction. This only runs on the
    trafilatura-failed path, so the extra parse is paid on a minority of pages.
    """
    try:
        # The same parser the rest of the crawl uses (see parse.py): html.parser was several
        # times slower on exactly the large documents this runs on, and recovers from
        # malformed markup by dropping content -- which this function would then report as a
        # page having fewer words than it has.
        clone = make_soup(html_source or str(soup))
    except Exception:
        clone = None
    if clone is None:
        return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    for tag in clone.find_all(_CHROME_TAGS):
        tag.decompose()
    body = clone.body or clone
    return re.sub(r"\s+", " ", body.get_text(" ", strip=True)).strip()


def _in_chrome(element) -> bool:
    """True when this element sits inside page furniture rather than page content."""
    return element.find_parent(["nav", "header", "footer", "aside"]) is not None


def extract_jsonld(soup: BeautifulSoup) -> list:
    blocks = []
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


_PAGE_TYPE_RULES = (
    ("case_study", ("case-stud", "case_stud", "casestud", "success-stor", "portfolio", "customer-stor")),
    ("article", ("/blog", "/news", "/insights", "/article", "/press", "/resources")),
    ("utility", ("/career", "/job", "/contact", "/privacy", "/terms", "/cookie", "/sitemap", "/legal")),
    ("about", ("/about", "/company", "/who-we-are", "/leadership", "/team")),
    ("service", ("/service", "/solution", "/product", "/offering", "/capabilit", "/pricing", "/platform")),
)
# Same intent as above, matched against the title when the path says nothing. Kept separate
# because a path segment is a deliberate structural signal and a title word is a weak one.
_TITLE_RULES = (
    ("case_study", ("case study", "success story")),
    ("article", ("blog", "news", "press release")),
    ("utility", ("careers", "contact us", "privacy policy", "terms of")),
    ("about", ("about us", "who we are", "our company")),
    ("service", ("services", "solutions", "pricing")),
)


def classify_page(url: str, title: str, text: str) -> str:
    """What kind of page this is, decided from structure before prose.

    The previous version matched every rule against `url + title + text[:800]` and returned
    the first hit in a fixed order, which had two consequences worth naming. Body copy
    decided the type: a service page whose opening paragraph mentioned "news" or "about" was
    filed as an article before the service rule was ever reached. And matching was substring-
    anywhere, so "/showcase/" matched "case" and any page mentioning "about" became an about
    page. Since page_type is the sole basis for "relevant pages" across roughly fifteen
    parameters, both errors propagate into every denominator downstream.

    Order now runs most-specific first, the URL path is authoritative, the title is a
    fallback, and body text is not consulted at all.
    """
    path = (urlparse(url).path or "").lower()
    if path in {"", "/"}:
        return "home"
    for page_type, needles in _PAGE_TYPE_RULES:
        if any(needle in path for needle in needles):
            return page_type
    lowered = (title or "").lower()
    for page_type, needles in _TITLE_RULES:
        if any(needle in lowered for needle in needles):
            return page_type
    return "other"


# Distinguishes "no extraction was supplied" from "extraction was run and found nothing",
# which are different instructions to parse_page and produce different page text.
_NOT_EXTRACTED = object()


def html_for(raw: FetchResult, rendered_html: str | None = None) -> str:
    """The document parse_page() will read for this page: the rendered DOM when one was
    fetched, the server's HTML when it is HTML, and nothing otherwise.

    Exposed because a batch extracts body text in worker processes BEFORE parsing, and has to
    send the same document parse_page is about to read -- see _parse_batch in crawl_site().
    """
    if rendered_html and rendered_html.strip():
        return rendered_html
    if is_html(raw) and raw.text:
        return raw.text
    return ""


def parse_page(raw: FetchResult, rendered_html: str | None = None, extracted=_NOT_EXTRACTED) -> Page:
    """Build a Page, reading structure from the rendered DOM when one was fetched.

    This is where the two halves of the hybrid crawl meet, and the split is strict:

      page.result  is ALWAYS the httpx FetchResult -- status, headers, redirect hops, raw
                   bytes, timing. Never the browser's view. Every technical parameter that
                   scores the HTTP exchange (TECH-06 and TECH-20 on hop counts, TECH-02 on
                   the user-agent-conditional body size) therefore reads the same numbers it
                   always did, whether or not this page was rendered.
      page.soup    and everything derived from it -- title, headings, links, images, schema,
      + content    visible text, word count, page type -- come from `rendered_html` when one
                   was supplied, and from raw.text otherwise.

    That asymmetry is the point. On a client-rendered site the server sends an empty shell,
    so every on-page parameter reading raw.text measures nothing; but the shell's HTTP
    response is still the truthful one for the technical checks. Handing content extraction
    a different document from the one the response carried is what lets both be right.

    `render` on the returned Page records which source was used, so no reader has to guess.

    `extracted` is the page's body text when the caller has already extracted it -- a crawl
    does that a batch at a time in worker processes, since extraction is the most expensive
    step here and the only one that can leave this process. Omit it and extraction runs
    in-line, which is what a single-page caller wants.
    """
    soup = None
    html_source = ""
    content_source = "none"
    if rendered_html and rendered_html.strip():
        # No is_html() guard: nothing but an HTML document reaches a browser render, and the
        # caller only renders pages whose raw fetch already parsed.
        html_source = rendered_html
        content_source = "rendered"
    elif is_html(raw) and raw.text:
        html_source = raw.text
        content_source = "raw"
    if html_source:
        soup = make_soup(html_source)
    page = Page(url=raw.url, result=raw, soup=soup)
    page.render = {"content_source": content_source}
    # Only when it was the document actually read. Storing the browser's HTML on a page whose
    # content came from raw.text would put a second copy of the same bytes in memory for every
    # page of the crawl and make "rendered_html" mean nothing in the saved output.
    page.rendered_html = html_source if content_source == "rendered" else ""
    if not soup:
        return page
    base_url = raw.final_url
    title_el = soup.find("title")
    page.title = title_el.get_text(strip=True) if title_el else ""
    canon = soup.find("link", rel=lambda v: v and "canonical" in v)
    page.canonical = (canon.get("href") or "").strip() if canon else ""
    meta = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    page.meta_description = (meta.get("content") or "").strip() if meta else ""
    if extracted is _NOT_EXTRACTED:
        extracted = extract_text_sync(html_source)
    page.text = re.sub(r"\s+", " ", extracted).strip() if extracted else visible_text(soup, html_source)
    page.word_count = len(page.text.split())
    # Headings inside the mega-menu, header or footer are site furniture, not this page's
    # structure. Counting them made one site's heading population 3,863 entries of which the
    # same handful of menu labels ("AI & Automation", "The Evoke Edge") accounted for ~2,000 --
    # so ON-01 measured the share of the MENU phrased as buyer questions, and TECH-10 graded
    # every page's hierarchy against the same repeated nav tree.
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            if _in_chrome(h):
                continue
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


# Mount points a client-rendered framework leaves empty in the server's HTML and fills in the
# browser. Their presence is not itself a shell -- plenty of server-rendered Next.js sites ship
# a full #__next -- so _looks_like_shell() checks whether the element is EMPTY, not whether it
# exists.
_SPA_ROOT_SELECTORS = ("#root", "#app", "#__next", "#___gatsby", "[data-reactroot]")


def _looks_like_shell(page: Page) -> bool:
    """Whether this page's raw HTML looks like it is waiting for JavaScript to fill it in.

    Used only to ORDER the render budget, never to decide a score: a page that trips this is
    rendered ahead of a fuller one, and if the render fails or changes nothing the page keeps
    exactly the reading it had. A false positive therefore costs a browser tab, not a finding.

    Bot-challenge pages are excluded deliberately. A browser would often pass the challenge
    and return real content, which sounds like a win, but blocked_reason is itself reported
    (it is how a blocked page is kept out of every parameter's denominator) and quietly
    rendering past the block would erase that signal rather than measure it.
    """
    if page.soup is None or not page.result.ok or page.blocked_reason:
        return False
    if page.word_count < RENDER_SHELL_WORDS:
        return True
    for selector in _SPA_ROOT_SELECTORS:
        try:
            element = page.soup.select_one(selector)
        except Exception:  # an exotic selector unsupported by the installed soupsieve
            continue
        if element is not None and len(element.get_text(" ", strip=True).split()) < RENDER_SHELL_WORDS:
            return True
    return False


def _render_comparison(raw_page: Page, rendered_page: Page) -> dict:
    """The raw-vs-rendered diff for one page: the measurement TECH-09 reports.

    gain_ratio is expressed against the RAW count, so 0.5 means the browser found half again
    as many words as the server sent. When the server sent none at all -- the pure shell case,
    where a ratio is undefined -- it is reported as 1.0 if the browser found anything, which
    is the answer the threshold comparison needs and the strongest possible signal.
    """
    raw_words = raw_page.word_count
    rendered_words = rendered_page.word_count
    gained = rendered_words - raw_words
    if raw_words > 0:
        ratio = gained / raw_words
    else:
        ratio = 1.0 if rendered_words > 0 else 0.0
    return {
        "raw_words": raw_words,
        "rendered_words": rendered_words,
        "gained_words": gained,
        "gain_ratio": round(ratio, 4),
        "raw_headings": len(raw_page.headings),
        "rendered_headings": len(rendered_page.headings),
        "raw_links": len(raw_page.links),
        "rendered_links": len(rendered_page.links),
        "raw_schema_blocks": len(raw_page.schema_blocks),
        "rendered_schema_blocks": len(rendered_page.schema_blocks),
    }


def _render_candidates(
    pages_by_target: dict,
    ordered_targets: list,
    *,
    exclude: set,
    budget: int,
) -> list:
    """Which pages get the browser, in priority order.

    Shell-looking pages first, because they are where rendering changes the reading most and
    the budget is far smaller than the crawl. Everything else follows in crawl order, which is
    already priority-ordered by _priority_urls(), so a partial budget still covers the service
    and solution pages rather than whatever the sitemap happened to list last.
    """
    if budget <= 0:
        return []
    shells = []
    rest = []
    for key in ordered_targets:
        if key in exclude:
            continue
        page = pages_by_target.get(key)
        if page is None or page.soup is None or not page.result.ok:
            continue
        (shells if _looks_like_shell(page) else rest).append(key)
    return (shells + rest)[:budget]


async def _run_render_pass(
    probe_key: Optional[str],
    pages_by_target: dict,
    ordered_targets: list,
    on_progress: Optional[Callable[[int], None]] = None,
    saved_renders: bool = False,
) -> dict:
    """Spend the render budget, if the site turns out to need it, and report what happened.

    Probe first, commit second. One page is rendered and compared against its own raw HTML;
    only if the browser found materially more content does the rest of the budget get spent.
    A server-rendered site therefore pays for exactly one browser tab and the scan costs what
    it always did, while a client-rendered site -- where every on-page parameter was
    previously reading an empty shell -- gets its content back.

    Pages in `pages_by_target` are REPLACED in place by their rendered equivalents. The
    replacement carries the same FetchResult, so nothing about the HTTP exchange moves.

    A homepage that is served fully rendered does not prove the rest of the site is: a
    product catalogue can be server-rendered on the homepage and assembled in the browser on
    every category page. So pages that look like empty shells are rendered either way, and
    only the rest of the budget depends on the probe.

    `saved_renders`: the scan reads a saved copy of the site that includes each page's
    rendered HTML (crawler/snapshot.py), so rendering costs nothing and is not budgeted.

    Returns a summary for SiteContext.render. It always returns one, and never raises: every
    failure path here leaves the raw pages untouched and records the reason.
    """
    budget = len(ordered_targets) if saved_renders else RENDER_BUDGET
    summary = {
        "enabled": ENABLE_RENDER,
        "decision": "disabled",
        "reason": "",
        "probe_url": "",
        "budget": budget,
        "threshold": RENDER_DELTA_THRESHOLD,
        "attempted": 0,
        "rendered_count": 0,
        "failed_count": 0,
        "probe": {},
    }
    if not ENABLE_RENDER:
        summary["reason"] = "JavaScript rendering is switched off (ENABLE_RENDER=false)."
        return summary

    unavailable = None if saved_renders else render_unavailable()
    if unavailable:
        summary["decision"] = "unavailable"
        summary["reason"] = unavailable
        return summary

    probe_page = pages_by_target.get(probe_key) if probe_key else None
    if probe_page is None or probe_page.soup is None:
        summary["decision"] = "skipped"
        summary["reason"] = "no successfully-parsed page was available to probe with."
        return summary

    probe_url = probe_page.result.final_url or probe_page.url
    summary["probe_url"] = probe_url
    probe_results = await render_many([probe_url])
    summary["attempted"] = 1
    probe_result = probe_results.get(probe_url)
    if probe_result is None or not probe_result.ok:
        summary["decision"] = "unavailable"
        summary["reason"] = probe_result.error if probe_result else "the probe returned no result"
        summary["failed_count"] = 1
        return summary

    probe_rendered = await asyncio.to_thread(parse_page, probe_page.result, probe_result.html)
    comparison = _render_comparison(probe_page, probe_rendered)
    summary["probe"] = comparison

    pct = int(RENDER_DELTA_THRESHOLD * 100)
    if comparison["gain_ratio"] < RENDER_DELTA_THRESHOLD:
        # The homepage is served rendered. Shell-looking pages still get the browser.
        candidates = [
            key for key in _render_candidates(pages_by_target, ordered_targets, exclude={probe_key}, budget=budget - 1)
            # From a saved copy every render is free, so every page gets its rendered copy.
            if saved_renders or _looks_like_shell(pages_by_target[key])
        ]
        if not candidates:
            summary["decision"] = "not_needed"
            summary["reason"] = (
                "the rendered homepage carried {0} words against {1} in the server's own HTML, "
                "a difference below the {2}% threshold, and no other page looked like an empty "
                "shell, so the site is served fully rendered and the browser was not run on the rest of it."
            ).format(comparison["rendered_words"], comparison["raw_words"], pct)
            if on_progress:
                on_progress(100)
            return summary
        summary["decision"] = "applied"
        summary["reason"] = (
            "the homepage is served fully rendered ({0} words raw, {1} rendered); {2} other "
            "page(s) were read as the browser renders them{3}."
        ).format(comparison["raw_words"], comparison["rendered_words"], len(candidates),
                 " (saved with the site copy)" if saved_renders else ", because the server's HTML looked like an empty shell")
    else:
        summary["decision"] = "applied"
        summary["reason"] = (
            "the rendered homepage carried {0} words against {1} in the server's own HTML, so "
            "content on this site is assembled in the browser and the raw HTML under-reports it."
        ).format(comparison["rendered_words"], comparison["raw_words"])
        probe_rendered.render = {**probe_rendered.render, **comparison, "applied": True}
        pages_by_target[probe_key] = probe_rendered
        summary["rendered_count"] = 1

        candidates = _render_candidates(
            pages_by_target, ordered_targets, exclude={probe_key}, budget=budget - 1
        )
        if not candidates:
            if on_progress:
                on_progress(100)
            return summary

    url_by_key = {k: (pages_by_target[k].result.final_url or pages_by_target[k].url) for k in candidates}
    # Deduplicated because two crawl targets can redirect to one final URL, and rendering the
    # same page twice would spend budget on a document already in hand.
    urls = list(dict.fromkeys(url_by_key.values()))
    summary["attempted"] += len(urls)
    rendered = await render_many(urls, on_progress=on_progress)

    for key, url in url_by_key.items():
        result = rendered.get(url)
        raw_page = pages_by_target[key]
        if result is None or not result.ok:
            summary["failed_count"] += 1
            raw_page.render["render_error"] = result.error if result else "no render result"
            continue
        new_page = await asyncio.to_thread(parse_page, raw_page.result, result.html)
        page_comparison = _render_comparison(raw_page, new_page)
        if page_comparison["gained_words"] < 0:
            # The browser produced LESS text than the server sent. That is a failed render
            # (a consent wall, a redirect to an app shell, a timeout mid-mount), not a
            # finding, and the server's own HTML is the better reading -- so keep it.
            raw_page.render.update(page_comparison)
            raw_page.render["applied"] = False
            raw_page.render["render_error"] = "the rendered copy carried less text than the raw HTML"
            summary["failed_count"] += 1
            continue
        new_page.render = {**new_page.render, **page_comparison, "applied": True}
        pages_by_target[key] = new_page
        summary["rendered_count"] += 1

    return summary


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
    # What the JavaScript-rendering pass decided and did -- see _run_render_pass(). Always
    # populated, including when rendering was off or unavailable, because "we did not render,
    # and here is why" is the evidence TECH-09 reports when it cannot score.
    render: dict = field(default_factory=dict)
    # Set when the scan read a saved copy of the site (crawler/snapshot.py) instead of the web.
    snapshot: dict = field(default_factory=dict)


def _priority_urls(origin: str, sitemap_urls: list[str], homepage_links: list[str]) -> list[str]:
    seeds = [origin.rstrip("/") + "/"]
    preferred = []
    for u in sitemap_urls + homepage_links:
        path = urlparse(u).path.lower()
        if any(k in path for k in ("service", "solution", "product", "about", "case", "insight", "blog", "pricing", "feature")):
            preferred.append(u)
    rest = [u for u in sitemap_urls if u not in preferred]
    ordered = list(dict.fromkeys(seeds + preferred + homepage_links + rest))
    # Sitemaps routinely list brochure PDFs and image assets, and the crawl used to fetch and
    # attempt to parse every one of them. They can carry no HTML, so they can carry nothing
    # any parameter scores.
    ordered = [u for u in ordered if looks_like_document(u)]
    return ordered[:MAX_PAGES]


def _band(lo: float, hi: float, bump: Callable[[int], None]) -> Callable[[int], None]:
    """A 0-100 progress callback for one phase, mapped into its slice of overall scan progress.

    Phases report their own completion from 0 to 100 and should not know where they sit in a
    scan; this is the one place that decides. Bands are contiguous and bump() is monotonic, so
    a phase that finishes early cannot make the bar go backwards.
    """
    def report(pct: int) -> None:
        bump(int(lo + max(0, min(100, pct)) / 100 * (hi - lo)))
    return report


async def crawl_site(input_url: str, on_progress: Callable[[int], None] | None = None) -> SiteContext:
    # Monotonic, because progress is now reported from several phases whose bands touch, and a
    # bar that jumps backwards reads as a restart. The only place the number may fall is the
    # scan as a whole moving on, which happens in scans.py, not here.
    highest = 0

    def bump(pct: int) -> None:
        nonlocal highest
        if pct <= highest:
            return
        highest = pct
        if on_progress:
            on_progress(pct)

    started_at = time.monotonic()
    deadline = started_at + CRAWL_TIME_BUDGET
    errors: list[str] = []

    normalized = normalize_url(input_url)
    # A saved copy of the site, if one exists, under either the host as typed or its
    # www/apex twin -- someone typing example.com means the www.example.com snapshot.
    snap = snapshot.load(normalized)
    if snap is None and _alt_host_url(normalized):
        alt_snap = snapshot.load(_alt_host_url(normalized))
        if alt_snap is not None:
            normalized, snap = _alt_host_url(normalized), alt_snap
    origin = origin_of(normalized)
    domain = urlparse(origin).netloc.lower()
    bump(2)
    if snap is not None:
        log.info("reading %d saved pages from snapshot %s", len(snap.pages), snap.folder)

    # Before anything is fetched: find out which of the host's addresses can actually be
    # reached, and pin the crawl to those. A host behind a CDN answers on several, and when
    # only some of them complete a TLS handshake from this network every fetch is a coin
    # toss between a page and a connect timeout -- see resolver.py. Pointless when the pages
    # come from disk.
    reach = await prefer_reachable_addresses(normalized) if snap is None else {}
    if reach.get("checked") and reach.get("reachable", 0) < reach.get("total", 0):
        unreachable = reach["total"] - reach["reachable"]
        errors.append(
            f"{reach['host']}: {unreachable} of {reach['total']} addresses for this host "
            "would not complete a connection from here; the crawl used the ones that did."
        )

    # Lightweight probe of the homepage purely to discover its internal links for seeding
    # crawl priority. The homepage Page used everywhere else below comes from the main
    # crawl batch instead of this quick early peek.
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
    # The locale the homepage serves ("/en-in/" after a geo redirect), or the one the saved
    # pages are in, so a multi-language sitemap is read for the pages being audited.
    locale = locale_of(seed_probe.final_url) or (locale_of(snap.pages[0]) if snap is not None and snap.pages else "")
    sitemap = await fetch_sitemaps(origin, robots.sitemap_urls, prefer=locale)
    bump(8)

    home_links: list[str] = []
    if is_html(seed_probe) and seed_probe.text:
        probe_soup = make_soup(seed_probe.text)
        for a in probe_soup.find_all("a", href=True):
            abs_url = join_url(seed_probe.final_url, a["href"])
            if abs_url and same_host(abs_url, origin):
                home_links.append(urldefrag(abs_url)[0])
    llms = await fetch(urljoin_safe(origin, "/llms.txt"))
    bump(10)

    if snap is not None:
        # The saved page list IS the site for this scan: every page in the folder, homepage
        # first, and no link discovery below -- that would only reach pages the folder lacks.
        home = origin.rstrip("/") + "/"
        targets = list(dict.fromkeys([home] + [u for u in snap.pages if looks_like_document(u)]))
    else:
        targets = _priority_urls(origin, sitemap.get("urls", []), home_links)

    pages_by_target: dict[str, Page] = {}
    all_targets: list[str] = list(targets)
    parse_semaphore = asyncio.Semaphore(max(1, PARSE_WORKERS))

    async def _parse_batch(
        url_list: list[str],
        fetched_pairs: dict,
        on_parse_progress: Callable[[int], None] | None = None,
    ) -> list[Page]:
        """Turn a batch of fetched responses into Pages.

        Two things changed here, and only one of them is parallelism.

        The expensive half of reading a page -- trafilatura extraction -- now happens for the
        whole batch first, in worker processes, because it is pure-Python CPU work that
        threads cannot speed up (extract.py has the measurements). What is left is the
        BeautifulSoup tree, which runs on a small pool of threads: that is a wash on CPU, but
        it keeps the event loop free and it is what lets this report how far through it is.

        The reporting is the other half, and it is why a scan used to look hung. This was a
        `for` loop awaiting one page at a time, in the middle of the crawl, saying nothing --
        minutes of silence on a thousand-page site.

        Results are collected concurrently and then applied in `url_list` order, because
        pages_by_target and the error list are read downstream as deterministic, request-
        ordered records of the crawl -- not as whatever finished first.
        """
        done = 0
        total = len(url_list) or 1
        last_reported = -1

        def tick() -> None:
            nonlocal done, last_reported
            done += 1
            if on_parse_progress:
                pct = int(done / total * 100)
                if pct != last_reported:
                    last_reported = pct
                    on_parse_progress(pct)

        # Body text first, for the whole batch, in worker processes -- see extract.py. It is
        # the one part of reading a page that can leave this process, and on a large batch it
        # is most of the cost.
        documents = [html_for(fetched_pairs[u]) if fetched_pairs.get(u) else "" for u in url_list]
        extracted = await extract_many(documents)

        async def _one(u: str, body_text: Optional[str]) -> tuple[str, Optional[Page]]:
            raw = fetched_pairs.get(u)
            page = None
            if raw is not None:
                async with parse_semaphore:
                    page = await asyncio.to_thread(parse_page, raw, None, body_text)
            tick()
            return u, page

        parsed = dict(await asyncio.gather(
            *(_one(u, extracted[i]) for i, u in enumerate(url_list))
        ))
        fresh: list[Page] = []
        for u in url_list:
            raw = fetched_pairs.get(u)
            page = parsed.get(u)
            if raw is None or page is None:
                errors.append(f"{u}: no response from scraper")
                continue
            if raw.error:
                errors.append(f"{u}: {raw.error}")
            pages_by_target[u] = page
            fresh.append(page)
        return fresh

    fetched = await crawl_raw(
        targets,
        user_agent=BROWSER_UA,
        on_progress=_band(10, 18, bump),
        deadline=deadline,
    )
    frontier = await _parse_batch(targets, fetched, on_parse_progress=_band(18, 21, bump))

    # The sitemap can be missing, stale, or blocked outright, which would otherwise cap
    # the crawl at whatever handful of links happen to be on the homepage. Keep following
    # same-host links actually found on the pages fetched so far -- in batches, across a
    # few rounds -- so site coverage doesn't depend on the sitemap succeeding.
    #
    # Each round reads links only from the pages the PREVIOUS round brought back. Re-walking
    # every page every round could not discover anything new -- `visited` already holds all of
    # their links -- it just re-read hundreds of thousands of hrefs to find nothing.
    visited = set(all_targets)
    for round_number in range(MAX_DISCOVERY_ROUNDS if snap is None else 0):
        if len(pages_by_target) >= MAX_PAGES or not frontier:
            break
        if time.monotonic() > deadline:
            errors.append(
                f"link discovery stopped after {int(time.monotonic() - started_at)}s: "
                "the crawl reached its time budget"
            )
            break
        discovered: list[str] = []
        for p in frontier:
            for link in p.links:
                href = urldefrag(link["href"])[0]
                if not href or href in visited:
                    continue
                visited.add(href)
                if same_host(href, origin) and looks_like_document(href):
                    discovered.append(href)
        if not discovered:
            break
        discovered = discovered[: MAX_PAGES - len(pages_by_target)]
        all_targets.extend(discovered)
        # Rounds share the 21-27 band, each taking an equal slice, and inside a slice the
        # fetch and the parse take their own sub-bands -- so a long crawl keeps moving instead
        # of sitting on one number until every round has finished.
        span = 6 / max(1, MAX_DISCOVERY_ROUNDS)
        lo = 21 + round_number * span
        hi = 21 + (round_number + 1) * span
        batch_fetched = await crawl_raw(
            discovered,
            user_agent=BROWSER_UA,
            on_progress=_band(lo, hi - span / 3, bump),
            deadline=deadline,
        )
        frontier = await _parse_batch(
            discovered, batch_fetched, on_parse_progress=_band(hi - span / 3, hi, bump)
        )
        log.info(
            "discovery round %d: %d new urls, %d pages held, %.0fs elapsed",
            round_number + 1, len(discovered), len(pages_by_target), time.monotonic() - started_at,
        )
    bump(26)

    # The second half of the hybrid crawl. Every page above was fetched over plain HTTP and
    # holds the response facts the technical parameters score; this re-reads a small, chosen
    # subset in a real browser and swaps in the DOM that JavaScript produced, so the on-page
    # parameters measure the content a reader sees rather than the shell the server sent.
    # It replaces entries in pages_by_target, so it has to run before homepage is picked out
    # of it -- otherwise the homepage would be the only page still holding its raw copy.
    home_key = next(
        (
            key
            for key in (normalized, origin.rstrip("/") + "/", targets[0] if targets else None)
            if key and key in pages_by_target
        ),
        None,
    )

    render_summary = await _run_render_pass(
        home_key, pages_by_target, all_targets, on_progress=_band(26, 28, bump),
        saved_renders=bool(snap is not None and snap.has_renders),
    )
    bump(28)

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
        # The company's own declared name before its homepage title: a title is often a
        # tagline ("Building a more Sustainable and Electrified World"), and every off-page
        # parameter searches the web for this name -- a tagline finds nothing about the company.
        org = next((i.get("name") for i in flatten_schema(homepage.schema_blocks)
                    if "Organization" in str(i.get("@type")) or "Corporation" in str(i.get("@type"))), None)
        if og and og.get("content"):
            name = og["content"].strip()
        elif isinstance(org, str) and org.strip():
            name = org.strip()
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
        render=render_summary,
        snapshot=snap.summary() if snap is not None else {},
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


def internal_links(ctx: SiteContext) -> list[str]:
    """Every distinct internal link on the site, in the order first encountered.

    This used to sample, stopping at forty. TECH-06 then reported the share of those forty
    that were broken as if it were the site's broken-link rate, which on a thousand-page site
    is a spot check presented as a measurement. It returns all of them now.
    """
    found: list[str] = []
    seen: set[str] = set()
    for page in ctx.pages:
        for link in page.links:
            href = urldefrag(link["href"])[0]
            if href in seen or not same_host(href, ctx.origin):
                continue
            seen.add(href)
            found.append(href)
    return found

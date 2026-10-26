from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from ..config import BROWSER_UA, LINK_RECHECK_LIMIT
from ..crawler.discover import _in_chrome, internal_links
from ..crawler.http import check, fetch
from ..crawler import vitals
from ..crawler.robots import AI_BOTS, bot_decision
from ..llm.client import judge
from ..llm.prompts import SYSTEM, tech11_prompt
from . import rules
from . import industry
from ..crawler.sitemap import locale_of
from .common import (VITAL_NAMES, VITALS, band, flatten_schema, heading_blocks, link, ms_since, no_data, not_applicable,
                     page_links, result, schema_types, scorable_pages, structured_authors, timed, url_links, vital_rating,
                     vital_score)

# Declared in scoring_rules.json rather than inline, for the same reason as onpage.py: these
# are the methodology, and they belong where a reviewer can read them together.
_MIN_CONTENT_WORDS = rules.threshold("min_content_words_for_density")
_REDIRECT_CHAIN_HOPS = rules.threshold("redirect_chain_hops")
_CHAIN_PENALTY = rules.threshold("redirect_chain_penalty", "per_chain")
_CHAIN_PENALTY_MAX = rules.threshold("redirect_chain_penalty", "max")
_PROXY_LATENCY = tuple(tuple(b) for b in rules.threshold("proxy_latency_ms", "bands"))
_PROXY_LATENCY_FLOOR = rules.threshold("proxy_latency_ms", "floor")
_PROXY_BYTES = tuple(tuple(b) for b in rules.threshold("proxy_page_bytes", "bands"))
_PROXY_BYTES_FLOOR = rules.threshold("proxy_page_bytes", "floor")
_PROXY_MIX = rules.threshold("proxy_mix", "latency"), rules.threshold("proxy_mix", "size")


def _at_most(value: float, bands: tuple[tuple[float, float], ...], floor: float) -> float:
    """First score whose limit `value` is at or under, else `floor` -- band() for lower-is-better."""
    for limit, score in bands:
        if value <= limit:
            return score
    return floor


async def tech_01(spec, ctx):
    t = timed()
    robots = ctx.robots
    links = [link(robots.url, "The site's robots.txt: read to see whether each AI crawler (GPTBot, ClaudeBot, PerplexityBot and the others) is allowed or blocked.")]
    if not robots.result.ok and not robots.raw.strip():
        return result(spec, score=None, unknown=True, evidence={"url": robots.url}, recommendation="Publish a robots.txt that explicitly allows AI crawlers.", checked=robots.url, error="robots.txt unavailable", duration_ms=ms_since(t), links=links)
    decisions = [bot_decision(robots, bot) for bot in AI_BOTS]
    known = [d for d in decisions if d["decision"] in {"ALLOWED", "BLOCKED"}]
    if not known:
        return result(spec, score=None, unknown=True, evidence={"url": robots.url, "decisions": decisions}, recommendation="Ensure robots.txt parses cleanly so AI-crawler rules can be evaluated.", checked=robots.url, error="robots.txt did not parse", duration_ms=ms_since(t), links=links)
    allowed = sum(1 for d in known if d["decision"] == "ALLOWED")
    score = allowed / len(known) * 100
    blocked = [d["name"] for d in known if d["decision"] == "BLOCKED"]
    rec = f"Allow these AI crawlers in robots.txt: {', '.join(blocked)}." if blocked else None
    return result(spec, score=score, evidence={"url": robots.url, "decisions": decisions}, recommendation=rec, checked=robots.url, duration_ms=ms_since(t), links=links)


async def tech_02(spec, ctx):
    t = timed()
    url = ctx.origin.rstrip("/") + "/"
    # The app's full browser user-agent. The abbreviated "Mozilla/5.0 (compatible; Chrome/...)"
    # used before is itself refused by bot-management CDNs, which left no baseline at all.
    browser_fetch = await fetch(url, user_agent=BROWSER_UA, live=True)
    links = [link(url, "The homepage: requested once as a normal browser and once as each AI crawler, to see whether the CDN or firewall blocks AI crawlers or serves them less content.")]
    if not browser_fetch.ok:
        return result(spec, score=None, unknown=True, evidence={"url": url, "error": browser_fetch.error}, recommendation="Homepage was unreachable; retry the scan.", checked=url, error=browser_fetch.error or "homepage unreachable", duration_ms=ms_since(t), links=links)
    baseline_len = len(browser_fetch.content)
    bot_results = await asyncio.gather(*(fetch(url, user_agent=f"{bot}/1.0") for bot in AI_BOTS))
    rows = []
    blocked = []
    for bot, res in zip(AI_BOTS, bot_results):
        body_len = len(res.content)
        starved = baseline_len > 500 and body_len < baseline_len * 0.2
        is_block = (res.status_code in {403, 406, 429}) or starved
        rows.append({"bot": bot, "status": res.status_code, "body_bytes": body_len, "blocked": is_block})
        if is_block:
            blocked.append(bot)
    score = (len(AI_BOTS) - len(blocked)) / len(AI_BOTS) * 100
    rec = f"Allow CDN/firewall traffic from: {', '.join(blocked)}." if blocked else None
    return result(spec, score=score, evidence={"baseline_bytes": baseline_len, "bots": rows}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.6, links=links)


_LLMS_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")
_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2}|20\d{2}[/.]\d{1,2}[/.]\d{1,2}|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+20\d{2})\b", re.I)


def _llms_link_target(link: str, origin: str) -> str | None:
    """The URL to request for one llms.txt link, or None when it is not a web page.

    mailto:, tel:, javascript: and #anchor links were prefixed with the origin (so a mailto:
    link became <origin>/mailto:...) and then counted as broken pages.
    """
    if link.startswith("#"):
        return None
    if link.startswith("//"):
        return "https:" + link
    scheme = urlparse(link).scheme.lower()
    if scheme in ("http", "https"):
        return link
    if scheme:
        return None
    return origin.rstrip("/") + "/" + link.lstrip("/")


async def tech_03(spec, ctx):
    t = timed()
    llms = ctx.llms
    file_link = [link(llms.url or ctx.origin.rstrip("/") + "/llms.txt", "The site's /llms.txt file: checked that it exists, has content, links to pages that resolve, and carries a date.")]
    if not llms.ok or not llms.text.strip():
        return result(spec, score=0, evidence={"url": llms.url, "status": llms.status_code}, recommendation="Publish an /llms.txt file listing the site's most important pages for AI assistants.", checked=ctx.origin.rstrip("/") + "/llms.txt", duration_ms=ms_since(t), links=file_link)
    text = llms.text
    non_empty = len(text.strip()) > 40
    found = _LLMS_LINK_RE.findall(text)
    links = [(link, url) for link in found if (url := _llms_link_target(link, ctx.origin))]
    resolved = []
    if links:
        checks = await asyncio.gather(*(check(url) for _, url in links))
        resolved = [{"url": link, "ok": c.ok} for (link, _), c in zip(links, checks)]
    link_ratio = (sum(1 for r in resolved if r["ok"]) / len(resolved)) if resolved else 0.0
    fresh = bool(_DATE_RE.search(text))
    score = 40 + (20 if non_empty else 0) + (20 * link_ratio if links else 10) + (20 if fresh else 0)
    rec = "Keep llms.txt non-empty, link to pages that resolve, and include a freshness/date signal." if score < 90 else None
    checked_links = file_link + url_links([u for _, u in links], "Linked from llms.txt: requested to confirm the link resolves.")
    return result(spec, score=min(100, score), evidence={"non_empty": non_empty, "links_found": len(links), "non_web_links_skipped": len(found) - len(links), "sampled_links": resolved, "freshness_signal": fresh}, recommendation=rec, checked=llms.final_url, duration_ms=ms_since(t), links=checked_links)


_OVERLAY_MARKERS = ("cookie", "consent", "gdpr", "onetrust", "cookiebot", "trustarc", "cookieyes", "modal", "overlay", "paywall")


async def tech_04(spec, ctx):
    t = timed()
    home = ctx.homepage
    if not home or not home.soup:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="Homepage was unavailable.", checked=ctx.origin, error="no homepage", duration_ms=ms_since(t))
    blob = str(home.soup)[:200000].lower()
    matched = [m for m in _OVERLAY_MARKERS if m in blob]
    login_form = bool(home.soup.find("form", attrs={"action": re.compile("login|signin", re.I)}) or home.soup.find("input", attrs={"type": "password"}))
    if not matched and not login_form:
        score = 100
    elif home.word_count >= 150:
        score = 70
    else:
        score = 20
    if login_form and home.word_count < 80:
        score = min(score, 20)
    rec = "Ensure the main page content is not hidden behind a login, pop-up or cookie wall." if score < 90 else None
    return result(spec, score=score, evidence={"markers_found": matched, "login_form": login_form, "word_count": home.word_count}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.55,
                  links=page_links([home], "The homepage: its HTML is searched for a login form and for cookie, consent or pop-up overlays covering the content."))


async def tech_05(spec, ctx):
    t = timed()
    sm = ctx.sitemap
    urls = sm.get("urls") or []
    files = url_links([s.get("url") for s in sm.get("sources") or []], "Sitemap file: read for the list of page URLs it publishes.")
    if not urls:
        tried = url_links(sm.get("candidates"), "Sitemap location tried (from robots.txt or the usual /sitemap.xml paths); no page URLs were found in it.")
        return result(spec, score=0, evidence={"candidates": sm.get("candidates"), "errors": sm.get("errors")}, recommendation="Publish an XML sitemap and reference it from robots.txt.", checked=ctx.origin, duration_ms=ms_since(t), links=files + tried)
    # Every sitemap URL is validated, not the first fifteen in document order -- "is the
    # sitemap accurate" cannot be answered from a fixed-size slice of the front of it.
    sample = urls
    checks = await asyncio.gather(*(check(u) for u in sample))
    valid = sum(1 for c in checks if c.ok)
    validity = valid / len(sample) * 100
    # Pages the crawl actually retrieved: a 404 or a bot challenge is not a page the sitemap should list.
    crawled = {p.result.final_url.rstrip("/") for p in scorable_pages(ctx)}
    sitemap_set = {u.rstrip("/") for u in urls}
    covered = sum(1 for u in crawled if u in sitemap_set)
    coverage = (covered / len(crawled) * 100) if crawled else 0
    score = 25 + validity * 0.45 + coverage * 0.30
    rec = "Fix sitemap URLs that don't resolve and ensure crawled pages are listed in the sitemap." if score < 90 else None
    checked_links = (files + url_links(sample, "Listed in the sitemap: requested to confirm the URL resolves (sitemap accuracy).")
                     + page_links(scorable_pages(ctx), "Crawled page: checked for whether the sitemap lists it (sitemap coverage)."))
    return result(spec, score=min(100, score), evidence={"sitemap_url_count": len(urls), "sampled": len(sample), "valid_sampled": valid, "crawled_coverage_pct": round(coverage, 1), "errors": sm.get("errors")}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.7, links=checked_links)


async def tech_06(spec, ctx):
    t = timed()
    # Every internal link found, not a fifteen-link spot check: a broken-link parameter that
    # samples reports the share of its sample that was broken, not the site's.
    links = internal_links(ctx)
    if not links:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="No internal links were found to sample.", checked=ctx.origin, error="no internal links", duration_ms=ms_since(t))
    # The crawl has already requested most of these links, and its response -- status and
    # redirect hops -- is the answer a second request would give. Re-checking all of them was
    # thousands of requests on a large site: it ran TECH-06 into its 15-minute timeout and
    # queued TECH-02's handful of requests behind it until that timed out too. Only links the
    # crawl never requested are checked now. Keyed by the URL requested, not the one it
    # redirected to, so a link straight to a redirect target is not charged the redirect's hops.
    crawled = {p.url: p.result for p in ctx.pages if p.result is not None}
    uncrawled = [u for u in links if u not in crawled]
    pending = uncrawled[:LINK_RECHECK_LIMIT]
    fresh = dict(zip(pending, await asyncio.gather(*(check(u) for u in pending))))
    # Links neither crawled nor in the re-check sample are left out, and counted below.
    links = [u for u in links if u in crawled or u in fresh]
    if not links:
        # Every link went uncrawled and the re-check budget is zero, so nothing was checked.
        return result(spec, score=None, unknown=True, evidence={"uncrawled_links": len(uncrawled), "not_checked": len(uncrawled)},
                      recommendation="No internal link could be checked; raise LINK_RECHECK_LIMIT and re-scan.",
                      checked=ctx.origin, error="no internal links checked", duration_ms=ms_since(t))
    checks =[crawled.get(u) or fresh[u] for u in links]
    checked_links = [link(u, "Internal link found on the site's pages: its status and redirect hops come from the crawl's own request for it."
                          if u in crawled else
                          "Internal link found on the site's pages that the crawl never requested: requested now to see whether it resolves.")
                     for u in links]
    rows = []
    broken = chains = 0
    for u, c in zip(links, checks):
        is_broken = not c.ok
        is_chain = c.hops >= _REDIRECT_CHAIN_HOPS
        broken += int(is_broken)
        chains += int(is_chain)
        rows.append({"url": u, "status": c.status_code, "hops": c.hops, "broken": is_broken, "chain": is_chain})
    score = (len(links) - broken) / len(links) * 100 - min(_CHAIN_PENALTY_MAX, chains * _CHAIN_PENALTY)
    rec = "Fix broken internal links and collapse redirect chains to 1-2 hops." if score < 90 else None
    return result(spec, score=max(0, score), evidence={"sampled": len(links), "from_crawl": len(links) - len(pending), "rechecked": len(pending),
                                                     "uncrawled_links": len(uncrawled), "not_checked": len(uncrawled) - len(pending), "broken": broken, "redirect_chains": chains, "pages": rows}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.7, links=checked_links)


async def tech_07(spec, ctx):
    """Real Core Web Vitals (crawler/vitals.py): CrUX field data, else a throttled lab run, else
    the latency/weight proxy this check used before either was wired in."""
    t = timed()
    vit = await vitals.measure(ctx)
    source = vit.get("source")
    links = _tech_07_links(ctx, vit)
    if source not in VITALS:
        return _tech_07_proxy(spec, ctx, t, vit.get("unavailable") or [], links)
    metrics = vit["metrics"]
    # Averaged unrounded, and stored that way: rounding each metric's points first, or having the
    # working re-derive them from the rounded metrics below, could land a tenth off the score.
    points = {m: vital_score(m, metrics[m]) for m in VITALS[source]}
    score = sum(points.values()) / len(points)
    evidence = {
        "vitals_source": source,
        "metrics": {m: round(metrics[m], 3 if m == "cls" else 0) for m in VITALS[source]},
        "ratings": {m: vital_rating(m, metrics[m]) for m in VITALS[source]},
        "metric_points": points,
    }
    if source == "field":
        evidence.update(crux_origin=vit.get("origin"), crux_form_factor=vit.get("form_factor"),
                        crux_collection_period=vit.get("collection_period"),
                        note="Real-user data from Google's Chrome UX Report: 75th percentile over the last 28 days.")
    else:
        evidence.update(lab_pages=vit.get("lab_pages"), lab_throttled=vit.get("throttled"),
                        field_data_unavailable=vit.get("field_unavailable"),
                        note=("Measured in headless Chromium" + (" throttled like Lighthouse mobile" if vit.get("throttled") else "")
                              + "; the median page per metric. Total Blocking Time stands in for INP, which needs real users."))
    weak = [VITAL_NAMES[m] for m in VITALS[source] if evidence["ratings"][m] != "good"]
    how = ("optimise the largest above-the-fold image or text block, cut long-running JavaScript, and reserve "
           "space for images, ads and embeds so the layout does not shift.")
    rec = None if score >= 90 else (f"Improve {', '.join(weak)}: {how}" if weak else how[:1].upper() + how[1:])
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=vit.get("origin") or ctx.origin,
                  duration_ms=ms_since(t), confidence=0.9 if source == "field" else 0.7, links=links)


def _tech_07_links(ctx, vit: dict) -> list:
    """What TECH-07 measured: the origin's Chrome UX Report record (only when the API was actually
    asked -- with no key configured no request is made), and the pages the browser lab run loaded."""
    reasons = list(vit.get("unavailable") or []) + [vit.get("field_unavailable") or ""]
    out = []
    if not any("CRUX_API_KEY" in r for r in reasons):
        out.append(link(vit.get("origin") or ctx.origin,
                        "Site origin: Google's Chrome UX Report was asked for this site's real-user Core Web Vitals (LCP, INP, CLS)."
                        + ("" if vit.get("source") == "field" else " It had no complete record, so it did not decide the score.")))
    for page in vit.get("lab_pages") or []:
        measured = all(k in page for k in ("lcp_ms", "cls", "tbt_ms"))
        out.append(link(page.get("url"), "Key page loaded in a throttled headless browser to measure LCP, CLS and Total Blocking Time."
                        if measured else "Key page a headless browser tried to measure; the measurement failed, so it did not count."))
    return [x for x in out if x]


def _tech_07_proxy(spec, ctx, t, unavailable: list[str], links: list):
    """Latency and HTML weight from the crawl itself: the fallback when no Core Web Vitals source works."""
    pages = [p for p in ctx.pages if p.result and p.result.ok]
    if not pages:
        return result(spec, score=None, unknown=True, evidence={"vitals_unavailable": unavailable}, recommendation="No successfully crawled pages to measure.", checked=ctx.origin, error="no pages", duration_ms=ms_since(t), links=links)
    latencies = [p.result.elapsed_ms for p in pages]
    sizes = [len(p.result.content) for p in pages]
    avg_latency = sum(latencies) / len(latencies)
    avg_size = sum(sizes) / len(sizes)
    latency_score = _at_most(avg_latency, _PROXY_LATENCY, _PROXY_LATENCY_FLOOR)
    weight_score = _at_most(avg_size, _PROXY_BYTES, _PROXY_BYTES_FLOOR)
    score = latency_score * _PROXY_MIX[0] + weight_score * _PROXY_MIX[1]
    rec = "Reduce server response time and page weight on representative pages; this proxy score is not a substitute for a real Lighthouse/CrUX audit." if score < 90 else None
    # The band points are stored as scored, from the unrounded averages: re-banding the rounded
    # averages in the working could cross a band edge the real averages did not.
    evidence = {"vitals_source": "proxy", "pages_measured": len(pages), "avg_latency_ms": round(avg_latency), "avg_page_bytes": round(avg_size),
                "latency_score": latency_score, "weight_score": weight_score, "vitals_unavailable": unavailable,
                "note": "Latency/weight proxy: real Core Web Vitals could not be measured for the reasons listed."}
    links = links + page_links(pages, "Crawled page: its response time and HTML size from the crawl feed the speed estimate, because no Core Web Vitals source was available.")
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.4, links=links)


# A fixed layout width of 1000px or more declared as a CSS rule (`min-width: 1200px`), which
# forces horizontal scrolling on a phone. The "(" guard skips media queries such as
# `@media (min-width:1200px)`: those are responsive breakpoints, the opposite signal. The
# old test -- "min-width:" and "1200px" anywhere in the HTML -- fired on any responsive
# theme, because `@media (min-width:922px)` and `max-width:1200px` satisfy it together.
_FIXED_MIN_WIDTH = re.compile(r"(?<![\w-])min-width\s*:\s*(?:[1-9]\d{3,})px", re.I)
_MEDIA_CONDITION = re.compile(r"\(\s*(?:min|max)-width\s*:[^)]*\)", re.I)
# A rule inside `@media (min-width: 768px) { ... }` or wider only applies on a tablet or desktop,
# so a `min-width: 1200px` there is the desktop layout of a responsive theme, not a width forced
# on a phone. Such blocks are dropped before the test.
_MEDIA_OPEN = re.compile(r"@media\b([^{;]*)\{", re.I)
_MEDIA_MIN_WIDTH = re.compile(r"min-width\s*:\s*(\d+(?:\.\d+)?)px", re.I)
_DESKTOP_BREAKPOINT_PX = 768
_BRACE = re.compile(r"[{}]")


def _without_desktop_media(css: str) -> str:
    out, pos = [], 0
    for match in _MEDIA_OPEN.finditer(css):
        if match.start() < pos:
            continue   # nested inside a block already dropped
        widths = [float(w) for w in _MEDIA_MIN_WIDTH.findall(match.group(1))]
        if not widths or max(widths) < _DESKTOP_BREAKPOINT_PX:
            continue
        depth, end = 1, len(css)
        for brace in _BRACE.finditer(css, match.end()):
            depth += 1 if brace.group() == "{" else -1
            if not depth:
                end = brace.end()
                break
        out.append(css[pos:match.start()])
        pos = end
    out.append(css[pos:])
    return "".join(out)


def has_fixed_width_layout(html: str) -> bool:
    return bool(_FIXED_MIN_WIDTH.search(_MEDIA_CONDITION.sub("", _without_desktop_media(html or ""))))


async def tech_08(spec, ctx):
    t = timed()
    home = ctx.homepage
    if not home:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="Homepage unavailable.", checked=ctx.origin, error="no homepage", duration_ms=ms_since(t))
    https = home.result.final_url.startswith("https://")
    viewport = False
    overflow = False
    if home.soup:
        vp = home.soup.find("meta", attrs={"name": re.compile("viewport", re.I)})
        viewport = bool(vp and "width" in (vp.get("content") or "").lower())
        overflow = has_fixed_width_layout(home.result.text or "")
    score = (50 if https else 0) + (50 if viewport else 0)
    if overflow:
        score = min(score, 80)
    rec = "Serve HTTPS and include a mobile viewport meta tag." if score < 90 else None
    return result(spec, score=score, evidence={"final_url": home.result.final_url, "https": https, "viewport": viewport, "fixed_width_layout": overflow}, recommendation=rec, checked=home.result.final_url, duration_ms=ms_since(t),
                  links=page_links([home], "The homepage: checked for HTTPS, a mobile viewport tag and fixed-width layout rules that force sideways scrolling on a phone."))


def _tech_09_row(url, info: dict):
    """One compared page as (evidence row, unrounded JS-only share), or None with no rendered words."""
    rendered_words = info.get("rendered_words") or 0
    if rendered_words <= 0:
        return None
    gained = max(0, info.get("gained_words") or 0)
    # Share of the page a JavaScript-blind crawler never sees.
    hidden = gained / rendered_words
    return {
        "url": url,
        "raw_words": info.get("raw_words"),
        "rendered_words": rendered_words,
        "gained_words": gained,
        "js_only_share_pct": round(hidden * 100, 1),
        "raw_headings": info.get("raw_headings"),
        "rendered_headings": info.get("rendered_headings"),
    }, hidden


async def tech_09(spec, ctx):
    t = timed()
    # Content that only exists after JavaScript runs. The crawl fetches every page over plain
    # HTTP and, on sites where that turns out to matter, re-fetches a subset in a real browser
    # (crawler/render.py); this is the comparison between the two copies.
    #
    # Direction of the score: a page whose words are all present in the server's own HTML is
    # readable by every AI crawler including the ones that do not execute JavaScript, and
    # scores 100. A page whose words appear only after the framework mounts is invisible to
    # them, and scores toward 0. The measure is the SHARE of the rendered page's words that
    # the raw HTML was missing, averaged over the pages actually compared.
    render = getattr(ctx, "render", None) or {}
    decision = render.get("decision")

    probe_url = render.get("probe_url")
    probe_row = _tech_09_row(probe_url, render.get("probe") or {}) if probe_url else None

    if decision == "not_needed":
        # The probe found the rendered homepage carried no materially more text than the raw
        # HTML, so the browser was not run on the rest of the site. That is not an absence of
        # evidence -- it is the finding: this site does not hide its content behind JavaScript.
        # Scored by the same share as every other compared page, so the few words a homepage
        # does gain in the browser still count rather than a flat 100.
        if probe_row is None:
            # The probe found no words in either copy: nothing to hide, and nothing to divide by.
            return result(spec, score=100, evidence={"compared_pages": 1, "probe_url": probe_url, "summary": render.get("reason"),
                                                     "pages": [{"url": probe_url, **(render.get("probe") or {})}]},
                          recommendation=None, checked=probe_url or ctx.origin, duration_ms=ms_since(t), confidence=0.7,
                          links=[link(probe_url, _TECH_09_PROBE)])
        rows, shares = [probe_row[0]], [probe_row[1]]
        summary, confidence = render.get("reason"), 0.7
    else:
        rows, shares = [], []
        for page in ctx.pages:
            info = getattr(page, "render", None) or {}
            if not info.get("applied"):
                continue
            row = _tech_09_row(page.result.final_url, info)
            if row:
                rows.append(row[0])
                shares.append(row[1])
        # The homepage probe is always one of the compared pages. When only pages that looked like
        # empty shells were rendered, they alone would otherwise decide the score -- and they were
        # picked precisely because they look worst.
        compared = {r["url"].rstrip("/") for r in rows}
        if rows and probe_row and probe_url.rstrip("/") not in compared:
            rows.insert(0, probe_row[0])
            shares.insert(0, probe_row[1])
        summary, confidence = None, 0.8

    if not rows:
        # No rendered copy exists to compare against: rendering was switched off, Playwright
        # was not installed, or the browser could not be launched. UNKNOWN rather than a
        # guess, and retry_pointless so the scan's automatic retry does not re-run a check whose
        # missing input is the crawl, not the model -- re-running it inside this scan cannot
        # produce a rendered copy that was never fetched.
        reason = render.get("reason") or "no JavaScript-rendered copy of any page was fetched."
        return result(
            spec,
            score=None,
            unknown=True,
            evidence={
                "retry_pointless": True,
                "render_decision": decision or "none",
                "summary": "Not scored: {0}".format(reason),
            },
            recommendation=(
                "This check compares the server's HTML against a JavaScript-rendered copy. "
                "Install the browser used for rendering (pip install playwright && playwright "
                "install chromium) and re-scan to score it."
            ),
            checked=ctx.origin,
            duration_ms=ms_since(t),
        )

    # Averaged unrounded: result() rounds the score to a tenth, and rounding the per-page shares
    # or the score to a whole number first could move it by up to half a point.
    average_hidden = sum(shares) / len(shares)
    score = max(0.0, min(100.0, (1 - average_hidden) * 100))
    worst = sorted(rows, key=lambda r: r["js_only_share_pct"], reverse=True)[:10]
    rec = None
    if score < 90:
        rec = (
            "Server-render or pre-render the content these pages assemble in the browser: "
            "AI crawlers that do not execute JavaScript read only the shell."
        )
    evidence = {
        "compared_pages": len(rows),
        "crawled_pages": len(ctx.pages),
        "render_budget": render.get("budget"),
        "average_js_only_share_pct": round(average_hidden * 100, 1),
        # Unrounded, so the working rebuilds the exact score rather than one from rounded shares.
        "average_js_only_share": average_hidden,
        "summary": summary or (
            "{0} of {1} crawled pages were fetched a second time in a browser; on average "
            "{2}% of their text was absent from the server's own HTML."
        ).format(len(rows), len(ctx.pages), round(average_hidden * 100, 1)),
        "worst_pages": worst,
        "pages": rows,
    }
    if probe_url:
        evidence["probe_url"] = probe_url
    checked_links = [link(r["url"], _TECH_09_PROBE if probe_url and r["url"].rstrip("/") == probe_url.rstrip("/") else
                          "Page fetched a second time in a real browser; its rendered text was compared with the server's raw HTML to measure how much only appears after JavaScript runs.")
                     for r in rows]
    return result(
        spec,
        score=score,
        evidence=evidence,
        recommendation=rec,
        checked=(probe_url or ctx.origin) if decision == "not_needed" else ctx.origin,
        duration_ms=ms_since(t),
        confidence=confidence,
        links=checked_links,
    )


_TECH_09_PROBE = ("Homepage probe: fetched a second time in a real browser and compared with the server's raw HTML, "
                  "to see whether the site hides content behind JavaScript.")


async def tech_10(spec, ctx):
    t = timed()
    rows = []
    pages = scorable_pages(ctx)
    links = page_links(pages, "Crawled page: its number of H1 headings and the order of its H1-H2-H3 headings are scored.")
    if not any(page.headings for page in pages):
        return result(spec, score=0, evidence={"note": "No headings extracted"}, recommendation="Add a single H1 and logical H2/H3 structure on every template.", checked=ctx.origin, duration_ms=ms_since(t), links=links)
    for page in pages:
        # A readable page with no headings at all is a page with no H1 and is scored as one;
        # skipping it left only the pages that have headings to set the average.
        h1s = [h for h in page.headings if h["level"] == 1]
        levels = [h["level"] for h in page.headings]
        skips = sum(1 for a, b in zip(levels, levels[1:]) if b - a > 1)
        score = 100
        if len(h1s) != 1:
            score -= 40 if len(h1s) == 0 else 25
        score -= min(30, skips * 10)
        rows.append({"url": page.result.final_url, "h1_count": len(h1s), "heading_count": len(page.headings), "skipped_levels": skips, "score": max(0, score)})
    avg =sum(r["score"] for r in rows) / len(rows)
    rec = "Use exactly one H1 and avoid skipped heading levels." if avg < 90 else None
    return result(spec, score=avg, evidence={"pages": rows, "page_count": len(rows)}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), links=links)


def _as_count(value) -> int | None:
    """A model-supplied count as a non-negative int, or None when it is not a whole number."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number != int(number):
        return None
    return max(0, int(number))


async def tech_11(spec, ctx):
    t = timed()
    rows = []
    pages = scorable_pages(ctx)
    for page in pages:
        if page.word_count < _MIN_CONTENT_WORDS:
            continue
        heads = [h for h in page.headings if h["level"] <= 3]
        density = (len(heads) / max(1, page.word_count)) * 1000
        words_per = page.word_count / max(1, len(heads))
        score = 100
        if words_per > 220:
            score -= 30
        if density < 1.5:
            score -= 20
        if not heads:
            score = 30
        rows.append({"url": page.result.final_url, "headings": len(heads), "words": page.word_count, "words_per_heading": round(words_per, 1), "score": max(0, score)})
    if not rows:
        return no_data(spec, reason="no page has enough body text broken up by headings",
                              evidence={"pages_assessed": 0}, checked=ctx.origin, duration_ms=ms_since(t))
    avg = sum(r["score"] for r in rows) / len(rows)
    confidence = 0.7
    evidence = {"pages": rows, "method": "heuristic"}

    # Real headings with the word count of their own section. This previously sent each
    # page's URL as the "heading" and its mean words-per-heading as the "word count", so the
    # model was judging URLs against an average and its verdict overwrote the score.
    sections_payload = []
    for page in pages:
        if page.word_count < _MIN_CONTENT_WORDS:
            continue
        for block in heading_blocks(page):
            if block["heading"]:
                sections_payload.append({"heading": block["heading"][:160], "word_count": block["words"]})
            if len(sections_payload) >= 40:
                break
        if len(sections_payload) >= 40:
            break

    if sections_payload:
        llm_res = await judge(tech11_prompt(sections_payload), system=SYSTEM)
        # The denominator is the number of sections actually sent, not the model's own "total":
        # a model that answered 12 of 10 scored 120. A count that is missing or not a number
        # leaves the heuristic score in place.
        parsed = llm_res.parsed if llm_res.ok and isinstance(llm_res.parsed, dict) else {}
        liftable = _as_count(parsed.get("liftable_count"))
        if liftable is not None:
            total = len(sections_payload)
            liftable = min(liftable, total)
            avg = liftable / total * 100
            confidence = 0.8
            evidence["method"] = "llm"
            evidence["llm"] = {"liftable_count": liftable, "total": total, "model_total": parsed.get("total")}
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error
        else:
            evidence["llm_unusable"] = "the model's reply had no usable liftable_count"

    rec = "Break long sections with H2/H3 question-style headings so assistants can lift a passage." if avg < 90 else None
    links = url_links([r["url"] for r in rows], f"Crawled page with at least {_MIN_CONTENT_WORDS} words of body text: words per heading and heading density are scored, and its sections may be sent to the AI to judge whether a passage can be lifted on its own.")
    return result(spec, score=avg, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence, links=links)


async def tech_12(spec, ctx):
    t = timed()
    tables = lists = 0
    pages = scorable_pages(ctx)
    for page in pages:
        if not page.soup:
            continue
        # Menus, headers and footers are lists too, and they repeat on every page: counting them
        # gave 17,425 "lists" on a 64-page site and a pass no content earned. Content only.
        tables += sum(1 for el in page.soup.find_all("table") if not _in_chrome(el))
        lists += sum(1 for el in page.soup.find_all(["ul", "ol"]) if not _in_chrome(el))
    score = 100 if lists + tables >= 8 else (70 if lists + tables >= 3 else 40)
    rec = "Use semantic <ul>/<ol>/<table> markup instead of image-only comparisons." if score < 90 else None
    links = page_links([p for p in pages if p.soup], "Crawled page: its tables and bulleted / numbered lists outside the menu, header and footer are counted.")
    return result(spec, score=score, evidence={"tables": tables, "lists": lists, "pages": len(pages)}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), links=links)


# A text-to-code ratio is a percentage of bytes, not a score: a genuinely content-rich
# page sits around 20-25% and essentially never approaches 100. Feeding the raw percentage
# into a 0-100 score therefore capped even an excellent page at about a quarter of the
# marks available for it, so the ratio is banded against what is actually achievable.
_TEXT_TO_CODE_BANDS, _TEXT_TO_CODE_FLOOR = rules.band("text_to_code")


async def tech_13(spec, ctx):
    t = timed()
    rows = []
    for page in scorable_pages(ctx):
        # The text is extracted from the rendered DOM when the page was rendered, so it is measured
        # against that document. Against the raw shell it came to well over 100% on a JS site.
        rendered = getattr(page, "rendered_html", "") or ""
        html = rendered if rendered.strip() else (page.result.text or "")
        html_len = max(1, len(html))
        text_len = len(page.text)
        ratio = text_len / html_len * 100
        ratio_score = band(ratio, _TEXT_TO_CODE_BANDS, _TEXT_TO_CODE_FLOOR)
        rows.append({"url": page.result.final_url, "text_to_code": round(ratio, 2), "text_to_code_score": ratio_score, "words": page.word_count,
                     "html_source": "rendered" if rendered.strip() else "raw"})
    # Text-to-code ratio only: how much of what a crawler downloads is readable text. Content
    # depth is ON-10's measure; counting word totals here as well charged a thin page twice.
    avg = sum(r["text_to_code_score"] for r in rows) / len(rows) if rows else 0
    rec = "Increase indexable copy relative to markup, inline scripts and styles." if avg < 90 else None
    links = url_links([r["url"] for r in rows], "Crawled page: its readable text is measured against the size of its HTML (text-to-code ratio).")
    return result(spec, score=min(100, avg), evidence={"pages": rows}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), links=links)


_VIDEO_HOST = re.compile(r"^(?:https?:)?//(?:[\w-]+\.)*(?:youtube\.com|youtube-nocookie\.com|youtu\.be|vimeo\.com|"
                         r"wistia\.(?:com|net)|wi\.st|vidyard\.com|brightcove\.(?:net|com)|bcove\.video|loom\.com|"
                         r"dailymotion\.com|dai\.ly)(?:[:/?#]|$)", re.I)


async def tech_14(spec, ctx):
    t = timed()
    images = []
    videos = 0
    transcripts = 0
    links = page_links(scorable_pages(ctx), "Crawled page: every image's alt text is checked, and any embedded video is checked for a transcript.")
    for page in scorable_pages(ctx):
        # The page each image sits on is kept, so the report's fix-by-page list can say where to
        # add the alt text rather than handing over a bare image URL.
        images.extend({**image, "page": page.result.final_url} for image in page.images)
        if page.soup:
            # A <video>, or an iframe that embeds a video player. Every iframe used to count, so
            # Google Tag Manager's noscript frame and an embedded map were "videos without transcripts".
            videos += len(page.soup.find_all("video"))
            videos += sum(1 for frame in page.soup.find_all("iframe")
                          if _VIDEO_HOST.search(frame.get("src") or frame.get("data-src") or ""))
            # The visible copy, not the raw HTML: "transcript" in a script, class name or JSON
            # blob is not a transcript a reader or an assistant can use.
            if re.search(r"transcript", page.text or "", re.I):
                transcripts += 1
    if not images and not videos:
        return no_data(spec, reason="the crawled pages contain no images or videos",
                              evidence={"images": 0, "videos": 0}, checked=ctx.origin, duration_ms=ms_since(t), links=links)
    usable = sum(1 for i in images if i["alt"] and i["alt"].lower() not in {"image", "img", "logo"} and len(i["alt"]) > 3)
    img_score = usable / len(images) * 100 if images else None
    vid_score = min(100, transcripts / videos * 100) if videos else None
    # Only what exists is scored: images 70% and videos 30% when both are present, otherwise the
    # one that is present carries the whole score. A site with no video used to get a free 30.
    if img_score is not None and vid_score is not None:
        score = img_score * 0.7 + vid_score * 0.3
    else:
        score = img_score if img_score is not None else vid_score
    rec = (f"Add descriptive alt text; {len(images) - usable}/{len(images)} images are missing useful alt."
           if img_score is not None and img_score < 90 else
           ("Publish transcripts for videos." if vid_score is not None and vid_score < 90 else None))
    return result(spec, score=score, evidence={"images": len(images), "usable_alt": usable, "videos": videos, "transcript_signals": transcripts, "missing_alt": [i for i in images if not i["alt"]]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), links=links)


async def tech_15(spec, ctx):
    t = timed()
    found = 0
    rows = []
    for page in scorable_pages(ctx):
        has = bool(page.dates.get("published") or page.dates.get("modified"))
        if page.soup:
            for item in flatten_schema(page.schema_blocks):
                if item.get("datePublished") or item.get("dateModified"):
                    has = True
        rows.append({"url": page.result.final_url, "has_date": has, "dates": page.dates})
        found += int(has)
    # dates are more relevant on articles; home/service absence is partial not total fail
    score = 60 + (found / max(1, len(rows))) * 40
    rec = "Expose datePublished/dateModified in visible copy and JSON-LD on articles and resources." if score < 90 else None
    links = url_links([r["url"] for r in rows], "Crawled page: checked for a published or last-updated date, in the visible copy or in its structured data.")
    return result(spec, score=score, evidence={"pages_with_dates": found, "samples": rows}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), links=links)


def _organization_nodes(obj, out: list) -> list:
    """Every Organization node in the JSON-LD, including nested ones such as a Service's
    `provider` or an Article's `publisher` -- flatten_schema() only follows @graph."""
    if isinstance(obj, dict):
        if "Organization" in str(obj.get("@type")) or "Corporation" in str(obj.get("@type")):
            out.append(obj)
        for value in obj.values():
            _organization_nodes(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _organization_nodes(value, out)
    return out


def _homepage_organization(blocks: list) -> dict | None:
    """The site's own Organization, merged across every homepage node that describes it.

    A site often states itself in pieces -- a Yoast @graph node with name, logo and sameAs,
    and a Service `provider` repeating the name with url and contactPoint. Reading only the
    first node scored the fields the site does publish as missing. Nodes are the same
    organization when they share the first node's @id or its name.
    """
    nodes: list = []
    for block in blocks:
        if block.get("ok"):
            _organization_nodes(block.get("data"), nodes)
    if not nodes:
        return None
    first = nodes[0]
    ident = first.get("@id")
    name = str(first.get("name") or "").strip().lower()
    merged: dict = {}
    for node in nodes:
        same = (ident and node.get("@id") == ident) or (name and str(node.get("name") or "").strip().lower() == name)
        if node is first or same:
            for key, value in node.items():
                if value and not merged.get(key):
                    merged[key] = value
    return merged


async def tech_16(spec, ctx):
    t = timed()
    home = ctx.homepage
    org = _homepage_organization(home.schema_blocks) if home else None
    expected = ["name", "url", "logo", "sameAs"]
    present = [f for f in expected if org and org.get(f)]
    extra = []
    if org:
        if org.get("address") or org.get("location"):
            extra.append("address")
        if org.get("contactPoint") or org.get("email") or org.get("telephone"):
            extra.append("contact")
    score = (len(present) / len(expected)) * 80 + (20 if extra else 0)
    if not org:
        score = 0
    rec = "Add Organization JSON-LD with name, url, logo, address/contact and sameAs profiles." if score < 90 else None
    links = page_links([home] if home else [], "The homepage: its Organization structured data is checked for name, url, logo, sameAs profiles, address and contact details.")
    return result(spec, score=score, evidence={"organization": bool(org), "fields": present + extra, "types": schema_types(home.schema_blocks) if home else []}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), links=links)


# Schema.org types that genuinely satisfy each page type, including the subtypes a CMS
# actually emits. Matching previously also accepted FAQPage for *any* expected type, so a
# blog post carrying only FAQPage counted as correctly marked-up Article markup.
# Below this many items a pass is reported, but flagged as resting on very little evidence.
_SMALL_SAMPLE = rules.threshold("small_sample_items")

#
# Types are matched by exact name. Substring matching let "offer" accept OfferCatalog and
# "product" accept ProductCollection, so the genuine subtypes that substring matching used to
# catch are listed here instead.
_ARTICLE_TYPES = ("article", "blogposting", "newsarticle", "techarticle", "report", "scholarlyarticle",
                  "analysisnewsarticle", "opinionnewsarticle", "reportagenewsarticle")
_ORGANIZATION_TYPES = ("organization", "corporation", "localbusiness", "ngo", "educationalorganization",
                       "medicalorganization", "governmentorganization", "newsmediaorganization",
                       "researchorganization", "onlinebusiness")
_PAGE_TYPE_SCHEMA = {
    "article": _ARTICLE_TYPES,
    # Exact names, so the genuine schema.org subtypes are listed: matching "service" or "offer" as
    # substrings also accepted OfferCatalog and ProductCollection, which say nothing about a page.
    "service": ("service", "product", "offer", "professionalservice", "financialservice", "legalservice",
                "governmentservice", "productmodel", "productgroup", "individualproduct", "someproducts",
                "aggregateoffer"),
    "about": _ORGANIZATION_TYPES + ("aboutpage",),
    "home": _ORGANIZATION_TYPES + ("website",),
    "case_study": ("article", "casestudy", "creativework") + _ARTICLE_TYPES[1:],
}


def _type_name(schema_type: str) -> str:
    """The bare, lower-case type name: a full schema.org IRI and "schema:Product" are both "product"."""
    return re.split(r"[/:#]", schema_type.strip())[-1].lower()


async def tech_17(spec, ctx):
    t = timed()
    rows = []
    expected_types = {**_PAGE_TYPE_SCHEMA, **{k: tuple(v) for k, v in industry.page_schema(ctx).items()}}
    for page in scorable_pages(ctx):
        types = schema_types(page.schema_blocks)
        accepted = expected_types.get(page.page_type)
        expected = accepted[0].title() if accepted else None
        hit = bool(accepted) and any(_type_name(ty) in accepted for ty in types)
        if expected is None:
            rows.append({"url": page.result.final_url, "page_type": page.page_type, "types": types, "applicable": False})
        else:
            rows.append({"url": page.result.final_url, "page_type": page.page_type, "expected": expected, "types": types, "match": bool(hit)})
    applicable = [r for r in rows if r.get("applicable", True) and "match" in r]
    links = [link(r["url"], f"Crawled {r['page_type'].replace('_', ' ')} page: checked for the structured-data type that page type needs ({r['expected']}).")
             for r in applicable]
    if not applicable:
        return no_data(spec, reason="no crawled page carries the structured data its page type needs (home, service or product, article, about, case study)",
                              evidence={"pages": rows}, checked=ctx.origin, duration_ms=ms_since(t), links=links)
    score = sum(1 for r in applicable if r["match"]) / len(applicable) * 100
    missing = sorted({r["expected"] for r in applicable if not r["match"]})
    rec = (f"Add page-type schema that matches each template ({', '.join(missing)})." if score < 90 else None)
    return result(spec, score=score, evidence={"pages": rows, "industry_profile": industry.for_ctx(ctx)["label"]},
                  recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), links=links)


def _collect_author_signals(pages) -> list[dict]:
    """The structured author markup behind TECH-18: Person nodes, and the people named in `author`.

    Markup only. A visible byline (an element styled .author or .byline) is not markup a machine can
    read as a person, and it used to count here: AAOS scored 80 on 166 such elements while ON-12 found
    that none of them named a real person. Visible bylines are ON-12's to judge. Organization authors
    (a publisher) do not count either.

    Run off the event loop via asyncio.to_thread(): on a large site (1000+ pages) the walk alone took
    74-101s with no `await` inside it.
    """
    return [{"url": page.result.final_url, "item": person}
            for page in pages for person in structured_authors(page.schema_blocks)]


async def tech_18(spec, ctx):
    t = timed()
    authors = await asyncio.to_thread(_collect_author_signals, ctx.pages)
    links = page_links(ctx.pages, "Crawled page: its structured data is searched for an author marked up as a Person (with a job title for full marks).")
    if not authors:
        return no_data(spec, reason="no page marks up an author as a Person in its structured data",
                       evidence={"author_signals": []}, checked=ctx.origin, duration_ms=ms_since(t), links=links)
    score = 95 if any("jobTitle" in (a.get("item") or {}) for a in authors) else 80
    rec = "Publish author/Person markup with role, credentials and a reachable profile URL." if score < 90 else None
    return result(spec, score=score, evidence={"author_signals": authors}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.65, links=links)


_RAW_TYPE = re.compile(r'"@type"\s*:\s*"([^"]+)"')


def _why_broken(error: str) -> str:
    """The parser's complaint about a JSON-LD block, in plain words."""
    e = (error or "").lower()
    if "control character" in e:
        return "a raw line break or tab inside a text value"
    if "expecting" in e or "delimiter" in e:
        return "a missing comma, quote or bracket"
    if "extra data" in e:
        return "more than one JSON object in one script tag"
    return "it is not valid JSON"


async def tech_19(spec, ctx):
    """Does the structured data the site has parse? Every block is named by its @type, broken
    ones included (read from their raw text), so the report says exactly what was validated --
    "1 of 4 blocks valid" next to TECH-16/17/18 at 0 read as a riddle until it said all four
    were VideoObject embeds. Whether the site has the markup it should is TECH-16/17/18's job."""
    t = timed()
    total = valid = 0
    mismatches = []
    validated = []   # every block, with its type, so the report says what markup this check read
    for page in ctx.pages:
        for block in page.schema_blocks:
            total += 1
            if block.get("ok"):
                types = sorted(set(schema_types([block])))
                validated.append({"url": page.result.final_url, "valid": True, "types": types})
                valid += 1
            else:
                types = sorted(set(_RAW_TYPE.findall(block.get("raw_preview") or "")))
                why = _why_broken(block.get("error"))
                validated.append({"url": page.result.final_url, "valid": False, "types": types,
                                  "error": block.get("error"), "why": why})
                mismatches.append({"url": page.result.final_url, "error": block.get("error"), "why": why})
    if not total:
        # Nothing to validate: N/A, not a third penalty for the absence TECH-16/17/18 already score.
        return not_applicable(spec, reason="the site has no JSON-LD structured data, so there is nothing to validate "
                                           "(whether it should have some is scored by TECH-16, TECH-17 and TECH-18)",
                              evidence={"blocks": 0}, checked=ctx.origin, duration_ms=ms_since(t))
    score = valid / total * 100
    links = url_links([b["url"] for b in validated], "Page carrying JSON-LD structured data: every block on it is parsed to see whether it is valid.")
    all_types = sorted({t for b in validated for t in b["types"]})
    broken = [b for b in validated if not b["valid"]]
    rec = None
    if score < 90:
        kinds = sorted({t for b in broken for t in b["types"]}) or ["JSON-LD"]
        reasons = sorted({b["why"] for b in broken})
        broken_on = list(dict.fromkeys(b["url"] for b in broken))
        # The recommendation names up to five pages to keep it readable; every block is scored and
        # every broken one is listed in the evidence.
        rec = (f"Fix the {len(broken)} broken {'/'.join(kinds)} block{'s' if len(broken) != 1 else ''} "
               f"({'; '.join(reasons)}) on: {', '.join(broken_on[:5])}{' and others' if len(broken_on) > 5 else ''}.")
    evidence = {"blocks": total, "valid": valid, "errors": mismatches, "validated_blocks": validated,
                "types_found": all_types,
                "markup_validated": (f"{total} JSON-LD block{'s' if total != 1 else ''} on "
                                     f"{len({b['url'] for b in validated})} page{'s' if len({b['url'] for b in validated}) != 1 else ''}: "
                                     + (", ".join(all_types) if all_types else "type unreadable")
                                     + f". {valid} valid, {total - valid} broken.")}
    confidence = 0.85
    if total < _SMALL_SAMPLE:
        # Valid, but there is almost nothing: 1 block passing is not "the site's markup is clean".
        evidence["small_sample"] = (f"Only {total} JSON-LD block{'s' if total != 1 else ''} on the whole site, so this "
                                    "result describes very little markup. How much markup the site has is scored by TECH-16 and TECH-17.")
        confidence = 0.4
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence, links=links)


async def tech_20(spec, ctx):
    t = timed()
    rows = []
    for page in scorable_pages(ctx):
        final = page.result.final_url
        canonical = page.canonical
        ok = True
        notes = []
        if canonical:
            # Only the homepage itself is exempt. A canonical pointing at "/" used to be exempt
            # as well, so every page canonicalised to the homepage -- the classic template bug
            # that de-indexes a whole site -- passed.
            target = urlparse(canonical)
            if target.netloc and urlparse(final).path.rstrip("/") != target.path.rstrip("/") and page.page_type != "home":
                notes.append("canonical differs from final URL")
                ok = False
        else:
            notes.append("missing canonical")
            ok = False
        if page.result.hops >= 3:
            notes.append(f"{page.result.hops} redirect hops")
            ok = False
        rows.append({"url": final, "canonical": canonical, "hops": page.result.hops, "ok": ok, "notes": notes})
    score = sum(1 for r in rows if r["ok"]) / max(1, len(rows)) * 100
    rec = "Set one canonical URL per page and collapse redirect chains." if score < 90 else None
    links = url_links([r["url"] for r in rows], "Crawled page: its canonical tag is compared with the URL it was read from, and its redirect hops are counted.")
    return result(spec, score=score, evidence={"pages": rows}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), links=links)


_MIN_PAGES_PER_LOCALE = 3
_MIN_SHARED_PATHS = 3   # translations share many page paths; unrelated sections rarely do


def _market_urls(ctx) -> list[str]:
    """Every URL _markets() reads for locale prefixes: the crawled pages, their links and the sitemap."""
    urls = [p.result.final_url for p in ctx.pages if getattr(p, "result", None)]
    urls += [item.get("href") if isinstance(item, dict) else item
             for p in ctx.pages for item in (getattr(p, "links", None) or []) if item]
    urls += list(((getattr(ctx, "sitemap", None) or {}).get("urls") or []))
    return urls


def _markets(ctx, tags=()) -> dict:
    """The markets a site visibly serves, from evidence that cannot be an ordinary site section.

    A site serves several markets when any of these holds:
      * its pages declare two or more languages in <html lang>;
      * two or more country-style locale prefixes ("/en-us/", "/de-de/") each open at least three URLs;
      * the same page paths (at least three) exist under two short language prefixes ("/en/about", "/fr/about").
    A bare two-letter first segment on its own proves nothing: AAOS has /dc/, /go/, /id/ and /us/
    sections, which an earlier version of this test read as six markets on an English-only site.
    URLs come from the crawled pages, their links and the sitemap.
    """
    urls = _market_urls(ctx)
    pages_per_locale: dict[str, int] = {}
    rest_by_locale: dict[str, set] = {}
    for u in {u for u in urls if isinstance(u, str)}:
        loc = locale_of(u)
        if not loc:
            continue
        pages_per_locale[loc] = pages_per_locale.get(loc, 0) + 1
        rest = "/".join(urlparse(u).path.strip("/").split("/")[1:])
        if rest:
            rest_by_locale.setdefault(loc, set()).add(rest)
    regional = sorted(loc for loc, n in pages_per_locale.items() if "-" in loc and n >= _MIN_PAGES_PER_LOCALE)
    bare = [loc for loc in rest_by_locale if "-" not in loc]
    pairs = [(a, b) for a in bare for b in bare if a < b and len(rest_by_locale[a] & rest_by_locale[b]) >= _MIN_SHARED_PATHS]
    translated = sorted({loc for pair in pairs for loc in pair})
    # A regional prefix names a language as well as a country: /en-us/ and /es-us/ are one
    # country in two languages, which counted as a single market when only countries were read.
    languages = {loc.split("-")[0] for loc in translated} | {loc.split("-")[0] for loc in regional}
    countries = {loc.split("-")[1] for loc in regional}
    codes = []
    for page in ctx.pages:
        html = page.soup.find("html") if getattr(page, "soup", None) else None
        codes.append(str((html.get("lang") if html else "") or ""))
    # The site's own hreflang tags are evidence too: tags pointing at two countries prove two markets.
    codes += [str(t.get("lang") or "") for t in tags]
    for code in codes:
        code = code.strip().lower().replace("_", "-")
        if not code or code == "x-default":
            continue
        lang, _, region = code.partition("-")
        if lang:
            languages.add(lang)
        if len(region) == 2:
            countries.add(region)
    locales = sorted(set(regional) | set(translated))
    return {"locales": locales[:20], "languages": sorted(languages), "countries": sorted(countries),
            "several_markets": len(countries) >= 2 or len(languages) >= 2}


async def tech_21(spec, ctx):
    t = timed()
    tags = []
    for page in ctx.pages:
        tags.extend({"url": page.result.final_url, **h} for h in page.hreflang)
    markets = _markets(ctx, tags)
    links = (page_links(ctx.pages, "Crawled page: read for hreflang tags and its <html lang> language code.")
             + url_links(_market_urls(ctx), "URL found on the site or in its sitemap: its path is read for a country or language prefix (such as /en-us/ or /fr/) to tell how many markets the site serves."))
    if not markets["several_markets"]:
        # hreflang links the country and language versions of a page. A site serving one country in
        # one language has none to link, so TECH-21 does not apply to it -- with or without a few
        # hreflang tags (a lone "en-us" plus "x-default" links the page to itself).
        where = f" ({', '.join(c.upper() for c in markets['countries'])})" if markets["countries"] else ""
        own = (f"; its {len(tags)} hreflang tag{'s' if len(tags) != 1 else ''} all point to that one market"
               if tags else "")
        return not_applicable(spec, reason=f"the site serves one country{where} in one language{own}, so there are no "
                                           "country or language versions for hreflang to link",
                              evidence={"hreflang": tags, **markets}, checked=ctx.origin, duration_ms=ms_since(t), links=links)
    if not tags:
        # Several countries or languages and no hreflang: a real gap.
        found = ", ".join(markets["locales"] or [c.upper() for c in markets["countries"]] or markets["languages"])
        return no_data(spec, reason=f"the site serves several countries or languages ({found}) but has no hreflang annotations",
                       evidence={"hreflang": [], **markets}, checked=ctx.origin, duration_ms=ms_since(t), links=links)
    # A multi-country site with hreflang: the share of tags that are complete (a language and a link).
    valid = sum(1 for h in tags if h.get("lang") and h.get("href"))
    score = valid / len(tags) * 100
    rec = "Make hreflang reciprocal and use valid locale codes." if score < 90 else None
    return result(spec, score=score, evidence={"tags": tags, **markets}, recommendation=rec, checked=ctx.origin,
                  duration_ms=ms_since(t), links=links)


async def tech_22(spec, ctx):
    t = timed()
    titles = []
    metas = []
    rows = []
    for page in scorable_pages(ctx):
        titles.append(page.title.strip().lower())
        metas.append(page.meta_description.strip().lower())
        t_ok = 20 <= len(page.title) <= 70
        d_ok = 70 <= len(page.meta_description) <= 170
        rows.append({"url": page.result.final_url, "title": page.title, "title_len": len(page.title), "description": page.meta_description[:180], "desc_len": len(page.meta_description), "title_ok": t_ok and bool(page.title), "desc_ok": d_ok})
    # The exact counts go into the evidence: the rows keep only the first 180 characters of a
    # description, so two that differ after that read as duplicates to anyone recounting them.
    counts = {
        "titles": {"unique": len({x for x in titles if x}), "nonempty": len([x for x in titles if x])},
        "descriptions": {"unique": len({x for x in metas if x}), "nonempty": len([x for x in metas if x])},
    }
    uniq_t = counts["titles"]["unique"] / max(1, counts["titles"]["nonempty"])
    uniq_d = counts["descriptions"]["unique"] / max(1, counts["descriptions"]["nonempty"])
    presence = sum(1 for r in rows if r["title"] and r["desc_len"]) / max(1, len(rows))
    length_q = sum(1 for r in rows if r["title_ok"] and r["desc_ok"]) / max(1, len(rows))
    score = presence * 50 + uniq_t * 15 + uniq_d * 10 + length_q * 25
    rec = "Write unique titles and meta descriptions in recommended length ranges." if score < 90 else None
    links = url_links([r["url"] for r in rows], "Crawled page: its title and meta description are checked for presence, length and uniqueness across the site.")
    return result(spec, score=score, evidence={"pages": rows, "unique_title_ratio": round(uniq_t, 2), "uniqueness_counts": counts},
                  recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), links=links)


HANDLERS = {
    "TECH-01": tech_01,
    "TECH-02": tech_02,
    "TECH-03": tech_03,
    "TECH-04": tech_04,
    "TECH-05": tech_05,
    "TECH-06": tech_06,
    "TECH-07": tech_07,
    "TECH-08": tech_08,
    "TECH-09": tech_09,
    "TECH-10": tech_10,
    "TECH-11": tech_11,
    "TECH-12": tech_12,
    "TECH-13": tech_13,
    "TECH-14": tech_14,
    "TECH-15": tech_15,
    "TECH-16": tech_16,
    "TECH-17": tech_17,
    "TECH-18": tech_18,
    "TECH-19": tech_19,
    "TECH-20": tech_20,
    "TECH-21": tech_21,
    "TECH-22": tech_22,
}

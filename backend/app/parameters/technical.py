from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from ..config import AI_CRAWLER_UAS, BROWSER_UA
from ..crawler.discover import sample_internal_links
from ..crawler.http import fetch
from ..crawler.robots import AI_BOTS, bot_decision
from ..llm.client import judge
from ..llm.prompts import SYSTEM, tech01_review_prompt, tech11_prompt
from .common import flatten_schema, ms_since, result, schema_types, timed


async def tech_01(spec, ctx):
    t = timed()
    robots = ctx.robots
    decisions = [bot_decision(robots, bot) for bot in AI_BOTS]
    if not robots.result.ok:
        return result(
            spec, score=None, unknown=True,
            evidence={"url": robots.url, "http_status": robots.result.status_code, "error": robots.result.error},
            recommendation="Publish a reachable robots.txt that explicitly allows desired AI crawlers.",
            checked=robots.url, error=robots.result.error or f"HTTP {robots.result.status_code}",
            duration_ms=ms_since(t),
        )
    allowed = sum(1 for d in decisions if d.get("decision") == "ALLOWED")
    score = allowed / len(decisions) * 100
    rec = None
    blocked = [d["name"] for d in decisions if d.get("decision") == "BLOCKED"]
    if blocked:
        rec = f"Allow {', '.join(blocked)} for public content in robots.txt."

    # Score stays fully deterministic -- robots.txt allow/block is unambiguous. The LLM
    # only adds a plain-English review/explanation layer here, never a scoring override.
    evidence = {"url": robots.url, "http_status": robots.result.status_code, "crawlers": decisions, "raw_preview": robots.raw[:1200], "method": "deterministic"}
    llm_res = await judge(tech01_review_prompt(robots.raw, decisions), system=SYSTEM)
    if llm_res.ok and llm_res.parsed:
        evidence["llm_review"] = llm_res.parsed
    elif not llm_res.ok:
        evidence["llm_unavailable"] = llm_res.error

    return result(
        spec, score=score,
        evidence=evidence,
        recommendation=rec, checked=robots.url, duration_ms=ms_since(t), confidence=0.95,
    )


async def tech_02(spec, ctx):
    t = timed()
    urls = [ctx.normalized_url]
    for u in ctx.sitemap.get("urls", [])[:3]:
        if u not in urls:
            urls.append(u)
    rows = []
    for url in urls[:4]:
        browser = await fetch(url, user_agent=BROWSER_UA)
        bot = await fetch(url, user_agent=AI_CRAWLER_UAS["GPTBot"])
        rows.append({
            "url": url,
            "browser_status": browser.status_code,
            "bot_status": bot.status_code,
            "bot_error": bot.error,
            "challenge": _looks_blocked(bot),
        })
    if not rows:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="Could not request test URLs.", checked=ctx.origin, error="no URLs", duration_ms=ms_since(t))
    ok = sum(1 for r in rows if r["bot_status"] and 200 <= r["bot_status"] < 400 and not r["challenge"])
    score = ok / len(rows) * 100
    rec = "Review WAF/CDN rules that treat AI crawler user-agents differently from browsers." if score < 90 else None
    return result(spec, score=score, evidence={"tests": rows}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.9)


def _looks_blocked(res) -> bool:
    if res.status_code in (401, 403, 429, 503):
        return True
    text = (res.text or "")[:1500].lower()
    return any(k in text for k in ("captcha", "access denied", "cf-challenge", "attention required", "bot detection"))


async def tech_03(spec, ctx):
    t = timed()
    res = ctx.llms
    url = res.url
    if res.error and res.status_code is None:
        return result(spec, score=None, unknown=True, evidence={"url": url, "error": res.error}, recommendation="Retry llms.txt fetch.", checked=url, error=res.error, duration_ms=ms_since(t))
    if res.status_code in (404, 410) or not res.ok:
        return result(spec, score=0, evidence={"url": url, "http_status": res.status_code}, recommendation="Publish /llms.txt with company summary and priority public URLs.", checked=url, duration_ms=ms_since(t))
    text = res.text.strip()
    links = re.findall(r"https?://[^\s)]+", text)
    score = 40
    if len(text) > 80:
        score += 20
    if links:
        score += 20
    if any(k in text.lower() for k in ("service", "about", "contact", "evoke", "#")):
        score += 20
    rec = "Expand llms.txt with current service descriptions and canonical links." if score < 90 else None
    return result(spec, score=score, evidence={"url": url, "http_status": res.status_code, "bytes": len(res.content), "link_count": len(links), "preview": text[:800]}, recommendation=rec, checked=url, duration_ms=ms_since(t))


async def tech_04(spec, ctx):
    t = timed()
    markers = []
    tested = 0
    blocked = 0
    for page in ctx.pages[:8]:
        if not page.soup:
            continue
        tested += 1
        html = page.result.text.lower()
        hits = [k for k in ("id=\"login\"", "cookie-consent", "cookie_banner", "paywall", "newsletter-popup", "modal-overlay") if k in html]
        overlay = page.soup.select(".modal, .popup, [class*=cookie], [id*=cookie], [class*=overlay]")
        content_len = len(page.text)
        if content_len < 80 and overlay:
            blocked += 1
            markers.append({"url": page.result.final_url, "blocked": True, "hits": hits})
        else:
            markers.append({"url": page.result.final_url, "blocked": False, "overlay_candidates": len(overlay), "content_chars": content_len})
    if not tested:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="Could not parse HTML for overlay checks.", checked=ctx.origin, error="no HTML", duration_ms=ms_since(t))
    score = (tested - blocked) / tested * 100
    rec = "Ensure cookie/login overlays do not hide primary content in the raw HTML." if blocked else None
    return result(spec, score=score, evidence={"tested": tested, "blocked": blocked, "samples": markers[:8]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def tech_05(spec, ctx):
    t = timed()
    sm = ctx.sitemap
    if not sm.get("urls"):
        return result(spec, score=0, evidence=sm, recommendation="Add a valid XML sitemap and reference it from robots.txt.", checked=ctx.origin + "/sitemap.xml", duration_ms=ms_since(t))
    sample = sm["urls"][:12]
    checks = []
    for url in sample:
        res = await fetch(url)
        checks.append({"url": url, "status": res.status_code, "ok": bool(res.ok)})
    valid = sum(1 for c in checks if c["ok"])
    coverage = min(100, sm["count"] / 20 * 40 + 40)
    sample_score = valid / max(1, len(checks)) * 100
    score = sample_score * 0.6 + coverage * 0.4
    rec = "Fix unreachable sitemap URLs and keep the sitemap aligned with canonical pages." if score < 90 else None
    return result(spec, score=score, evidence={"sitemap_count": sm["count"], "sources": sm["sources"], "sample": checks, "errors": sm.get("errors", [])[:6]}, recommendation=rec, checked=sm["sources"][0]["url"] if sm.get("sources") else ctx.origin, duration_ms=ms_since(t))


async def tech_06(spec, ctx):
    t = timed()
    urls = sample_internal_links(ctx, 36)
    if not urls:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="No internal links discovered.", checked=ctx.origin, error="no internal links", duration_ms=ms_since(t))
    sem = asyncio.Semaphore(6)

    async def check(url):
        async with sem:
            res = await fetch(url)
            return {"url": url, "status": res.status_code, "hops": res.hops, "error": res.error, "broken": (res.status_code or 0) >= 400 or bool(res.error)}

    rows = await asyncio.gather(*(check(u) for u in urls))
    working = sum(1 for r in rows if not r["broken"])
    score = working / len(rows) * 100
    broken = [r for r in rows if r["broken"]]
    rec = f"Repair {len(broken)} broken internal URLs." if broken else None
    return result(spec, score=score, evidence={"checked": len(rows), "broken": broken[:15], "working": working}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), pass_at=98, partial_at=90)


async def tech_07(spec, ctx):
    t = timed()
    pages = ctx.pages[:8]
    if not pages:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="No pages fetched for performance.", checked=ctx.origin, error="no pages", duration_ms=ms_since(t))
    psi = await _pagespeed(ctx.normalized_url)
    timings = [{"url": p.result.final_url, "ttfb_ms": p.result.elapsed_ms, "bytes": len(p.result.content)} for p in pages]
    avg_ttfb = sum(x["ttfb_ms"] for x in timings) / len(timings)
    lab = max(0, min(100, 100 - (avg_ttfb - 400) / 20))
    if psi.get("available"):
        score = psi.get("performance", lab)
        evidence = {"pagespeed": psi, "lab_timings": timings}
        confidence = 0.8
    else:
        score = lab * 0.7
        evidence = {"pagespeed": psi, "lab_timings": timings, "note": "PageSpeed unavailable; score uses lab TTFB fallback and is capped."}
        confidence = 0.45
        if avg_ttfb > 2500:
            score = min(score, 40)
    rec = "Improve LCP/INP/CLS: compress hero media, reduce JS, and cache HTML/assets." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.normalized_url, duration_ms=ms_since(t), confidence=confidence)


async def _pagespeed(url: str) -> dict:
    api = f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url={url}&strategy=mobile&category=performance"
    res = await fetch(api, user_agent=BROWSER_UA)
    if not res.ok:
        return {"available": False, "reason": res.error or f"HTTP {res.status_code}"}
    try:
        import json
        data = json.loads(res.text)
        lh = data.get("lighthouseResult", {})
        audits = lh.get("audits", {})
        cats = lh.get("categories", {})
        perf = ((cats.get("performance") or {}).get("score") or 0) * 100
        def metric(key, field="numericValue"):
            a = audits.get(key) or {}
            return a.get(field)
        return {
            "available": True,
            "performance": round(perf, 1),
            "lcp_ms": metric("largest-contentful-paint"),
            "cls": metric("cumulative-layout-shift"),
            "inp_ms": metric("interaction-to-next-paint") or metric("total-blocking-time"),
            "source": "Google PageSpeed Insights API",
        }
    except Exception as exc:
        return {"available": False, "reason": str(exc)}


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
        overflow = "min-width:" in (home.result.text or "") and "1200px" in (home.result.text or "")
    score = (50 if https else 0) + (50 if viewport else 0)
    if overflow:
        score = min(score, 80)
    rec = "Serve HTTPS and include a mobile viewport meta tag." if score < 90 else None
    return result(spec, score=score, evidence={"final_url": home.result.final_url, "https": https, "viewport": viewport}, recommendation=rec, checked=home.result.final_url, duration_ms=ms_since(t))


async def tech_09(spec, ctx):
    t = timed()
    rows = []
    for page in ctx.pages[:10]:
        raw = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", page.result.text or "", flags=re.I)
        raw_text = re.sub(r"<[^>]+>", " ", raw)
        raw_words = len(re.findall(r"\w+", raw_text))
        rendered = page.word_count
        ratio = (raw_words / rendered * 100) if rendered else 0
        rows.append({"url": page.result.final_url, "raw_words": raw_words, "rendered_words": rendered, "ratio": round(ratio, 1)})
    if not rows:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="No HTML to compare.", checked=ctx.origin, error="no HTML", duration_ms=ms_since(t))
    avg = sum(r["ratio"] for r in rows) / len(rows)
    rec = "Ensure primary copy is present in raw HTML, not injected only after JavaScript." if avg < 70 else None
    return result(spec, score=min(100, avg), evidence={"pages": rows, "average_ratio": round(avg, 1)}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def tech_10(spec, ctx):
    t = timed()
    rows = []
    for page in ctx.pages:
        if not page.headings:
            continue
        h1s = [h for h in page.headings if h["level"] == 1]
        levels = [h["level"] for h in page.headings]
        skips = sum(1 for a, b in zip(levels, levels[1:]) if b - a > 1)
        score = 100
        if len(h1s) != 1:
            score -= 40 if len(h1s) == 0 else 25
        score -= min(30, skips * 10)
        rows.append({"url": page.result.final_url, "h1_count": len(h1s), "heading_count": len(page.headings), "skipped_levels": skips, "score": max(0, score)})
    if not rows:
        return result(spec, score=0, evidence={"note": "No headings extracted"}, recommendation="Add a single H1 and logical H2/H3 structure on every template.", checked=ctx.origin, duration_ms=ms_since(t))
    avg = sum(r["score"] for r in rows) / len(rows)
    rec = "Use exactly one H1 and avoid skipped heading levels." if avg < 90 else None
    return result(spec, score=avg, evidence={"pages": rows[:15], "page_count": len(rows)}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def tech_11(spec, ctx):
    t = timed()
    rows = []
    for page in ctx.pages:
        if page.word_count < 40:
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
    avg = sum(r["score"] for r in rows) / len(rows) if rows else 40
    confidence = 0.7
    evidence = {"pages": rows[:12], "method": "heuristic"}

    if rows:
        sections_payload = [{"heading": r["url"], "word_count": r["words_per_heading"]} for r in rows[:20]]
        llm_res = await judge(tech11_prompt(sections_payload), system=SYSTEM)
        if llm_res.ok and llm_res.parsed and llm_res.parsed.get("total"):
            liftable = llm_res.parsed.get("liftable_count", 0)
            total = llm_res.parsed["total"]
            avg = liftable / total * 100
            confidence = 0.8
            evidence["method"] = "llm"
            evidence["llm"] = {"liftable_count": liftable, "total": total}
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Break long sections with H2/H3 question-style headings so assistants can lift a passage." if avg < 90 else None
    return result(spec, score=avg, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def tech_12(spec, ctx):
    t = timed()
    tables = lists = 0
    for page in ctx.pages:
        if not page.soup:
            continue
        tables += len(page.soup.find_all("table"))
        lists += len(page.soup.find_all(["ul", "ol"]))
    score = 100 if lists + tables >= 8 else (70 if lists + tables >= 3 else 40)
    rec = "Use semantic <ul>/<ol>/<table> markup instead of image-only comparisons." if score < 90 else None
    return result(spec, score=score, evidence={"tables": tables, "lists": lists, "pages": len(ctx.pages)}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def tech_13(spec, ctx):
    t = timed()
    rows = []
    for page in ctx.pages:
        html_len = max(1, len(page.result.text or ""))
        text_len = len(page.text)
        ratio = text_len / html_len * 100
        depth = 100 if page.word_count >= 400 else page.word_count / 400 * 100
        rows.append({"url": page.result.final_url, "text_to_code": round(ratio, 2), "words": page.word_count, "combined": round(ratio * 0.4 + depth * 0.6, 1)})
    avg = sum(r["combined"] for r in rows) / len(rows) if rows else 0
    rec = "Increase indexable copy relative to chrome/JS and deepen service-page content." if avg < 90 else None
    return result(spec, score=min(100, avg), evidence={"pages": rows[:12]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def tech_14(spec, ctx):
    t = timed()
    images = []
    videos = 0
    transcripts = 0
    for page in ctx.pages:
        images.extend(page.images)
        if page.soup:
            videos += len(page.soup.find_all(["video", "iframe"]))
            if re.search(r"transcript", page.result.text or "", re.I):
                transcripts += 1
    if not images:
        return result(spec, score=80, evidence={"images": 0, "videos": videos}, recommendation="Add meaningful alt text as images are published.", checked=ctx.origin, duration_ms=ms_since(t))
    usable = sum(1 for i in images if i["alt"] and i["alt"].lower() not in {"image", "img", "logo"} and len(i["alt"]) > 3)
    img_score = usable / len(images) * 100
    vid_score = 100 if videos == 0 else (transcripts / videos * 100)
    score = img_score * 0.7 + min(100, vid_score) * 0.3
    rec = f"Add descriptive alt text; {len(images) - usable}/{len(images)} images are missing useful alt." if img_score < 90 else None
    return result(spec, score=score, evidence={"images": len(images), "usable_alt": usable, "videos": videos, "transcript_signals": transcripts, "sample_missing": [i for i in images if not i["alt"]][:8]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def tech_15(spec, ctx):
    t = timed()
    found = 0
    rows = []
    for page in ctx.pages:
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
    return result(spec, score=score, evidence={"pages_with_dates": found, "samples": rows[:12]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def tech_16(spec, ctx):
    t = timed()
    home = ctx.homepage
    items = flatten_schema(home.schema_blocks) if home else []
    org = next((i for i in items if "Organization" in str(i.get("@type"))), None)
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
    return result(spec, score=score, evidence={"organization": bool(org), "fields": present + extra, "types": schema_types(home.schema_blocks) if home else []}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def tech_17(spec, ctx):
    t = timed()
    rows = []
    for page in ctx.pages:
        types = schema_types(page.schema_blocks)
        expected = None
        if page.page_type == "article":
            expected = "Article"
        elif page.page_type == "service":
            expected = "Service"
        elif page.page_type == "about":
            expected = "Organization"
        elif page.page_type == "home":
            expected = "Organization"
        hit = expected and any(expected.lower() in t.lower() or "faqpage" in t.lower() for t in types)
        if expected is None:
            rows.append({"url": page.result.final_url, "page_type": page.page_type, "types": types, "applicable": False})
        else:
            rows.append({"url": page.result.final_url, "page_type": page.page_type, "expected": expected, "types": types, "match": bool(hit)})
    applicable = [r for r in rows if r.get("applicable", True) and "match" in r]
    score = (sum(1 for r in applicable if r["match"]) / max(1, len(applicable))) * 100 if applicable else 50
    rec = "Add page-type schema (Service, Article, FAQPage, Organization) that matches the visible template." if score < 90 else None
    return result(spec, score=score, evidence={"pages": rows[:16]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def tech_18(spec, ctx):
    t = timed()
    authors = []
    for page in ctx.pages:
        for item in flatten_schema(page.schema_blocks):
            if "Person" in str(item.get("@type")) or item.get("author"):
                authors.append({"url": page.result.final_url, "item": {k: item.get(k) for k in ("name", "jobTitle", "url", "@type") if item.get(k)}})
        if page.soup:
            by = page.soup.select_one("[rel=author], .author, .byline")
            if by:
                authors.append({"url": page.result.final_url, "visible": by.get_text(" ", strip=True)[:160]})
    score = 80 if authors else 25
    if any("jobTitle" in (a.get("item") or {}) for a in authors):
        score = 95
    rec = "Publish author/Person markup with role, credentials and a reachable profile URL." if score < 90 else None
    return result(spec, score=score, evidence={"author_signals": authors[:10]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.65)


async def tech_19(spec, ctx):
    t = timed()
    total = valid = 0
    mismatches = []
    for page in ctx.pages:
        for block in page.schema_blocks:
            total += 1
            if block.get("ok"):
                valid += 1
            else:
                mismatches.append({"url": page.result.final_url, "error": block.get("error")})
    score = (valid / total * 100) if total else 40
    rec = "Fix invalid JSON-LD blocks so markup parses and matches visible entity values." if score < 90 else None
    return result(spec, score=score, evidence={"blocks": total, "valid": valid, "errors": mismatches[:8]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def tech_20(spec, ctx):
    t = timed()
    rows = []
    for page in ctx.pages:
        final = page.result.final_url
        canonical = page.canonical
        ok = True
        notes = []
        if canonical:
            if urlparse(canonical).path and urlparse(final).path.rstrip("/") != urlparse(canonical).path.rstrip("/") and urlparse(canonical).netloc:
                if urlparse(canonical).path not in {"", "/"} and page.page_type != "home":
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
    return result(spec, score=score, evidence={"pages": rows[:16]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def tech_21(spec, ctx):
    t = timed()
    tags = []
    for page in ctx.pages:
        tags.extend({"url": page.result.final_url, **h} for h in page.hreflang)
    if not tags:
        return result(spec, score=100, evidence={"hreflang": [], "note": "No multi-market hreflang detected; treated as not applicable rather than fail."}, recommendation=None, checked=ctx.origin, duration_ms=ms_since(t))
    # if present, require valid lang codes
    valid = sum(1 for h in tags if h.get("lang") and h.get("href"))
    score = valid / len(tags) * 100
    rec = "Make hreflang reciprocal and use valid locale codes." if score < 90 else None
    return result(spec, score=score, evidence={"tags": tags[:20]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def tech_22(spec, ctx):
    t = timed()
    titles = []
    metas = []
    rows = []
    for page in ctx.pages:
        titles.append(page.title.strip().lower())
        metas.append(page.meta_description.strip().lower())
        t_ok = 20 <= len(page.title) <= 70
        d_ok = 70 <= len(page.meta_description) <= 170
        rows.append({"url": page.result.final_url, "title": page.title, "title_len": len(page.title), "description": page.meta_description[:180], "desc_len": len(page.meta_description), "title_ok": t_ok and bool(page.title), "desc_ok": d_ok})
    uniq_t = len({x for x in titles if x}) / max(1, len([x for x in titles if x]))
    uniq_d = len({x for x in metas if x}) / max(1, len([x for x in metas if x]))
    presence = sum(1 for r in rows if r["title"] and r["desc_len"]) / max(1, len(rows))
    length_q = sum(1 for r in rows if r["title_ok"] and r["desc_ok"]) / max(1, len(rows))
    score = presence * 50 + uniq_t * 15 + uniq_d * 10 + length_q * 25
    rec = "Write unique titles and meta descriptions in recommended length ranges." if score < 90 else None
    return result(spec, score=score * 100 if score <= 1 else score, evidence={"pages": rows[:16], "unique_title_ratio": round(uniq_t, 2)}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


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

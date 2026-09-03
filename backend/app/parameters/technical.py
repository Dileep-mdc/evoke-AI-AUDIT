from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from ..crawler.discover import sample_internal_links
from ..crawler.http import fetch
from ..crawler.robots import AI_BOTS, bot_decision
from ..llm.client import judge
from ..llm.prompts import SYSTEM, tech11_prompt
from .common import flatten_schema, ms_since, result, schema_types, timed


async def tech_01(spec, ctx):
    t = timed()
    robots = ctx.robots
    if not robots.result.ok and not robots.raw.strip():
        return result(spec, score=None, unknown=True, evidence={"url": robots.url}, recommendation="Publish a robots.txt that explicitly allows AI crawlers.", checked=robots.url, error="robots.txt unavailable", duration_ms=ms_since(t))
    decisions = [bot_decision(robots, bot) for bot in AI_BOTS]
    known = [d for d in decisions if d["decision"] in {"ALLOWED", "BLOCKED"}]
    if not known:
        return result(spec, score=None, unknown=True, evidence={"url": robots.url, "decisions": decisions}, recommendation="Ensure robots.txt parses cleanly so AI-crawler rules can be evaluated.", checked=robots.url, error="robots.txt did not parse", duration_ms=ms_since(t))
    allowed = sum(1 for d in known if d["decision"] == "ALLOWED")
    score = allowed / len(known) * 100
    blocked = [d["name"] for d in known if d["decision"] == "BLOCKED"]
    rec = f"Allow these AI crawlers in robots.txt: {', '.join(blocked)}." if blocked else None
    return result(spec, score=score, evidence={"url": robots.url, "decisions": decisions}, recommendation=rec, checked=robots.url, duration_ms=ms_since(t))


async def tech_02(spec, ctx):
    t = timed()
    url = ctx.origin.rstrip("/") + "/"
    browser_fetch = await fetch(url, user_agent="Mozilla/5.0 (compatible; Chrome/126.0.0.0 Safari/537.36)")
    if not browser_fetch.ok:
        return result(spec, score=None, unknown=True, evidence={"url": url, "error": browser_fetch.error}, recommendation="Homepage was unreachable; retry the scan.", checked=url, error=browser_fetch.error or "homepage unreachable", duration_ms=ms_since(t))
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
    return result(spec, score=score, evidence={"baseline_bytes": baseline_len, "bots": rows}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.6)


_LLMS_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")
_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2}|20\d{2}[/.]\d{1,2}[/.]\d{1,2}|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+20\d{2})\b", re.I)


async def tech_03(spec, ctx):
    t = timed()
    llms = ctx.llms
    if not llms.ok or not llms.text.strip():
        return result(spec, score=0, evidence={"url": llms.url, "status": llms.status_code}, recommendation="Publish an /llms.txt file listing the site's most important pages for AI assistants.", checked=ctx.origin.rstrip("/") + "/llms.txt", duration_ms=ms_since(t))
    text = llms.text
    non_empty = len(text.strip()) > 40
    links = _LLMS_LINK_RE.findall(text)[:5]
    resolved = []
    if links:
        checks = await asyncio.gather(*(fetch(link if link.startswith("http") else ctx.origin.rstrip("/") + "/" + link.lstrip("/")) for link in links[:3]))
        resolved = [{"url": links[i], "ok": c.ok} for i, c in enumerate(checks)]
    link_ratio = (sum(1 for r in resolved if r["ok"]) / len(resolved)) if resolved else 0.0
    fresh = bool(_DATE_RE.search(text))
    score = 40 + (20 if non_empty else 0) + (20 * link_ratio if links else 10) + (20 if fresh else 0)
    rec = "Keep llms.txt non-empty, link to pages that resolve, and include a freshness/date signal." if score < 90 else None
    return result(spec, score=min(100, score), evidence={"non_empty": non_empty, "links_found": len(links), "sampled_links": resolved, "freshness_signal": fresh}, recommendation=rec, checked=llms.final_url, duration_ms=ms_since(t))


_OVERLAY_MARKERS = ("cookie", "consent", "gdpr", "onetrust", "cookiebot", "trustarc", "cookieyes", "modal", "overlay", "paywall")


async def tech_04(spec, ctx):
    t = timed()
    home = ctx.homepage
    if not home or not home.soup:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="Homepage was unavailable to render.", checked=ctx.origin, error="no rendered homepage", duration_ms=ms_since(t))
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
    return result(spec, score=score, evidence={"markers_found": matched, "login_form": login_form, "rendered_word_count": home.word_count}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.55)


async def tech_05(spec, ctx):
    t = timed()
    sm = ctx.sitemap
    urls = sm.get("urls") or []
    if not urls:
        return result(spec, score=0, evidence={"candidates": sm.get("candidates"), "errors": sm.get("errors")}, recommendation="Publish an XML sitemap and reference it from robots.txt.", checked=ctx.origin, duration_ms=ms_since(t))
    sample = urls[:15]
    checks = await asyncio.gather(*(fetch(u) for u in sample))
    valid = sum(1 for c in checks if c.ok)
    validity = valid / len(sample) * 100
    crawled = {p.result.final_url.rstrip("/") for p in ctx.pages}
    sitemap_set = {u.rstrip("/") for u in urls}
    covered = sum(1 for u in crawled if u in sitemap_set)
    coverage = (covered / len(crawled) * 100) if crawled else 0
    score = 25 + validity * 0.45 + coverage * 0.30
    rec = "Fix sitemap URLs that don't resolve and ensure crawled pages are listed in the sitemap." if score < 90 else None
    return result(spec, score=min(100, score), evidence={"sitemap_url_count": len(urls), "sampled": len(sample), "valid_sampled": valid, "crawled_coverage_pct": round(coverage, 1), "errors": sm.get("errors")[:8]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.7)


async def tech_06(spec, ctx):
    t = timed()
    links = sample_internal_links(ctx, limit=15)
    if not links:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="No internal links were found to sample.", checked=ctx.origin, error="no internal links", duration_ms=ms_since(t))
    checks = await asyncio.gather(*(fetch(u) for u in links))
    rows = []
    broken = chains = 0
    for u, c in zip(links, checks):
        is_broken = not c.ok
        is_chain = c.hops >= 3
        broken += int(is_broken)
        chains += int(is_chain)
        rows.append({"url": u, "status": c.status_code, "hops": c.hops, "broken": is_broken, "chain": is_chain})
    score = (len(links) - broken) / len(links) * 100 - min(20, chains * 5)
    rec = "Fix broken internal links and collapse redirect chains to 1-2 hops." if score < 90 else None
    return result(spec, score=max(0, score), evidence={"sampled": len(links), "broken": broken, "redirect_chains": chains, "pages": rows}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.7)


async def tech_07(spec, ctx):
    t = timed()
    pages = [p for p in ctx.pages if p.result and p.result.ok][:12]
    if not pages:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="No successfully crawled pages to measure.", checked=ctx.origin, error="no pages", duration_ms=ms_since(t))
    latencies = [p.result.elapsed_ms for p in pages]
    sizes = [len(p.result.content) for p in pages]
    avg_latency = sum(latencies) / len(latencies)
    avg_size = sum(sizes) / len(sizes)
    if avg_latency <= 800:
        latency_score = 100
    elif avg_latency <= 1800:
        latency_score = 70
    elif avg_latency <= 3000:
        latency_score = 40
    else:
        latency_score = 20
    if avg_size <= 300_000:
        weight_score = 100
    elif avg_size <= 800_000:
        weight_score = 70
    elif avg_size <= 1_500_000:
        weight_score = 40
    else:
        weight_score = 20
    score = latency_score * 0.6 + weight_score * 0.4
    rec = "Reduce server response time and page weight on representative pages; this proxy score is not a substitute for a real Lighthouse/CrUX audit." if score < 90 else None
    return result(spec, score=score, evidence={"pages_measured": len(pages), "avg_latency_ms": round(avg_latency), "avg_page_bytes": round(avg_size), "note": "Latency/weight proxy -- no Lighthouse LCP/INP/CLS run is wired into this build."}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.4)


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

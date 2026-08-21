from __future__ import annotations

import json
import re
from urllib.parse import quote_plus

from ..config import BROWSER_UA
from ..crawler.http import fetch
from ..llm.client import judge
from ..llm.prompts import SYSTEM, off02_prompt
from .common import ms_since, result, timed


async def _json(url: str) -> tuple[dict | list | None, dict]:
    res = await fetch(url, user_agent=BROWSER_UA)
    meta = {"url": url, "status": res.status_code, "error": res.error}
    if not res.ok:
        return None, meta
    try:
        return json.loads(res.text), meta
    except Exception as exc:
        meta["error"] = str(exc)
        return None, meta


async def off_01(spec, ctx):
    t = timed()
    q = quote_plus(ctx.company_name)
    data, meta = await _json(f"https://www.wikidata.org/w/api.php?action=wbsearchentities&search={q}&language=en&format=json&type=item&limit=5")
    if data is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "Wikidata", **meta}, recommendation="Connect Wikidata and retry.", checked=meta["url"], error=meta.get("error") or "Wikidata unavailable", duration_ms=ms_since(t))
    hits = data.get("search") or []
    match = None
    for h in hits:
        blob = f"{h.get('label','')} {h.get('description','')}".lower()
        if "evoke" in blob or "software" in blob or "technolog" in blob:
            match = h
            break
    if not match and hits:
        match = hits[0]
    if not match:
        return result(spec, score=0, evidence={"provider": "Wikidata", "hits": []}, recommendation="Create and maintain an accurate Wikidata organization item.", checked=meta["url"], duration_ms=ms_since(t))
    score = 70
    if "evoke" in (match.get("label") or "").lower():
        score += 20
    if match.get("description"):
        score += 10
    rec = "Complete Wikidata fields (website, aliases, headquarters) and keep them accurate." if score < 90 else None
    return result(spec, score=min(100, score), evidence={"provider": "Wikidata", "entity": match, "hits": hits[:3]}, recommendation=rec, checked=meta["url"], duration_ms=ms_since(t))


async def off_02(spec, ctx):
    t = timed()
    q = quote_plus(ctx.company_name)
    data, meta = await _json(f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&utf8=1&format=json")
    if data is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "Wikipedia", **meta}, recommendation="Retry Wikipedia search.", checked=meta["url"], error=meta.get("error") or "Wikipedia unavailable", duration_ms=ms_since(t))
    hits = ((data.get("query") or {}).get("search") or [])
    company_key = ctx.company_name.split()[0].lower() if ctx.company_name else ""
    about = [h for h in hits if company_key and company_key in (h.get("title") or "").lower()]
    hit_rows = [{"title": h.get("title"), "snippet": re.sub(r"<[^>]+>", "", h.get("snippet") or "")} for h in hits[:5]]

    if not hits:
        return result(spec, score=20, evidence={"provider": "Wikipedia", "hits": [], "method": "substring"}, recommendation="Earn a notable Wikipedia or comparable reference page, or improve third-party reference coverage.", checked=meta["url"], duration_ms=ms_since(t))

    score = 80.0 if about else 20.0
    evidence = {"provider": "Wikipedia", "hits": hit_rows, "method": "substring", "about_match": bool(about)}

    llm_res = await judge(off02_prompt(ctx.company_name, hit_rows), system=SYSTEM)
    if llm_res.ok and llm_res.parsed:
        p = llm_res.parsed
        if p.get("about_company"):
            entity_accuracy = float(p.get("entity_accuracy") or 70)
            source_authority = float(p.get("source_authority") or 70)
            score = 50 + source_authority * 0.3 + entity_accuracy * 0.2
        else:
            score = 20.0
        evidence["method"] = "llm"
        evidence["llm"] = p
    elif not llm_res.ok:
        evidence["llm_unavailable"] = llm_res.error

    score = min(100.0, score)
    rec = None if score >= 90 else "Earn a notable Wikipedia or comparable reference page, or improve third-party reference coverage."
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=meta["url"], duration_ms=ms_since(t))


async def off_03(spec, ctx):
    t = timed()
    return result(
        spec, score=None, unknown=True,
        evidence={"provider": "Search / Knowledge Graph", "reason": "Knowledge Panel UI cannot be reliably observed without a licensed SERP/knowledge API."},
        recommendation="Connect a search/knowledge provider to measure Knowledge Panel presence.",
        checked="knowledge-panel", error="No licensed SERP/knowledge API configured",
        duration_ms=ms_since(t),
    )


async def off_04(spec, ctx):
    t = timed()
    profiles = {
        "linkedin": f"https://www.linkedin.com/company/evoke-technologies/",
        "crunchbase": f"https://www.crunchbase.com/organization/evoke-technologies",
    }
    rows = []
    for name, url in profiles.items():
        res = await fetch(url, user_agent=BROWSER_UA)
        rows.append({"source": name, "url": url, "status": res.status_code, "bytes": len(res.content), "error": res.error})
    reachable = [r for r in rows if r["status"] and r["status"] < 400 and r["bytes"] > 500]
    if not reachable and all((r["status"] in (999, 403, 401, None) or r["error"]) for r in rows):
        return result(spec, score=None, unknown=True, evidence={"provider": "Public profiles", "rows": rows}, recommendation="Use licensed profile APIs; public pages are blocked or login-walled.", checked="company-profiles", error="Public profile pages unavailable (blocked or login-walled)", duration_ms=ms_since(t))
    score = len(reachable) / len(rows) * 100
    rec = "Complete LinkedIn/Crunchbase fields and keep descriptions consistent." if score < 90 else None
    return result(spec, score=score, evidence={"rows": rows}, recommendation=rec, checked=rows[0]["url"], duration_ms=ms_since(t), confidence=0.45)


async def off_05(spec, ctx):
    t = timed()
    site_bits = {
        "name": ctx.company_name,
        "domain": ctx.domain,
    }
    if ctx.homepage:
        text = ctx.homepage.text.lower()
        if "hyderabad" in text:
            site_bits["city"] = "Hyderabad"
        if "ohio" in text or "dublin" in text:
            site_bits["us_office"] = "Ohio"
    data, meta = await _json("https://www.wikidata.org/w/api.php?action=wbsearchentities&search=Evoke%20Technologies&language=en&format=json&limit=1")
    if data is None:
        return result(spec, score=None, unknown=True, evidence={"canonical": site_bits, **meta}, recommendation="Need external directory records to compare NAP.", checked="nap-consistency", error="External NAP sources unavailable", duration_ms=ms_since(t))
    hits = data.get("search") or []
    match = hits[0] if hits else None
    score = 70 if match and "evoke" in (match.get("label") or "").lower() else 40
    rec = "Align company name, addresses and website across directories and the site footer." if score < 90 else None
    return result(spec, score=score, evidence={"canonical": site_bits, "wikidata": match, "provider": "Wikidata"}, recommendation=rec, checked=meta["url"], duration_ms=ms_since(t), confidence=0.5)


async def off_06(spec, ctx):
    t = timed()
    claims = []
    blob = " ".join(p.text for p in ctx.pages)
    for label, pat in (("CMMI", r"cmmi"), ("ISO", r"iso\s?9"), ("Microsoft Partner", r"microsoft\s+partner"), ("AWS", r"aws\s+partner")):
        if re.search(pat, blob, re.I):
            claims.append(label)
    if not claims:
        return result(spec, score=40, evidence={"claims": []}, recommendation="Publish verifiable certifications and link to issuer records.", checked=ctx.origin, duration_ms=ms_since(t))
    return result(spec, score=55, evidence={"claims": claims, "note": "Issuer registries were not queried with a verification adapter; score reflects claimed-but-unverified."}, recommendation="Verify each certification on the issuer or partner directory and link the record.", checked=ctx.origin, duration_ms=ms_since(t), confidence=0.4)


async def off_07(spec, ctx):
    t = timed()
    targets = {
        "g2": "https://www.g2.com/products/evoke-technologies/reviews",
        "clutch": "https://clutch.co/profile/evoke-technologies",
        "trustradius": "https://www.trustradius.com/vendors/evoke-technologies",
    }
    rows = []
    for name, url in targets.items():
        res = await fetch(url, user_agent=BROWSER_UA)
        rows.append({"source": name, "url": url, "status": res.status_code, "error": res.error, "bytes": len(res.content)})
    blocked = all((r["status"] in (403, 401, 999, None) or r["error"]) for r in rows)
    if blocked:
        return result(spec, score=None, unknown=True, evidence={"provider": "Review platforms", "rows": rows}, recommendation="Connect review-platform adapters or licensed APIs.", checked="review-profiles", error="G2/Clutch/TrustRadius pages unavailable without platform API", duration_ms=ms_since(t))
    found = sum(1 for r in rows if r["status"] and 200 <= r["status"] < 400)
    score = found / len(rows) * 100
    rec = "Complete G2, Clutch and adjacent review profiles." if score < 90 else None
    return result(spec, score=score, evidence={"rows": rows}, recommendation=rec, checked=rows[0]["url"], duration_ms=ms_since(t), confidence=0.45)


async def off_08(spec, ctx):
    t = timed()
    return result(
        spec, score=None, unknown=True,
        evidence={"provider": "Review APIs", "reason": "Review volume/recency/velocity requires historical review-platform data."},
        recommendation="Connect a review provider to benchmark volume, recency and velocity.",
        checked="review-velocity", error="No review API configured", duration_ms=ms_since(t),
    )


async def off_09(spec, ctx):
    t = timed()
    desired = []
    text = (ctx.homepage.text if ctx.homepage else "").lower()
    for cat in ("digital engineering", "product engineering", "it services", "staffing", "quality engineering", "data engineering"):
        if cat in text:
            desired.append(cat)
    if not desired:
        desired = ["it services"]
    return result(
        spec, score=None, unknown=True,
        evidence={"desired_categories": desired, "reason": "Third-party category placements need directory/profile APIs."},
        recommendation="Connect directory adapters and align external categories with site positioning.",
        checked="category-placement", error="External directory categories unavailable", duration_ms=ms_since(t),
    )


async def off_10(spec, ctx):
    t = timed()
    q = quote_plus(f'"{ctx.company_name}" site:reddit.com OR site:stackoverflow.com OR site:quora.com')
    url = f"https://html.duckduckgo.com/html/?q={q}"
    res = await fetch(url, user_agent=BROWSER_UA)
    if not res.ok:
        return result(spec, score=None, unknown=True, evidence={"provider": "DuckDuckGo HTML", "error": res.error, "status": res.status_code}, recommendation="Connect a search adapter for community mentions.", checked=url, error=res.error or f"HTTP {res.status_code}", duration_ms=ms_since(t))
    links = re.findall(r"https?://(?:[\w.-]+\.)?(reddit\.com|stackoverflow\.com|quora\.com)/[^\s\"']+", res.text, re.I)
    score = 70 if links else 25
    rec = "Encourage authentic community participation and indexable expert answers." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "mentions": list(dict.fromkeys(links))[:10]}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.4)


async def off_11(spec, ctx):
    t = timed()
    q = quote_plus(f"{ctx.company_name} site:youtube.com")
    url = f"https://html.duckduckgo.com/html/?q={q}"
    res = await fetch(url, user_agent=BROWSER_UA)
    if not res.ok:
        return result(spec, score=None, unknown=True, evidence={"provider": "DuckDuckGo HTML", "error": res.error}, recommendation="Connect YouTube Data API.", checked=url, error=res.error or f"HTTP {res.status_code}", duration_ms=ms_since(t))
    vids = re.findall(r"youtube\.com/watch\?v=[\w-]+|youtu\.be/[\w-]+", res.text, re.I)
    score = 65 if vids else 20
    rec = "Publish official, transcribed videos and earn relevant third-party mentions." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "videos": list(dict.fromkeys(vids))[:8]}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.4)


async def off_12(spec, ctx):
    t = timed()
    q = quote_plus(f'"{ctx.company_name}" (podcast OR webinar OR conference)')
    url = f"https://html.duckduckgo.com/html/?q={q}"
    res = await fetch(url, user_agent=BROWSER_UA)
    if not res.ok:
        return result(spec, score=None, unknown=True, evidence={"error": res.error}, recommendation="Connect a search/event adapter.", checked=url, error=res.error or f"HTTP {res.status_code}", duration_ms=ms_since(t))
    hits = len(re.findall(r"uddg=", res.text))
    score = 60 if hits >= 3 else (30 if hits else 15)
    rec = "Index webinar/podcast appearances with transcripts on authoritative domains." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "approx_results": hits}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.35)


async def off_13(spec, ctx):
    t = timed()
    return result(
        spec, score=None, unknown=True,
        evidence={"provider": "Backlink API", "reason": "Referring-domain quality requires a backlink provider."},
        recommendation="Connect a backlink provider to score topical relevance and referring-domain quality.",
        checked="backlink-provider", error="No backlink provider configured", duration_ms=ms_since(t),
    )


async def off_14(spec, ctx):
    t = timed()
    q = quote_plus(f'"{ctx.company_name}" -site:{ctx.domain}')
    url = f"https://html.duckduckgo.com/html/?q={q}"
    res = await fetch(url, user_agent=BROWSER_UA)
    if not res.ok:
        return result(spec, score=None, unknown=True, evidence={"error": res.error}, recommendation="Connect a news/mention provider.", checked=url, error=res.error or f"HTTP {res.status_code}", duration_ms=ms_since(t))
    hits = len(re.findall(r"uddg=", res.text))
    score = 55 if hits >= 4 else 25
    rec = "Reclaim unlinked brand mentions and pursue earned news in priority markets." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "approx_mentions": hits}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.35)


async def off_15(spec, ctx):
    t = timed()
    return result(
        spec, score=None, unknown=True,
        evidence={"provider": "AI assistant query runner", "reason": "Citation-share measurement needs controlled assistant queries."},
        recommendation="Add an AI-citation research adapter to query assistants and collect cited domains.",
        checked="ai-citations", error="AI assistant query adapter not configured", duration_ms=ms_since(t),
    )


async def off_16(spec, ctx):
    t = timed()
    q = quote_plus(f'best digital engineering companies "{ctx.company_name}"')
    url = f"https://html.duckduckgo.com/html/?q={q}"
    res = await fetch(url, user_agent=BROWSER_UA)
    if not res.ok:
        return result(spec, score=None, unknown=True, evidence={"error": res.error}, recommendation="Connect a SERP adapter for list-inclusion checks.", checked=url, error=res.error or f"HTTP {res.status_code}", duration_ms=ms_since(t))
    mentioned = ctx.company_name.split()[0].lower() in res.text.lower()
    score = 50 if mentioned else 20
    rec = "Pursue legitimate inclusion on authoritative best/top/leading service lists." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "query": q, "brand_mentioned": mentioned}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.35)


async def off_17(spec, ctx):
    t = timed()
    q = quote_plus(f'alternatives to "{ctx.company_name}"')
    url = f"https://html.duckduckgo.com/html/?q={q}"
    res = await fetch(url, user_agent=BROWSER_UA)
    if not res.ok:
        return result(spec, score=None, unknown=True, evidence={"error": res.error}, recommendation="Connect a SERP adapter.", checked=url, error=res.error or f"HTTP {res.status_code}", duration_ms=ms_since(t))
    mentioned = ctx.company_name.split()[0].lower() in res.text.lower()
    score = 45 if mentioned else 15
    rec = "Earn accurate inclusion on alternatives-to-competitor pages." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "brand_mentioned": mentioned}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.35)


async def off_18(spec, ctx):
    t = timed()
    q = quote_plus(f'"{ctx.company_name}" (analyst OR "trade press" OR directory OR "it services")')
    url = f"https://html.duckduckgo.com/html/?q={q}"
    res = await fetch(url, user_agent=BROWSER_UA)
    if not res.ok:
        return result(spec, score=None, unknown=True, evidence={"error": res.error}, recommendation="Connect a web-index adapter.", checked=url, error=res.error or f"HTTP {res.status_code}", duration_ms=ms_since(t))
    hits = len(re.findall(r"uddg=", res.text))
    score = 55 if hits >= 4 else 25
    rec = "Increase accurate analyst, directory and trade-press coverage." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "approx_results": hits}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.35)


HANDLERS = {
    "OFF-01": off_01, "OFF-02": off_02, "OFF-03": off_03, "OFF-04": off_04, "OFF-05": off_05,
    "OFF-06": off_06, "OFF-07": off_07, "OFF-08": off_08, "OFF-09": off_09, "OFF-10": off_10,
    "OFF-11": off_11, "OFF-12": off_12, "OFF-13": off_13, "OFF-14": off_14, "OFF-15": off_15,
    "OFF-16": off_16, "OFF-17": off_17, "OFF-18": off_18,
}

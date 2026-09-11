from __future__ import annotations

import json
import re
from urllib.parse import quote_plus, unquote, urlparse

from ..config import BROWSER_UA
from ..crawler.http import fetch
from ..llm.client import judge
from ..llm.prompts import SYSTEM, off02_prompt, off09_prompt, off18_prompt
from .common import derive_site_categories, derive_site_geographies, ms_since, primary_brand, result, timed


# Every off-page search goes through one endpoint. It was written out in full at twelve
# separate call sites, so changing provider (or adding a key/proxy) meant twelve edits and
# any one of them could be missed.
DDG_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"

# Public read-only APIs used for entity lookups. Same reason: named once, not inline.
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"


def _ddg_url(query: str) -> str:
    """A DuckDuckGo HTML search URL for an already URL-encoded query string."""
    return f"{DDG_HTML_ENDPOINT}?q={query}"


def _wikidata_search_url(query: str, limit: int = 5, typed: bool = True) -> str:
    """Wikidata entity search for an already URL-encoded company name."""
    kind = "&type=item" if typed else ""
    return f"{WIKIDATA_API}?action=wbsearchentities&search={query}&language=en&format=json{kind}&limit={limit}"


async def _ddg_html(url: str):
    """Fetch DuckDuckGo's HTML search endpoint.

    DuckDuckGo returns HTTP 202 with a generic non-result shell page when it suspects
    automated/bot traffic, rather than a hard error -- `res.ok` alone (200-399) treats
    202 as success, which silently turns a blocked search into a false "zero mentions
    found" result. Anything other than a clean 200 is treated as unavailable here.
    """
    res = await fetch(url, user_agent=BROWSER_UA)
    if res.status_code == 200 and res.ok:
        return res, None
    if res.status_code and res.status_code != 200:
        return None, f"DuckDuckGo returned HTTP {res.status_code} (likely a bot-detection challenge, not real results)"
    return None, res.error or "DuckDuckGo unavailable"


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


_UDDG_RE = re.compile(r'uddg=([^&"]+)')
_RESULT_TITLE_RE = re.compile(r'class="result__a"[^>]*>(.*?)</a>', re.S)
_RESULT_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "").strip()


def _extract_result_urls(html: str, limit: int = 15) -> list[str]:
    """Real target URLs behind DuckDuckGo's /l/?uddg= redirect links, in result order."""
    urls: list[str] = []
    for m in _UDDG_RE.finditer(html or ""):
        try:
            u = unquote(m.group(1))
        except Exception:
            continue
        if u.startswith("http") and u not in urls:
            urls.append(u)
        if len(urls) >= limit:
            break
    return urls


def _domains_present(urls: list[str], domains: tuple[str, ...]) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {d: [] for d in domains}
    for u in urls:
        host = (urlparse(u).hostname or "").lower()
        for d in domains:
            if host == d or host.endswith("." + d):
                hits[d].append(u)
    return hits


def _extract_snippets(html: str, limit: int = 8) -> list[dict]:
    titles = [_strip_tags(m) for m in _RESULT_TITLE_RE.findall(html or "")]
    snippets = [_strip_tags(m) for m in _RESULT_SNIPPET_RE.findall(html or "")]
    rows = []
    for i in range(min(len(titles), len(snippets), limit)):
        if titles[i] or snippets[i]:
            rows.append({"title": titles[i], "snippet": snippets[i]})
    return rows


async def off_01(spec, ctx):
    t = timed()
    q = quote_plus(ctx.company_name)
    data, meta = await _json(_wikidata_search_url(q))
    if data is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "Wikidata", **meta}, recommendation="Connect Wikidata and retry.", checked=meta["url"], error=meta.get("error") or "Wikidata unavailable", duration_ms=ms_since(t))
    hits = data.get("search") or []
    brand = primary_brand(ctx)
    match = None
    for h in hits:
        blob = f"{h.get('label','')} {h.get('description','')}".lower()
        if (brand and brand in blob) or any(k in blob for k in ("company", "business", "corporation", "organization", "organisation", "enterprise", "firm")):
            match = h
            break
    if not match and hits:
        match = hits[0]
    if not match:
        return result(spec, score=0, evidence={"provider": "Wikidata", "hits": []}, recommendation="Create and maintain an accurate Wikidata organization item.", checked=meta["url"], duration_ms=ms_since(t))
    score = 70
    if brand and brand in (match.get("label") or "").lower():
        score += 20
    if match.get("description"):
        score += 10
    rec = "Complete Wikidata fields (website, aliases, headquarters) and keep them accurate." if score < 90 else None
    return result(spec, score=min(100, score), evidence={"provider": "Wikidata", "entity": match, "hits": hits[:3]}, recommendation=rec, checked=meta["url"], duration_ms=ms_since(t))


async def off_02(spec, ctx):
    t = timed()
    q = quote_plus(ctx.company_name)
    data, meta = await _json(f"{WIKIPEDIA_API}?action=query&list=search&srsearch={q}&utf8=1&format=json")
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
    q = quote_plus(ctx.company_name)
    data, meta = await _json(_wikidata_search_url(q))
    if data is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "Wikidata (Knowledge Panel proxy)", **meta}, recommendation="Retry once the Wikidata proxy signal is reachable.", checked=meta["url"], error=meta.get("error") or "Wikidata unavailable", duration_ms=ms_since(t))
    hits = data.get("search") or []
    brand = primary_brand(ctx)
    match = next((h for h in hits if brand and brand in f"{h.get('label','')} {h.get('description','')}".lower()), None)
    if not match:
        return result(
            spec, score=None, unknown=True,
            evidence={"provider": "Wikidata (Knowledge Panel proxy)", "hits": hits[:3], "note": "Google's own Knowledge Panel UI cannot be scraped reliably; no confidently-matched Wikidata entity was found to use as a proxy."},
            recommendation="Establish a Wikidata entity and consistent NAP details so a Knowledge Panel can be built.",
            checked=meta["url"], duration_ms=ms_since(t),
        )
    score = 50 + (30 if match.get("description") else 0) + (20 if brand in (match.get("label") or "").lower() else 0)
    rec = "Keep the identity fields a Knowledge Panel would draw on (name, description, website) accurate and consistent." if score < 90 else None
    return result(spec, score=min(100, score), evidence={"provider": "Wikidata (Knowledge Panel proxy)", "entity": match, "note": "Approximated via Wikidata; the live Google Knowledge Panel UI is not directly scraped."}, recommendation=rec, checked=meta["url"], duration_ms=ms_since(t), confidence=0.4)


async def off_04(spec, ctx):
    t = timed()
    domains = ("linkedin.com", "crunchbase.com", "bloomberg.com", "zoominfo.com")
    q = quote_plus(f'"{ctx.company_name}"') + "+" + quote_plus("(" + " OR ".join(f"site:{d}" for d in domains) + ")")
    url = _ddg_url(q)
    res, err = await _ddg_html(url)
    if res is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "DuckDuckGo HTML", "error": err}, recommendation="Connect a licensed company-data API (LinkedIn/Crunchbase/Bloomberg/ZoomInfo).", checked=url, error=err, duration_ms=ms_since(t))
    urls = _extract_result_urls(res.text)
    hits = _domains_present(urls, domains)
    found = [d for d, v in hits.items() if v]
    score = len(found) / len(domains) * 100
    rec = f"Create or claim public company profiles on: {', '.join(d for d in domains if d not in found)}." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "query": q, "platforms_found": found, "matches": hits, "note": "Presence signal only; no licensed profile-data API is connected, so field-level completeness is not verified."}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.4)


async def off_05(spec, ctx):
    t = timed()
    site_bits = {
        "name": ctx.company_name,
        "domain": ctx.domain,
        "locations": derive_site_geographies(ctx),
    }
    q = quote_plus(ctx.company_name)
    data, meta = await _json(_wikidata_search_url(q, limit=1, typed=False))
    if data is None:
        return result(spec, score=None, unknown=True, evidence={"canonical": site_bits, **meta}, recommendation="Need external directory records to compare NAP.", checked="nap-consistency", error="External NAP sources unavailable", duration_ms=ms_since(t))
    hits = data.get("search") or []
    match = hits[0] if hits else None
    brand = primary_brand(ctx)
    score = 70 if match and brand and brand in (match.get("label") or "").lower() else 40
    rec = "Align company name, addresses and website across directories and the site footer." if score < 90 else None
    return result(spec, score=score, evidence={"canonical": site_bits, "wikidata": match, "provider": "Wikidata"}, recommendation=rec, checked=meta["url"], duration_ms=ms_since(t), confidence=0.5)


async def off_06(spec, ctx):
    t = timed()
    claims = []
    blob = " ".join(p.text for p in ctx.pages)
    for label, pat in (
        ("Certified/Accredited", r"\b(certifi\w*|accredit\w*)\b"),
        ("Compliance", r"\bcompliance\b|\bcompliant\b"),
        ("Licensed", r"\blicens(e|ed|ing|ure)\b"),
        ("ISO standard", r"\biso\s?\d{3,6}\b"),
        ("Certified partner/vendor tier", r"\b(certified|authorized|accredited|premier|gold|platinum|elite)\s+partner\b"),
    ):
        if re.search(pat, blob, re.I):
            claims.append(label)
    if not claims:
        return result(spec, score=40, evidence={"claims": []}, recommendation="Publish verifiable certifications and link to issuer records.", checked=ctx.origin, duration_ms=ms_since(t))
    return result(spec, score=55, evidence={"claims": claims, "note": "Issuer registries were not queried with a verification adapter; score reflects claimed-but-unverified."}, recommendation="Verify each certification on the issuer or partner directory and link the record.", checked=ctx.origin, duration_ms=ms_since(t), confidence=0.4)


_REVIEW_DOMAINS = ("g2.com", "clutch.co", "gartner.com", "trustradius.com")


async def off_07(spec, ctx):
    t = timed()
    q = quote_plus(f'"{ctx.company_name}"') + "+" + quote_plus("(" + " OR ".join(f"site:{d}" for d in _REVIEW_DOMAINS) + ")")
    url = _ddg_url(q)
    res, err = await _ddg_html(url)
    if res is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "DuckDuckGo HTML", "error": err}, recommendation="Connect a review-platform API (G2/Clutch/Gartner Peer Insights/TrustRadius).", checked=url, error=err, duration_ms=ms_since(t))
    urls = _extract_result_urls(res.text)
    hits = _domains_present(urls, _REVIEW_DOMAINS)
    found = [d for d, v in hits.items() if v]
    score = len(found) / len(_REVIEW_DOMAINS) * 100
    rec = f"Claim and complete a profile on: {', '.join(d for d in _REVIEW_DOMAINS if d not in found)}." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "query": q, "platforms_found": found, "matches": hits}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.4)


async def off_08(spec, ctx):
    t = timed()
    q = quote_plus(f'"{ctx.company_name}" reviews') + "+" + quote_plus("(" + " OR ".join(f"site:{d}" for d in _REVIEW_DOMAINS) + ")")
    url = _ddg_url(q)
    res, err = await _ddg_html(url)
    if res is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "DuckDuckGo HTML", "error": err}, recommendation="Connect a review-platform API for volume/recency/velocity data.", checked=url, error=err, duration_ms=ms_since(t))
    urls = _extract_result_urls(res.text)
    hits = _domains_present(urls, _REVIEW_DOMAINS)
    count = sum(len(v) for v in hits.values())
    score = min(60, 15 + count * 15)
    rec = "Grow review volume on G2/Clutch/Gartner Peer Insights/TrustRadius, then connect a review-platform API to measure recency/velocity against named competitors."
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "query": q, "review_platform_hits": count, "matches": hits, "note": "Volume proxy only -- recency, velocity and a competitor benchmark require a review-platform API and are not measured here; score is capped at 60."}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.3)


async def off_09(spec, ctx):
    t = timed()
    q = quote_plus(f'"{ctx.company_name}"') + "+" + quote_plus("(" + " OR ".join(f"site:{d}" for d in _REVIEW_DOMAINS) + ")")
    url = _ddg_url(q)
    res, err = await _ddg_html(url)
    if res is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "DuckDuckGo HTML", "error": err}, recommendation="Connect a directory/category adapter.", checked=url, error=err, duration_ms=ms_since(t))
    snippets = _extract_snippets(res.text)
    categories = derive_site_categories(ctx, limit=6)
    if not snippets:
        return result(spec, score=30, evidence={"provider": "DuckDuckGo HTML", "query": q, "snippets": [], "site_categories": categories}, recommendation="Get listed on review/directory platforms under an accurate category.", checked=url, duration_ms=ms_since(t), confidence=0.35)
    blob = " ".join(f"{s['title']} {s['snippet']}" for s in snippets).lower()
    lexical_match = any(c in blob for c in categories) if categories else False
    score = 65 if lexical_match else 35
    confidence = 0.45
    evidence = {"provider": "DuckDuckGo HTML", "query": q, "snippets": snippets, "site_categories": categories, "method": "heuristic"}

    if categories:
        llm_res = await judge(off09_prompt(ctx.company_name, categories, snippets), system=SYSTEM)
        if llm_res.ok and llm_res.parsed and "matches" in llm_res.parsed:
            score = 85 if llm_res.parsed.get("matches") else 30
            confidence = 0.6
            evidence["method"] = "llm"
            evidence["llm"] = llm_res.parsed
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Correct the category/positioning shown on third-party directories and review platforms." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=confidence)


async def off_10(spec, ctx):
    t = timed()
    domains = ("reddit.com", "stackoverflow.com", "quora.com")
    q = quote_plus(f'"{ctx.company_name}"') + "+" + quote_plus("(" + " OR ".join(f"site:{d}" for d in domains) + ")")
    url = _ddg_url(q)
    res, err = await _ddg_html(url)
    if res is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "DuckDuckGo HTML", "error": err}, recommendation="Connect a community-search adapter.", checked=url, error=err, duration_ms=ms_since(t))
    urls = _extract_result_urls(res.text)
    hits = _domains_present(urls, domains)
    count = sum(len(v) for v in hits.values())
    score = 15 if count == 0 else (40 if count <= 2 else 65)
    rec = "Engage authentically in relevant Reddit/Stack Overflow/Quora/industry-forum discussions." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "query": q, "mentions_found": count, "matches": hits, "note": "Presence signal only; substantiveness/spam is not individually verified."}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.35)


async def off_11(spec, ctx):
    t = timed()
    q = quote_plus(f'"{ctx.company_name}"') + "+" + quote_plus("site:youtube.com")
    url = _ddg_url(q)
    res, err = await _ddg_html(url)
    if res is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "DuckDuckGo HTML", "error": err}, recommendation="Connect a YouTube search adapter.", checked=url, error=err, duration_ms=ms_since(t))
    urls = _extract_result_urls(res.text)
    count = len(urls)
    score = 15 if count == 0 else (45 if count <= 2 else 65)
    rec = "Publish or earn relevant YouTube video coverage, with transcripts/captions enabled." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "query": q, "video_results": count, "sample_urls": urls[:8], "note": "Transcript/caption availability is not independently verified."}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.35)


async def off_12(spec, ctx):
    t = timed()
    q = quote_plus(f'"{ctx.company_name}" (podcast OR webinar OR conference)')
    url = _ddg_url(q)
    res, err = await _ddg_html(url)
    if res is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "DuckDuckGo HTML", "error": err}, recommendation="Connect a search/event adapter.", checked=url, error=err, duration_ms=ms_since(t))
    hits = len(re.findall(r"uddg=", res.text))
    score = 60 if hits >= 3 else (30 if hits else 15)
    rec = "Index webinar/podcast appearances with transcripts on authoritative domains." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "approx_results": hits}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.35)


async def off_13(spec, ctx):
    t = timed()
    q = quote_plus(f'"{ctx.domain}"') + "+" + quote_plus(f"-site:{ctx.domain}")
    url = _ddg_url(q)
    res, err = await _ddg_html(url)
    if res is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "DuckDuckGo HTML", "error": err}, recommendation="Connect a backlink data provider (Ahrefs/Moz/Majestic).", checked=url, error=err, duration_ms=ms_since(t))
    urls = _extract_result_urls(res.text)
    own_host = ctx.domain.removeprefix("www.")
    external = [u for u in urls if (urlparse(u).hostname or "").lower().removeprefix("www.") != own_host]
    count = len(external)
    score = min(55, 10 + count * 9)
    return result(
        spec, score=score,
        evidence={"provider": "DuckDuckGo HTML", "query": q, "mentioning_pages": count, "sample_urls": external[:10], "note": "Mention-count proxy only -- topical relevance, link authority and follow/nofollow status require a backlink index (Ahrefs/Moz/Majestic) and are not verified here; score is capped at 55."},
        recommendation="Earn links from topically-relevant, authoritative sites, then connect a backlink data provider for verified quality scoring.",
        checked=url, duration_ms=ms_since(t), confidence=0.25,
    )


async def off_14(spec, ctx):
    t = timed()
    q = quote_plus(f'"{ctx.company_name}"') + "+" + quote_plus(f"-site:{ctx.domain}")
    url = _ddg_url(q)
    res, err = await _ddg_html(url)
    if res is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "DuckDuckGo HTML", "error": err}, recommendation="Connect a media-monitoring/search adapter.", checked=url, error=err, duration_ms=ms_since(t))
    urls = _extract_result_urls(res.text)
    own_host = ctx.domain.removeprefix("www.")
    external = [u for u in urls if (urlparse(u).hostname or "").lower().removeprefix("www.") != own_host]
    count = len(external)
    score = 15 if count == 0 else (40 if count <= 3 else 65)
    rec = "Pursue earned coverage, and ask unlinked mentions to add a link back to the site." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "query": q, "third_party_mentions": count, "sample_urls": external[:10], "note": "Whether each mention omits a link back is not individually verified."}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.3)


async def off_15(spec, ctx):
    t = timed()
    categories = derive_site_categories(ctx, limit=1)
    topic = categories[0] if categories else "this kind of service"
    prompt = (
        f'If someone asked an AI assistant a buyer question about "{topic}", name up to 5 real, '
        "specific websites (with domains) it would most likely reference or cite in its answer. "
        f'Consider whether "{ctx.domain}" (the website of "{ctx.company_name}") would plausibly be '
        "one of them, given how well-known it is.\n\n"
        'Reply as JSON: {"sources": [<string domain>, ...], "site_included": <bool>}'
    )
    llm_res = await judge(prompt, system=SYSTEM)
    if not llm_res.ok or not llm_res.parsed:
        return result(spec, score=None, unknown=True, evidence={"topic": topic, "llm_error": llm_res.error}, recommendation="Enable LLM scoring (ENABLE_LLM_SCORING + OPENAI_API_KEY) to approximate AI-citation visibility.", checked="llm-citation-probe", error=llm_res.error or "LLM unavailable", duration_ms=ms_since(t))
    sources = llm_res.parsed.get("sources") or []
    included = bool(llm_res.parsed.get("site_included")) or any(ctx.domain.removeprefix("www.") in str(s).lower() for s in sources)
    score = 85 if included else (30 if sources else 15)
    rec = "Build citable authority (original data, press coverage, directory listings) so AI assistants surface the site for this topic." if score < 90 else None
    return result(spec, score=score, evidence={"topic": topic, "cited_sources": sources, "site_included": included, "note": "Single-model approximation using this tool's own LLM, not a multi-assistant citation audit."}, recommendation=rec, checked="llm-citation-probe", duration_ms=ms_since(t), confidence=0.4)


async def off_16(spec, ctx):
    t = timed()
    categories = derive_site_categories(ctx, limit=1)
    topic = categories[0] if categories else "companies"
    q = quote_plus(f'best {topic} "{ctx.company_name}"')
    url = _ddg_url(q)
    res, err = await _ddg_html(url)
    if res is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "DuckDuckGo HTML", "error": err}, recommendation="Connect a SERP adapter for list-inclusion checks.", checked=url, error=err, duration_ms=ms_since(t))
    mentioned = ctx.company_name.split()[0].lower() in res.text.lower()
    score = 50 if mentioned else 20
    rec = "Pursue legitimate inclusion on authoritative best/top/leading service lists." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "query": q, "brand_mentioned": mentioned}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.35)


async def off_17(spec, ctx):
    t = timed()
    q = quote_plus(f'"{ctx.company_name}"') + "+" + quote_plus('(alternatives OR "alternative to")')
    url = _ddg_url(q)
    res, err = await _ddg_html(url)
    if res is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "DuckDuckGo HTML", "error": err}, recommendation="Connect a SERP adapter for alternatives-page checks.", checked=url, error=err, duration_ms=ms_since(t))
    urls = _extract_result_urls(res.text)
    alt_pages = [u for u in urls if "alternative" in u.lower()]
    score = 90 if len(alt_pages) >= 2 else (60 if alt_pages else 20)
    rec = "Pursue inclusion on legitimate 'alternatives to' comparison pages relevant to the category." if score < 90 else None
    return result(spec, score=score, evidence={"provider": "DuckDuckGo HTML", "query": q, "alternatives_pages": alt_pages[:8], "note": "No fixed competitor list is configured; this measures inclusion on alternatives-style pages generally rather than against named competitors."}, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=0.35)


async def off_18(spec, ctx):
    t = timed()
    q = quote_plus(f'"{ctx.company_name}"') + "+" + quote_plus('(gartner OR forrester OR idc OR "analyst report" OR "industry report")')
    url = _ddg_url(q)
    res, err = await _ddg_html(url)
    if res is None:
        return result(spec, score=None, unknown=True, evidence={"provider": "DuckDuckGo HTML", "error": err}, recommendation="Connect an analyst/trade-press monitoring adapter.", checked=url, error=err, duration_ms=ms_since(t))
    snippets = _extract_snippets(res.text)
    if not snippets:
        return result(spec, score=20, evidence={"provider": "DuckDuckGo HTML", "query": q, "snippets": []}, recommendation="Pursue analyst, directory and trade-press coverage.", checked=url, duration_ms=ms_since(t), confidence=0.3)
    score = 55
    confidence = 0.35
    evidence = {"provider": "DuckDuckGo HTML", "query": q, "snippets": snippets, "method": "heuristic"}
    llm_res = await judge(off18_prompt(ctx.company_name, snippets), system=SYSTEM)
    if llm_res.ok and llm_res.parsed and "accurate" in llm_res.parsed:
        score = 80 if llm_res.parsed.get("accurate") else 40
        confidence = 0.55
        evidence["method"] = "llm"
        evidence["llm"] = llm_res.parsed
    elif not llm_res.ok:
        evidence["llm_unavailable"] = llm_res.error
    rec = "Pursue additional analyst/trade-press coverage and correct any inaccurate descriptions found." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=url, duration_ms=ms_since(t), confidence=confidence)


HANDLERS = {
    "OFF-01": off_01, "OFF-02": off_02, "OFF-03": off_03, "OFF-04": off_04, "OFF-05": off_05,
    "OFF-06": off_06, "OFF-07": off_07, "OFF-08": off_08, "OFF-09": off_09, "OFF-10": off_10,
    "OFF-11": off_11, "OFF-12": off_12, "OFF-13": off_13, "OFF-14": off_14, "OFF-15": off_15,
    "OFF-16": off_16, "OFF-17": off_17, "OFF-18": off_18,
}

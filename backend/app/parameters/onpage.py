from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..llm.client import judge
from ..llm.prompts import (
    JOURNEY_RUBRIC,
    SERVICE_PAGE_RUBRIC,
    SYSTEM,
    on03_prompt,
    on04_prompt,
    on09_prompt,
    on11_prompt,
    on18_prompt,
)
from .common import (
    derive_site_categories,
    derive_site_geographies,
    flatten_schema,
    heading_blocks,
    is_question,
    ms_since,
    primary_brand,
    result,
    timed,
    word_count,
)

# Openings that say nothing about the business. Only used when the model is unavailable.
_FILLER_OPENINGS = ("welcome to", "in today's", "in today’s", "we are a leading", "leveraging cutting")

# Peak single-term density bands, shared by ON-09 (naturalness) and ON-10 (stuffing) so the
# same rule cannot drift between them.
_DENSITY_BANDS = ((0.025, 100), (0.04, 75))
_DENSITY_FLOOR = 45

# How many pages to put in front of the model for whole-site judgments. Spread across page
# types rather than taken off the top, so a large site's later sections are still represented.
_JOURNEY_SAMPLE = 60


def density_score(average_max_density: float) -> int:
    """Score a page-set's peak keyword density against the shared bands."""
    for ceiling, points in _DENSITY_BANDS:
        if average_max_density < ceiling:
            return points
    return _DENSITY_FLOOR


def _spread_by_page_type(pages, limit: int):
    """A deterministic sample that takes pages round-robin across page types, so one huge
    section (usually the blog) cannot crowd out every other kind of page."""
    buckets: dict[str, list] = defaultdict(list)
    for page in pages:
        buckets[page.page_type or "other"].append(page)
    picked = []
    index = 0
    while len(picked) < limit:
        added = False
        for page_type in sorted(buckets):
            bucket = buckets[page_type]
            if index < len(bucket):
                picked.append(bucket[index])
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
        index += 1
    return picked


JOURNEY = {
    "awareness": ("what is", "overview", "insights", "blog", "guide"),
    "qualification": ("service", "solution", "product", "feature", "how it works", "how we"),
    "comparison": ("vs", "versus", "compar", "alternative"),
    "objection": ("faq", "security", "compliance", "risk", "guarantee", "return policy"),
    "trust": ("case stud", "testimonial", "client", "review", "award", "certified", "accredit"),
    "decision": ("contact", "get in touch", "get started", "book", "schedule", "buy", "order", "sign up"),
}

SUBTOPICS = {
    "problem/need": ("problem", "need", "challenge", "pain point"),
    "approach/process": ("approach", "process", "how it works", "how we", "methodology"),
    "features": ("feature", "capabilit", "what you get", "includes"),
    "outcomes/benefits": ("outcome", "result", "benefit"),
    "audience/market": ("industry", "audience", "market", "who we serve", "for you"),
    "pricing/plans": ("pricing", "price", "cost", "plan", "package"),
    "proof/trust": ("review", "testimonial", "case stud", "client", "rated"),
    "contact/next step": ("contact", "get started", "book", "schedule", "sign up"),
}


def _service_pages(ctx):
    return [p for p in ctx.pages if p.page_type in {"service", "home", "case_study"} or "service" in (p.result.final_url or "").lower()]


def _question_blocks(ctx):
    pages = ctx.pages or []
    html_pages = [p for p in pages if (p.word_count or 0) > 0]
    headings = []
    answers = []
    for page in pages:
        url = page.result.final_url if page.result else page.url
        for h in page.headings:
            headings.append({"url": url, "heading": h["text"], "level": h["level"], "question": is_question(h["text"])})
        for block in heading_blocks(page):
            if not is_question(block["heading"]):
                continue
            wc = word_count(block["answer"])
            direct = wc >= 20
            window = 40 <= wc <= 80
            quality = 70 if direct else 30
            if window:
                quality += 30
            elif 20 <= wc <= 120:
                quality += 15
            answers.append({
                "url": url,
                "question": block["heading"],
                "word_count": wc,
                "score": min(100, quality),
                "preview": block["answer"][:220],
            })
    return pages, html_pages, headings, answers


_NAV_LABEL_RE = re.compile(r"^(home|about|about us|contact|contact us|services?|solutions?|products?|blog|careers?|pricing)$", re.I)


async def on_01(spec, ctx):
    t = timed()
    rows = []
    for page in _service_pages(ctx) or ctx.pages:
        for h in page.headings:
            if h["level"] > 3:
                continue
            text = h["text"].strip()
            if len(text.split()) < 3 or _NAV_LABEL_RE.match(text):
                continue  # short nav-style labels aren't real buyer-facing headings
            rows.append({"url": page.result.final_url, "heading": text, "question": is_question(text)})
    if not rows:
        return result(spec, score=30, evidence={"note": "No eligible H1-H3 headings found on service/solution pages."}, recommendation="Phrase key H2s on service pages as the questions buyers actually ask.", checked=ctx.origin, duration_ms=ms_since(t))
    hits = sum(1 for r in rows if r["question"])
    score = hits / len(rows) * 100
    rec = "Phrase more H2/H3 headings as buyer questions (How/What/Why/Which…) rather than generic labels." if score < 90 else None
    return result(spec, score=score, evidence={"headings": rows[:16], "question_headings": hits, "eligible_headings": len(rows)}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.75)


async def on_02(spec, ctx):
    t = timed()
    pages, html_pages, headings, rows = _question_blocks(ctx)
    if not rows:
        if not html_pages:
            why = "The crawl did not retrieve any HTML, so there was no page text to extract answers from. Check the URL and run a new scan."
        elif not headings:
            why = f"{len(html_pages)} page(s) were crawled, but no H1–H3 headings were found."
        else:
            why = (
                f"{len(headings)} heading(s) were found, but none were phrased as questions "
                "(How / What / Why / When / Where, or ending with ?). This check only scores answers under question headings."
            )
        return result(
            spec,
            score=25,
            evidence={
                "summary": why,
                "pages_checked": len(pages),
                "pages_with_html": len(html_pages),
                "headings_found": len(headings),
                "question_headings": sum(1 for h in headings if h["question"]),
                "heading_samples": headings[:12],
                "answers": [],
            },
            recommendation="Phrase key H2s as buyer questions and place a self-contained 40–60 word answer directly under each one.",
            checked=ctx.origin,
            duration_ms=ms_since(t),
        )
    avg = sum(r["score"] for r in rows) / len(rows)
    rec = "Write a 40–60 word, self-contained answer under every question heading." if avg < 90 else None
    return result(spec, score=avg, evidence={"answers": rows[:12], "question_headings": len(rows)}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.7)


async def on_03(spec, ctx):
    t = timed()
    concepts = derive_site_categories(ctx, limit=8)
    blob = " ".join((p.text or "") for p in ctx.pages).lower()
    defined = []
    for c in concepts:
        if c in blob:
            idx = blob.find(c)
            window = blob[max(0, idx - 40): idx + 160]
            has_def = bool(re.search(rf"{re.escape(c)}.{{0,40}}(is|are|means|refers to|helps)", window))
            defined.append({"concept": c, "defined": has_def, "span": window[:180]})
    present = defined
    hits = sum(1 for d in defined if d["defined"])
    score = (hits / max(1, len(present))) * 100 if present else 30
    confidence = 0.65
    evidence = {"concepts": defined, "method": "regex"}

    if present:
        llm_res = await judge(on03_prompt([{"concept": d["concept"], "window": d["span"]} for d in present]), system=SYSTEM)
        if llm_res.ok and llm_res.parsed and isinstance(llm_res.parsed.get("results"), list) and llm_res.parsed["results"]:
            llm_rows = llm_res.parsed["results"]
            llm_hits = sum(1 for r in llm_rows if r.get("defined"))
            score = llm_hits / len(llm_rows) * 100
            confidence = 0.8
            evidence["method"] = "llm"
            evidence["llm"] = {"results": llm_rows}
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Add one-sentence definitions for core services and technologies." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_04(spec, ctx):
    t = timed()
    rows = []
    site_terms = ctx.brand_terms + derive_site_categories(ctx, limit=6)
    for page in ctx.pages[:12]:
        paras = [p for p in re.split(r"\n+|(?<=\.)\s", page.text) if len(p.split()) > 8][:2]
        opening = " ".join(paras)[:400]
        generic = any(f in opening.lower() for f in _FILLER_OPENINGS)
        specific = any(k in opening.lower() for k in site_terms)
        score = 85 if specific and not generic else (55 if specific else 35)
        rows.append({"url": page.result.final_url, "opening": opening, "generic": generic, "score": score})
    avg = sum(r["score"] for r in rows) / len(rows) if rows else 40
    confidence = 0.6
    evidence = {"pages": rows[:10], "method": "heuristic"}

    # Whether an opening "leads with the conclusion" is a judgment about meaning, so the
    # model decides it; the keyword heuristic above only stands in when it cannot.
    openings = [{"url": r["url"], "opening": r["opening"]} for r in rows if r["opening"]]
    if openings:
        llm_res = await judge(on04_prompt(openings), system=SYSTEM)
        verdicts = (llm_res.parsed or {}).get("results") if llm_res.ok else None
        if isinstance(verdicts, list) and verdicts:
            leads = sum(1 for v in verdicts if v.get("leads_with_substance"))
            avg = leads / len(verdicts) * 100
            confidence = 0.8
            evidence["method"] = "llm"
            evidence["llm"] = {"leads_with_substance": leads, "assessed": len(verdicts)}
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Lead each page with the conclusion / value proposition, then supporting detail." if avg < 90 else None
    return result(spec, score=avg, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_05(spec, ctx):
    t = timed()
    services = [p for p in ctx.pages if p.page_type in {"service", "home"} or re.search(r"service|solution", p.result.final_url, re.I)]
    rows = []
    for page in services or ctx.pages:
        has_h = any(re.search(r"\bfaqs?\b|frequently asked", h["text"], re.I) for h in page.headings)
        has_schema = any("FAQPage" in str(i.get("@type")) for i in flatten_schema(page.schema_blocks))
        rows.append({"url": page.result.final_url, "faq_heading": has_h, "faq_schema": has_schema})
    score = sum(1 for r in rows if r["faq_heading"] or r["faq_schema"]) / max(1, len(rows)) * 100
    rec = "Add a visible FAQ section (and FAQPage schema) on every service/solution page." if score < 90 else None
    return result(spec, score=score, evidence={"pages": rows[:16]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def on_06(spec, ctx):
    t = timed()
    rows = []
    for page in ctx.pages:
        faqs = [i for i in flatten_schema(page.schema_blocks) if "FAQPage" in str(i.get("@type")) or i.get("mainEntity")]
        visible = bool(re.search(r"\bfaqs?\b", page.text, re.I))
        if not faqs and not visible:
            continue
        valid = bool(faqs)
        rows.append({"url": page.result.final_url, "schema": valid, "visible": visible})
    if not rows:
        return result(spec, score=20, evidence={"note": "No FAQ schema or visible FAQ detected"}, recommendation="Add valid FAQPage JSON-LD that matches visible Q&A.", checked=ctx.origin, duration_ms=ms_since(t))
    score = sum(50 * r["schema"] + 50 * r["visible"] for r in rows) / len(rows)
    rec = "Keep FAQ schema valid and consistent with on-page questions and answers." if score < 90 else None
    return result(spec, score=score, evidence={"pages": rows}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def on_07(spec, ctx):
    t = timed()
    eligible = []
    for page in ctx.pages:
        blob = f"{page.title} {page.text[:500]} {page.result.final_url}".lower()
        intent = any(k in blob for k in ("vs", "compar", "alternative", "pricing", "feature", "capability"))
        tables = len(page.soup.find_all("table")) if page.soup else 0
        if intent or page.page_type == "service":
            eligible.append({"url": page.result.final_url, "intent": intent, "tables": tables})
    if not eligible:
        return result(spec, score=40, evidence={}, recommendation="Add comparison/specification tables on evaluation pages.", checked=ctx.origin, duration_ms=ms_since(t))
    score = sum(1 for e in eligible if e["tables"] > 0) / len(eligible) * 100
    rec = "Add real HTML comparison tables where buyers evaluate capabilities or alternatives." if score < 90 else None
    return result(spec, score=score, evidence={"pages": eligible[:12]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.65)


async def on_08(spec, ctx):
    t = timed()
    useful = total = 0
    samples = []
    for page in ctx.pages:
        if not page.soup:
            continue
        for lst in page.soup.find_all(["ul", "ol"]):
            if lst.find_parent(["nav", "header", "footer"]):
                continue  # navigation/menu lists repeat on every page and aren't real content
            items = [li.get_text(" ", strip=True) for li in lst.find_all("li")]
            if len(items) < 2:
                continue
            total += 1
            text = " ".join(items).lower()
            good = len(items) >= 3 and any(k in text for k in ("step", "how", "benefit", "includ", "deliver", "process"))
            useful += int(good)
            if len(samples) < 8:
                samples.append({"url": page.result.final_url, "items": items[:6], "useful": good})
    score = useful / total * 100 if total else 35
    rec = "Use lists for procedures, benefits and evaluation criteria — not decorative nav repeats." if score < 90 else None
    return result(spec, score=score, evidence={"lists": total, "useful": useful, "samples": samples}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def on_09(spec, ctx):
    t = timed()
    rows = []
    samples = []
    for page in ctx.pages:
        words = re.findall(r"[A-Za-z]{3,}", (page.text or "").lower())
        if len(words) < 40:
            continue
        counts = Counter(words)
        dens = counts.most_common(1)[0][1] / len(words)
        rows.append({"url": page.result.final_url, "max_density": round(dens, 4)})
        if len(samples) < 8 and page.text:
            samples.append({"url": page.result.final_url, "excerpt": page.text[:400]})
    if not rows:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="No page copy available to evaluate.", checked=ctx.origin, error="no page text", duration_ms=ms_since(t))
    avg_d = sum(r["max_density"] for r in rows) / len(rows)
    lexical = density_score(avg_d)
    score = lexical
    confidence = 0.55
    evidence = {"pages": rows[:10], "average_max_density": round(avg_d, 4), "method": "heuristic"}

    if samples:
        llm_res = await judge(on09_prompt(samples), system=SYSTEM)
        if llm_res.ok and llm_res.parsed and isinstance(llm_res.parsed.get("naturalness"), (int, float)):
            naturalness = float(llm_res.parsed["naturalness"])
            score = lexical * 0.4 + naturalness * 0.6
            confidence = 0.75
            evidence["method"] = "llm"
            evidence["llm"] = llm_res.parsed
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Rewrite stiff or repetitive copy into natural, conversational sentences." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_10(spec, ctx):
    t = timed()
    rows = []
    for page in ctx.pages:
        words = re.findall(r"[A-Za-z]{3,}", (page.text or "").lower())
        if len(words) < 40:
            continue
        counts = Counter(words)
        dens = counts.most_common(1)[0][1] / len(words)
        rows.append({"url": page.result.final_url, "top": counts.most_common(5), "max_density": round(dens, 4)})
    avg_d = sum(r["max_density"] for r in rows) / len(rows) if rows else 0
    score = density_score(avg_d)
    rec = "Reduce repeated phrases that look like keyword stuffing." if score < 90 else None
    return result(spec, score=score, evidence={"pages": rows[:10], "average_max_density": round(avg_d, 4)}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def on_11(spec, ctx):
    t = timed()
    pages = _service_pages(ctx) or ctx.pages
    rows = []
    for page in pages:
        text = (page.text or "").lower()
        covered = [name for name, kws in SUBTOPICS.items() if any(k in text for k in kws)]
        rows.append({"url": page.result.final_url, "covered": covered, "missing": [name for name in SUBTOPICS if name not in covered], "words": page.word_count})
    rubric_points = len(SERVICE_PAGE_RUBRIC)
    score = sum(len(r["covered"]) / len(SUBTOPICS) * 100 for r in rows) / max(1, len(rows))
    confidence = 0.6
    evidence = {"pages": rows[:12], "method": "heuristic", "rubric_points": rubric_points}

    # Whether a page really covers "outcomes" or "proof" is a reading-comprehension call, so
    # the model counts the rubric points it actually meets. The denominator is unchanged --
    # the same pages, out of the same number of rubric points.
    excerpts = [{"url": p.result.final_url, "excerpt": (p.text or "")[:1200]}
                for p in _spread_by_page_type(pages, 12) if p.text]
    if excerpts:
        llm_res = await judge(on11_prompt(excerpts), system=SYSTEM)
        verdicts = (llm_res.parsed or {}).get("results") if llm_res.ok else None
        if isinstance(verdicts, list) and verdicts:
            covered_counts = [min(rubric_points, max(0, int(v.get("covered") or 0))) for v in verdicts]
            score = sum(c / rubric_points * 100 for c in covered_counts) / len(covered_counts)
            confidence = 0.8
            evidence["method"] = "llm"
            evidence["llm"] = {"covered_per_page": covered_counts, "assessed": len(covered_counts)}
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Fill missing subtopics on service pages: outcomes, industries, engagement model and proof." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_12(spec, ctx):
    t = timed()
    rows = []
    # "Major pages" per the registry: the pages a buyer actually lands on to evaluate the
    # business, not every blog post. The previous filter was `count >= 0`, which admitted
    # every crawled page and made the denominator the whole site.
    major_types = {"home", "service", "case_study", "about"}
    for page in ctx.pages:
        nums = re.findall(r"\b\d{2,}(?:\.\d+)?%?\b|\b\d+\+\b", page.text or "")
        rows.append({
            "url": page.result.final_url,
            "stats": nums[:8],
            "count": len(nums),
            "major": page.page_type in major_types,
        })
    major = [r for r in rows if r["major"]] or rows
    hit = sum(1 for r in major if r["count"] >= 1)
    score = hit / max(1, len(major)) * 100
    rec = "Add original, citable statistics on major pages with a source or date." if score < 90 else None
    return result(spec, score=score, evidence={"pages": major[:12], "major_pages": len(major)}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def on_13(spec, ctx):
    t = timed()
    signals = []
    for page in ctx.pages:
        if page.soup and page.soup.select_one("[rel=author], .author, .byline"):
            signals.append({"url": page.result.final_url, "visible_author": True})
        for item in flatten_schema(page.schema_blocks):
            if "Person" in str(item.get("@type")):
                signals.append({"url": page.result.final_url, "schema": item.get("name")})
    score = 80 if signals else 30
    rec = "Name an author with role, credentials and a real profile on thought-leadership pages." if score < 90 else None
    return result(spec, score=score, evidence={"signals": signals[:10]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def on_14(spec, ctx):
    t = timed()
    rows = []
    for page in ctx.pages:
        text = page.text or ""
        clients = bool(re.search(r"\b(client|customer|partner)\b", text, re.I))
        numbers = bool(re.search(r"\b\d{2,}\s?%|\b\d+\+", text))
        deploy = bool(re.search(r"deploy|implementation|production|rolled out", text, re.I))
        score = clients * 25 + numbers * 30 + deploy * 25 + (20 if clients and numbers else 0)
        rows.append({"url": page.result.final_url, "client": clients, "quantified": numbers, "deployment": deploy, "score": score})
    avg = sum(r["score"] for r in rows) / max(1, len(rows))
    rec = "Name clients, quantify outcomes and describe deployment context on case-study pages." if avg < 90 else None
    return result(spec, score=avg, evidence={"pages": rows[:12]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.65)


async def on_15(spec, ctx):
    t = timed()
    claims = 0
    sourced = 0
    samples = []
    for page in ctx.pages:
        for sent in re.split(r"(?<=[.!?])\s+", page.text or ""):
            if re.search(r"\b\d{2,}\b", sent) and len(sent.split()) > 6:
                claims += 1
                hrefs = [l for l in page.links if l["text"] and l["text"].lower()[:40] in sent.lower()]
                if hrefs or re.search(r"source|according to|report", sent, re.I):
                    sourced += 1
                    if len(samples) < 8:
                        samples.append({"sentence": sent[:220], "sourced": True})
                elif len(samples) < 8:
                    samples.append({"sentence": sent[:220], "sourced": False})
    score = sourced / claims * 100 if claims else 35
    rec = "Cite dated primary sources next to material claims and statistics." if score < 90 else None
    return result(spec, score=score, evidence={"claims": claims, "sourced": sourced, "samples": samples}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.55)


async def on_16(spec, ctx):
    t = timed()
    hits = []
    for page in ctx.pages:
        if re.search(r"reviewed by|technically reviewed|approver|medical review", page.text or "", re.I):
            hits.append(page.result.final_url)
    need = [p for p in ctx.pages if p.page_type in {"article", "service"}]
    score = (len(hits) / max(1, len(need))) * 100 if need else (60 if hits else 25)
    rec = "Name a reviewer or technical approver on pages that make expertise claims." if score < 90 else None
    return result(spec, score=min(100, score), evidence={"reviewer_pages": hits, "evaluated": len(need)}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def on_17(spec, ctx):
    t = timed()
    dated = 0
    rows = []
    for page in ctx.pages:
        has = bool(page.dates.get("published") or page.dates.get("modified"))
        dated += int(has)
        rows.append({"url": page.result.final_url, "dates": page.dates})
    score = 50 + dated / max(1, len(rows)) * 50
    rec = "Show last-substantive-update dates and refresh evergreen service copy." if score < 90 else None
    return result(spec, score=score, evidence={"dated_pages": dated, "samples": rows[:12]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.6)


async def on_18(spec, ctx):
    t = timed()
    covered = defaultdict(list)
    corpus = [(p.result.final_url, f"{p.title} {p.text[:1000]}".lower()) for p in ctx.pages]
    brand = primary_brand(ctx)
    journey = {**JOURNEY, "comparison": JOURNEY["comparison"] + ((f"why {brand}",) if brand else ())}
    for stage, keys in journey.items():
        for url, blob in corpus:
            if any(k in blob or k in url.lower() for k in keys):
                covered[stage].append(url)
    stages = len(JOURNEY)
    score = len([s for s in JOURNEY if covered[s]]) / stages * 100
    confidence = 0.7
    evidence = {"stages": {k: v[:5] for k, v in covered.items()}, "covered": list(covered), "method": "heuristic"}

    # What stage a page serves is a judgment about purpose, not vocabulary -- a pricing page
    # aids comparison whether or not it contains the word "vs". Denominator stays the six stages.
    titles = [{"url": p.result.final_url, "title": p.title or ""}
              for p in _spread_by_page_type(ctx.pages, _JOURNEY_SAMPLE)]
    if titles:
        llm_res = await judge(on18_prompt(titles), system=SYSTEM)
        verdict = (llm_res.parsed or {}).get("stages") if llm_res.ok else None
        if isinstance(verdict, dict) and verdict:
            hit = [s for s in JOURNEY if verdict.get(s)]
            score = len(hit) / stages * 100
            confidence = 0.85
            evidence["method"] = "llm"
            evidence["covered"] = hit
            evidence["llm"] = {"stages": verdict, "pages_assessed": len(titles)}
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Add missing journey stages — especially comparison, objection-handling and decision pages." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_19(spec, ctx):
    t = timed()
    patterns = []
    for page in ctx.pages:
        blob = f"{page.result.final_url} {page.title}".lower()
        if re.search(r"best .+(for)| vs |versus|alternative", blob):
            patterns.append({"url": page.result.final_url, "title": page.title})
    score = 80 if len(patterns) >= 3 else (45 if patterns else 15)
    rec = "Create Best X for Y and X vs Y pages for priority industries and services." if score < 90 else None
    return result(spec, score=score, evidence={"matches": patterns}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.7)


async def on_20(spec, ctx):
    t = timed()
    titles = [(p.result.final_url, (p.title or "").lower().strip()) for p in ctx.pages if p.title]
    pairs = []
    for i, (u1, t1) in enumerate(titles):
        w1 = set(re.findall(r"[a-z0-9]{4,}", t1))
        for u2, t2 in titles[i + 1:]:
            w2 = set(re.findall(r"[a-z0-9]{4,}", t2))
            if not w1 or not w2:
                continue
            sim = len(w1 & w2) / len(w1 | w2)
            if sim >= 0.7 and t1 != t2:
                pairs.append({"a": u1, "b": u2, "similarity": round(sim, 2), "titles": [t1, t2]})
    score = 100 if not pairs else max(40, 100 - len(pairs) * 12)
    rec = "Consolidate pages that compete for the same buyer question." if pairs else None
    return result(spec, score=score, evidence={"overlapping_pairs": pairs[:10]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.6)


async def on_21(spec, ctx):
    t = timed()
    thin = []
    for page in ctx.pages:
        if page.page_type != "utility" and page.word_count < 180:
            thin.append({"url": page.result.final_url, "words": page.word_count})
    titles = [(p.result.final_url, p.text[:400]) for p in ctx.pages if p.text]
    dups = []
    for i, (u1, a) in enumerate(titles):
        wa = set(a.lower().split())
        for u2, b in titles[i + 1:]:
            wb = set(b.lower().split())
            if len(wa & wb) / max(1, len(wa | wb)) > 0.72:
                dups.append([u1, u2])
    score = 100 - min(60, len(thin) * 8 + len(dups) * 10)
    rec = "Merge or expand thin/near-duplicate pages so topics are not diluted." if score < 90 else None
    return result(spec, score=score, evidence={"thin": thin[:12], "near_duplicates": dups[:8]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


_GENERIC_REACH_TERMS = ("united states", "usa", "uk", "europe", "asia", "north america", "global", "worldwide", "nationwide")


async def on_22(spec, ctx):
    t = timed()
    geos = tuple(derive_site_geographies(ctx)) + _GENERIC_REACH_TERMS
    cats = tuple(derive_site_categories(ctx))
    rows = []
    for page in ctx.pages:
        text = (page.text or "").lower()
        brand = any(b in text for b in ctx.brand_terms)
        cat = any(c in text for c in cats)
        geo = any(g in text for g in geos)
        rows.append({"url": page.result.final_url, "brand": brand, "category": cat, "geo": geo})
    score = sum(1 for r in rows if r["brand"] and r["category"] and r["geo"]) / max(1, len(rows)) * 100
    rec = "Name the brand together with category, industry and geography on key pages." if score < 90 else None
    return result(spec, score=score, evidence={"pages": rows[:14]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=0.7)


HANDLERS = {
    "ON-01": on_01, "ON-02": on_02, "ON-03": on_03, "ON-04": on_04, "ON-05": on_05, "ON-06": on_06,
    "ON-07": on_07, "ON-08": on_08, "ON-09": on_09, "ON-10": on_10, "ON-11": on_11, "ON-12": on_12,
    "ON-13": on_13, "ON-14": on_14, "ON-15": on_15, "ON-16": on_16, "ON-17": on_17, "ON-18": on_18,
    "ON-19": on_19, "ON-20": on_20, "ON-21": on_21, "ON-22": on_22,
}

from __future__ import annotations

import asyncio
import re
from collections import defaultdict

from ..llm.client import judge
from ..llm.prompts import (
    SERVICE_PAGE_RUBRIC,
    SYSTEM,
    on01_prompt,
    on03_prompt,
    on04_prompt,
    on07_prompt,
    on08_prompt,
    on11_prompt,
    on12_prompt,
    on13_prompt,
    on14_prompt,
    on15_prompt,
    on18_prompt,
    on19_prompt,
    on20_prompt,
)
from .common import (
    band,
    derive_site_categories,
    derive_site_geographies,
    flatten_schema,
    heading_blocks,
    is_question,
    ms_since,
    near_duplicate_pairs,
    primary_brand,
    result,
    timed,
    top_term_density,
    word_count,
)

# Openings that say nothing about the business. Only used when the model is unavailable.
_FILLER_OPENINGS = ("welcome to", "in today's", "in today’s", "we are a leading", "leveraging cutting")

# Peak single-term density bands behind ON-10 (keyword stuffing). Measured over content
# words only (see common.content_words): when function words were included, the most
# frequent token on essentially every English page was "the" at 4-7%, which sits above the
# worst band -- so every site on every scan scored the floor and the check told us nothing
# about stuffing.
# Read best-first as (ceiling, points): a peak term under 3.5% of content words is normal
# prose, 3.5-6% is repetitive, above that is stuffing.
_DENSITY_BANDS = ((0.035, 100), (0.06, 75))
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
            # The target window is the registry's own 40-60 words, not 40-80. The wider
            # band it used handed full marks to answers the parameter defines as too long.
            window = 40 <= wc <= 60
            quality = 70 if direct else 30
            if window:
                quality += 30
            elif 25 <= wc <= 90:
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

# What share of a page's real headings should be phrased as buyer questions. Scoring the
# raw share meant full marks required ~90% of every H1-H3 to be a question, which no
# well-written page does (section labels, product names and the H1 itself are not
# questions), so the check could only ever return FAIL. A third of headings framed as
# questions is the realistic target these bands score against.
_QUESTION_HEADING_BANDS = ((0.33, 100.0), (0.20, 85.0), (0.10, 65.0), (0.03, 45.0))
_QUESTION_HEADING_FLOOR = 25.0


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
    share = hits / len(rows)
    score = band(share, _QUESTION_HEADING_BANDS, _QUESTION_HEADING_FLOOR)
    confidence = 0.75
    evidence = {"headings": rows[:16], "question_headings": hits, "eligible_headings": len(rows),
                "question_share": round(share, 3), "method": "heuristic"}

    # The regex detects question FORM, which is not what the parameter asks about. "What We
    # Do" passes it and no buyer ever typed it; "Pricing for mid-market teams" fails it and
    # answers a question buyers ask constantly. Intent is a reading judgment, so the model
    # makes it where available. Strided, not head-40, so one heading-heavy page cannot fill
    # the whole sample.
    step = max(1, len(rows) // 40)
    candidates = [{"url": r["url"], "heading": r["heading"]} for r in rows[::step]][:40]
    llm_res = await judge(on01_prompt(candidates), system=SYSTEM)
    verdicts = (llm_res.parsed or {}).get("results") if llm_res.ok else None
    if isinstance(verdicts, list) and verdicts:
        buyer = sum(1 for v in verdicts if v.get("buyer_question"))
        share = buyer / len(verdicts)
        score = band(share, _QUESTION_HEADING_BANDS, _QUESTION_HEADING_FLOOR)
        confidence = 0.85
        evidence["method"] = "llm"
        evidence["question_share"] = round(share, 3)
        evidence["llm"] = {"buyer_questions": buyer, "assessed": len(verdicts)}
    elif not llm_res.ok:
        evidence["llm_unavailable"] = llm_res.error

    rec = "Phrase more H2/H3 headings as buyer questions (How/What/Why/Which…) rather than generic labels." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


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
    if not services:
        # The parameter is "FAQ on every service and solution page". With no such page to
        # inspect there is nothing to score; grading every crawled page instead (the old
        # `or ctx.pages` fallback) demanded an FAQ on contact and careers pages too.
        return result(spec, score=None, unknown=True, evidence={"note": "No service or solution pages were identified on this site, so there were no pages this check applies to."}, recommendation="If the site offers services, give each service page a visible FAQ section and FAQPage schema.", checked=ctx.origin, error="no service or solution pages found", duration_ms=ms_since(t))
    rows = []
    for page in services:
        has_h = any(re.search(r"\bfaqs?\b|frequently asked", h["text"], re.I) for h in page.headings)
        has_schema = any("FAQPage" in str(i.get("@type")) for i in flatten_schema(page.schema_blocks))
        rows.append({"url": page.result.final_url, "faq_heading": has_h, "faq_schema": has_schema})
    score = sum(1 for r in rows if r["faq_heading"] or r["faq_schema"]) / max(1, len(rows)) * 100
    rec = "Add a visible FAQ section (and FAQPage schema) on every service/solution page." if score < 90 else None
    return result(spec, score=score, evidence={"pages": rows[:16]}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


async def on_05_1(spec, ctx):
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
    tables_by_url = {}
    eligible = []
    for page in ctx.pages:
        blob = f"{page.title} {page.text[:500]} {page.result.final_url}".lower()
        intent = any(k in blob for k in ("vs", "compar", "alternative", "pricing", "feature", "capability"))
        tables = len(page.soup.find_all("table")) if page.soup else 0
        tables_by_url[page.result.final_url] = tables
        if intent or page.page_type == "service":
            eligible.append({"url": page.result.final_url, "intent": intent, "tables": tables})
    if not eligible:
        return result(spec, score=40, evidence={}, recommendation="Add comparison/specification tables on evaluation pages.", checked=ctx.origin, duration_ms=ms_since(t))
    score = sum(1 for e in eligible if e["tables"] > 0) / len(eligible) * 100
    confidence = 0.65
    evidence = {"pages": eligible[:12], "method": "heuristic"}

    # Whether a page is one where a buyer is actually evaluating options is a judgment
    # about purpose, not vocabulary: a pricing page qualifies without containing "vs", and
    # a blog post titled "X vs Y" may not. The model picks the denominator; the numerator
    # stays the same measured fact -- does that page carry a real HTML table.
    candidates = [{"url": p.result.final_url, "title": p.title or "", "excerpt": (p.text or "")[:600]}
                  for p in _spread_by_page_type(ctx.pages, 24) if p.text]
    if candidates:
        llm_res = await judge(on07_prompt(candidates), system=SYSTEM)
        verdicts = (llm_res.parsed or {}).get("results") if llm_res.ok else None
        if isinstance(verdicts, list) and verdicts:
            judged = [v for v in verdicts if v.get("evaluation_intent") and v.get("url") in tables_by_url]
            if judged:
                score = sum(1 for v in judged if tables_by_url[v["url"]] > 0) / len(judged) * 100
                confidence = 0.8
                evidence["method"] = "llm"
                evidence["llm"] = {"evaluation_pages": len(judged), "assessed": len(verdicts),
                                   "with_tables": sum(1 for v in judged if tables_by_url[v["url"]] > 0)}
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Add real HTML comparison tables where buyers evaluate capabilities or alternatives." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_08(spec, ctx):
    t = timed()
    useful = total = 0
    samples = []
    candidates: list[dict] = []
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
            if len(candidates) < 30:
                candidates.append({"url": page.result.final_url, "items": items[:10]})
    if not total:
        return result(spec, score=35, evidence={"lists": 0, "note": "No content lists were found outside navigation."}, recommendation="Use lists for procedures, benefits and evaluation criteria.", checked=ctx.origin, duration_ms=ms_since(t))
    score = useful / total * 100
    confidence = 0.7
    evidence = {"lists": total, "useful": useful, "samples": samples, "method": "heuristic"}

    # Whether a list actually helps answer a question is a reading judgment. The keyword
    # test above only recognises a list that happens to contain "step" or "benefit", so it
    # misses a genuine specification or criteria list that uses none of those words.
    if candidates:
        llm_res = await judge(on08_prompt(candidates), system=SYSTEM)
        verdicts = (llm_res.parsed or {}).get("results") if llm_res.ok else None
        if isinstance(verdicts, list) and verdicts:
            score = sum(1 for v in verdicts if v.get("useful")) / len(verdicts) * 100
            confidence = 0.8
            evidence["method"] = "llm"
            evidence["llm"] = {"useful": sum(1 for v in verdicts if v.get("useful")), "assessed": len(verdicts)}
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Use lists for procedures, benefits and evaluation criteria — not decorative nav repeats." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_10(spec, ctx):
    t = timed()
    rows = []
    for page in ctx.pages:
        dens, top = top_term_density(page.text)
        if dens is None:
            continue
        rows.append({"url": page.result.final_url, "top": top, "max_density": round(dens, 4)})
    if not rows:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="No page copy available to evaluate.", checked=ctx.origin, error="no page text", duration_ms=ms_since(t))
    avg_d = sum(r["max_density"] for r in rows) / len(rows)
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


# What counts as a citable data point: a percentage, a "500+", a currency figure, a
# multiplier, or a number large enough to be written with thousands separators. The plain
# `\d{2,}` this replaces matched every year, street number, phone fragment and list index
# on the page, so a copyright footer alone satisfied "has an original statistic".
_STAT_RE = re.compile(
    r"\d+(?:\.\d+)?\s?%"
    r"|\b\d{1,3}(?:,\d{3})+\+?\b"
    r"|\b\d+(?:\.\d+)?\s?(?:x|×)\b"
    r"|[$£€]\s?\d[\d,.]*\s?(?:k|m|bn|b|million|billion|crore|lakh)?\b"
    r"|\b\d{2,}\+"
    r"|\b\d+(?:\.\d+)?\s?(?:million|billion|trillion|crore|lakh)\b",
    re.I,
)
# A bare four-digit year is a date, not a statistic, even when it survives the patterns above.
_YEAR_ONLY_RE = re.compile(r"^(?:19|20)\d{2}$")


def _statistics(text: str) -> list[str]:
    return [m.group(0).strip() for m in _STAT_RE.finditer(text or "") if not _YEAR_ONLY_RE.match(m.group(0).strip())]


async def on_12(spec, ctx):
    t = timed()
    rows = []
    # "Major pages" per the registry: the pages a buyer actually lands on to evaluate the
    # business, not every blog post. The previous filter was `count >= 0`, which admitted
    # every crawled page and made the denominator the whole site.
    major_types = {"home", "service", "case_study", "about"}
    for page in ctx.pages:
        nums = _statistics(page.text)
        rows.append({
            "url": page.result.final_url,
            "stats": nums[:8],
            "count": len(nums),
            "major": page.page_type in major_types,
            "excerpt": (page.text or "")[:600],
        })
    major = [r for r in rows if r["major"]] or rows
    hit = sum(1 for r in major if r["count"] >= 1)
    carry = hit / max(1, len(major))
    score = carry * 100
    confidence = 0.85
    evidence = {"pages": [{k: v for k, v in r.items() if k != "excerpt"} for r in major[:12]],
                "major_pages": len(major), "method": "heuristic"}

    # The pattern proves a statistic is PRESENT, never that it is the company's own -- a
    # figure quoted off an analyst firm satisfies it and not the parameter, which asks for
    # an ORIGINAL data point. The model judges originality on the pages that have a
    # statistic; the denominator stays every major page, so only the numerator moves.
    with_stats = [r for r in major if r["count"] >= 1][:24]
    if with_stats:
        llm_res = await judge(on12_prompt([{"url": r["url"], "statistics": r["stats"], "excerpt": r["excerpt"]}
                                           for r in with_stats]), system=SYSTEM)
        verdicts = (llm_res.parsed or {}).get("results") if llm_res.ok else None
        if isinstance(verdicts, list) and verdicts:
            original = sum(1 for v in verdicts if v.get("original"))
            score = carry * (original / len(verdicts)) * 100
            confidence = 0.8
            evidence["method"] = "llm"
            evidence["llm"] = {"original": original, "assessed": len(verdicts),
                               "pages_with_a_statistic": hit}
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Add original, citable statistics on major pages with a source or date." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


def _collect_thought_leadership_signals(pages) -> list[dict]:
    """The synchronous per-page CSS-selector + schema-walk behind ON-13.

    Same shape as technical._collect_author_signals and the same reason it
    runs off the event loop: an unyielding per-page loop over a large site
    otherwise stalls every parameter sharing its concurrency batch, not just
    this one.
    """
    signals = []
    for page in pages:
        element = page.soup.select_one("[rel=author], .author, .byline") if page.soup else None
        if element:
            # The byline TEXT, not just the fact a matching element exists: whether it names
            # a real person with a role is the question, and that cannot be asked of a bool.
            signals.append({"url": page.result.final_url, "visible_author": True,
                            "byline": element.get_text(" ", strip=True)[:160]})
        for item in flatten_schema(page.schema_blocks):
            if "Person" in str(item.get("@type")):
                named = " - ".join(str(item[k]) for k in ("name", "jobTitle") if item.get(k))
                signals.append({"url": page.result.final_url, "schema": item.get("name"),
                                "byline": named[:160]})
    return signals


async def on_13(spec, ctx):
    t = timed()
    signals = await asyncio.to_thread(_collect_thought_leadership_signals, ctx.pages)
    score = 80.0 if signals else 30.0
    confidence = 0.85
    evidence = {"signals": signals[:10], "method": "heuristic"}

    # Presence of a .byline element is not the parameter, which asks for a NAMED person with
    # a role and real credentials. "Posted by Admin" satisfies the selector and nothing else,
    # so where the model is available it reads the byline text and decides.
    bylines = [{"url": s["url"], "byline": s["byline"]} for s in signals if s.get("byline")][:20]
    if bylines:
        llm_res = await judge(on13_prompt(bylines), system=SYSTEM)
        verdicts = (llm_res.parsed or {}).get("results") if llm_res.ok else None
        if isinstance(verdicts, list) and verdicts:
            named = sum(1 for v in verdicts if v.get("named_person"))
            with_role = sum(1 for v in verdicts if v.get("named_person") and v.get("has_role_or_credentials"))
            # Half the marks for naming a real person, half for stating their role. Floored
            # at the no-byline score: markup that names nobody is no worse than no markup.
            score = max(30.0, named / len(verdicts) * 50 + with_role / len(verdicts) * 50)
            confidence = 0.8
            evidence["method"] = "llm"
            evidence["llm"] = {"named_people": named, "with_role_or_credentials": with_role, "assessed": len(verdicts)}
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Name an author with role, credentials and a real profile on thought-leadership pages." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_14(spec, ctx):
    t = timed()
    rows = []
    for page in ctx.pages:
        text = page.text or ""
        clients = bool(re.search(r"\b(client|customer|partner)\b", text, re.I))
        numbers = bool(re.search(r"\b\d{2,}\s?%|\b\d+\+", text))
        deploy = bool(re.search(r"deploy|implementation|production|rolled out", text, re.I))
        page_score = clients * 25 + numbers * 30 + deploy * 25 + (20 if clients and numbers else 0)
        rows.append({"url": page.result.final_url, "client": clients, "quantified": numbers, "deployment": deploy, "score": page_score})
    score = sum(r["score"] for r in rows) / max(1, len(rows))
    confidence = 0.65
    evidence = {"pages": rows[:12], "method": "heuristic"}

    # The regex cannot tell a NAMED client from the generic word "client", which is the
    # whole of what the parameter asks for -- and it counts any two-digit number as a
    # quantified outcome. The model re-scores a page-type spread on the same three signals
    # with the same weights, so only the reading of each signal changes, not the rubric.
    sample = [{"url": p.result.final_url, "excerpt": (p.text or "")[:700]}
              for p in _spread_by_page_type(ctx.pages, 20) if p.text]
    if sample:
        llm_res = await judge(on14_prompt(sample), system=SYSTEM)
        verdicts = (llm_res.parsed or {}).get("results") if llm_res.ok else None
        if isinstance(verdicts, list) and verdicts:
            judged = []
            for v in verdicts:
                named, quantified, deployed = bool(v.get("names_client")), bool(v.get("quantified_outcome")), bool(v.get("deployment_detail"))
                judged.append(named * 25 + quantified * 30 + deployed * 25 + (20 if named and quantified else 0))
            score = sum(judged) / len(judged)
            confidence = 0.8
            evidence["method"] = "llm"
            evidence["llm"] = {"assessed": len(verdicts),
                               "named_client": sum(1 for v in verdicts if v.get("names_client")),
                               "quantified_outcome": sum(1 for v in verdicts if v.get("quantified_outcome"))}
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Name clients, quantify outcomes and describe deployment context on case-study pages." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_15(spec, ctx):
    t = timed()
    claims = 0
    sourced = 0
    samples = []
    candidates = []
    for page in ctx.pages:
        for sent in re.split(r"(?<=[.!?])\s+", page.text or ""):
            if _statistics(sent) and len(sent.split()) > 6:
                claims += 1
                hrefs = [l for l in page.links if l["text"] and l["text"].lower()[:40] in sent.lower()]
                if len(candidates) < 24:
                    candidates.append({"sentence": sent[:300], "nearby_links": [l["text"][:60] for l in hrefs[:4]]})
                if hrefs or re.search(r"source|according to|report", sent, re.I):
                    sourced += 1
                    if len(samples) < 8:
                        samples.append({"sentence": sent[:220], "sourced": True})
                elif len(samples) < 8:
                    samples.append({"sentence": sent[:220], "sourced": False})
    score = sourced / claims * 100 if claims else 35
    confidence = 0.55
    evidence = {"claims": claims, "sourced": sourced, "samples": samples, "method": "heuristic"}

    # The regex only establishes that something source-LIKE sits near the claim: the bare
    # word "report" anywhere in the sentence satisfies it, and a link whose anchor text
    # happens to appear in the sentence satisfies it too. Whether a reader could actually
    # go and check the source is a reading judgment, so the model makes it.
    if candidates:
        llm_res = await judge(on15_prompt(candidates), system=SYSTEM)
        verdicts = (llm_res.parsed or {}).get("results") if llm_res.ok else None
        if isinstance(verdicts, list) and verdicts:
            attributed = sum(1 for v in verdicts if v.get("sourced"))
            score = attributed / len(verdicts) * 100
            confidence = 0.75
            evidence["method"] = "llm"
            evidence["llm"] = {"sourced": attributed, "assessed": len(verdicts), "claims_found": claims}
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Cite dated primary sources next to material claims and statistics." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


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


def _journey_stage_coverage(pages, journey: dict) -> dict:
    """The synchronous per-page keyword scan behind ON-18's heuristic path.

    A 6-stage x N-page x ~keywords scan with no `await` inside it -- same
    event-loop-blocking shape as the two helpers above, moved off the loop
    for the same reason. The model call further down stays untouched -- it's
    already real, non-blocking I/O.
    """
    covered = defaultdict(list)
    corpus = [(p.result.final_url, f"{p.title} {p.text[:1000]}".lower()) for p in pages]
    for stage, keys in journey.items():
        for url, blob in corpus:
            if any(k in blob or k in url.lower() for k in keys):
                covered[stage].append(url)
    return covered


async def on_18(spec, ctx):
    t = timed()
    brand = primary_brand(ctx)
    journey = {**JOURNEY, "comparison": JOURNEY["comparison"] + ((f"why {brand}",) if brand else ())}
    covered = await asyncio.to_thread(_journey_stage_coverage, ctx.pages, journey)
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
    confidence = 0.7
    evidence = {"matches": patterns[:12], "matched_pages": len(patterns), "method": "heuristic"}

    # The token match both over- and under-selects. A blog post titled "Docker vs Podman" is
    # not a buyer comparison page, and "Choosing an ERP for manufacturers" is one while
    # matching none of the tokens. Which pages serve a high-intent comparison query is a
    # judgment about purpose, so the model makes it and the same three tiers are applied.
    sample = [{"url": p.result.final_url, "title": p.title or ""}
              for p in _spread_by_page_type(ctx.pages, 30) if p.title]
    if sample:
        llm_res = await judge(on19_prompt(sample), system=SYSTEM)
        verdicts = (llm_res.parsed or {}).get("results") if llm_res.ok else None
        if isinstance(verdicts, list) and verdicts:
            found = [v for v in verdicts if v.get("comparison_page")]
            score = 80 if len(found) >= 3 else (45 if found else 15)
            confidence = 0.8
            evidence["method"] = "llm"
            evidence["llm"] = {"comparison_pages": len(found), "assessed": len(verdicts),
                               "pages": [v.get("url") for v in found[:10]]}
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Create Best X for Y and X vs Y pages for priority industries and services." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_20(spec, ctx):
    t = timed()
    titles = [(p.result.final_url, (p.title or "").lower().strip()) for p in ctx.pages if p.title]
    if not titles:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="No page titles were available to compare.", checked=ctx.origin, error="no page titles", duration_ms=ms_since(t))
    by_url = dict(titles)
    pairs = [p for p in near_duplicate_pairs(titles, 0.7) if by_url.get(p["a"]) != by_url.get(p["b"])]
    for p in pairs:
        p["titles"] = [by_url.get(p["a"], ""), by_url.get(p["b"], "")]
    # Scored as a share of the site, not as a raw count. A fixed 12 points per pair meant
    # nine overlapping titles bottomed out the check whether the site had 20 pages or 2000.
    cannibalized = len({u for p in pairs for u in (p["a"], p["b"])})
    share = cannibalized / len(titles)
    score = band(1 - share, _CANNIBALIZATION_BANDS, _CANNIBALIZATION_FLOOR)
    confidence = 0.6
    competing_pairs = pairs
    evidence = {"overlapping_pairs": pairs[:10], "pairs_found": len(pairs), "pages_affected": cannibalized,
                "pages_compared": len(titles), "method": "heuristic"}

    # Title overlap is a candidate filter, not the finding. A service page and the case study
    # about that service share most of their tokens without competing, and so do the same
    # service written for two different industries. The model confirms which candidates
    # really answer the same buyer question; unconfirmed pairs stop counting against the
    # site. Pairs past the sample keep their heuristic verdict rather than being dropped.
    assessed, beyond = pairs[:20], pairs[20:]
    if assessed:
        llm_res = await judge(on20_prompt([{"a": p["a"], "b": p["b"], "title_a": p["titles"][0], "title_b": p["titles"][1]}
                                           for p in assessed]), system=SYSTEM)
        verdicts = (llm_res.parsed or {}).get("results") if llm_res.ok else None
        if isinstance(verdicts, list) and verdicts:
            confirmed = {(v.get("a"), v.get("b")) for v in verdicts if v.get("competing")}
            competing_pairs = [p for p in assessed if (p["a"], p["b"]) in confirmed] + beyond
            cannibalized = len({u for p in competing_pairs for u in (p["a"], p["b"])})
            score = band(1 - cannibalized / len(titles), _CANNIBALIZATION_BANDS, _CANNIBALIZATION_FLOOR)
            confidence = 0.8
            evidence.update(method="llm", overlapping_pairs=competing_pairs[:10],
                            pairs_found=len(competing_pairs), pages_affected=cannibalized,
                            llm={"candidate_pairs": len(assessed), "confirmed": len(competing_pairs) - len(beyond)})
        elif not llm_res.ok:
            evidence["llm_unavailable"] = llm_res.error

    rec = "Consolidate pages that compete for the same buyer question." if competing_pairs else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_21(spec, ctx):
    t = timed()
    scorable = [p for p in ctx.pages if p.page_type != "utility"]
    if not scorable:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="No content pages were available to assess.", checked=ctx.origin, error="no content pages", duration_ms=ms_since(t))
    thin = [{"url": p.result.final_url, "words": p.word_count} for p in scorable if p.word_count < 180]
    excerpts = [(p.result.final_url, p.text[:400]) for p in scorable if p.text]
    dups = near_duplicate_pairs(excerpts, 0.72)
    duplicated = {u for d in dups for u in (d["a"], d["b"])}
    # Proportional, like ON-20: the old absolute penalty (8 points per thin page, 10 per
    # duplicate pair) hit its 60-point cap at eight thin pages, so a 2000-page site with
    # eight thin pages scored identically to a 10-page site that was almost entirely thin.
    affected = len({t["url"] for t in thin} | duplicated)
    share = affected / len(scorable)
    score = band(1 - share, _DILUTION_BANDS, _DILUTION_FLOOR)
    rec = "Merge or expand thin/near-duplicate pages so topics are not diluted." if score < 90 else None
    return result(spec, score=score, evidence={"thin": thin[:12], "thin_count": len(thin), "near_duplicates": dups[:8], "duplicate_pairs": len(dups), "pages_affected": affected, "pages_assessed": len(scorable)}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


# Both duplicate checks score the share of the site that is *clean*, read best-first as
# (minimum clean share, points), so the penalty scales with the site instead of with a
# raw count of findings.
_CANNIBALIZATION_BANDS = ((1.0, 100.0), (0.95, 90.0), (0.85, 75.0), (0.70, 60.0))
_CANNIBALIZATION_FLOOR = 40.0
_DILUTION_BANDS = ((0.95, 100.0), (0.85, 85.0), (0.70, 70.0), (0.50, 55.0))
_DILUTION_FLOOR = 40.0

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
    "ON-01": on_01, "ON-02": on_02, "ON-03": on_03, "ON-04": on_04, "ON-05": on_05, "ON-5.1": on_05_1,
    "ON-07": on_07, "ON-08": on_08, "ON-10": on_10, "ON-11": on_11, "ON-12": on_12,
    "ON-13": on_13, "ON-14": on_14, "ON-15": on_15, "ON-17": on_17, "ON-18": on_18,
    "ON-19": on_19, "ON-20": on_20, "ON-21": on_21, "ON-22": on_22,
}

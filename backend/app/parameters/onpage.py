from __future__ import annotations

import asyncio
import re
from collections import defaultdict

from ..llm.batch import judge_all, judge_one
from ..llm.prompts import (
    SERVICE_PAGE_RUBRIC,
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
from . import rules
from .common import (
    band,
    derive_site_categories,
    derive_site_geographies,
    excluded_page_counts,
    flatten_schema,
    heading_blocks,
    is_question,
    ms_since,
    near_duplicate_pairs,
    primary_brand,
    result,
    scorable_pages,
    timed,
    top_term_density,
    word_count,
)

# Openings that say nothing about the business. Only used when the model is unavailable.
_FILLER_OPENINGS = ("welcome to", "in today's", "in today’s", "we are a leading", "leveraging cutting")

# Every band table and threshold in this module is declared in scoring_rules.json, not here.
# They are the audit's methodology and belong somewhere a reviewer can read them together,
# with the reasoning attached, rather than as literals beside the code that happens to use them.
# Bound once at import rather than looked up per page: these are read inside per-page loops
# that run tens of thousands of times on a large site.
_DENSITY_BANDS, _DENSITY_FLOOR = rules.band("keyword_density")

_THIN_PAGE_WORDS = rules.threshold("thin_page_words")
_NEAR_DUPLICATE_SIMILARITY = rules.threshold("near_duplicate_body_similarity")
_COMPETING_TITLE_SIMILARITY = rules.threshold("competing_title_similarity")
_ANSWER_IDEAL_MIN = rules.threshold("answer_ideal_words", "min")
_ANSWER_IDEAL_MAX = rules.threshold("answer_ideal_words", "max")
_ANSWER_OK_MIN = rules.threshold("answer_acceptable_words", "min")
_ANSWER_OK_MAX = rules.threshold("answer_acceptable_words", "max")
_USEFUL_LIST_MIN_ITEMS = rules.threshold("useful_list_min_items")
_COMPARISON_PAGES_FOR_CREDIT = rules.threshold("comparison_pages_for_full_credit")

# No per-parameter excerpt lengths and no per-parameter item ceilings any more.
#
# There used to be six of them -- 600/700/1200 characters of page text, 600 candidate pairs,
# 8 concepts, 12 pages -- and every one was a number somebody picked. Together they decided
# how much of a site the audit was willing to look at, which is the question the audit exists
# to answer rather than one it should be quietly answering for itself.
#
# Pages now go to the model whole. Requests stay a sane size because llm/batch.py packs each
# batch by CHARACTER BUDGET rather than by a fixed item count, so one 8,000-word page travels
# in a batch of its own and forty short ones share a batch -- without either case dropping
# anything. The only remaining bound is the size of a single request, which is physics, not
# policy, and it never decides what gets scored.


def density_score(average_max_density: float) -> int:
    """Score a page-set's peak keyword density against the shared bands."""
    for ceiling, points in _DENSITY_BANDS:
        if average_max_density < ceiling:
            return points
    return _DENSITY_FLOOR


# _spread_by_page_type used to pick 12-60 pages round-robin across page types so the blog
# could not crowd out the sample. It is gone because there is no sample any more: handlers
# batch through every candidate via llm.batch.judge_all, which removes the question of which
# pages to favour rather than answering it well.


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
    return [p for p in scorable_pages(ctx) if p.page_type in {"service", "home", "case_study"} or "service" in (p.result.final_url or "").lower()]


def _question_blocks(ctx):
    pages = scorable_pages(ctx)
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
            direct = wc >= rules.threshold("answer_direct_words")
            # The target window is the registry's own 40-60 words, not 40-80. The wider
            # band it used handed full marks to answers the parameter defines as too long.
            window = _ANSWER_IDEAL_MIN <= wc <= _ANSWER_IDEAL_MAX
            quality = 70 if direct else 30
            if window:
                quality += 30
            elif _ANSWER_OK_MIN <= wc <= _ANSWER_OK_MAX:
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

_QUESTION_HEADING_BANDS, _QUESTION_HEADING_FLOOR = rules.band("question_headings")


async def on_01(spec, ctx):
    t = timed()
    rows = []
    for page in _service_pages(ctx) or scorable_pages(ctx):
        for h in page.headings:
            if h["level"] > 3:
                continue
            text = h["text"].strip()
            if len(text.split()) < 3 or _NAV_LABEL_RE.match(text):
                continue  # short nav-style labels aren't real buyer-facing headings
            rows.append({"url": page.result.final_url, "heading": text, "question": is_question(text)})
    if not rows:
        return result(
            spec,
            score=30,
            evidence={"note": "No eligible H1-H3 headings found on service/solution pages.",
                      "eligible_headings": 0, "pages_assessed": len(_service_pages(ctx) or scorable_pages(ctx)),
                      "pages_crawled": len(ctx.pages)},
            recommendation="Phrase key H2s on service pages as the questions buyers actually ask.",
            checked=ctx.origin,
            duration_ms=ms_since(t),
        )
    hits = sum(1 for r in rows if r["question"])
    share = hits / len(rows)
    score = band(share, _QUESTION_HEADING_BANDS, _QUESTION_HEADING_FLOOR)
    confidence = 0.75
    evidence = {"headings": rows, "eligible_headings": len(rows),
                "question_headings_by_pattern": hits, "question_headings": hits,
                "question_share": round(share, 3), "method": "heuristic"}

    # The regex detects question FORM, which is not what the parameter asks about. "What We
    # Do" passes it and no buyer ever typed it; "Pricing for mid-market teams" fails it and
    # answers a question buyers ask constantly. Intent is a reading judgment, so the model
    # makes it -- over every DISTINCT eligible heading.
    #
    # Distinct, because a site-wide heading is one editorial decision, not N of them. Judging
    # every occurrence meant asking the model the same question about "The Evoke Edge" two
    # thousand times, at two thousand times the cost, and weighting that single heading as
    # two thousand data points in the share. Occurrences are still counted -- they are what
    # the share is computed over -- but each distinct heading is judged once.
    occurrences: dict[str, int] = defaultdict(int)
    first_url: dict[str, str] = {}
    for row in rows:
        key = row["heading"].strip().lower()
        occurrences[key] += 1
        first_url.setdefault(key, row["url"])
    distinct = [{"url": first_url[key], "heading": key} for key in occurrences]
    evidence["distinct_headings"] = len(distinct)

    verdicts, coverage = await judge_all(distinct, on01_prompt)
    if verdicts:
        # Weighted back up by how often each heading actually appears, so the share still
        # describes the site's headings rather than its heading vocabulary.
        judged = {str(v.get("heading", "")).strip().lower(): bool(v.get("buyer_question")) for v in verdicts}
        weighed = [(occurrences[key], judged[key]) for key in occurrences if key in judged]
        total_weight = sum(count for count, _ in weighed)
        buyer = sum(count for count, is_buyer in weighed if is_buyer)
        share = buyer / total_weight if total_weight else 0.0
        score = band(share, _QUESTION_HEADING_BANDS, _QUESTION_HEADING_FLOOR)
        confidence = 0.85 if coverage.complete else 0.75
        evidence["method"] = "llm"
        # Both the count and the share move together. They used to not: the model's verdict
        # overwrote question_share and left question_headings holding the pattern count, so
        # one real scan shipped evidence reading "385 question headings, 0.0% share" and the
        # grader wrote "only 0 of them are classified as buyer questions" from it.
        evidence["question_headings"] = buyer
        evidence["question_share"] = round(share, 3)
        evidence["llm"] = {"buyer_questions_by_occurrence": buyer,
                           "distinct_headings_judged_as_buyer_questions": sum(1 for v in judged.values() if v),
                           "heading_occurrences_counted": total_weight,
                           **coverage.as_evidence()}
    elif coverage.error:
        evidence["llm_unavailable"] = coverage.error

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
                "heading_samples": headings,
                "answers": [],
            },
            recommendation="Phrase key H2s as buyer questions and place a self-contained 40–60 word answer directly under each one.",
            checked=ctx.origin,
            duration_ms=ms_since(t),
        )
    avg = sum(r["score"] for r in rows) / len(rows)
    rec = "Write a 40–60 word, self-contained answer under every question heading." if avg < 90 else None
    # Every answer is scored; `answers` is a sample of them. The totals say so explicitly,
    # because a bare 12-row list reads as the whole population to both the grader and the client.
    empty = sum(1 for r in rows if r["word_count"] == 0)
    return result(
        spec,
        score=avg,
        evidence={
            "answers": rows,
            "question_headings": len(rows),
            "answers_total": len(rows),
            "answers_scored": len(rows),
            "answers_with_no_extracted_text": empty,
            "pages_checked": len(pages),
            "headings_found": len(headings),
        },
        recommendation=rec,
        checked=ctx.origin,
        duration_ms=ms_since(t),
        confidence=0.7,
    )


_DEFINITION_RE_TEMPLATE = r"{concept}\s*(?:\([^)]{{0,60}}\)\s*)?(?:is|are|means|refers to|helps|enables|describes)\b"
# A window is worth quoting as evidence only if it reads like prose. Navigation and footer
# text is a run of short comma/space-separated labels with no verb, and on a site whose menu
# lists every service it is also the FIRST place every concept appears -- which is how the
# old blob.find() landed on it every time.
_PROSE_RE = re.compile(r"\b(is|are|was|were|means|provides|helps|enables|allows|delivers|refers)\b")


def _definition_windows(blob: str, concept: str, limit: int = 40) -> list[str]:
    """Every place a concept is mentioned, best-reading first.

    The previous implementation took `blob.find(concept)` -- the single first occurrence --
    and quoted it as the site's definition. On one real scan that produced the span
    "27001, hipaa, gdpr and soc 2 standards. ai & automation application development data &
    analytics enterprise solutions quality assurance security", which is a menu, and the
    parameter then scored 0 for the site failing to define a term it may well define further
    down the page. Windows are now ranked so prose beats nav.
    """
    windows: list[str] = []
    start = 0
    needle = concept.lower()
    while len(windows) < limit:
        idx = blob.find(needle, start)
        if idx < 0:
            break
        windows.append(blob[max(0, idx - 60): idx + 220].strip())
        start = idx + max(1, len(needle))
    definition_re = re.compile(_DEFINITION_RE_TEMPLATE.format(concept=re.escape(concept)))
    windows.sort(key=lambda w: (bool(definition_re.search(w)), bool(_PROSE_RE.search(w))), reverse=True)
    return windows


async def on_03(spec, ctx):
    t = timed()
    concepts = derive_site_categories(ctx)
    blob = " ".join((p.text or "") for p in scorable_pages(ctx)).lower()
    defined = []
    for c in concepts:
        windows = _definition_windows(blob, c)
        if not windows:
            continue
        definition_re = re.compile(_DEFINITION_RE_TEMPLATE.format(concept=re.escape(c)))
        has_def = any(definition_re.search(w) for w in windows)
        defined.append({
            "concept": c,
            "defined": has_def,
            "span": windows[0][:220],
            "mentions": len(windows),
        })
    present = defined
    hits = sum(1 for d in defined if d["defined"])
    score = (hits / max(1, len(present))) * 100 if present else 30
    confidence = 0.65
    evidence = {
        "concepts": defined,
        "concepts_derived": len(concepts),
        "concepts_found_on_site": len(present),
        "method": "regex",
    }

    if present:
        verdicts, coverage = await judge_all(
            [{"concept": d["concept"], "window": d["span"]} for d in present], on03_prompt
        )
        if verdicts:
            llm_hits = sum(1 for r in verdicts if r.get("defined"))
            score = llm_hits / len(verdicts) * 100
            confidence = 0.8
            evidence["method"] = "llm"
            evidence["llm"] = {"defined": llm_hits, "results": verdicts, **coverage.as_evidence()}
        elif coverage.error:
            evidence["llm_unavailable"] = coverage.error

    rec = "Add one-sentence definitions for core services and technologies." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_04(spec, ctx):
    t = timed()
    rows = []
    site_terms = ctx.brand_terms + derive_site_categories(ctx)
    # Every page, not ctx.pages[:12]. The old slice was not a sample of the site -- it was
    # the first twelve URLs in crawl order, which is the homepage plus whatever the priority
    # keywords happened to put at the front of the queue -- and the resulting average was
    # reported as the site's score.
    for page in scorable_pages(ctx):
        paras = [p for p in re.split(r"\n+|(?<=\.)\s", page.text or "") if len(p.split()) > 8][:2]
        opening = " ".join(paras)[:400]
        generic = any(f in opening.lower() for f in _FILLER_OPENINGS)
        specific = any(k in opening.lower() for k in site_terms)
        score = 85 if specific and not generic else (55 if specific else 35)
        rows.append({"url": page.result.final_url, "opening": opening, "generic": generic, "score": score})
    avg = sum(r["score"] for r in rows) / len(rows) if rows else 40
    confidence = 0.6
    evidence = {"pages": rows, "pages_assessed": len(rows), "method": "heuristic"}

    # Whether an opening "leads with the conclusion" is a judgment about meaning, so the
    # model decides it; the keyword heuristic above only stands in when it cannot.
    openings = [{"url": r["url"], "opening": r["opening"]} for r in rows if r["opening"]]
    evidence["pages_with_an_opening"] = len(openings)
    if openings:
        verdicts, coverage = await judge_all(openings, on04_prompt)
        if verdicts:
            leads = sum(1 for v in verdicts if v.get("leads_with_substance"))
            avg = leads / len(verdicts) * 100
            confidence = 0.8 if coverage.complete else 0.7
            evidence["method"] = "llm"
            evidence["llm"] = {"leads_with_substance": leads, **coverage.as_evidence()}
        elif coverage.error:
            evidence["llm_unavailable"] = coverage.error

    rec = "Lead each page with the conclusion / value proposition, then supporting detail." if avg < 90 else None
    return result(spec, score=avg, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_05(spec, ctx):
    t = timed()
    services = [p for p in scorable_pages(ctx) if p.page_type in {"service", "home"} or re.search(r"service|solution", p.result.final_url, re.I)]
    if not services:
        # The parameter is "FAQ on every service and solution page". With no such page to
        # inspect there is nothing to score; grading every crawled page instead (the old
        # `or scorable_pages(ctx)` fallback) demanded an FAQ on contact and careers pages too.
        return result(spec, score=None, unknown=True, evidence={"note": "No service or solution pages were identified on this site, so there were no pages this check applies to."}, recommendation="If the site offers services, give each service page a visible FAQ section and FAQPage schema.", checked=ctx.origin, error="no service or solution pages found", duration_ms=ms_since(t))
    rows = []
    for page in services:
        has_h = any(re.search(r"\bfaqs?\b|frequently asked", h["text"], re.I) for h in page.headings)
        has_schema = any("FAQPage" in str(i.get("@type")) for i in flatten_schema(page.schema_blocks))
        rows.append({"url": page.result.final_url, "faq_heading": has_h, "faq_schema": has_schema})
    with_faq = sum(1 for r in rows if r["faq_heading"] or r["faq_schema"])
    score = with_faq / max(1, len(rows)) * 100
    rec = "Add a visible FAQ section (and FAQPage schema) on every service/solution page." if score < 90 else None
    # Every service page is scored; `pages` shows sixteen of them. Without the totals the
    # grader read the sample as the population and wrote "none of the 16 service pages have
    # an FAQ" for a site that had far more than sixteen.
    return result(
        spec,
        score=score,
        evidence={
            "pages": rows,
            "service_pages_total": len(rows),
            "service_pages_with_faq": with_faq,
            "pages_crawled": len(ctx.pages),
        },
        recommendation=rec,
        checked=ctx.origin,
        duration_ms=ms_since(t),
    )


async def on_05_1(spec, ctx):
    t = timed()
    rows = []
    for page in scorable_pages(ctx):
        faqs = [i for i in flatten_schema(page.schema_blocks) if "FAQPage" in str(i.get("@type")) or i.get("mainEntity")]
        visible = bool(re.search(r"\bfaqs?\b", page.text, re.I))
        if not faqs and not visible:
            continue
        valid = bool(faqs)
        rows.append({"url": page.result.final_url, "schema": valid, "visible": visible})
    if not rows:
        # "No FAQ detected" on its own does not say how hard anyone looked. The page count
        # is what turns it from an assertion into a finding.
        return result(
            spec,
            score=20,
            evidence={"note": "No FAQ schema or visible FAQ detected",
                      "faq_pages_total": 0, "pages_assessed": len(scorable_pages(ctx)), **excluded_page_counts(ctx)},
            recommendation="Add valid FAQPage JSON-LD that matches visible Q&A.",
            checked=ctx.origin,
            duration_ms=ms_since(t),
        )
    score = sum(50 * r["schema"] + 50 * r["visible"] for r in rows) / len(rows)
    rec = "Keep FAQ schema valid and consistent with on-page questions and answers." if score < 90 else None
    return result(
        spec,
        score=score,
        evidence={
            "pages": rows,
            "faq_pages_total": len(rows),
            "with_valid_schema": sum(1 for r in rows if r["schema"]),
            "with_visible_faq": sum(1 for r in rows if r["visible"]),
            # The mismatch is the actual finding, so it is counted rather than left for the
            # reader to infer from a sixteen-row sample.
            "schema_without_visible_faq": sum(1 for r in rows if r["schema"] and not r["visible"]),
            "visible_faq_without_schema": sum(1 for r in rows if r["visible"] and not r["schema"]),
            "pages_crawled": len(ctx.pages),
        },
        recommendation=rec,
        checked=ctx.origin,
        duration_ms=ms_since(t),
    )


async def on_07(spec, ctx):
    t = timed()
    tables_by_url = {}
    eligible = []
    for page in scorable_pages(ctx):
        blob = f"{page.title} {page.text[:500]} {page.result.final_url}".lower()
        intent = any(k in blob for k in ("vs", "compar", "alternative", "pricing", "feature", "capability"))
        tables = len(page.soup.find_all("table")) if page.soup else 0
        tables_by_url[page.result.final_url] = tables
        if intent or page.page_type == "service":
            eligible.append({"url": page.result.final_url, "intent": intent, "tables": tables})
    if not eligible:
        return result(
            spec,
            score=40,
            evidence={"note": "No pages showed buyer-evaluation intent.",
                      "eligible_pages_total": 0, "pages_crawled": len(ctx.pages)},
            recommendation="Add comparison/specification tables on evaluation pages.",
            checked=ctx.origin,
            duration_ms=ms_since(t),
        )
    score = sum(1 for e in eligible if e["tables"] > 0) / len(eligible) * 100
    confidence = 0.65
    evidence = {"pages": eligible, "eligible_pages_total": len(eligible),
                "pages_crawled": len(ctx.pages), "method": "heuristic"}

    # Whether a page is one where a buyer is actually evaluating options is a judgment
    # about purpose, not vocabulary: a pricing page qualifies without containing "vs", and
    # a blog post titled "X vs Y" may not. The model picks the denominator; the numerator
    # stays the same measured fact -- does that page carry a real HTML table. Every page
    # with text is judged, so the denominator is the site rather than a 24-page spread of it.
    candidates = [{"url": p.result.final_url, "title": p.title or "", "excerpt": (p.text or "")}
                  for p in scorable_pages(ctx) if p.text]
    if candidates:
        verdicts, coverage = await judge_all(candidates, on07_prompt)
        judged = [v for v in verdicts if v.get("evaluation_intent") and v.get("url") in tables_by_url]
        if judged:
            with_tables = sum(1 for v in judged if tables_by_url[v["url"]] > 0)
            score = with_tables / len(judged) * 100
            confidence = 0.8 if coverage.complete else 0.7
            evidence["method"] = "llm"
            evidence["llm"] = {"evaluation_pages": len(judged), "with_tables": with_tables,
                               **coverage.as_evidence()}
        elif coverage.error:
            evidence["llm_unavailable"] = coverage.error

    rec = "Add real HTML comparison tables where buyers evaluate capabilities or alternatives." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_08(spec, ctx):
    t = timed()
    useful = total = empty_lists = 0
    samples = []
    candidates: list[dict] = []
    for page in scorable_pages(ctx):
        if not page.soup:
            continue
        for lst in page.soup.find_all(["ul", "ol"]):
            if lst.find_parent(["nav", "header", "footer"]):
                continue  # navigation/menu lists repeat on every page and aren't real content
            # Empty <li> text is markup, not content: an icon row or a JS-populated list
            # extracts as ["", "", "", "", "", ""] and was being counted as a six-item list
            # and then graded as "not useful", which is a real finding about the extractor
            # reported as a finding about the site. Blanks are dropped before the length test.
            raw_items = [li.get_text(" ", strip=True) for li in lst.find_all("li")]
            items = [text for text in raw_items if text]
            if len(items) < 2:
                # Counted only when the list HAD items and none of them carried text, so the
                # tally means "extraction produced nothing here" rather than folding in
                # ordinary one-item lists, which were skipped before this change too.
                if len(raw_items) >= 2:
                    empty_lists += 1
                continue
            total += 1
            text = " ".join(items).lower()
            good = len(items) >= _USEFUL_LIST_MIN_ITEMS and any(k in text for k in ("step", "how", "benefit", "includ", "deliver", "process"))
            useful += int(good)
            if len(samples) < 8:
                samples.append({"url": page.result.final_url, "items": items, "useful": good})
            candidates.append({"url": page.result.final_url, "items": items})
    if not total:
        return result(spec, score=35, evidence={"lists": 0, "empty_or_markup_only_lists": empty_lists, "note": "No content lists were found outside navigation."}, recommendation="Use lists for procedures, benefits and evaluation criteria.", checked=ctx.origin, duration_ms=ms_since(t))
    score = useful / total * 100
    confidence = 0.7
    evidence = {"lists": total, "lists_total": total, "useful": useful,
                "useful_by_keyword": useful, "empty_or_markup_only_lists": empty_lists,
                "samples": samples, "method": "heuristic"}

    # Whether a list actually helps answer a question is a reading judgment. The keyword
    # test above only recognises a list that happens to contain "step" or "benefit", so it
    # misses a genuine specification or criteria list that uses none of those words. Every
    # content list is judged rather than the first thirty found.
    if candidates:
        verdicts, coverage = await judge_all(candidates, on08_prompt)
        if verdicts:
            useful_judged = sum(1 for v in verdicts if v.get("useful"))
            score = useful_judged / len(verdicts) * 100
            confidence = 0.8 if coverage.complete else 0.7
            evidence["method"] = "llm"
            evidence["useful"] = useful_judged
            evidence["llm"] = {"useful": useful_judged, **coverage.as_evidence()}
        elif coverage.error:
            evidence["llm_unavailable"] = coverage.error

    rec = "Use lists for procedures, benefits and evaluation criteria — not decorative nav repeats." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_10(spec, ctx):
    t = timed()
    rows = []
    for page in scorable_pages(ctx):
        dens, top = top_term_density(page.text)
        if dens is None:
            continue
        rows.append({"url": page.result.final_url, "top": top, "max_density": round(dens, 4)})
    if not rows:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="No page copy available to evaluate.", checked=ctx.origin, error="no page text", duration_ms=ms_since(t))
    avg_d = sum(r["max_density"] for r in rows) / len(rows)
    score = density_score(avg_d)
    rec = "Reduce repeated phrases that look like keyword stuffing." if score < 90 else None
    # The skipped count matters to how this score should be read: pages under the content-word
    # floor are excluded, and on a site with many thin pages that means the average describes
    # the healthier part of the site. Stating it lets the grader say so.
    return result(
        spec,
        score=score,
        evidence={
            "pages": rows,
            "pages_assessed": len(rows),
            "pages_crawled": len(ctx.pages),
            "pages_skipped_too_few_content_words": len(scorable_pages(ctx)) - len(rows),
            "average_max_density": round(avg_d, 4),
        },
        recommendation=rec,
        checked=ctx.origin,
        duration_ms=ms_since(t),
    )


async def on_11(spec, ctx):
    t = timed()
    pages = _service_pages(ctx) or scorable_pages(ctx)
    rows = []
    for page in pages:
        text = (page.text or "").lower()
        covered = [name for name, kws in SUBTOPICS.items() if any(k in text for k in kws)]
        rows.append({"url": page.result.final_url, "covered": covered, "missing": [name for name in SUBTOPICS if name not in covered], "words": page.word_count})
    rubric_points = len(SERVICE_PAGE_RUBRIC)
    score = sum(len(r["covered"]) / len(SUBTOPICS) * 100 for r in rows) / max(1, len(rows))
    confidence = 0.6
    evidence = {"pages": rows, "pages_assessed": len(rows), "used_service_pages": bool(_service_pages(ctx)),
                "method": "heuristic", "rubric_points": rubric_points}

    # Whether a page really covers "outcomes" or "proof" is a reading-comprehension call, so
    # the model counts the rubric points it actually meets. Every candidate page is read,
    # not a 12-page spread of them, so the average describes the pages rather than the sample.
    excerpts = [{"url": p.result.final_url, "excerpt": (p.text or "")}
                for p in pages if p.text]
    if excerpts:
        verdicts, coverage = await judge_all(excerpts, on11_prompt)
        if verdicts:
            covered_counts = [min(rubric_points, max(0, int(v.get("covered") or 0))) for v in verdicts]
            score = sum(c / rubric_points * 100 for c in covered_counts) / len(covered_counts)
            confidence = 0.8 if coverage.complete else 0.7
            evidence["method"] = "llm"
            evidence["llm"] = {"mean_points_covered": round(sum(covered_counts) / len(covered_counts), 2),
                               "covered_per_page": covered_counts, **coverage.as_evidence()}
        elif coverage.error:
            evidence["llm_unavailable"] = coverage.error

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
    for page in scorable_pages(ctx):
        nums = _statistics(page.text)
        rows.append({
            "url": page.result.final_url,
            "stats": nums,
            "count": len(nums),
            "major": page.page_type in major_types,
            "excerpt": (page.text or ""),
        })
    major = [r for r in rows if r["major"]] or rows
    hit = sum(1 for r in major if r["count"] >= 1)
    carry = hit / max(1, len(major))
    score = carry * 100
    confidence = 0.85
    evidence = {"pages": [{k: v for k, v in r.items() if k != "excerpt"} for r in major],
                "major_pages": len(major), "major_pages_with_a_statistic": hit,
                "pages_crawled": len(ctx.pages), "method": "heuristic"}

    # The pattern proves a statistic is PRESENT, never that it is the company's own -- a
    # figure quoted off an analyst firm satisfies it and not the parameter, which asks for
    # an ORIGINAL data point. The model judges originality on every major page that has a
    # statistic; the denominator stays every major page, so only the numerator moves.
    with_stats = [r for r in major if r["count"] >= 1]
    if with_stats:
        verdicts, coverage = await judge_all(
            [{"url": r["url"], "statistics": r["stats"], "excerpt": r["excerpt"]} for r in with_stats],
            on12_prompt,
        )
        if verdicts:
            original = sum(1 for v in verdicts if v.get("original"))
            score = carry * (original / len(verdicts)) * 100
            confidence = 0.8 if coverage.complete else 0.7
            evidence["method"] = "llm"
            evidence["llm"] = {"pages_with_a_statistic": hit, "judged_original": original,
                               **coverage.as_evidence()}
        elif coverage.error:
            evidence["llm_unavailable"] = coverage.error

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
    signals = await asyncio.to_thread(_collect_thought_leadership_signals, scorable_pages(ctx))
    score = 80.0 if signals else 30.0
    confidence = 0.85
    evidence = {"signals": signals, "author_signals_total": len(signals),
                "pages_crawled": len(ctx.pages), "method": "heuristic"}

    # Presence of a .byline element is not the parameter, which asks for a NAMED person with
    # a role and real credentials. "Posted by Admin" satisfies the selector and nothing else,
    # so the model reads every byline found and decides.
    bylines = [{"url": s["url"], "byline": s["byline"]} for s in signals if s.get("byline")]
    evidence["bylines_found"] = len(bylines)
    if bylines:
        verdicts, coverage = await judge_all(bylines, on13_prompt)
        if verdicts:
            named = sum(1 for v in verdicts if v.get("named_person"))
            with_role = sum(1 for v in verdicts if v.get("named_person") and v.get("has_role_or_credentials"))
            # Half the marks for naming a real person, half for stating their role. Floored
            # at the no-byline score: markup that names nobody is no worse than no markup.
            score = max(30.0, named / len(verdicts) * 50 + with_role / len(verdicts) * 50)
            confidence = 0.8 if coverage.complete else 0.7
            evidence["method"] = "llm"
            evidence["llm"] = {"named_people": named, "with_role_or_credentials": with_role,
                               **coverage.as_evidence()}
        elif coverage.error:
            evidence["llm_unavailable"] = coverage.error

    rec = "Name an author with role, credentials and a real profile on thought-leadership pages." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_14(spec, ctx):
    t = timed()
    rows = []
    for page in scorable_pages(ctx):
        text = page.text or ""
        clients = bool(re.search(r"\b(client|customer|partner)\b", text, re.I))
        numbers = bool(re.search(r"\b\d{2,}\s?%|\b\d+\+", text))
        deploy = bool(re.search(r"deploy|implementation|production|rolled out", text, re.I))
        page_score = clients * 25 + numbers * 30 + deploy * 25 + (20 if clients and numbers else 0)
        rows.append({"url": page.result.final_url, "client": clients, "quantified": numbers, "deployment": deploy, "score": page_score})
    score = sum(r["score"] for r in rows) / max(1, len(rows))
    confidence = 0.65
    evidence = {"pages": rows, "pages_assessed": len(rows), "method": "heuristic"}

    # The regex cannot tell a NAMED client from the generic word "client", which is the
    # whole of what the parameter asks for -- and it counts any two-digit number as a
    # quantified outcome. The model re-scores every page on the same three signals with the
    # same weights, so only the reading of each signal changes, not the rubric.
    sample = [{"url": p.result.final_url, "excerpt": (p.text or "")}
              for p in scorable_pages(ctx) if p.text]
    if sample:
        verdicts, coverage = await judge_all(sample, on14_prompt)
        if verdicts:
            judged = []
            for v in verdicts:
                named, quantified, deployed = bool(v.get("names_client")), bool(v.get("quantified_outcome")), bool(v.get("deployment_detail"))
                judged.append(named * 25 + quantified * 30 + deployed * 25 + (20 if named and quantified else 0))
            score = sum(judged) / len(judged)
            confidence = 0.8 if coverage.complete else 0.7
            evidence["method"] = "llm"
            evidence["llm"] = {"named_client": sum(1 for v in verdicts if v.get("names_client")),
                               "quantified_outcome": sum(1 for v in verdicts if v.get("quantified_outcome")),
                               "deployment_detail": sum(1 for v in verdicts if v.get("deployment_detail")),
                               **coverage.as_evidence()}
        elif coverage.error:
            evidence["llm_unavailable"] = coverage.error

    rec = "Name clients, quantify outcomes and describe deployment context on case-study pages." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_15(spec, ctx):
    t = timed()
    claims = 0
    sourced = 0
    samples = []
    candidates = []
    for page in scorable_pages(ctx):
        for sent in re.split(r"(?<=[.!?])\s+", page.text or ""):
            if _statistics(sent) and len(sent.split()) > 6:
                claims += 1
                hrefs = [l for l in page.links if l["text"] and l["text"].lower()[:40] in sent.lower()]
                candidates.append({"sentence": sent[:300], "nearby_links": [l["text"] for l in hrefs]})
                if hrefs or re.search(r"source|according to|report", sent, re.I):
                    sourced += 1
                    if len(samples) < 8:
                        samples.append({"sentence": sent[:220], "sourced": True})
                elif len(samples) < 8:
                    samples.append({"sentence": sent[:220], "sourced": False})
    score = sourced / claims * 100 if claims else 35
    confidence = 0.55
    evidence = {"claims": claims, "claims_total": claims, "sourced": sourced,
                "sourced_by_pattern": sourced, "samples": samples, "method": "heuristic"}

    # The regex only establishes that something source-LIKE sits near the claim: the bare
    # word "report" anywhere in the sentence satisfies it, and a link whose anchor text
    # happens to appear in the sentence satisfies it too. Whether a reader could actually
    # go and check the source is a reading judgment, so the model makes it -- on every
    # claim found, not the first twenty-four.
    if candidates:
        verdicts, coverage = await judge_all(candidates, on15_prompt)
        if verdicts:
            attributed = sum(1 for v in verdicts if v.get("sourced"))
            score = attributed / len(verdicts) * 100
            confidence = 0.75 if coverage.complete else 0.65
            evidence["method"] = "llm"
            evidence["sourced"] = attributed
            evidence["llm"] = {"sourced": attributed, "claims_found": claims, **coverage.as_evidence()}
        elif coverage.error:
            evidence["llm_unavailable"] = coverage.error

    rec = "Cite dated primary sources next to material claims and statistics." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_17(spec, ctx):
    t = timed()
    dated = 0
    rows = []
    for page in scorable_pages(ctx):
        has = bool(page.dates.get("published") or page.dates.get("modified"))
        dated += int(has)
        rows.append({"url": page.result.final_url, "dates": page.dates})
    score = 50 + dated / max(1, len(rows)) * 50
    rec = "Show last-substantive-update dates and refresh evergreen service copy." if score < 90 else None
    # "dated_pages: 333" alone gave the grader a numerator and no denominator, so it could
    # not state the share and neither could the reader.
    return result(
        spec,
        score=score,
        evidence={
            "dated_pages": dated,
            "pages_assessed": len(rows),
            "undated_pages": len(rows) - dated,
            "dated_share": round(dated / max(1, len(rows)), 3),
            "samples": rows,
        },
        recommendation=rec,
        checked=ctx.origin,
        duration_ms=ms_since(t),
        confidence=0.6,
    )


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
    covered = await asyncio.to_thread(_journey_stage_coverage, scorable_pages(ctx), journey)
    stages = len(JOURNEY)
    score = len([s for s in JOURNEY if covered[s]]) / stages * 100
    confidence = 0.7
    evidence = {"stages": {k: v for k, v in covered.items()},
                "pages_per_stage": {k: len(v) for k, v in covered.items()},
                "covered": list(covered), "pages_crawled": len(ctx.pages), "method": "heuristic"}

    # What stage a page serves is a judgment about purpose, not vocabulary -- a pricing page
    # aids comparison whether or not it contains the word "vs". Denominator stays the six
    # stages; the pages behind them are now every page, merged across batches, because a
    # stage the site covers only on page 200 was invisible to a 60-page sample.
    titles = [{"url": p.result.final_url, "title": p.title or ""} for p in scorable_pages(ctx)]
    if titles:
        merged, coverage = await judge_one(titles, on18_prompt)
        verdict = (merged or {}).get("stages") if isinstance(merged, dict) else None
        if isinstance(verdict, dict) and verdict:
            hit = [s for s in JOURNEY if verdict.get(s)]
            score = len(hit) / stages * 100
            confidence = 0.85 if coverage.complete else 0.75
            evidence["method"] = "llm"
            evidence["covered"] = hit
            evidence["llm"] = {"stages": verdict, **coverage.as_evidence()}
        elif coverage.error:
            evidence["llm_unavailable"] = coverage.error

    rec = "Add missing journey stages — especially comparison, objection-handling and decision pages." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_19(spec, ctx):
    t = timed()
    patterns = []
    for page in scorable_pages(ctx):
        blob = f"{page.result.final_url} {page.title}".lower()
        if re.search(r"best .+(for)| vs |versus|alternative", blob):
            patterns.append({"url": page.result.final_url, "title": page.title})
    score = 80 if len(patterns) >= _COMPARISON_PAGES_FOR_CREDIT else (45 if patterns else 15)
    confidence = 0.7
    # `title_pattern_candidates` is what the regex matched; `matched_pages` is the finding.
    # They were the same key, so when the model rejected all ten candidates the evidence
    # still read "matched_pages: 10" beside a verdict of zero genuine comparison pages.
    evidence = {"title_pattern_candidates": patterns, "matched_by_title_pattern": len(patterns),
                "matched_pages": len(patterns), "pages_crawled": len(ctx.pages),
                "method": "heuristic"}

    # The token match both over- and under-selects. A blog post titled "Docker vs Podman" is
    # not a buyer comparison page, and "Choosing an ERP for manufacturers" is one while
    # matching none of the tokens. Which pages serve a high-intent comparison query is a
    # judgment about purpose, so the model makes it and the same three tiers are applied.
    sample = [{"url": p.result.final_url, "title": p.title or ""} for p in scorable_pages(ctx) if p.title]
    if sample:
        verdicts, coverage = await judge_all(sample, on19_prompt)
        if verdicts:
            found = [v for v in verdicts if v.get("comparison_page")]
            score = 80 if len(found) >= _COMPARISON_PAGES_FOR_CREDIT else (45 if found else 15)
            confidence = 0.8 if coverage.complete else 0.7
            evidence["method"] = "llm"
            # The model's count replaces the title-pattern count in the headline field, so the
            # evidence cannot say "10 matched pages" beside a verdict of zero genuine ones.
            evidence["matched_pages"] = len(found)
            evidence["llm"] = {"comparison_pages": len(found),
                               "pages": [v.get("url") for v in found], **coverage.as_evidence()}
        elif coverage.error:
            evidence["llm_unavailable"] = coverage.error

    rec = "Create Best X for Y and X vs Y pages for priority industries and services." if score < 90 else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_20(spec, ctx):
    t = timed()
    titles = [(p.result.final_url, (p.title or "").lower().strip()) for p in scorable_pages(ctx) if p.title]
    if not titles:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="No page titles were available to compare.", checked=ctx.origin, error="no page titles", duration_ms=ms_since(t))
    by_url = dict(titles)
    pairs = [p for p in near_duplicate_pairs(titles, _COMPETING_TITLE_SIMILARITY) if by_url.get(p["a"]) != by_url.get(p["b"])]
    for p in pairs:
        p["titles"] = [by_url.get(p["a"], ""), by_url.get(p["b"], "")]
    # Scored as a share of the site, not as a raw count. A fixed 12 points per pair meant
    # nine overlapping titles bottomed out the check whether the site had 20 pages or 2000.
    cannibalized = len({u for p in pairs for u in (p["a"], p["b"])})
    share = cannibalized / len(titles)
    score = band(1 - share, _CANNIBALIZATION_BANDS, _CANNIBALIZATION_FLOOR)
    confidence = 0.6
    competing_pairs = pairs
    evidence = {"overlapping_pairs": pairs, "pairs_found": len(pairs), "pages_affected": cannibalized,
                "pages_compared": len(titles), "method": "heuristic"}

    # Title overlap is a candidate filter, not the finding. A service page and the case study
    # about that service share most of their tokens without competing, and so do the same
    # service written for two different industries. The model confirms which candidates
    # really answer the same buyer question; unconfirmed pairs stop counting against the
    # site. Every candidate pair is confirmed rather than the old fixed
    # twenty -- on one real scan the model rejected 18 of the 20 it saw while the other 1,325
    # kept their heuristic verdict and the site was scored as if all 1,345 were real.
    if pairs:
        # Ranked first, so if the ceiling bites it drops the least similar pairs -- the ones
        # least likely to be genuine competition -- rather than whichever came out of the
        # index first. The whole ranked list is handed over with the ceiling as max_items, so
        # the coverage record reports the true population and marks itself capped; slicing
        # here instead would have let it report `complete: true` over 600 of 1,600 pairs.
        ranked = sorted(pairs, key=lambda p: p.get("similarity", 0), reverse=True)
        verdicts, coverage = await judge_all(
            [{"a": p["a"], "b": p["b"], "title_a": p["titles"][0], "title_b": p["titles"][1]} for p in ranked],
            on20_prompt,
        )
        if verdicts:
            confirmed = {(v.get("a"), v.get("b")) for v in verdicts if v.get("competing")}
            judged = {(v.get("a"), v.get("b")) for v in verdicts}
            # A pair the model never saw keeps its heuristic verdict rather than being
            # silently dropped; with full coverage that set is normally empty.
            competing_pairs = [p for p in pairs
                               if (p["a"], p["b"]) in confirmed or (p["a"], p["b"]) not in judged]
            cannibalized = len({u for p in competing_pairs for u in (p["a"], p["b"])})
            score = band(1 - cannibalized / len(titles), _CANNIBALIZATION_BANDS, _CANNIBALIZATION_FLOOR)
            confidence = 0.8 if coverage.complete else 0.7
            evidence.update(method="llm", overlapping_pairs=competing_pairs,
                            pairs_found=len(competing_pairs), pages_affected=cannibalized,
                            llm={"candidate_pairs": len(pairs), "confirmed_competing": len(confirmed),
                                 **coverage.as_evidence()})
        elif coverage.error:
            evidence["llm_unavailable"] = coverage.error

    rec = "Consolidate pages that compete for the same buyer question." if competing_pairs else None
    return result(spec, score=score, evidence=evidence, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t), confidence=confidence)


async def on_21(spec, ctx):
    t = timed()
    scorable = [p for p in scorable_pages(ctx) if p.page_type != "utility"]
    if not scorable:
        return result(spec, score=None, unknown=True, evidence={}, recommendation="No content pages were available to assess.", checked=ctx.origin, error="no content pages", duration_ms=ms_since(t))
    thin = [{"url": p.result.final_url, "words": p.word_count} for p in scorable if p.word_count < _THIN_PAGE_WORDS]
    excerpts = [(p.result.final_url, p.text[:400]) for p in scorable if p.text]
    dups = near_duplicate_pairs(excerpts, _NEAR_DUPLICATE_SIMILARITY)
    duplicated = {u for d in dups for u in (d["a"], d["b"])}
    # Proportional, like ON-20: the old absolute penalty (8 points per thin page, 10 per
    # duplicate pair) hit its 60-point cap at eight thin pages, so a 2000-page site with
    # eight thin pages scored identically to a 10-page site that was almost entirely thin.
    affected = len({t["url"] for t in thin} | duplicated)
    share = affected / len(scorable)
    score = band(1 - share, _DILUTION_BANDS, _DILUTION_FLOOR)
    rec = "Merge or expand thin/near-duplicate pages so topics are not diluted." if score < 90 else None
    return result(spec, score=score, evidence={"thin": thin, "thin_count": len(thin), "near_duplicates": dups, "duplicate_pairs": len(dups), "pages_affected": affected, "pages_assessed": len(scorable)}, recommendation=rec, checked=ctx.origin, duration_ms=ms_since(t))


# Both duplicate checks score the share of the site that is *clean*, so the penalty scales
# with the site instead of with a raw count of findings.
_CANNIBALIZATION_BANDS, _CANNIBALIZATION_FLOOR = rules.band("cannibalization")
_DILUTION_BANDS, _DILUTION_FLOOR = rules.band("dilution")

_GENERIC_REACH_TERMS = ("united states", "usa", "uk", "europe", "asia", "north america", "global", "worldwide", "nationwide")


async def on_22(spec, ctx):
    t = timed()
    geos = tuple(derive_site_geographies(ctx)) + _GENERIC_REACH_TERMS
    cats = tuple(derive_site_categories(ctx))
    rows = []
    for page in scorable_pages(ctx):
        # Title and URL count as the page naming the brand, not just body text. Reading
        # page.text alone reported `brand: false` for the audited company's own homepage --
        # whose <title> is the brand -- because the extractor had dropped the header, and the
        # parameter then scored 0 on an extraction artefact rather than on the site.
        blob = " ".join(filter(None, (page.text, page.title, page.result.final_url))).lower()
        brand = any(b in blob for b in ctx.brand_terms)
        cat = any(c in blob for c in cats)
        geo = any(g in blob for g in geos)
        rows.append({"url": page.result.final_url, "brand": brand, "category": cat, "geo": geo})
    complete = sum(1 for r in rows if r["brand"] and r["category"] and r["geo"])
    score = complete / max(1, len(rows)) * 100
    rec = "Name the brand together with category, industry and geography on key pages." if score < 90 else None
    return result(
        spec,
        score=score,
        evidence={
            "pages": rows,
            "pages_assessed": len(rows),
            "pages_with_all_three": complete,
            "pages_naming_brand": sum(1 for r in rows if r["brand"]),
            "pages_naming_category": sum(1 for r in rows if r["category"]),
            "pages_naming_geography": sum(1 for r in rows if r["geo"]),
            "brand_terms": list(ctx.brand_terms),
            "categories_derived": list(cats),
        },
        recommendation=rec,
        checked=ctx.origin,
        duration_ms=ms_since(t),
        confidence=0.7,
    )


HANDLERS = {
    "ON-01": on_01, "ON-02": on_02, "ON-03": on_03, "ON-04": on_04, "ON-05": on_05, "ON-5.1": on_05_1,
    "ON-07": on_07, "ON-08": on_08, "ON-10": on_10, "ON-11": on_11, "ON-12": on_12,
    "ON-13": on_13, "ON-14": on_14, "ON-15": on_15, "ON-17": on_17, "ON-18": on_18,
    "ON-19": on_19, "ON-20": on_20, "ON-21": on_21, "ON-22": on_22,
}

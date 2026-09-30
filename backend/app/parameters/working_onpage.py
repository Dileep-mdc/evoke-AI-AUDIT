"""One plain-English sentence per on-page (ON-*) parameter, saying how its score came about.

Each function takes a stored row's evidence dict and returns ``(text, recomputed_score)``:
what was found, with the real measured numbers, and how that becomes the rules-based score,
plus the number that working yields. It is rebuilt from the evidence alone, so it works on
any stored scan, and it returns None rather than guess when the evidence does not carry what
the calculation needs (or the row was never scored). The words avoid maths symbols so
anyone can follow them.

Band tables and thresholds come from scoring_rules.json via ``rules``, the same source the
handlers in onpage.py read, so the text cannot drift from the scoring.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from . import rules

Result = Optional[tuple[str, float]]

_JOURNEY_STAGES = ("awareness", "qualification", "comparison", "objection", "trust", "decision")


# ---------------------------------------------------------------- formatting helpers

def _n(value: Any) -> str:
    """An integer count with thousands separators."""
    return f"{int(value):,}"


def _s(value: float) -> str:
    """A score formatted like the stored one: 1 decimal, trailing .0 dropped."""
    text = f"{round(float(value), 1):,.1f}"
    return text[:-2] if text.endswith(".0") else text


def _so(score: float) -> str:
    return f"so it scores {_s(score)}."


def _pct(share: float) -> str:
    return f"{share * 100:.1f}%"


def _pct_threshold(threshold: float) -> str:
    return f"{threshold * 100:g}%"


def _v(n: int, one: str, many: str) -> str:
    """The verb that agrees with a count: 1 page has, 2 pages have."""
    return one if n == 1 else many


def _and(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _num(ev: dict, key: str) -> Optional[float]:
    value = ev.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _ai(ev: dict) -> Optional[dict]:
    """The in-handler AI block, only when the handler says the AI verdicts produced the score."""
    llm = ev.get("llm")
    if ev.get("method") == "llm" and isinstance(llm, dict):
        return llm
    return None


def _coverage(llm: dict, noun: str = "") -> str:
    """'' when the AI saw everything, else a short note saying how much of it the AI checked.

    With no noun the note assumes the sentence already counts what the AI checked and adds
    only the total ("out of 972"); with a noun it names both, for sentences that count
    something else (heading occurrences, pages found, stages covered).
    """
    if llm.get("complete") is not False:
        return ""
    assessed, population = llm.get("assessed"), llm.get("population")
    if not isinstance(assessed, int) or not isinstance(population, int):
        return ""
    why = ("the AI checks at most that many in one scan" if llm.get("capped_at") and not llm.get("batches_failed")
           else "the rest could not be checked this time")
    if not noun:
        return f" (out of {_n(population)}; {why})"
    return f" (the AI checked {_n(assessed)} of {_n(population)} {noun}; {why})"


def _band_higher(value: float, name: str) -> tuple[float, str]:
    """Score and a phrase naming the band reached and the next one up, higher-is-better."""
    bands, floor = rules.band(name)
    for i, (minimum, points) in enumerate(bands):
        if value >= minimum:
            text = f"{_pct_threshold(minimum)} or more earns {_s(points)}"
            if i:
                up_min, up_points = bands[i - 1]
                text += f" ({_pct_threshold(up_min)} or more would earn {_s(up_points)})"
            return float(points), text
    if not bands:
        return float(floor), f"the minimum is {_s(floor)}"
    low_min, low_points = bands[-1]
    return float(floor), (f"under {_pct_threshold(low_min)} gets the minimum of {_s(floor)}"
                          f" ({_pct_threshold(low_min)} or more would earn {_s(low_points)})")


def _band_lower(value: float, name: str) -> tuple[float, str]:
    """Score and a phrase naming the band reached, lower-is-better (keyword density)."""
    bands, floor = rules.band(name)
    for i, (ceiling, points) in enumerate(bands):
        if value < ceiling:
            text = f"under {_pct_threshold(ceiling)} earns {_s(points)}"
            if i:
                best_ceiling, best_points = bands[i - 1]
                text += f" (under {_pct_threshold(best_ceiling)} would earn {_s(best_points)})"
            return float(points), text
    if not bands:
        return float(floor), f"the minimum is {_s(floor)}"
    top_ceiling, top_points = bands[-1]
    return float(floor), (f"{_pct_threshold(top_ceiling)} or more gets the minimum of {_s(floor)}"
                          f" (under {_pct_threshold(top_ceiling)} would earn {_s(top_points)})")


def _ratio(num: float, den: float, lead: str, note: str = "") -> Result:
    """'<lead><note>, so it scores N.' for the many plain share-of-items parameters."""
    if not den:
        return None
    score = num / den * 100
    return f"{_cap(lead)}{note}, {_so(score)}", score


def _safe(fn: Callable[[dict], Result]) -> Callable[[dict], Result]:
    """Never raise, and never describe a row the rules-based pass could not score."""
    def wrapper(ev: dict) -> Result:
        try:
            if not isinstance(ev, dict):
                return None
            if "rules_based_score" in ev and ev.get("rules_based_score") is None:
                return None
            if ev.get("llm_skipped"):
                return None
            out = fn(ev)
            if out is None:
                return None
            text, score = out
            return str(text), float(score)
        except Exception:
            return None
    wrapper.__name__ = getattr(fn, "__name__", "working")
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ---------------------------------------------------------------- parameters

@_safe
def on_01(ev: dict) -> Result:
    if not ev.get("eligible_headings"):
        return "No headings on service or solution pages could be checked, so it gets a set score of 30.", 30.0
    llm = _ai(ev)
    if llm is not None:
        hits, total = llm.get("buyer_questions_by_occurrence"), llm.get("heading_occurrences_counted")
        if not isinstance(hits, int) or not isinstance(total, int):
            return None
        share = hits / total if total else 0.0
        score, band = _band_higher(share, "question_headings")
        return (f"The AI found {_n(hits)} of {_n(total)} headings ({_pct(share)}) {_v(hits, 'is a real buyer question', 'are real buyer questions')}"
                f"{_coverage(llm, 'distinct headings')}. {_cap(band)}, {_so(score)}"), score
    hits, total = _num(ev, "question_headings_by_pattern"), _num(ev, "eligible_headings")
    if hits is None or not total:
        return None
    share = hits / total
    score, band = _band_higher(share, "question_headings")
    return (f"{_n(hits)} of {_n(total)} headings ({_pct(share)}) {_v(hits, 'is', 'are')} phrased as questions. {_cap(band)},"
            f" {_so(score)}"), score


@_safe
def on_02(ev: dict) -> Result:
    answers = ev.get("answers")
    if not isinstance(answers, list):
        return None
    if not answers:
        return "There are no question headings with answers to rate, so it gets a set score of 25.", 25.0
    total = ev.get("answers_total", len(answers))
    if total != len(answers):
        return None  # only a sample of the answers is stored
    groups: dict[float, int] = {}
    for a in answers:
        pts = a.get("score")
        if not isinstance(pts, (int, float)):
            return None
        groups[pts] = groups.get(pts, 0) + 1
    score = sum(p * c for p, c in groups.items()) / len(answers)
    parts = _and([f"{_n(c)} scored {_s(p)}" for p, c in sorted(groups.items(), reverse=True)])
    empty = ev.get("answers_with_no_extracted_text")
    extra = f", {_n(empty)} of them with no text at all" if isinstance(empty, int) and empty else ""
    word = "answer" if len(answers) == 1 else "answers"
    return (f"{_n(len(answers))} {word} under question headings {_v(len(answers), 'was', 'were')} rated on how direct and complete each one is"
            f" ({parts}{extra}), so the average score is {_s(score)}."), score


@_safe
def on_03(ev: dict) -> Result:
    found = ev.get("concepts_found_on_site")
    if found == 0:
        return "None of the site's key concepts appear in its page text, so it gets a set score of 30.", 30.0
    llm = _ai(ev)
    if llm is not None:
        results = llm.get("results")
        if not isinstance(results, list) or not results:
            return None
        defined = sum(1 for r in results if isinstance(r, dict) and r.get("defined"))
        return _ratio(defined, len(results),
                      f"of {_n(len(results))} key concepts the site talks about, the AI found {_n(defined)} clearly defined",
                      _coverage(llm))
    concepts = ev.get("concepts")
    if not isinstance(concepts, list) or not concepts:
        return None
    defined = sum(1 for c in concepts if isinstance(c, dict) and c.get("defined"))
    return _ratio(defined, len(concepts),
                  f"of {_n(len(concepts))} key concepts the site talks about, {_n(defined)} have a clear definition")


@_safe
def on_04(ev: dict) -> Result:
    llm = _ai(ev)
    if llm is not None:
        leads, judged = llm.get("leads_with_substance"), llm.get("assessed")
        if not isinstance(leads, int) or not judged:
            return None
        return _ratio(leads, judged, f"the AI found {_n(leads)} of {_n(judged)} page openings {_v(leads, 'gets', 'get')} straight to the point",
                      _coverage(llm))
    pages = ev.get("pages")
    if not isinstance(pages, list):
        return None
    if not pages:
        return "There were no pages to check, so it gets a set score of 40.", 40.0
    score = sum(float(p.get("score")) for p in pages) / len(pages)
    return (f"{_n(len(pages))} page openings were rated 85 if concrete, 55 if mixed and 35 if generic, so the"
            f" average score is {_s(score)}."), score


@_safe
def on_05(ev: dict) -> Result:
    total, hits = _num(ev, "service_pages_total"), _num(ev, "service_pages_with_faq")
    if not total or hits is None:
        return None
    return _ratio(hits, total, f"{_n(hits)} of {_n(total)} service or solution pages {_v(hits, 'has', 'have')} an FAQ section or FAQ structured data")


@_safe
def on_06(ev: dict) -> Result:
    total = _num(ev, "faq_pages_total")
    if total == 0:
        return "No page has an FAQ, either visible or in structured data, so it gets a set score of 20.", 20.0
    schema, visible = _num(ev, "with_valid_schema"), _num(ev, "with_visible_faq")
    if not total or schema is None or visible is None:
        return None
    score = (50 * schema + 50 * visible) / total
    return (f"Of {_n(total)} pages with an FAQ, {_n(schema)} {_v(schema, 'has', 'have')} FAQ structured data and {_n(visible)} {_v(visible, 'shows', 'show')} the FAQ"
            f" on the page. Each counts for half, {_so(score)}"), score


@_safe
def on_07(ev: dict) -> Result:
    if ev.get("eligible_pages_total") == 0:
        return "No pages compare options for buyers, so it gets a set score of 40.", 40.0
    llm = _ai(ev)
    if llm is not None:
        judged, tables = llm.get("evaluation_pages"), llm.get("with_tables")
        if not judged or not isinstance(tables, int):
            return None
        return _ratio(tables, judged, f"of {_n(judged)} pages that compare options, {_n(tables)} {_v(tables, 'includes', 'include')} a real table",
                      _coverage(llm, "pages"))
    pages = ev.get("pages")
    if not isinstance(pages, list) or not pages:
        return None
    if ev.get("eligible_pages_total", len(pages)) != len(pages):
        return None
    tables = sum(1 for p in pages if (p.get("tables") or 0) > 0)
    return _ratio(tables, len(pages), f"of {_n(len(pages))} pages that compare options, {_n(tables)} {_v(tables, 'includes', 'include')} a real table")


@_safe
def on_08(ev: dict) -> Result:
    if ev.get("lists") == 0:
        return "No content lists were found outside the menus, so it gets a set score of 35.", 35.0
    llm = _ai(ev)
    if llm is not None:
        useful, judged = llm.get("useful"), llm.get("assessed")
        if not isinstance(useful, int) or not judged:
            return None
        return _ratio(useful, judged, f"the AI found {_n(useful)} of {_n(judged)} content lists genuinely useful",
                      _coverage(llm))
    useful, total = _num(ev, "useful_by_keyword"), _num(ev, "lists_total")
    if useful is None:
        useful = _num(ev, "useful")
    if useful is None or not total:
        return None
    return _ratio(useful, total, f"{_n(useful)} of {_n(total)} content lists {_v(useful, 'looks', 'look')} useful from their wording")


@_safe
def on_09(ev: dict) -> Result:
    avg = _num(ev, "average_max_density")
    pages = _num(ev, "pages_assessed")
    if avg is None:
        return None
    score, band = _band_lower(avg, "keyword_density")
    skipped = ev.get("pages_skipped_too_few_content_words")
    extra = (f", {_n(skipped)} short pages skipped" if isinstance(skipped, int) and skipped else "")
    over = f" ({_n(pages)} pages checked{extra})" if pages else ""
    return (f"On average the most repeated word makes up {avg * 100:.1f}% of a page's words{over}."
            f" {_cap(band)}, {_so(score)}"), score


@_safe
def on_10(ev: dict) -> Result:
    llm = _ai(ev)
    points = ev.get("rubric_points") or 8
    if llm is not None:
        covered = llm.get("covered_per_page")
        if not isinstance(covered, list) or not covered:
            return None
        total = sum(int(c) for c in covered)
        score = total / (len(covered) * points) * 100
        mean = total / len(covered)
        return (f"The AI checked {_n(len(covered))} service pages{_coverage(llm)} against"
                f" {_n(points)} things a buyer needs to know. On average each page covers {mean:.1f} of {_n(points)},"
                f" {_so(score)}"), score
    pages = ev.get("pages")
    if not isinstance(pages, list) or not pages:
        return None
    total = 0
    denom = None
    for p in pages:
        cov, miss = p.get("covered") or [], p.get("missing") or []
        denom = denom or (len(cov) + len(miss))
        total += len(cov)
    if not denom:
        return None
    score = total / (len(pages) * denom) * 100
    return (f"{_n(len(pages))} service pages were checked against {_n(denom)} things a buyer needs to know. On"
            f" average each page covers {total / len(pages):.1f} of {_n(denom)}, {_so(score)}"), score


@_safe
def on_11(ev: dict) -> Result:
    major, hit = _num(ev, "major_pages"), _num(ev, "major_pages_with_a_statistic")
    if not major or hit is None:
        return None
    llm = _ai(ev)
    if llm is not None:
        original, judged = llm.get("judged_original"), llm.get("assessed")
        if not isinstance(original, int) or not judged:
            return None
        score = (hit / major) * (original / judged) * 100
        return (f"{_n(hit)} of {_n(major)} key pages {_v(hit, 'includes', 'include')} a statistic, and the AI judged {_n(original)} of"
                f" {_n(judged)} of those statistics original{_coverage(llm, 'pages with a statistic')}. Counting only"
                f" the original ones, it scores {_s(score)}."), score
    note = " (the AI was unavailable, so originality was not checked)" if ev.get("llm_unavailable") else ""
    return _ratio(hit, major, f"{_n(hit)} of {_n(major)} key pages {_v(hit, 'includes', 'include')} a statistic", note)


@_safe
def on_12(ev: dict) -> Result:
    llm = _ai(ev)
    if llm is not None:
        named, role, judged = llm.get("named_people"), llm.get("with_role_or_credentials"), llm.get("assessed")
        if not isinstance(named, int) or not isinstance(role, int) or not judged:
            return None
        raw = named / judged * 50 + role / judged * 50
        score = max(30.0, raw)
        lead = (f"Of {_n(judged)} bylines{_coverage(llm)}, {_n(named)} {_v(named, 'names', 'name')} a real person and {_n(role)}"
                f" also {_v(role, 'gives', 'give')} their role. A name and a role count for half each")
        if raw < 30:
            return f"{lead}, which gives {_s(raw)}, but the minimum is 30, so it scores 30.", score
        return f"{lead}, {_so(score)}", score
    signals = _num(ev, "author_signals_total")
    if signals is None:
        return None
    if signals:
        return f"{_n(signals)} author signals were found (the AI could not check them), so it scores 80.", 80.0
    return "No author byline or author markup was found on any page, so it scores 30.", 30.0


@_safe
def on_13(ev: dict) -> Result:
    llm = _ai(ev)
    if llm is not None:
        named, quant, deploy, judged = (llm.get("named_client"), llm.get("quantified_outcome"),
                                        llm.get("deployment_detail"), llm.get("assessed"))
        if not all(isinstance(v, int) for v in (named, quant, deploy)) or not judged:
            return None
        if named and quant:
            return None  # the +20 bonus needs the count of pages with BOTH, which is not stored
        score = (named * 25 + quant * 30 + deploy * 25) / judged
        return (f"Of {_n(judged)} proof pages{_coverage(llm)}, {_n(named)} {_v(named, 'names', 'name')} a client (25 points),"
                f" {_n(quant)} {_v(quant, 'gives', 'give')} a measured result (30 points) and {_n(deploy)}"
                f" {_v(deploy, 'describes', 'describe')} the work done (25 points)."
                f" Averaged over all {_n(judged)} pages, it scores {_s(score)}."), score
    pages = ev.get("pages")
    if not isinstance(pages, list) or not pages:
        return None
    score = sum(float(p.get("score")) for p in pages) / len(pages)
    return (f"{_n(len(pages))} proof pages were rated on naming a client, giving a measured result and describing"
            f" the work done, so the average score is {_s(score)}."), score


@_safe
def on_14(ev: dict) -> Result:
    claims = _num(ev, "claims_total")
    if claims is None:
        claims = _num(ev, "claims")
    if claims == 0:
        return "No statistics or claims were found, so it gets a set score of 35.", 35.0
    llm = _ai(ev)
    if llm is not None:
        sourced, judged = llm.get("sourced"), llm.get("assessed")
        if not isinstance(sourced, int) or not judged:
            return None
        return _ratio(sourced, judged, f"the AI found {_n(sourced)} of {_n(judged)} claims {_v(sourced, 'points', 'point')} to a source a reader could check",
                      _coverage(llm))
    sourced = _num(ev, "sourced_by_pattern")
    if sourced is None or not claims:
        return None
    return _ratio(sourced, claims, f"{_n(sourced)} of {_n(claims)} claims {_v(sourced, 'links to or names', 'link to or name')} a source")


@_safe
def on_15(ev: dict) -> Result:
    dated, pages = _num(ev, "dated_pages"), _num(ev, "pages_assessed")
    if dated is None or pages is None:
        return None
    share_pts = dated / max(1, pages) * 50
    score = 50 + share_pts
    return (f"{_n(dated)} of {_n(pages)} pages {_v(dated, 'shows', 'show')} a published or updated date. Every site starts at 50 and gains"
            f" up to 50 more for dated pages ({_s(share_pts)} here), {_so(score)}"), score


@_safe
def on_16(ev: dict) -> Result:
    llm = _ai(ev)
    if llm is not None:
        stages = llm.get("stages")
        if not isinstance(stages, dict):
            return None
        hit = [s for s in _JOURNEY_STAGES if stages.get(s)]
        who = f"The AI found content for {len(hit)} of the 6 buying stages{_coverage(llm, 'pages')}"
    else:
        per = ev.get("pages_per_stage")
        if not isinstance(per, dict):
            return None
        hit = [s for s in _JOURNEY_STAGES if per.get(s)]
        who = f"A wording check found content for {len(hit)} of the 6 buying stages"
    score = len(hit) / 6 * 100
    missing = [s for s in _JOURNEY_STAGES if s not in hit]
    gap = f" (missing: {_and(missing)})" if missing and hit else ""
    return f"{who}{gap}, {_so(score)}", score


@_safe
def on_17(ev: dict) -> Result:
    need = rules.threshold("comparison_pages_for_full_credit")
    llm = _ai(ev)
    if llm is not None:
        found = llm.get("comparison_pages")
        if not isinstance(found, int):
            found = ev.get("matched_pages")
        who = f"The AI found {{}} genuine comparison pages{_coverage(llm, 'page titles')}"
    else:
        found = ev.get("matched_by_title_pattern", ev.get("matched_pages"))
        who = "{} pages look like comparison pages from their titles"
    if not isinstance(found, int):
        return None
    score = 80.0 if found >= need else 45.0 if found else 15.0
    return (f"{who.format(_n(found))}. {need} or more earns 80, 1 to {need - 1} earns 45 and none earns 15,"
            f" {_so(score)}"), score


@_safe
def on_18(ev: dict) -> Result:
    affected, compared = _num(ev, "pages_affected"), _num(ev, "pages_compared")
    if affected is None or not compared:
        return None
    clean = 1 - affected / compared
    score, band = _band_higher(clean, "cannibalization")
    pairs = ev.get("pairs_found")
    llm = _ai(ev)
    note = ""
    if llm is not None:
        conf = llm.get("confirmed_competing")
        if isinstance(pairs, int) and isinstance(conf, int) and pairs != conf:
            note = f" (the AI confirmed {_n(conf)}; the {_n(pairs - conf)} it could not check still count)"
        elif isinstance(conf, int):
            note = " (confirmed by the AI)"
    lead = (f"{_n(pairs)} pairs of pages have near-identical titles{note}, affecting {_n(affected)} of {_n(compared)} pages"
            if isinstance(pairs, int) else f"{_n(affected)} of {_n(compared)} pages {_v(affected, 'has', 'have')} a near-identical title")
    return f"{lead}, so {_pct(clean)} of pages are clean. {_cap(band)}, {_so(score)}", score


@_safe
def on_19(ev: dict) -> Result:
    affected, pages = _num(ev, "pages_affected"), _num(ev, "pages_assessed")
    if affected is None or not pages:
        return None
    clean = 1 - affected / pages
    score, band = _band_higher(clean, "dilution")
    thin = ev.get("thin_count")
    dups = ev.get("near_duplicates")
    parts = []
    if isinstance(thin, int):
        parts.append(f"{_n(thin)} thin")
    if isinstance(dups, list):
        in_pairs = len({u for d in dups if isinstance(d, dict) for u in (d.get("a"), d.get("b")) if u})
        parts.append(f"{_n(in_pairs)} near-copies of another page")
    detail = f" ({', '.join(parts)})" if parts else ""
    return (f"{_n(affected)} of {_n(pages)} content pages {_v(affected, 'is', 'are')} thin or near-copies{detail}, so {_pct(clean)} are clean."
            f" {_cap(band)}, {_so(score)}"), score


@_safe
def on_20(ev: dict) -> Result:
    hits, pages = _num(ev, "pages_with_all_three"), _num(ev, "pages_assessed")
    if hits is None or not pages:
        return None
    b, c, g = ev.get("pages_naming_brand"), ev.get("pages_naming_category"), ev.get("pages_naming_geography")
    extra = (f" (the brand is on {_n(b)}, a service category on {_n(c)} and a location on {_n(g)})"
             if all(isinstance(v, int) for v in (b, c, g)) else "")
    return _ratio(hits, pages, f"{_n(hits)} of {_n(pages)} pages {_v(hits, 'names', 'name')} the brand, a service category and a location together{extra}")


WORKING: dict[str, Callable[[dict], Result]] = {
    "ON-01": on_01, "ON-02": on_02, "ON-03": on_03, "ON-04": on_04, "ON-05": on_05, "ON-06": on_06,
    "ON-07": on_07, "ON-08": on_08, "ON-09": on_09, "ON-10": on_10, "ON-11": on_11,
    "ON-12": on_12, "ON-13": on_13, "ON-14": on_14, "ON-15": on_15, "ON-16": on_16,
    "ON-17": on_17, "ON-18": on_18, "ON-19": on_19, "ON-20": on_20,
}

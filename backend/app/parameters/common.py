from __future__ import annotations

import math
import re
import time
from typing import Any, Optional

from ..errors import humanize_error
from . import rules

_ID_DIGITS_RE = re.compile(r"(\d+)")

def parameter_sort_key(parameter_id: str) -> tuple:
    """Order parameter ids the way they read rather than the way they sort as text.

    Ids are PREFIX-NN, which sorts correctly as a plain string -- until one is sub-numbered
    (ON-5.1 was, before the renumbering): as text it lands after ON-20 and before TECH-01;
    comparing its digit runs as numbers puts it back where it belongs, after ON-05.
    """
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in _ID_DIGITS_RE.split(parameter_id or "")
        if part
    )


def evidence_summary(spec: dict, evidence: dict, *, score: Optional[float], error: Optional[str], unknown: bool) -> str:
    if error:
        return humanize_error(error) or "This source was unavailable."
    if unknown or score is None:
        return "This check could not be completed because a required source was unavailable."
    lists = [(k, v) for k, v in (evidence or {}).items() if isinstance(v, list)]
    empty_lists = [k.replace("_", " ") for k, v in lists if not v]
    filled = sum(len(v) for _, v in lists)
    name = spec.get("name") or "This parameter"
    if empty_lists and filled == 0:
        return f"{name}: nothing matching was found on the crawled pages."
    if score >= 90:
        return f"{name} passed. Supporting details from the live crawl are listed below."
    if score >= 60:
        return f"{name} is partial. The crawl found some signals, with gaps listed below."
    return f"{name} did not meet the pass threshold. What was inspected is listed below."


def enrich_evidence(spec: dict, evidence: dict, *, score: Optional[float], error: Optional[str], unknown: bool) -> dict:
    ev = dict(evidence or {})
    if not ev.get("summary"):
        ev["summary"] = evidence_summary(spec, ev, score=score, error=error, unknown=unknown)
    if error and "error_detail" not in ev:
        ev["error_detail"] = humanize_error(error)
    return ev

QUESTION_RE = re.compile(
    r"^\s*(how|what|why|when|where|which|who|can|should|is|are|do|does|will)\b|[?？]\s*$",
    re.I,
)


def status_from_score(score: Optional[float], pass_at: int = 90, partial_at: int = 60, unknown: bool = False) -> str:
    if unknown or score is None:
        return "UNKNOWN"
    if score >= pass_at:
        return "PASS"
    if score >= partial_at:
        return "PARTIAL"
    return "FAIL"


def result(
    spec: dict,
    *,
    score: Optional[float],
    evidence: dict,
    recommendation: Optional[str],
    checked: str,
    error: Optional[str] = None,
    confidence: float = 0.85,
    duration_ms: int = 0,
    unknown: bool = False,
    pass_at: Optional[int] = None,
    partial_at: Optional[int] = None,
    links: Optional[list] = None,
) -> dict:
    pa = pass_at if pass_at is not None else spec.get("pass_threshold", 90)
    pb = partial_at if partial_at is not None else spec.get("partial_threshold", 60)
    friendly = humanize_error(error)
    unknown = bool(unknown or score is None)
    st = "UNKNOWN" if unknown else status_from_score(score, pa, pb)
    row = _row(spec, st, score, confidence, checked, evidence, friendly, unknown, recommendation, duration_ms)
    if links:
        row[CHECKED_LINKS] = merge_links(links)
    return row


# The links a check read, with why, kept on the row beside the evidence -- never inside it. The
# evidence is what the model grades and what the report stores; a thousand-URL list in it would be
# sent to the model and shipped to the dashboard on every report view. parameter_links.py writes
# these to the per-company workbook and strips them before the report is built.
CHECKED_LINKS = "checked_links"


def link(url: Optional[str], why: str) -> Optional[dict]:
    return {"url": url.strip(), "why": why} if isinstance(url, str) and url.strip() else None


def page_links(pages, why: str) -> list[dict]:
    """One link per crawled page, by the URL it was finally read from (after redirects)."""
    out = []
    for page in pages or []:
        res = getattr(page, "result", None)
        out.append(link((getattr(res, "final_url", None) or getattr(page, "url", None)), why))
    return [x for x in out if x]


def url_links(urls, why: str) -> list[dict]:
    return [x for x in (link(u, why) for u in urls or []) if x]


def merge_links(links) -> list[dict]:
    """Deduplicated by URL in first-seen order; a URL read for two reasons keeps both."""
    merged: dict[str, list[str]] = {}
    for item in links or []:
        if not item:
            continue
        reasons = merged.setdefault(item["url"], [])
        if item["why"] not in reasons:
            reasons.append(item["why"])
    return [{"url": url, "why": " Also: ".join(reasons)} for url, reasons in merged.items()]


def _row(spec, st, score, confidence, checked, evidence, friendly, unknown, recommendation, duration_ms) -> dict:
    return {
        "parameter_id": spec["parameter_id"],
        "section": spec["section"],
        "name": spec["name"],
        "status": st,
        "score": None if st == "UNKNOWN" else round(float(score), 1),
        "max_score": spec.get("max_score", 100),
        "weight": spec.get("weight", 1.0),
        "confidence": confidence if st != "UNKNOWN" else 0.0,
        "checked_url_or_source": checked,
        "evidence": enrich_evidence(spec, evidence, score=score, error=friendly, unknown=unknown),
        "recommendation": recommendation if st != "PASS" else None,
        "error": friendly,
        "duration_ms": duration_ms,
    }


# What to do when a check found none of what it looks for -- the recommendation on its FAIL row.
NO_DATA_ADVICE = {
    "TECH-11": "Add substantial body text to the key pages, broken up by a descriptive heading every 150-200 words.",
    "TECH-14": "Add relevant images with descriptive alt text to the key pages, and a transcript for any video.",
    "TECH-17": "Add structured data to the key pages: Organization on the home and about pages, Service or Product on offering pages, Article on articles.",
    "TECH-21": "If the site serves several countries or languages, add hreflang tags that link each language version to the others.",
    "ON-01": "Add descriptive H1-H3 headings to the key pages, phrased the way buyers ask about the service.",
    "ON-02": "Add question-style headings (How / What / Why ...) with a direct 40-60 word answer under each.",
    "ON-03": "Define each core service or concept in one or two plain sentences where it is first mentioned.",
    "ON-04": "Open each key page with two plain sentences that say what the page offers and for whom.",
    "ON-06": "Add an FAQ section to the key service pages and mark it up with FAQPage structured data.",
    "ON-07": "Add pages where buyers compare options (features, alternatives, pricing) and present them in real HTML comparison tables.",
    "ON-13": "Publish client stories that name the client, give measurable results and describe how the work was delivered.",
    "ON-14": "Back the site's claims with specific figures, each linked to the study, report or data behind it.",
    "ON-18": "Give every page its own distinct, descriptive title.",
    "ON-20": "State the brand name together with its service categories in page titles, headings and opening copy.",
    "OFF-05": "Get the company listed and reviewed on the review platforms buyers in this market use.",
    "OFF-06": "Ask clients for reviews on the review platforms buyers in this market use, and keep them recent.",
}


NOT_APPLICABLE = "N/A"


def not_applicable(spec: dict, *, reason: str, checked: str, evidence: Optional[dict] = None,
                   duration_ms: int = 0, links: Optional[list] = None) -> dict:
    """A check that does not apply to this site: status N/A, no score, left out of the pillar score.

    Different from no_data(), which fails a site for lacking something it should have. N/A is only
    for a check whose question does not arise: hreflang on a site that serves one market, IT
    certification tiers for a medical association. `reason` says why, and is shown in the report.
    The evidence key is `does_not_apply`, not the retired `not_applicable`/`na_reason`, so
    upgrade_na_row() leaves these rows as they are.
    """
    ev = {**(evidence or {}), "does_not_apply": reason, "retry_pointless": True,
          "summary": f"Not applicable: {reason}."}
    row = result(spec, score=None, unknown=True, evidence=ev, recommendation=None, checked=checked,
                 duration_ms=duration_ms, links=links)
    row.update(status=NOT_APPLICABLE, confidence=0.9)
    return row


def no_data(spec: dict, *, reason: str, checked: str, evidence: Optional[dict] = None,
            recommendation: Optional[str] = None, duration_ms: int = 0, links: Optional[list] = None) -> dict:
    """A check that found none of what it looks for on the site: a FAIL at 0, with the fix.

    Nothing to evaluate is scored as missing, not left out: an FAQ check on a site with no FAQs
    fails, and counts in the pillar score like any other failure. `reason` says what was missing.
    """
    ev = {**(evidence or {}), "no_data": True, "no_data_reason": reason, "summary": f"Failed: {reason}."}
    advice = recommendation or NO_DATA_ADVICE.get(spec["parameter_id"]) or f"Add what this check looks for: {spec['name'].lower()}."
    return result(spec, score=0.0, evidence=ev, recommendation=advice, checked=checked, duration_ms=duration_ms,
                  confidence=0.9, links=links)


def upgrade_na_row(row: dict) -> bool:
    """Turn a row saved as N/A (before N/A was retired) into what it is scored as now; True if changed.

    Rows the N/A helper wrote (they carry `na_reason`) become a no-data FAIL at 0. Two kinds stay
    unscored instead, because nothing about the site was ever measured: a check the industry profile
    skipped without running it (evidence `industry`), which a re-check can now run for real, and
    TECH-12's "no rendered copy was fetched", a gap in the crawl rather than in the site.
    """
    ev = row.get("evidence") or {}
    if not isinstance(ev, dict):
        return False
    if ev.get("no_data") and ("industry" in ev or re.search(r", so | \(missing ", ev.get("no_data_reason") or "")):
        # Converted by the first version of this upgrade, which kept those two cases as they were.
        ev = {k: v for k, v in ev.items() if k not in ("no_data", "no_data_reason", "summary")}
        ev["na_reason"] = row["evidence"]["no_data_reason"]
    elif not ev.get("not_applicable"):
        return False
    ev = {k: v for k, v in ev.items() if k != "not_applicable"}
    reason = ev.pop("na_reason", None)
    if reason is None:
        row["evidence"] = {**ev, "retry_pointless": True}
        return True
    if "industry" in ev:
        row.update(status="UNKNOWN", score=None, evidence={"summary": "Not measured yet: run a re-check to measure it."},
                   recommendation=None, error="Not measured yet: run a re-check to measure it.")
        return True
    # The old wording explained why nothing was scored ("..., so there was no FAQ to validate");
    # the finding is the part before that.
    reason = re.split(r", so | \(missing ", reason)[0]
    spec = {"parameter_id": row["parameter_id"], "section": row["section"], "name": row["name"],
            "max_score": row.get("max_score", 100), "weight": row.get("weight", 1)}
    fresh = no_data(spec, reason=reason.rstrip("."), checked=row.get("checked_url_or_source") or "", evidence=ev,
                    duration_ms=row.get("duration_ms") or 0)
    for key in ("status", "score", "evidence", "recommendation", "confidence", "error"):
        row[key] = fresh.get(key)
    return True


def scorable_pages(ctx) -> list:
    """The pages a content parameter may legitimately score, and nothing else.

    `ctx.pages` is the crawl's full record, which deliberately includes pages that failed:
    parse_page() returns a Page for a timeout or a 404 so the failure is visible, and
    detect_bot_block() marks a Cloudflare interstitial that came back as a normal 200.

    Handlers were consuming that list raw and disagreeing about it. Some filtered on
    `result.ok`, some on `word_count`, most not at all -- so a fetch failure DEFLATED ON-19
    (a zero-word page is "thin content") while it INFLATED TECH-07 (the timed-out pages are
    exactly the slow ones, and filtering them out left only the fast pages in the average).
    Neither effect was recorded anywhere.

    One rule, applied everywhere: a page counts if it was actually retrieved and is not a bot
    challenge. Anything excluded here is a crawl finding, reported by the failures file and by
    TECH's own reachability checks -- not a content finding to be folded into a content score.
    """
    pages = []
    for page in getattr(ctx, "pages", None) or []:
        outcome = getattr(page, "result", None)
        if outcome is None or not getattr(outcome, "ok", False):
            continue
        if getattr(page, "blocked_reason", ""):
            # Scored as page copy before this, so "Just a moment... please enable JavaScript"
            # counted as the site's content on every on-page parameter.
            continue
        pages.append(page)
    return pages


def excluded_page_counts(ctx) -> dict:
    """Why pages were left out, so a handler's evidence can say so instead of just shrinking."""
    all_pages = getattr(ctx, "pages", None) or []
    failed = sum(1 for p in all_pages if not getattr(getattr(p, "result", None), "ok", False))
    blocked = sum(1 for p in all_pages
                  if getattr(getattr(p, "result", None), "ok", False) and getattr(p, "blocked_reason", ""))
    return {"pages_crawled": len(all_pages), "pages_excluded_fetch_failed": failed,
            "pages_excluded_bot_blocked": blocked}


def timed() -> float:
    return time.perf_counter()


def ms_since(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def schema_types(blocks: list) -> list[str]:
    types: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            t = obj.get("@type")
            if isinstance(t, list):
                types.extend(str(x) for x in t)
            elif t:
                types.append(str(t))
            graph = obj.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    walk(item)
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for block in blocks:
        if block.get("ok"):
            walk(block.get("data"))
    return types


def flatten_schema(blocks: list) -> list[dict]:
    items: list[dict] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if "@type" in obj:
                items.append(obj)
            if isinstance(obj.get("@graph"), list):
                for item in obj["@graph"]:
                    walk(item)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for block in blocks:
        if block.get("ok"):
            walk(block.get("data"))
    return items


def _person(value) -> Optional[dict]:
    """A named person from a structured-data author or Person node, or None (an Organization is not one)."""
    if isinstance(value, list):
        return next((p for p in (_person(v) for v in value) if p), None)
    if isinstance(value, str):
        return {"name": value.strip()} if value.strip() else None
    if isinstance(value, dict) and "Organization" not in str(value.get("@type")):
        name = str(value.get("name") or "").strip()
        if name:
            return {k: value.get(k) for k in ("name", "jobTitle", "url", "@type") if value.get(k)}
    return None


def structured_authors(schema_blocks: list) -> list[dict]:
    """The people a page's structured data names as authors: Person nodes, and the people named in
    `author` fields -- including an author nested inside an Article or BlogPosting, which is where
    most sites put it. TECH-18 (is there author markup?) and ON-12 (does it name a real person with
    a role?) both read this, so they cannot disagree about whether authors are marked up at all."""
    people = []
    for item in flatten_schema(schema_blocks):
        person = _person(item) if "Person" in str(item.get("@type")) else _person(item.get("author"))
        if person:
            people.append(person)
    return people


def is_question(text: str) -> bool:
    return bool(text and QUESTION_RE.search(text.strip()))


def heading_blocks(page) -> list[dict]:
    """Every H1-H3 on the page paired with the text that follows it up to the next heading.

    Shared by the checks that judge answers under a heading (ON-02) and whether a section is
    short enough for an assistant to lift whole (TECH-11) -- both need real section text, not
    a per-page average.
    """
    if not page.soup:
        return []
    blocks = []
    for h in page.soup.find_all(re.compile(r"^h[1-3]$")):
        texts = []
        for sib in h.next_siblings:
            if getattr(sib, "name", None) and re.match(r"^h[1-6]$", sib.name or ""):
                break
            if getattr(sib, "get_text", None):
                texts.append(sib.get_text(" ", strip=True))
        body = re.sub(r"\s+", " ", " ".join(texts)).strip()
        blocks.append({"heading": h.get_text(" ", strip=True), "answer": body, "words": word_count(body)})
    return blocks


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text or ""))


# Function words carry no topical signal, so they must not be counted when measuring
# keyword density. Without this filter the most frequent 3+ letter token on almost any
# English page is "the" (typically 4-7% of such tokens), which sits above the stuffing
# bands in onpage.py -- so every site scored the same floor regardless of its copy.
STOPWORDS = frozenset("""
and are was were for with that this these those from have has had not but you your our their its his her they them
who whom which what when where why how all any both each few more most other some such only own same than too very
can will just should now the she him let out off over under again further then once here there while about against
between into through during before after above below down upon per via such also been being does did doing would
could shall may might must because until unless whether within without across among along around behind beside
beyond except inside outside since toward towards upon whereas whose yours ours theirs itself himself herself
themselves ourselves yourself yourselves get got make made take taken give given come came go went know known see
seen say said want need use used using like well back even still way much many lot new old good great best
""".split())


def content_words(text: str) -> list[str]:
    """Lower-cased topical words in `text`: 3+ letters, function words removed.

    Shared by every density measurement so "keyword density" means density of a term a
    buyer could actually search for, not of English grammar.
    """
    return [w for w in re.findall(r"[a-z]{3,}", (text or "").lower()) if w not in STOPWORDS]


def top_term_density(text: str, minimum_words: int = 40) -> tuple[float, list[tuple[str, int]]] | tuple[None, list]:
    """(peak single-term density, the five most frequent terms) over content words only.

    Returns (None, []) when the page has too little copy for a density to mean anything.
    """
    from collections import Counter

    words = content_words(text)
    if len(words) < minimum_words:
        return None, []
    counts = Counter(words)
    top = counts.most_common(5)
    return top[0][1] / len(words), top


# Joining words skipped when forming an organisation's initials: "American Academy OF
# Orthopaedic Surgeons" abbreviates to AAOS, not AAOOS.
_NAME_GLUE = {"of", "the", "and", "for", "in", "on", "at", "to", "de", "du", "la"}
_ACRONYM_AFTER = re.compile(r"^(?P<name>.+?)[\s\-|:,(\u2013\u2014]+(?P<acr>[A-Z][A-Z0-9&.]{1,9})\)?$")
_ACRONYM_BEFORE = re.compile(r"^\(?(?P<acr>[A-Z][A-Z0-9&.]{1,9})\)?[\s\-|:\u2013\u2014]+(?P<name>.+)$")


def drop_acronym(name: str) -> str:
    """The name without an abbreviation of itself stuck on the front or end.

    Sites often declare their name as "American Academy of Orthopaedic Surgeons AAOS" or
    "AAOS | American Academy ...". Wikipedia and Wikidata list the plain name, and every
    off-page search looks for the exact phrase, so the extra word made the company
    unfindable. The abbreviation is only removed when it really is the initials of the rest,
    so a name that merely ends in capitals ("Evoke Technologies", "Bank of America BAC") is kept.
    """
    text = " ".join((name or "").split())
    for pattern in (_ACRONYM_AFTER, _ACRONYM_BEFORE):
        m = pattern.match(text)
        if not m:
            continue
        rest = m.group("name").strip(" -|:,(")
        acr = re.sub(r"[^A-Z0-9]", "", m.group("acr").upper())
        words = re.findall(r"[A-Za-z][\w'-]*", rest)
        initials = {"".join(w[0] for w in words).upper(),
                    "".join(w[0] for w in words if w.lower() not in _NAME_GLUE).upper()}
        if len(words) >= 2 and acr in initials:
            return rest
    return text


# The three Core Web Vitals TECH-07 scores, per data source, in display order.
VITALS = {"field": ("lcp_ms", "inp_ms", "cls"), "lab": ("lcp_ms", "tbt_ms", "cls")}
VITAL_NAMES = {"lcp_ms": "Largest Contentful Paint", "inp_ms": "Interaction to Next Paint",
               "tbt_ms": "Total Blocking Time", "cls": "Cumulative Layout Shift"}


def vital_score(metric: str, value: float) -> float:
    """Points for one Core Web Vital (lower is better), from the thresholds in scoring_rules.json."""
    good, poor = rules.threshold(f"vitals_{metric}", "good"), rules.threshold(f"vitals_{metric}", "poor")
    if value <= good:
        return float(rules.threshold("vitals_points", "good"))
    if value >= poor:
        return float(rules.threshold("vitals_points", "poor"))
    best = rules.threshold("vitals_points", "needs_improvement_best")
    worst = rules.threshold("vitals_points", "needs_improvement_worst")
    return best - (best - worst) * (value - good) / (poor - good)


def vital_rating(metric: str, value: float) -> str:
    if value <= rules.threshold(f"vitals_{metric}", "good"):
        return "good"
    return "poor" if value >= rules.threshold(f"vitals_{metric}", "poor") else "needs improvement"


def band(value: float, thresholds: tuple[tuple[float, float], ...], floor: float) -> float:
    """First score whose threshold `value` meets or exceeds, else `floor`.

    `thresholds` is ordered best-first as ((minimum_value, score), ...). Used wherever a
    raw measurement has no natural 0-100 scale (a text-to-code ratio never reaches 100%,
    so using the raw percentage as a score would cap a perfect page at ~25).
    """
    for minimum, score in thresholds:
        if value >= minimum:
            return score
    return floor


_TOKEN_RE = re.compile(r"[a-z0-9]{4,}")


def near_duplicate_pairs(documents: list[tuple[str, str]], threshold: float) -> list[dict]:
    """Pairs of documents whose token sets overlap at or above `threshold` (Jaccard).

    Returns exactly what comparing every pair would return, without doing so. The all-pairs
    form this replaces was O(n^2): at the crawler's 2500-page ceiling that is ~3.1M set
    intersections per parameter, which blew the 25s parameter timeout and turned both
    duplicate checks UNKNOWN on precisely the large sites where duplication matters most.

    Exactness comes from the standard Jaccard prefix filter. Order every document's tokens
    by how rare they are site-wide. If J(a,b) >= t then |a & b| >= t*|a|, so at most
    ceil(t*|a|) - 1 of a's tokens may fall outside any shared region -- meaning a and b
    must share a token inside the first |a| - ceil(t*|a|) + 1 tokens of each. Only those
    prefixes are indexed, so a pair that cannot reach the threshold is never scored, and
    every pair that can is still checked in full. test_parameters.py asserts this against
    brute force on random corpora.
    """
    tokens = [(url, set(_TOKEN_RE.findall((text or "").lower()))) for url, text in documents]
    document_frequency: dict[str, int] = {}
    for _, toks in tokens:
        for tok in toks:
            document_frequency[tok] = document_frequency.get(tok, 0) + 1

    def prefix(toks: set[str]) -> list[str]:
        ordered = sorted(toks, key=lambda tok: (document_frequency[tok], tok))
        keep = len(ordered) - math.ceil(threshold * len(ordered)) + 1
        return ordered[:max(1, keep)]

    index: dict[str, list[int]] = {}
    candidates: set[tuple[int, int]] = set()
    for i, (_, toks) in enumerate(tokens):
        if not toks:
            continue
        for tok in prefix(toks):
            for j in index.setdefault(tok, []):
                candidates.add((j, i))
            index[tok].append(i)

    pairs = []
    for a, b in sorted(candidates):
        url_a, toks_a = tokens[a]
        url_b, toks_b = tokens[b]
        similarity = len(toks_a & toks_b) / len(toks_a | toks_b)
        if similarity >= threshold:
            pairs.append({"a": url_a, "b": url_b, "similarity": round(similarity, 2)})
    return pairs


def primary_brand(ctx) -> str:
    """The site's own brand term, derived per scan -- never a fixed company name."""
    brand = (getattr(ctx, "identity", None) or {}).get("brand")
    if brand:
        return brand
    name = (getattr(ctx, "company_name", "") or "").strip()
    if name:
        return name.split()[0].lower()
    domain = (getattr(ctx, "domain", "") or "").split(".")[0]
    return domain.lower()


_NAV_STOPWORDS = {
    "home", "contact", "contact us", "about", "about us", "careers", "career", "blog",
    "insights", "news", "login", "sign in", "sign up", "search", "menu", "get in touch",
    "resources", "privacy policy", "privacy", "terms", "terms of use", "sitemap", "faq",
    "faqs", "team", "leadership", "investors", "events", "media", "press", "gallery",
    "our story", "clients", "testimonials", "get started", "book a demo", "request a demo",
}
# Menu labels addressed to an audience ("Patients & Public", "For Members") name who a page is for,
# not what the organisation does, so they are never taken as its category.
_AUDIENCE_LABEL = re.compile(
    r"\b(?:patients?|public|members?|membership|partners?|investors?|customers?|students?|parents?|employers?|"
    r"job seekers|media|donors?|volunteers?|shop|store|cart|account|my |login|support|help|events?|news|"
    r"jobs|careers?|community|subscribe|donate|"
    # The language picker is a menu too: "English", "Deutsch", "Español".
    r"english|deutsch|español|espanol|français|francais|italiano|português|portugues|nederlands|polski|"
    r"svenska|dansk|norsk|suomi|türkçe|turkce|русский|中文|日本語|한국어|العربية|हिन्दी)\b", re.I)


_TITLE_LEAD_RE = re.compile(r"^(?:(?:top|best|leading|trusted|premier|expert|#1|the|our)\s+)+")
_TITLE_TAIL_RE = re.compile(
    r"(?:\s+(?:for|in the|on|across|with)\s+.*$)"
    r"|(?:\s+(?:services?|solutions?|providers?|company|companies|partner|experts?))+$"
)


def core_concept(title: str) -> str:
    """The concept a service-page title names, without the SEO wrapping around it.

    Titles like "Best AI Governance Consulting Services for Enterprises" were used whole as
    the site's concepts, and ON-03, ON-04 and ON-20 then searched page copy for that exact
    phrase -- which almost no page contains, so a site that explains AI governance on dozens
    of pages was scored as never mentioning it. "ai governance consulting" is what a page says.
    """
    t = title.split("|")[0].split(" - ")[0].split(",")[0].strip().lower()
    t = _TITLE_LEAD_RE.sub("", t)
    while (shorter := _TITLE_TAIL_RE.sub("", t).strip(" &-:")) != t:
        t = shorter
    return t


def derive_site_categories(ctx, limit: int = 10) -> list[str]:
    """The business categories/concepts this specific site covers, derived from its own
    navigation and service pages -- never a fixed, one-company keyword list, since every
    site being audited runs a different business."""
    candidates: list[str] = []
    for page in getattr(ctx, "pages", None) or []:
        if getattr(page, "page_type", None) == "service" and getattr(page, "title", ""):
            t = core_concept(page.title)
            if t and t not in _NAV_STOPWORDS and 3 <= len(t) <= 60:
                candidates.append(t)
    homepage = getattr(ctx, "homepage", None)
    if homepage:
        for link in getattr(homepage, "links", None) or []:
            txt = (link.get("text") or "").strip().lower()
            if txt and txt not in _NAV_STOPWORDS and 3 <= len(txt) <= 40 and not txt.startswith(("http", "www.")):
                candidates.append(txt)
    ident = getattr(ctx, "identity", None) or {}
    own = [t.lower() for t in [ident.get("brand"), ident.get("acronym"), ident.get("short_name")] + list(getattr(ctx, "brand_terms", None) or []) if t]
    seen: list[str] = []
    for c in candidates:
        c = re.sub(r"\s*\([^)]*\)", "", c).strip()      # "emergency medical services (ems)" -> the words
        if any(re.search(rf"\b{re.escape(t)}\b", c) for t in own):
            continue                                    # the organisation's own name is not a category
        if _AUDIENCE_LABEL.search(c):
            continue                                    # "patients & public" is an audience, not a category
        if c and c not in seen:
            seen.append(c)
    return seen[:limit]


_GEO_PAIR_RE = re.compile(r"\b([A-Z][a-zA-Z.\-]{2,24}),\s?([A-Z]{2}\b|[A-Z][a-zA-Z.\-]{2,24}\b)")


def derive_site_geographies(ctx, limit: int = 8) -> list[str]:
    """Office/market locations this specific site mentions, derived from its own structured
    data and page text -- never a fixed list of one company's cities/offices."""
    geos: list[str] = []
    for page in getattr(ctx, "pages", None) or []:
        for item in flatten_schema(getattr(page, "schema_blocks", None) or []):
            addr = item.get("address")
            if isinstance(addr, dict):
                for key in ("addressLocality", "addressRegion", "addressCountry"):
                    v = addr.get(key)
                    if isinstance(v, str) and v.strip():
                        geos.append(v.strip().lower())
                    elif isinstance(v, dict) and v.get("name"):
                        geos.append(str(v["name"]).strip().lower())
    if not geos:
        relevant = [p for p in (getattr(ctx, "pages", None) or []) if getattr(p, "page_type", None) in ("about", "utility", "home")]
        blob = " ".join((p.text or "") for p in relevant)[:20000]
        for m in _GEO_PAIR_RE.finditer(blob):
            geos.append(m.group(0).lower())
    seen: list[str] = []
    for g in geos:
        if g not in seen:
            seen.append(g)
    return seen[:limit]

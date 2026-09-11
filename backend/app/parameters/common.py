from __future__ import annotations

import math
import re
import time
from typing import Any, Optional

from ..errors import humanize_error


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
) -> dict:
    pa = pass_at if pass_at is not None else spec.get("pass_threshold", 90)
    pb = partial_at if partial_at is not None else spec.get("partial_threshold", 60)
    friendly = humanize_error(error)
    unknown = bool(unknown or score is None)
    st = "UNKNOWN" if unknown else status_from_score(score, pa, pb)
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


def derive_site_categories(ctx, limit: int = 10) -> list[str]:
    """The business categories/concepts this specific site covers, derived from its own
    navigation and service pages -- never a fixed, one-company keyword list, since every
    site being audited runs a different business."""
    candidates: list[str] = []
    for page in getattr(ctx, "pages", None) or []:
        if getattr(page, "page_type", None) == "service" and getattr(page, "title", ""):
            t = page.title.split("|")[0].split(" - ")[0].strip().lower()
            if t and t not in _NAV_STOPWORDS and 3 <= len(t) <= 60:
                candidates.append(t)
    homepage = getattr(ctx, "homepage", None)
    if homepage:
        for link in getattr(homepage, "links", None) or []:
            txt = (link.get("text") or "").strip().lower()
            if txt and txt not in _NAV_STOPWORDS and 3 <= len(txt) <= 40 and not txt.startswith(("http", "www.")):
                candidates.append(txt)
    seen: list[str] = []
    for c in candidates:
        if c not in seen:
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

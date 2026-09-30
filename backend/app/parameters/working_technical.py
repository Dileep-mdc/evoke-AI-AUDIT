"""Plain-English calculation sentences for the TECH-* parameters.

Each function takes a parameter row's stored evidence dict and returns
(text, recomputed_score): one sentence, built from the measured numbers, that says what was
found and how that becomes the score, and the score that working produces. It returns None
when the evidence does not carry what is needed, or the row is UNKNOWN. The arithmetic
mirrors the handlers in technical.py; the words avoid maths symbols so anyone can follow them.

Pure functions, stdlib only, and never raise: a malformed evidence dict gives None.
"""
from __future__ import annotations

from collections import Counter
from typing import Callable, Optional

Result = Optional[tuple[str, float]]


# ---------------------------------------------------------------- formatting helpers

def _num(x: float, places: int = 1) -> str:
    """A number with thousands separators, rounded to `places`, trailing ".0" dropped."""
    s = f"{float(x):,.{places}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _int(n) -> str:
    return f"{int(n):,}"


def _score(x: float) -> float:
    return round(float(x), 1)


def _so(score: float) -> str:
    return f"so it scores {_num(score)}."


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{_int(n)} {word if n == 1 else many or word + 's'}"


def _v(n: int, one: str, many: str) -> str:
    """The verb that agrees with a count: 1 page has, 2 pages have."""
    return one if n == 1 else many


def _and(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _unknown(ev: dict) -> bool:
    if not isinstance(ev, dict):
        return True
    if ev.get("not_applicable"):
        return True
    return "rules_based_score" in ev and ev.get("rules_based_score") is None


def _safe(fn: Callable[[dict], Result]) -> Callable[[dict], Result]:
    def wrapper(ev: dict) -> Result:
        try:
            if _unknown(ev):
                return None
            return fn(ev)
        except Exception:
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _band(value: float, bands: tuple[tuple[float, float], ...], floor: float) -> float:
    for limit, score in bands:
        if value <= limit:
            return score
    return floor


# ---------------------------------------------------------------- TECH-01 .. TECH-22

@_safe
def tech_01(ev: dict) -> Result:
    decisions = ev.get("decisions")
    if not isinstance(decisions, list):
        return None
    known = [d for d in decisions if d.get("decision") in ("ALLOWED", "BLOCKED")]
    if not known:
        return None
    allowed = sum(1 for d in known if d.get("decision") == "ALLOWED")
    blocked = [str(d.get("name") or "a crawler") for d in known if d.get("decision") == "BLOCKED"]
    score = allowed / len(known) * 100
    n = len(known)
    if not blocked:
        return f"robots.txt lets in all {n} AI crawlers, {_so(score)}", _score(score)
    if not allowed:
        return f"robots.txt blocks all {n} AI crawlers, {_so(score)}", _score(score)
    verb = "is" if len(blocked) == 1 else "are"
    return f"robots.txt lets in {allowed} of {n} AI crawlers ({_and(blocked)} {verb} blocked), {_so(score)}", _score(score)


def _refusal(status) -> str:
    if status is None:
        return "no response"
    if 200 <= int(status) < 300:
        return "a near-empty page"
    return f"error {status}"


@_safe
def tech_02(ev: dict) -> Result:
    bots = ev.get("bots")
    if not isinstance(bots, list) or not bots:
        return None
    blocked = [b for b in bots if b.get("blocked")]
    n = len(bots)
    served = n - len(blocked)
    score = served / n * 100
    if not blocked:
        return f"All {n} AI crawlers got the real homepage, {_so(score)}", _score(score)
    reasons = Counter(_refusal(b.get("status")) for b in blocked)
    why = _and([f"{c} got {r}" for r, c in sorted(reasons.items(), key=lambda kv: -kv[1])])
    if not served:
        return f"All {n} AI crawlers were turned away ({why}), {_so(score)}", _score(score)
    return (f"{len(blocked)} of {n} AI crawlers were turned away ({why}) and only {served} got the real homepage, "
            f"{_so(score)}"), _score(score)


@_safe
def tech_03(ev: dict) -> Result:
    if "non_empty" not in ev:
        # The missing/empty branch records only the URL and HTTP status.
        if "status" in ev or "url" in ev:
            status = ev.get("status")
            where = f" (the server returned {status})" if status is not None else ""
            return f"No usable llms.txt file was found{where}, so it scores 0.", 0.0
        return None
    non_empty = bool(ev.get("non_empty"))
    fresh = bool(ev.get("freshness_signal"))
    links = ev.get("sampled_links") or []
    found = int(ev.get("links_found") or 0)
    parts = ["The llms.txt file exists (40 points)",
             "has real content (20 of 20 points)" if non_empty else "is almost empty (0 of 20 points)"]
    if found:
        if not links:
            return None
        ok = sum(1 for r in links if r.get("ok"))
        link_points = 20 * ok / len(links)
        parts.append(f"{_int(ok)} of {_int(len(links))} of its links work ({_num(link_points)} of 20 points)")
    else:
        link_points = 10
        parts.append("has no links (10 of 20 points)")
    parts.append("shows a date (20 of 20 points)" if fresh else "shows no date (0 of 20 points)")
    raw = 40 + (20 if non_empty else 0) + link_points + (20 if fresh else 0)
    score = min(100, raw)
    cap = " (capped at 100)" if raw > 100 else ""
    return f"{_and(parts)}{cap}, {_so(score)}", _score(score)


@_safe
def tech_04(ev: dict) -> Result:
    if "markers_found" not in ev or "word_count" not in ev:
        return None
    markers = ev.get("markers_found") or []
    login = bool(ev.get("login_form"))
    words = int(ev.get("word_count") or 0)
    if not markers and not login:
        return "No pop-ups, cookie banners or login wall were found on the homepage, so it scores 100.", 100.0
    found = []
    if markers:
        found.append(f"pop-up or banner code ({', '.join(markers)})")
    if login:
        found.append("a login form")
    what = " and ".join(found)
    if words >= 150:
        score = 70
        text = f"The homepage has {what}, but still shows {_int(words)} words of text, "
        tail = " Without pop-ups or a login wall it would score 100."
    else:
        score = 20
        text = f"The homepage has {what} and shows only {_int(words)} words of text, "
        tail = " With 150 or more words it would score 70."
    if login and words < 80 and score > 20:
        score = 20
        text += "and a login form with under 80 words caps it at 20, "
    return text + _so(score) + tail, _score(score)


@_safe
def tech_05(ev: dict) -> Result:
    count = ev.get("sitemap_url_count")
    if count is None:
        if "candidates" in ev or "errors" in ev:
            return "No sitemap was found, so it scores 0.", 0.0
        return None
    sampled = int(ev.get("sampled") or 0)
    valid = int(ev.get("valid_sampled") or 0)
    coverage = ev.get("crawled_coverage_pct")
    if not sampled or coverage is None:
        return None
    validity = valid / sampled * 100
    raw = 25 + validity * 0.45 + float(coverage) * 0.30
    score = min(100, raw)
    cap = " (capped at 100)" if raw > 100 else ""
    return (f"The sitemap exists (25 points), {_int(valid)} of {_int(sampled)} sampled links in it work"
            f" ({_num(validity * 0.45)} of 45 points) and it lists {_num(coverage)}% of the crawled pages"
            f" ({_num(float(coverage) * 0.30)} of 30 points){cap}, {_so(score)}"), _score(score)


@_safe
def tech_06(ev: dict) -> Result:
    rows = ev.get("pages")
    if isinstance(rows, list) and rows:
        n = len(rows)
        broken = sum(1 for r in rows if r.get("broken"))
        chains = sum(1 for r in rows if r.get("chain"))
    elif ev.get("sampled"):
        n = int(ev["sampled"])
        broken = int(ev.get("broken") or 0)
        chains = int(ev.get("redirect_chains") or 0)
    else:
        return None
    share = (n - broken) / n * 100
    penalty = min(20, chains * 5)
    score = max(0, share - penalty)
    text = f"{_int(n - broken)} of {_int(n)} internal links {_v(n - broken, 'works', 'work')}"
    if broken:
        text += f" ({_int(broken)} {'is' if broken == 1 else 'are'} broken)"
    if chains:
        text += (f", which gives {_num(share)}. {_plural(chains, 'long redirect chain')} then"
                 f" {'takes' if chains == 1 else 'take'} off {_num(penalty)} points")
        if chains * 5 > 20:
            text += " (the most it can lose)"
    else:
        text += ", and there are no long redirect chains"
    floor = " (it cannot go below 0)" if share - penalty < 0 else ""
    return f"{text}{floor}, {_so(score)}", _score(score)


_LATENCY = ((800, 100), (1800, 70), (3000, 40))
_WEIGHT = ((300_000, 100), (800_000, 70), (1_500_000, 40))
_RATING = {100: "good", 70: "fair", 40: "slow", 20: "very slow"}
_SIZE_RATING = {100: "light", 70: "fair", 40: "heavy", 20: "very heavy"}


@_safe
def tech_07(ev: dict) -> Result:
    latency = ev.get("avg_latency_ms")
    size = ev.get("avg_page_bytes")
    if latency is None or size is None:
        return None
    ls = _band(float(latency), _LATENCY, 20)
    ws = _band(float(size), _WEIGHT, 20)
    score = ls * 0.6 + ws * 0.4
    return (f"Pages respond in {_int(latency)} ms on average ({_RATING[ls]}, {_num(ls)} points) and weigh"
            f" {_int(float(size) / 1000)} KB ({_SIZE_RATING[ws]}, {_num(ws)} points). Speed counts for 60% and size"
            f" for 40%, {_so(score)}"), _score(score)


@_safe
def tech_08(ev: dict) -> Result:
    if "https" not in ev or "viewport" not in ev:
        return None
    https = bool(ev.get("https"))
    viewport = bool(ev.get("viewport"))
    base = (50 if https else 0) + (50 if viewport else 0)
    if "fixed_width_layout" in ev:
        fixed = bool(ev.get("fixed_width_layout"))
    else:
        # Rows written before the flag was recorded: the cap is the only way 50 + 50 became 80.
        rb = ev.get("rules_based_score")
        fixed = https and viewport and rb is not None and abs(float(rb) - 80) < 0.05
    score = min(base, 80) if fixed else base
    text = (f"The site {'uses HTTPS (50 points)' if https else 'does not use HTTPS (0 of 50 points)'} and"
            f" {'has a mobile viewport setting (50 points)' if viewport else 'has no mobile viewport setting (0 of 50 points)'}")
    if fixed and base > 80:
        text += ", but the page forces a fixed desktop width, which caps it at 80"
    elif fixed:
        text += " (it also forces a fixed desktop width, but is already under the cap of 80)"
    return f"{text}, {_so(score)}", _score(score)


@_safe
def tech_09(ev: dict) -> Result:
    pages = ev.get("pages")
    compared = ev.get("compared_pages")
    shares = None
    if isinstance(pages, list) and pages and all("js_only_share_pct" in p for p in pages):
        shares = [float(p["js_only_share_pct"]) for p in pages]
    if shares:
        avg = sum(shares) / len(shares)
        n = len(shares)
    elif ev.get("average_js_only_share_pct") is not None and compared:
        avg = float(ev["average_js_only_share_pct"])
        n = int(compared)
    elif compared == 1 and ev.get("probe_url"):
        # The "not_needed" branch: the homepage probe showed rendering adds no material text.
        return "Running JavaScript on the homepage added no real text, so nothing is hidden and it scores 100.", 100.0
    else:
        return None
    score = max(0, min(100, round((1 - avg / 100) * 100)))
    return (f"On average {_num(avg)}% of the text on {_plural(n, 'page')} only appears after JavaScript runs,"
            f" {_so(score)}"), _score(score)


@_safe
def tech_10(ev: dict) -> Result:
    rows = ev.get("pages")
    if not isinstance(rows, list):
        if ev.get("note"):
            return "No headings were found on any page, so it scores 0.", 0.0
        return None
    if not rows:
        return "No headings were found on any page, so it scores 0.", 0.0
    scores = [float(r["score"]) for r in rows]
    score = sum(scores) / len(scores)
    n = len(scores)
    clean = sum(1 for s in scores if s >= 100)
    if clean == n:
        return f"All {_plural(n, 'page')} have a clean heading structure, {_so(score)}", _score(score)
    return (f"{_int(clean)} of {_plural(n, 'page')} {_v(clean, 'has', 'have')} a clean heading structure and the other {_int(n - clean)}"
            f" {_v(n - clean, 'loses', 'lose')} some points for a missing or repeated H1 or skipped heading levels, so the average"
            f" score is {_num(score)}."), _score(score)


@_safe
def tech_11(ev: dict) -> Result:
    llm = ev.get("llm")
    if isinstance(llm, dict) and llm.get("total"):
        liftable = float(llm.get("liftable_count") or 0)
        total = float(llm["total"])
        score = liftable / total * 100
        return (f"The AI found that {_num(liftable)} of {_num(total)} sampled sections could be quoted on their own,"
                f" {_so(score)}"), _score(score)
    rows = ev.get("pages")
    if not isinstance(rows, list):
        return None
    if not rows:
        return "No page had 40 or more words to check, so it gets a set score of 40.", 40.0
    scores = [float(r["score"]) for r in rows]
    score = sum(scores) / len(scores)
    return (f"{_plural(len(scores), 'page')} were checked for sections that can be quoted on their own"
            f", so the average score is {_num(score)}."), _score(score)


@_safe
def tech_12(ev: dict) -> Result:
    if "tables" not in ev or "lists" not in ev:
        return None
    tables = int(ev.get("tables") or 0)
    lists = int(ev.get("lists") or 0)
    total = tables + lists
    if total >= 8:
        score, rule = 100, "8 or more gets full marks"
    elif total >= 3:
        score, rule = 70, "3 to 7 gets 70, and 8 or more would get 100"
    else:
        score, rule = 40, "fewer than 3 gets 40, and 3 or more would get 70"
    return (f"The site has {_plural(tables, 'table')} and {_plural(lists, 'list')} ({rule}),"
            f" {_so(score)}"), _score(score)


@_safe
def tech_13(ev: dict) -> Result:
    rows = ev.get("pages")
    if not isinstance(rows, list):
        return None
    if not rows:
        return "There were no pages to measure, so it scores 0.", 0.0
    n = len(rows)
    ratio = sum(float(r["text_to_code_score"]) for r in rows) / n
    depth = sum(min(100.0, float(r["words"]) / 400 * 100) for r in rows) / n
    score = min(100, sum(float(r["combined"]) for r in rows) / n)
    return (f"Across {_plural(n, 'page')}, the text-to-code balance averages {_num(ratio, 0)} out of 100 and page"
            f" length reaches {_num(depth, 0)}% of the 400-word target on average. Length counts for 60% and"
            f" balance for 40%, {_so(score)}"), _score(score)


@_safe
def tech_14(ev: dict) -> Result:
    if "images" not in ev:
        return None
    images = int(ev.get("images") or 0)
    videos = int(ev.get("videos") or 0)
    if images == 0:
        return "The site has no images, so it gets a set score of 80.", 80.0
    usable = int(ev.get("usable_alt") or 0)
    transcripts = int(ev.get("transcript_signals") or 0)
    img = usable / images * 100
    vid = 100.0 if videos == 0 else min(100.0, transcripts / videos * 100)
    score = img * 0.7 + vid * 0.3
    vid_text = ("there are no videos (counted as full marks)" if videos == 0
                else f"{_int(transcripts)} of {_plural(videos, 'video')} {'has' if videos == 1 else 'have'} a transcript ({_num(vid, 0)}%)")
    return (f"{_int(usable)} of {_plural(images, 'image')} {'has' if images == 1 else 'have'} useful alt text"
            f" ({_num(img, 0)}%) and {vid_text}. Images count for 70% and videos for 30%, {_so(score)}"), _score(score)


@_safe
def tech_15(ev: dict) -> Result:
    rows = ev.get("samples")
    if isinstance(rows, list):
        n = len(rows)
        found = sum(1 for r in rows if r.get("has_date"))
    else:
        return None
    share = found / max(1, n) * 40
    score = 60 + share
    return (f"{_int(found)} of {_plural(n, 'page')} {_v(found, 'shows', 'show')} a date. Every site starts at 60 and gains up to 40 more"
            f" for dated pages ({_num(share)} here), {_so(score)}"), _score(score)


_ORG_EXPECTED = ("name", "url", "logo", "sameAs")
_ORG_LABEL = {"name": "name", "url": "website", "logo": "logo", "sameAs": "social profiles"}


@_safe
def tech_16(ev: dict) -> Result:
    if "organization" not in ev:
        return None
    if not ev.get("organization"):
        return "The homepage has no company (Organization) structured data, so it scores 0.", 0.0
    fields = [f for f in (ev.get("fields") or []) if isinstance(f, str)]
    present = [_ORG_LABEL[f] for f in _ORG_EXPECTED if f in fields]
    missing = [_ORG_LABEL[f] for f in _ORG_EXPECTED if f not in fields]
    bonus = [f for f in fields if f in ("address", "contact")]
    points = len(present) / 4 * 80
    score = points + (20 if bonus else 0)
    miss = f"; {_and(missing)} {'is' if len(missing) == 1 else 'are'} missing" if missing else ""
    bonus_text = (f", plus 20 points for {' and '.join(bonus)} details" if bonus
                  else ", and no address or contact details (0 of 20 points)")
    return (f"The company structured data on the homepage has {len(present)} of the 4 main details"
            f" ({_and(present) or 'none'}{miss}) for {_num(points)} of 80 points{bonus_text}, {_so(score)}"), _score(score)


@_safe
def tech_17(ev: dict) -> Result:
    rows = ev.get("pages")
    if not isinstance(rows, list):
        return None
    applicable = [r for r in rows if "match" in r and r.get("applicable", True)]
    excluded = len(rows) - len(applicable)
    if not applicable:
        return "No page is of a type that needs structured data, so it gets a set score of 50.", 50.0
    matched = sum(1 for r in applicable if r.get("match"))
    score = matched / len(applicable) * 100
    ex = f" ({_plural(excluded, 'other page')} did not need it)" if excluded else ""
    return (f"{_int(matched)} of {_plural(len(applicable), 'page')} {_v(matched, 'carries', 'carry')} structured data that matches its page"
            f" type{ex}, {_so(score)}"), _score(score)


@_safe
def tech_18(ev: dict) -> Result:
    signals = ev.get("author_signals")
    if not isinstance(signals, list):
        return None
    if not signals:
        return "No author information was found on any page, so it scores 25.", 25.0
    titled = sum(1 for a in signals if "jobTitle" in (a.get("item") or {}))
    found = f"{_plural(len(signals), 'author signal')} {'was' if len(signals) == 1 else 'were'} found"
    if titled:
        return (f"{found} and {_int(titled)} {'includes' if titled == 1 else 'include'} a job title"
                f" (a job title earns the top score), so it scores 95."), 95.0
    return f"{found} but none include a job title (adding one would raise it to 95), so it scores 80.", 80.0


@_safe
def tech_19(ev: dict) -> Result:
    blocks = ev.get("blocks")
    if not blocks:
        return None
    valid = int(ev.get("valid") or 0)
    score = valid / int(blocks) * 100
    return (f"{_int(valid)} of {_plural(int(blocks), 'structured-data (JSON-LD) block')} {_v(valid, 'is', 'are')} valid,"
            f" {_so(score)}"), _score(score)


@_safe
def tech_20(ev: dict) -> Result:
    rows = ev.get("pages")
    if not isinstance(rows, list):
        return None
    n = len(rows)
    ok = sum(1 for r in rows if r.get("ok"))
    score = ok / max(1, n) * 100
    return (f"{_int(ok)} of {_plural(n, 'page')} {_v(ok, 'has', 'have')} a correct canonical tag and no long redirect,"
            f" {_so(score)}"), _score(score)


@_safe
def tech_21(ev: dict) -> Result:
    tags = ev.get("tags")
    if not isinstance(tags, list) or not tags:
        return None
    valid = sum(1 for h in tags if h.get("lang") and h.get("href"))
    score = valid / len(tags) * 100
    return (f"{_int(valid)} of {_plural(len(tags), 'language (hreflang) tag')} {_v(valid, 'is', 'are')} complete, with both a language"
            f" and a link, {_so(score)}"), _score(score)


@_safe
def tech_22(ev: dict) -> Result:
    rows = ev.get("pages")
    if not isinstance(rows, list):
        return None
    n = max(1, len(rows))
    titles = [str(r.get("title") or "").strip().lower() for r in rows]
    descs = [str(r.get("description") or "").strip().lower() for r in rows]
    nt = [t for t in titles if t]
    nd = [d for d in descs if d]
    present = sum(1 for r in rows if r.get("title") and r.get("desc_len"))
    length_ok = sum(1 for r in rows if r.get("title_ok") and r.get("desc_ok"))
    p = present / n * 50
    ut = len(set(nt)) / max(1, len(nt)) * 15
    ud = len(set(nd)) / max(1, len(nd)) * 10
    lq = length_ok / n * 25
    score = p + ut + ud + lq
    return (f"{_int(present)} of {_int(len(rows))} pages {_v(present, 'has', 'have')} both a title and a description ({_num(p)} of 50 points),"
            f" {_int(len(set(nt)))} of {_int(len(nt))} titles {_v(len(set(nt)), 'is', 'are')} unique ({_num(ut)} of 15 points),"
            f" {_int(len(set(nd)))} of {_int(len(nd))} descriptions {_v(len(set(nd)), 'is', 'are')} unique ({_num(ud)} of 10 points) and"
            f" {_int(length_ok)} of {_int(len(rows))} pages {_v(length_ok, 'has', 'have')} both at a good length ({_num(lq)} of 25 points),"
            f" {_so(score)}"), _score(score)


WORKING: dict[str, Callable[[dict], Result]] = {
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

"""Fix by page: for one check, every URL that needs changing, what is wrong with it, and what to do.

The recommended fix on a row is one paragraph for the whole site. This is the list behind it: a
row per page (or per item -- a crawler, a concept, a pair of competing pages) that the check found
wanting, each with the problem stated in that page's own numbers and the change that resolves it.

It is built from the evidence the check recorded, by the same rule that scored it -- the H1 count
TECH-10 scored, the word count ON-19 compared against its threshold -- never re-measured and never
asked of a model. So it covers every page the check judged, costs nothing to produce, works on any
report already saved, and cannot disagree with the score it sits under.

Checks measured across the site as a whole (an off-page search, a sitemap total, a buying-journey
stage count) have no page list; `scope` says so, and the recommended fix is the whole answer.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

from . import rules

_THIN_WORDS = rules.threshold("thin_page_words")
_ANSWER_MIN = rules.threshold("answer_ideal_words", "min")
_ANSWER_MAX = rules.threshold("answer_ideal_words", "max")
_CHAIN_HOPS = rules.threshold("redirect_chain_hops")
_DENSITY_OK = rules.band("keyword_density")[0][0][0]          # under this peak density scores full marks
_TEXT_TO_CODE_OK = rules.band("text_to_code")[0][0][0]        # at or over this ratio scores full marks
_LCP_GOOD = rules.threshold("vitals_lcp_ms", "good")
_TBT_GOOD = rules.threshold("vitals_tbt_ms", "good")
_CLS_GOOD = rules.threshold("vitals_cls", "good")
# Below this, a page's JavaScript-only share is rounding noise (a date stamp, a cookie notice).
_JS_ONLY_NOTICEABLE_PCT = 5.0
# TECH-11: the handler's own lines -- over this many words under one heading, or under this many
# headings per 1,000 words, a page loses points.
_WORDS_PER_HEADING = 220
_HEADINGS_PER_1000 = 1.5


@dataclass
class Site:
    origin: str
    company: str

    @property
    def home(self) -> str:
        return self.origin.rstrip("/") + "/"


def _fix(target: str, issue: str, fix: str) -> dict:
    return {"target": target, "issue": issue, "fix": fix}


def _cap(text: str) -> str:
    """First letter upper-cased, the rest untouched -- str.capitalize() would turn "H1" into "h1"."""
    return text[:1].upper() + text[1:]


def _quote(text: str, limit: int = 90) -> str:
    text = " ".join(str(text or "").split())
    return f"“{text[:limit].rstrip()}{'…' if len(text) > limit else ''}”"


def _list(items: list[str], limit: int = 3) -> str:
    items = [i for i in items if i]
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def _rows(evidence: dict, key: str) -> list[dict]:
    value = evidence.get(key)
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def _num(value, default=0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


# --- Technical --------------------------------------------------------------------------------

def _tech_01(ev: dict, site: Site) -> list[dict]:
    robots = ev.get("url") or site.home + "robots.txt"
    return [
        _fix(robots, f"{d.get('name')} is disallowed ({d.get('rule') or 'robots.txt rule'})",
             f"Add a group for it to robots.txt: “User-agent: {d.get('name')}” followed by "
             "“Allow: /”, or remove the Disallow line that blocks it.")
        for d in _rows(ev, "decisions") if d.get("decision") == "BLOCKED"
    ]


def _tech_02(ev: dict, site: Site) -> list[dict]:
    return [
        _fix(site.home, f"{b.get('bot')} is refused by the CDN or firewall (HTTP {b.get('status')}, "
                        f"{b.get('body_bytes', 0):,} bytes returned)",
             f"Allow the {b.get('bot')} user-agent in your CDN/WAF bot rules (Cloudflare, Akamai, AWS WAF), "
             "so it receives the same page a browser does.")
        for b in _rows(ev, "bots") if b.get("blocked")
    ]


def _tech_03(ev: dict, site: Site) -> list[dict]:
    url = ev.get("url") or site.home + "llms.txt"
    status = ev.get("status")
    if status and status != 200:
        return [_fix(url, f"No llms.txt (HTTP {status})",
                     f"Publish /llms.txt as Markdown: an H1 with “{site.company}”, a one-paragraph summary "
                     "of what you offer and for whom, then H2 sections linking your key service, product and "
                     "about pages with a one-line description each.")]
    return []


def _tech_04(ev: dict, site: Site) -> list[dict]:
    markers = ev.get("markers_found") or []
    if not markers and not ev.get("login_form"):
        return []
    found = []
    if markers:
        found.append(f"a cookie/consent overlay ({_list(markers)})")
    if ev.get("login_form"):
        found.append("a login form")
    return [_fix(site.home, f"Homepage shows {' and '.join(found)}; {ev.get('word_count', 0)} words of content were readable",
                 "Keep the overlay from covering the main content: render the page text in the HTML behind it, "
                 "load the banner after the content, and never require consent or login before the content is shown.")]


def _tech_06(ev: dict, site: Site) -> list[dict]:
    out = []
    for r in _rows(ev, "pages"):
        if r.get("broken"):
            out.append(_fix(r.get("url"), f"Linked URL returns {('HTTP ' + str(r['status'])) if r.get('status') else 'no response'}",
                            "Update every internal link that points here to a live page, or 301-redirect this URL "
                            "to the closest equivalent page."))
        elif r.get("chain"):
            out.append(_fix(r.get("url"), f"Reached through {r.get('hops')} redirects ({_CHAIN_HOPS} or more count as a chain)",
                            "Point internal links straight at the final URL, and make the first redirect go to the "
                            "final destination in one hop."))
    return out


def _tech_07(ev: dict, site: Site) -> list[dict]:
    out = []
    for r in _rows(ev, "lab_pages"):
        if r.get("error"):
            first = str(r["error"]).splitlines()[0][:140]
            out.append(_fix(r.get("url"), f"Could not be measured: {first}",
                            "The page did not finish loading on a throttled mobile connection. Find what keeps it "
                            "loading (a slow third-party script, a long-polling widget) and defer or remove it."))
            continue
        issues, fixes = [], []
        lcp, tbt, cls = r.get("lcp_ms"), r.get("tbt_ms"), r.get("cls")
        if lcp is not None and _num(lcp) > _LCP_GOOD:
            issues.append(f"LCP {_num(lcp) / 1000:.1f}s (good ≤ {_LCP_GOOD / 1000:.1f}s)")
            fixes.append("compress and preload the largest above-the-fold image or text block and remove render-blocking CSS/JS")
        if tbt is not None and _num(tbt) > _TBT_GOOD:
            issues.append(f"Total Blocking Time {_num(tbt):,.0f}ms (good ≤ {_TBT_GOOD}ms)")
            fixes.append("split, defer or remove heavy JavaScript, especially third-party tags")
        if cls is not None and _num(cls) > _CLS_GOOD:
            issues.append(f"CLS {_num(cls):.3f} (good ≤ {_CLS_GOOD})")
            fixes.append("reserve space for images, embeds and banners with width/height or aspect-ratio")
        if issues:
            out.append(_fix(r.get("url"), "; ".join(issues), _cap("; ".join(fixes)) + "."))
    return out


def _tech_08(ev: dict, site: Site) -> list[dict]:
    issues, fixes = [], []
    if ev.get("https") is False:
        issues.append("not served over HTTPS")
        fixes.append("redirect every http:// URL to https:// with a valid certificate")
    if ev.get("viewport") is False:
        issues.append("no viewport meta tag")
        fixes.append('add <meta name="viewport" content="width=device-width, initial-scale=1"> to every template')
    if ev.get("fixed_width_layout"):
        issues.append("fixed-width layout")
        fixes.append("replace fixed pixel widths with responsive CSS")
    url = ev.get("final_url") or site.home
    return [_fix(url, _cap("; ".join(issues)), _cap("; ".join(fixes)) + ".")] if issues else []


def _tech_09(ev: dict, site: Site) -> list[dict]:
    rows = sorted(_rows(ev, "pages"), key=lambda r: -_num(r.get("js_only_share_pct")))
    return [
        _fix(r.get("url"), f"{_num(r.get('js_only_share_pct')):.0f}% of the text ({r.get('gained_words', 0):,} of "
                           f"{r.get('rendered_words', 0):,} words) only appears after JavaScript runs",
             "Render this content in the server's HTML (server-side rendering or static generation) so AI crawlers "
             "that do not run JavaScript see it.")
        for r in rows if _num(r.get("js_only_share_pct")) >= _JS_ONLY_NOTICEABLE_PCT
    ]


def _tech_10(ev: dict, site: Site) -> list[dict]:
    out = []
    for r in _rows(ev, "pages"):
        h1, skips = int(_num(r.get("h1_count"))), int(_num(r.get("skipped_levels")))
        issues, fixes = [], []
        if h1 == 0:
            issues.append("no H1 heading")
            fixes.append("add one H1 that states what the page is about")
        elif h1 > 1:
            issues.append(f"{h1} H1 headings")
            fixes.append("keep one H1 and turn the others into H2s")
        if skips:
            issues.append(f"{skips} skipped heading level{'s' if skips > 1 else ''} (e.g. H2 straight to H4)")
            fixes.append("nest headings in order, H1 > H2 > H3, without jumping a level")
        if issues:
            out.append(_fix(r.get("url"), _cap("; ".join(issues)), _cap("; ".join(fixes)) + "."))
    return out


def _tech_11(ev: dict, site: Site) -> list[dict]:
    out = []
    for r in _rows(ev, "pages"):
        if _num(r.get("score"), 100) >= 100:
            continue
        words, heads = int(_num(r.get("words"))), int(_num(r.get("headings")))
        wanted = max(1, math.ceil(words / 200))
        if heads == 0:
            issue = f"{words:,} words with no H2/H3 subheadings"
        else:
            issue = (f"{heads} subheading{'s' if heads > 1 else ''} for {words:,} words "
                     f"({_num(r.get('words_per_heading')):.0f} words per heading; aim for ≤ {_WORDS_PER_HEADING})")
        out.append(_fix(r.get("url"), issue,
                        f"Break the copy into self-contained sections with an H2/H3 every 150–200 words "
                        f"(about {max(1, wanted - heads)} more), each heading naming what its section answers."))
    return sorted(out, key=lambda f: f["issue"])


def _tech_13(ev: dict, site: Site) -> list[dict]:
    rows = sorted(_rows(ev, "pages"), key=lambda r: _num(r.get("text_to_code")))
    return [
        _fix(r.get("url"), f"Readable text is {_num(r.get('text_to_code')):.1f}% of the HTML (full marks at "
                           f"{_TEXT_TO_CODE_OK:.0f}%); {int(_num(r.get('words'))):,} words on the page",
             "Move inline scripts and styles into external files, remove unused markup and hidden duplicate menus, "
             "and keep the main copy in plain HTML.")
        for r in rows if _num(r.get("text_to_code")) < _TEXT_TO_CODE_OK
    ]


def _tech_14(ev: dict, site: Site) -> list[dict]:
    rows = _rows(ev, "missing_alt")
    by_page: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        src = str(r.get("src") or "")
        name = urlparse(src).path.rsplit("/", 1)[-1] or src
        by_page[r.get("page") or ""].append(name)
    out = []
    for page, names in by_page.items():
        if page:
            out.append(_fix(page, f"{len(names)} image{'s' if len(names) > 1 else ''} without alt text: {_list(names)}",
                            "Give each image an alt attribute that says what it shows in a few words; use alt=\"\" "
                            "only for purely decorative images."))
        else:
            # Reports saved before the page was recorded on each image: one row per image instead.
            out.extend(_fix(r.get("src"), "Image has no alt text",
                            "Add an alt attribute that says what the image shows; alt=\"\" only if it is purely decorative.")
                       for r in rows if not r.get("page"))
    return sorted(out, key=lambda f: -int(f["issue"].split()[0]) if f["issue"][0].isdigit() else 0)


def _undated(ev: dict, key: str) -> list[dict]:
    out = []
    for r in _rows(ev, key):
        dates = r.get("dates") or {}
        if r.get("has_date") or dates.get("published") or dates.get("modified"):
            continue
        out.append(_fix(r.get("url"), "No published or last-updated date, visible or in the code",
                        "Show “Published” and “Updated” dates on the page and add datePublished/"
                        "dateModified to its JSON-LD (or article:published_time / article:modified_time meta tags)."))
    return out


def _tech_15(ev: dict, site: Site) -> list[dict]:
    return _undated(ev, "samples")


def _tech_16(ev: dict, site: Site) -> list[dict]:
    if ev.get("organization"):
        present = set(ev.get("fields") or [])
        missing = [f for f in ("name", "logo", "address", "contactPoint", "sameAs") if f not in present]
        if not missing:
            return []
        return [_fix(site.home, f"Organization markup is missing {_list(missing, 5)}",
                     f"Add {_list(missing, 5)} to the Organization JSON-LD on the homepage; sameAs should list "
                     "your LinkedIn, Wikipedia/Wikidata and other official profiles.")]
    return [_fix(site.home, "No Organization markup on the homepage",
                 'Add a <script type="application/ld+json"> block with "@type": "Organization", '
                 f'"name": "{site.company}", "url": "{site.home}", "logo", "address", "contactPoint" and '
                 '"sameAs" (your official LinkedIn, Wikipedia/Wikidata and social profiles).')]


def _tech_17(ev: dict, site: Site) -> list[dict]:
    out = []
    for r in _rows(ev, "pages"):
        if r.get("match") or r.get("applicable") is False:
            continue
        expected = r.get("expected") or "page-type"
        found = _list([str(t) for t in r.get("types") or []]) or "none"
        out.append(_fix(r.get("url"), f"No {expected} markup on this {str(r.get('page_type') or 'page').replace('_', ' ')} page (found: {found})",
                        f'Add a JSON-LD block with "@type": "{expected}" describing this page (name, description, url, '
                        f'and "provider"/"publisher": {site.company}).'))
    return out


def _tech_19(ev: dict, site: Site) -> list[dict]:
    return [
        _fix(r.get("url"), f"Broken JSON-LD: {r.get('why') or 'does not parse'} ({r.get('error')})",
             "Correct the JSON so it parses (escape line breaks and quotes inside values, remove trailing commas), "
             "then confirm it at validator.schema.org.")
        for r in _rows(ev, "errors")
    ]


def _tech_20(ev: dict, site: Site) -> list[dict]:
    out = []
    for r in _rows(ev, "pages"):
        if r.get("ok"):
            continue
        notes = r.get("notes") or []
        url = r.get("url") or ""
        fixes = []
        if "missing canonical" in notes:
            fixes.append(f'add <link rel="canonical" href="{url}"> to the page head')
        if "canonical differs from final URL" in notes:
            fixes.append(f"point the canonical (now {r.get('canonical')}) at this URL, or redirect this URL to the canonical one")
        if any("redirect hops" in n for n in notes):
            fixes.append("link to the final URL directly so it loads without redirects")
        out.append(_fix(url, _cap("; ".join(notes)) or "URL is not clean",
                        _cap("; ".join(fixes) or "give the page one canonical URL") + "."))
    return out


def _tech_22(ev: dict, site: Site) -> list[dict]:
    rows = _rows(ev, "pages")
    title_count: dict[str, int] = defaultdict(int)
    desc_count: dict[str, int] = defaultdict(int)
    for r in rows:
        if (r.get("title") or "").strip():
            title_count[r["title"].strip().lower()] += 1
        if (r.get("description") or "").strip():
            desc_count[r["description"].strip().lower()] += 1
    out = []
    for r in rows:
        title = (r.get("title") or "").strip()
        desc = (r.get("description") or "").strip()
        issues, fixes = [], []
        if not title:
            issues.append("no title")
            fixes.append("write a 20–70 character title naming the page's topic and the brand")
        elif not r.get("title_ok"):
            issues.append(f"title is {r.get('title_len')} characters (aim for 20–70)")
            fixes.append(f"rewrite the title {_quote(title, 60)} to 20–70 characters")
        elif title_count[title.lower()] > 1:
            issues.append(f"title shared with {title_count[title.lower()] - 1} other page(s)")
            fixes.append(f"make {_quote(title, 60)} unique to this page")
        if not desc:
            issues.append("no meta description")
            fixes.append("write a 70–170 character meta description summarising the page")
        elif not r.get("desc_ok"):
            issues.append(f"meta description is {r.get('desc_len')} characters (aim for 70–170)")
            fixes.append("rewrite the meta description to 70–170 characters")
        elif desc_count[desc.lower()] > 1:
            issues.append(f"meta description shared with {desc_count[desc.lower()] - 1} other page(s)")
            fixes.append("write a description specific to this page")
        if issues:
            out.append(_fix(r.get("url"), _cap("; ".join(issues)), _cap("; ".join(fixes)) + "."))
    return out


# --- On-page ----------------------------------------------------------------------------------

def _on_01(ev: dict, site: Site) -> list[dict]:
    by_page: dict[str, list[str]] = defaultdict(list)
    total: dict[str, int] = defaultdict(int)
    for r in _rows(ev, "headings"):
        total[r.get("url")] += 1
        if not r.get("question"):
            by_page[r.get("url")].append(r.get("heading") or "")
    return [
        _fix(url, f"{len(heads)} of {total[url]} headings are not phrased as questions, e.g. {_quote(heads[0], 70)}",
             f"Rewrite headings such as {_quote(heads[0], 50)} as the question that section answers for a buyer "
             "(“What …?”, “How does … work?”, “Which … is right for …?”), "
             "and answer it directly underneath.")
        for url, heads in by_page.items() if url
    ]


def _on_02(ev: dict, site: Site) -> list[dict]:
    out = []
    for r in _rows(ev, "answers"):
        if _num(r.get("score"), 100) >= 100:
            continue
        words = int(_num(r.get("word_count")))
        if words < _ANSWER_MIN:
            change = f"Expand it to a self-contained {_ANSWER_MIN}–{_ANSWER_MAX} word answer"
        else:
            change = f"Tighten it to a self-contained {_ANSWER_MIN}–{_ANSWER_MAX} word answer"
        out.append(_fix(r.get("url"), f"The answer under {_quote(r.get('question'), 70)} is {words} words",
                        f"{change} directly under the heading, opening with the answer itself."))
    return out


def _on_03(ev: dict, site: Site) -> list[dict]:
    return [
        _fix(str(r.get("concept")), f"“{r.get('concept')}” is mentioned {r.get('mentions', 0)} times but never defined",
             f"On the page that covers it most, add one plain sentence that defines it: "
             f"“{_cap(str(r.get('concept')))} means …”, followed by why it matters to the reader.")
        for r in _rows(ev, "concepts") if r.get("defined") is False
    ]


def _on_04(ev: dict, site: Site) -> list[dict]:
    return [
        _fix(r.get("url"), f"Opens with filler rather than the point: {_quote(r.get('opening'), 110)}",
             "Make the first sentence the answer: state what the page offers or concludes and for whom, then give the detail.")
        for r in _rows(ev, "pages") if r.get("generic")
    ]


def _on_05(ev: dict, site: Site) -> list[dict]:
    out = []
    for r in _rows(ev, "pages"):
        if r.get("faq_heading") and r.get("faq_schema"):
            continue
        if not r.get("faq_heading"):
            out.append(_fix(r.get("url"), "No FAQ section on this service/solution page",
                            "Add an FAQ section with 4–6 questions buyers actually ask about this service (cost, "
                            "timeline, fit, process), each with a 40–60 word answer, plus FAQPage JSON-LD."))
        else:
            out.append(_fix(r.get("url"), "FAQ section is not marked up",
                            "Add FAQPage JSON-LD that repeats the visible questions and answers word for word."))
    return out


def _on_06(ev: dict, site: Site) -> list[dict]:
    out = []
    for r in _rows(ev, "pages"):
        if r.get("valid_schema") and r.get("consistent"):
            continue
        if not r.get("schema"):
            out.append(_fix(r.get("url"), "Visible FAQ with no FAQPage markup",
                            "Add FAQPage JSON-LD with one Question/acceptedAnswer pair per visible question, using the same wording."))
        elif not r.get("valid_schema"):
            out.append(_fix(r.get("url"), "FAQPage markup is invalid",
                            "Correct the FAQPage JSON-LD (each mainEntity needs a Question with name and acceptedAnswer.text), "
                            "then confirm it at validator.schema.org."))
        else:
            out.append(_fix(r.get("url"), f"Markup has {r.get('questions_marked_up')} questions; the page shows {r.get('questions_on_page')}",
                            "Make the FAQPage markup match the visible questions and answers exactly."))
    return out


def _on_07(ev: dict, site: Site) -> list[dict]:
    return [
        _fix(r.get("url"), "Buyers compare options here, but the page has no comparison or specification table",
             "Add an HTML <table> comparing the options, plans or specifications (rows = criteria, columns = options), "
             "not an image of one.")
        for r in _rows(ev, "pages") if r.get("intent") and not int(_num(r.get("tables")))
    ]


def _on_09(ev: dict, site: Site) -> list[dict]:
    out = []
    for r in sorted(_rows(ev, "pages"), key=lambda r: -_num(r.get("max_density"))):
        density = _num(r.get("max_density"))
        if density < _DENSITY_OK:
            continue
        top = (r.get("top") or [[None, 0]])[0]
        out.append(_fix(r.get("url"), f"“{top[0]}” makes up {density * 100:.1f}% of the content words "
                                      f"(natural is under {_DENSITY_OK * 100:.1f}%)",
                        f"Use “{top[0]}” where it is the right word and replace repeats with pronouns, "
                        "synonyms or more specific terms."))
    return out


def _on_10(ev: dict, site: Site) -> list[dict]:
    return [
        _fix(r.get("url"), f"Missing: {_list(r.get('missing') or [], 6)}",
             f"Add a short section for each missing point ({_list(r.get('missing') or [], 6)}), written for the "
             "reader this page serves.")
        for r in _rows(ev, "pages") if r.get("missing")
    ]


def _on_11(ev: dict, site: Site) -> list[dict]:
    return [
        _fix(r.get("url"), "No statistic or data point on this major page",
             f"Add one original, citable figure — a result {site.company} measured, a survey finding or a "
             "benchmark — with its source and date.")
        for r in _rows(ev, "pages") if r.get("major") and not int(_num(r.get("count")))
    ]


def _looks_named(byline: str) -> bool:
    """Whether a byline reads like a person's name rather than a placeholder or a team label."""
    text = (byline or "").strip()
    if not text or "{{" in text or "}}" in text:
        return False
    words = [w for w in text.replace("By ", "").split() if w[:1].isalpha()]
    return sum(1 for w in words if w[:1].isupper()) >= 2


def _on_12(ev: dict, site: Site) -> list[dict]:
    out = []
    for r in _rows(ev, "signals"):
        byline = str(r.get("byline") or "").strip()
        if _looks_named(byline):
            continue
        what = f"Byline is {_quote(byline, 60)}" if byline else "No visible author"
        if "{{" in byline:
            what += " — an unfilled template placeholder"
        out.append(_fix(r.get("url"), what,
                        "Show the author's full name, role and credentials, link it to their profile page, and mark "
                        "the author up as a Person in the page's JSON-LD."))
    return out


def _on_13(ev: dict, site: Site) -> list[dict]:
    out = []
    for r in _rows(ev, "pages"):
        parts = (("client", "a named client", "the client's name and sector"),
                 ("quantified", "a measured outcome", "the result in numbers (time saved, cost reduced, % change)"),
                 ("deployment", "deployment detail", "what was delivered, how and over what timeframe"))
        missing = [(label, how) for key, label, how in parts if not r.get(key)]
        if missing and any(r.get(key) for key, _, _ in parts):
            # Only pages already telling a client story: asking a policy page for a client name helps nobody.
            out.append(_fix(r.get("url"), f"Client story is missing {_list([m[0] for m in missing])}",
                            f"Add {_list([m[1] for m in missing])}."))
    return out


def _on_14(ev: dict, site: Site) -> list[dict]:
    return [
        _fix("Claim on the site", f"Unsourced claim: {_quote(r.get('sentence'), 140)}",
             "Cite the primary source and date for this figure in the sentence or a footnote link.")
        for r in _rows(ev, "samples") if r.get("sourced") is False
    ]


def _on_15(ev: dict, site: Site) -> list[dict]:
    return _undated(ev, "samples")


def _on_18(ev: dict, site: Site) -> list[dict]:
    return [
        _fix(r.get("a"), f"Competes with {r.get('b')} for the same question ({_num(r.get('similarity')) * 100:.0f}% similar titles)",
             "Merge the two into one authoritative page and 301-redirect the other, or give each a distinct question "
             "and title so they stop competing.")
        for r in sorted(_rows(ev, "overlapping_pairs"), key=lambda r: -_num(r.get("similarity")))
    ]


def _on_19(ev: dict, site: Site) -> list[dict]:
    out = [
        _fix(r.get("url"), f"Thin page: {int(_num(r.get('words')))} words (under {_THIN_WORDS})",
             f"Expand it to at least {_THIN_WORDS} words of useful content, merge it into a related page, or "
             "mark it noindex if it is not meant to rank.")
        for r in sorted(_rows(ev, "thin"), key=lambda r: _num(r.get("words")))
    ]
    out += [
        _fix(r.get("a"), f"Near-duplicate of {r.get('b')} ({_num(r.get('similarity')) * 100:.0f}% of the text is the same)",
             "Rewrite one page so it covers a distinct topic, or merge them and 301-redirect the duplicate.")
        for r in sorted(_rows(ev, "near_duplicates"), key=lambda r: -_num(r.get("similarity")))
    ]
    return out


def _on_20(ev: dict, site: Site) -> list[dict]:
    out = []
    for r in _rows(ev, "pages"):
        missing = [label for key, label in (("brand", "the brand"), ("category", "the category"), ("geo", "the geography"))
                   if not r.get(key)]
        if missing:
            out.append(_fix(r.get("url"), f"Does not name {_list(missing)}",
                            f"Add one sentence that names {site.company} together with what it is and where it "
                            f"operates, e.g. “{site.company} is a [category] serving [region].”"))
    return out


BUILDERS: dict[str, Callable[[dict, Site], list[dict]]] = {
    "TECH-01": _tech_01, "TECH-02": _tech_02, "TECH-03": _tech_03, "TECH-04": _tech_04,
    "TECH-06": _tech_06, "TECH-07": _tech_07, "TECH-08": _tech_08, "TECH-09": _tech_09,
    "TECH-10": _tech_10, "TECH-11": _tech_11, "TECH-13": _tech_13, "TECH-14": _tech_14,
    "TECH-15": _tech_15, "TECH-16": _tech_16, "TECH-17": _tech_17, "TECH-19": _tech_19,
    "TECH-20": _tech_20, "TECH-22": _tech_22,
    "ON-01": _on_01, "ON-02": _on_02, "ON-03": _on_03, "ON-04": _on_04, "ON-05": _on_05,
    "ON-06": _on_06, "ON-07": _on_07, "ON-09": _on_09, "ON-10": _on_10, "ON-11": _on_11,
    "ON-12": _on_12, "ON-13": _on_13, "ON-14": _on_14, "ON-15": _on_15, "ON-18": _on_18,
    "ON-19": _on_19, "ON-20": _on_20,
}


def site_from_report(report: dict) -> Site:
    domain = report.get("domain") or urlparse(report.get("input_url") or "").netloc or ""
    origin = f"https://{domain}" if domain else (report.get("input_url") or "")
    company = ((report.get("identity") or {}).get("name") or domain.removeprefix("www.")).strip()
    return Site(origin=origin, company=company or domain)


def fixes_for(row: dict, site: Site) -> dict:
    """Every page (or item) this check found needing a change, with the problem and the fix."""
    pid = row.get("parameter_id") or ""
    builder = BUILDERS.get(pid)
    evidence = row.get("evidence") or {}
    if isinstance(evidence, str):
        evidence = {}
    if builder is None:
        return {"parameter_id": pid, "scope": "site", "total": 0, "fixes": [],
                "note": "This check is measured across the site as a whole, not page by page; the recommended fix covers it."}
    if row.get("status") in ("UNKNOWN", "N/A"):
        return {"parameter_id": pid, "scope": "page", "total": 0, "fixes": [],
                "note": "This check was not measured on this audit, so there is no page list."}
    try:
        fixes = [f for f in builder(evidence, site) if f.get("target")]
    except Exception as exc:   # a malformed evidence row must not break the report view
        return {"parameter_id": pid, "scope": "page", "total": 0, "fixes": [],
                "note": f"The page list could not be built from this audit's evidence ({type(exc).__name__})."}
    note = ""
    if not fixes:
        note = "No page needs a change for this check."
    elif pid in ("ON-14",):
        note = "Only a sample of the site's claims was recorded, so this lists that sample."
    return {"parameter_id": pid, "scope": "page", "total": len(fixes), "fixes": fixes, "note": note}


def counts_for(rows: list[dict], site: Site) -> dict[str, int]:
    """How many pages need fixing, per check -- for the report table."""
    return {r["parameter_id"]: fixes_for(r, site)["total"] for r in rows if r.get("parameter_id") in BUILDERS}

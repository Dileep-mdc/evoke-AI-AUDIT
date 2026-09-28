"""Why each parameter looked at the pages it looked at, and not at the rest.

The workbook shows a sample of the data a parameter graded, and a reader's first question is
always the same: this check reports on twelve pages, so what happened to the other 738? Until
now the answer was not written down anywhere. Worse, for several parameters the honest answer
used to be "nothing -- they were silently ignored", which is the defect that made the sample
look like the population.

This module turns that into a sentence per parameter, assembled from two halves:

  * a fixed description of the RULE -- which pages the check is defined to read. This is prose
    about intent and changes rarely.
  * the actual COUNTS for this scan, read from the evidence the handler produced. These are
    never hardcoded, so they cannot drift from what really happened.

A parameter with no entry in SCOPES still produces a usable sentence from the counts alone,
and test_link_reason.py asserts every registry id has one.
"""
from __future__ import annotations

from typing import Optional

# Scope of each parameter, as (what it reads, why the rest is out of scope).
#
# The second half is the part readers actually want: "the other 738 pages" is only alarming
# until you know the check is defined over service pages, or over the homepage alone.
_SITE = "Every page of the site that was successfully crawled."
_SITE_WHY = "No page is excluded by this check's own rule -- only pages that could not be fetched, or that returned a bot-challenge page instead of content, are left out."

SCOPES: dict[str, tuple[str, str]] = {
    # --- Technical: mostly single well-known documents, which is why their page counts are 1
    "TECH-01": ("The site's robots.txt file only.", "No site pages are read. This check is about the crawl rules the site publishes, not its content."),
    "TECH-02": ("The homepage only, fetched a second time with a browser user-agent.", "Other pages are not read: the question is whether the site serves AI crawlers differently from browsers, which one page answers."),
    "TECH-03": ("The /llms.txt file only.", "No site pages are read. This checks for a single well-known file at the site root."),
    "TECH-04": ("The homepage only.", "Other pages are not read: this measures the entry point a first-time visitor and an AI crawler both land on."),
    "TECH-05": ("The sitemap's URL list, compared against every page actually crawled.", "Nothing is excluded. Note this measures how much of the CRAWL appears in the sitemap, and validates the first 15 sitemap URLs in document order."),
    "TECH-06": ("A sample of 15 internal links, re-fetched to see if they resolve.", "The rest are not re-fetched: this is a spot-check for broken links and redirect chains, not a full link audit."),
    "TECH-07": ("The first 12 successfully-fetched pages.", "The rest are not measured. Pages that failed to fetch are excluded by design -- but note that those are often the slowest ones, so this figure describes the healthy part of the site."),
    "TECH-08": ("The homepage only.", "Other pages are not read: this inspects a site-wide signal that is declared once."),
    "TECH-09": ("The pages that were fetched a second time in a browser, compared against the same pages' raw HTML.", "The rest are not re-fetched: a browser render costs orders of magnitude more than an HTTP GET, so a budgeted subset is rendered -- pages whose raw HTML looks like an empty shell first. If the homepage probe shows the site is served fully rendered, that one comparison is the whole finding and nothing else is rendered."),
    "TECH-10": ("Every crawled page that has at least one heading.", "Pages with no headings at all contribute nothing, because there is no hierarchy to assess on them."),
    "TECH-11": ("Every crawled page with at least 40 words of body text.", "Shorter pages are skipped: section length cannot be judged on a page with almost no prose."),
    "TECH-12": ("Every crawled page whose HTML parsed into a document tree.", "Pages that returned no usable HTML (a PDF, an image, a failed fetch) have no tables or lists to count."),
    "TECH-13": (_SITE, _SITE_WHY),
    "TECH-14": ("Every crawled page whose HTML parsed, and every image on it.", "Pages with no parsed HTML carry no images to check for alt text."),
    "TECH-15": (_SITE, _SITE_WHY),
    "TECH-16": ("The homepage only.", "Other pages are not read: this inspects a signal declared once for the whole site."),
    "TECH-17": ("Every crawled page whose page type has an expected schema type (home, service, article, case study).", "Pages typed 'other' or 'utility' have no expected schema, so they are marked not-applicable rather than failed."),
    "TECH-18": (_SITE, "Nothing is excluded by rule; pages simply contribute no author signal if they carry none."),
    "TECH-19": ("Every JSON-LD block found on every crawled page.", "The denominator is blocks, not pages: a page with no structured data contributes nothing rather than counting as a failure."),
    "TECH-20": (_SITE, _SITE_WHY),
    "TECH-21": (_SITE, "Nothing is excluded. If no page declares hreflang the check reports UNKNOWN rather than scoring zero, because a single-market site is not failing anything."),
    "TECH-22": (_SITE, _SITE_WHY),

    # --- On-page
    "ON-01": ("H1-H3 headings on service, solution, home and case-study pages. Navigation, header and footer headings are excluded, as are labels under three words.", "Blog and utility pages are outside the rule. Menu headings are excluded because they are authored once and repeat on every page -- counting them once per page would measure the site's menu rather than its content."),
    "ON-02": ("Every question-style heading found anywhere on the site, and the text directly beneath it.", "Headings that are not phrased as questions are outside the rule: this check scores the ANSWER under a question, so a page with no questions has nothing for it to grade."),
    "ON-03": ("The site's core concepts, searched for across the combined text of every crawled page.", "Nothing is excluded. Each concept is looked for in every page, and the most prose-like mention is the one quoted."),
    "ON-04": (_SITE, "Nothing is excluded by rule. Pages whose opening paragraph could not be extracted contribute no opening to judge."),
    "ON-05": ("Pages typed as service or home, or whose URL contains 'service' or 'solution'.", "Blog posts, contact, careers and policy pages are outside the rule -- the parameter asks for an FAQ on service and solution pages, and demanding one on a careers page would be wrong."),
    "ON-5.1": ("Only pages that carry an FAQ -- either FAQPage structured data or a visible FAQ section.", "Pages with no FAQ at all are outside the rule: this checks whether existing FAQs are marked up correctly, not whether every page has one."),
    "ON-07": ("Every crawled page with body text; the model then decides which of them are pages where a buyer is evaluating options.", "Pages the model judges not to be evaluation pages are excluded from the denominator -- a company news post is not failing for having no comparison table."),
    "ON-08": ("Every list on every crawled page, excluding lists inside navigation, header and footer.", "Menu lists repeat site-wide and are not content. Lists whose items extracted as empty are also excluded and reported separately, because that is an extraction problem rather than a finding about the site."),
    "ON-10": ("Every crawled page with at least 40 content words.", "Shorter pages are skipped: keyword density is meaningless on a page with almost no prose. Because thin pages are excluded, this score describes the more substantial part of the site."),
    "ON-11": ("Service, solution, home and case-study pages, falling back to every page if none are identified.", "Blog and utility pages are outside the rule -- the 8-point rubric describes what a service page should cover."),
    "ON-12": ("Pages typed home, service, case study or about -- the pages a buyer lands on to evaluate the business.", "Blog posts and utility pages are outside the rule: the parameter asks for original data on MAJOR pages, not on every article."),
    "ON-13": ("Every author or byline signal found on any crawled page.", "Pages with no byline contribute nothing rather than counting as failures: the check grades the quality of the bylines that exist."),
    "ON-14": (_SITE, "Nothing is excluded by rule. Pages with no text contribute no proof signals."),
    "ON-15": ("Every sentence on the site that states a claim containing a statistic.", "Sentences without a statistic are outside the rule -- this checks whether factual claims are sourced, and a sentence making no numeric claim has nothing to source."),
    "ON-17": (_SITE, "Nothing is excluded. Pages carrying no published or modified date count against the score rather than being skipped."),
    "ON-18": ("Every crawled page, by title and URL, assessed for which buying-journey stage it serves.", "Nothing is excluded. The score is out of six stages, so it measures coverage of the journey rather than a count of pages."),
    "ON-19": ("Every crawled page with a title; the model then decides which are genuine comparison pages.", "Nothing is excluded. A title matching 'best' or 'vs' is only a candidate -- the model confirms whether the page actually answers a comparison query."),
    "ON-20": ("Every pair of crawled pages whose titles overlap enough to be candidates for competing.", "Pairs with little title overlap are not candidates. Identical titles are also excluded, and the model then confirms which remaining pairs genuinely compete."),
    "ON-21": ("Every crawled page except those typed utility (contact, careers, policy pages).", "Utility pages are outside the rule: a contact page is legitimately short and should not count as thin content diluting the site."),
    "ON-22": (_SITE, _SITE_WHY),

    # --- Off-page: these do not read the site at all
    **{
        pid: ("No pages of your site. This check queries third-party sources -- public search results, Wikipedia and Wikidata -- for how the web describes your company.",
              "Your own pages are deliberately not read: the parameter measures external signals, and a site describing itself proves nothing about them.")
        for pid in (
            "OFF-01", "OFF-02", "OFF-03", "OFF-04", "OFF-05", "OFF-06", "OFF-07", "OFF-08",
            "OFF-09", "OFF-10", "OFF-11", "OFF-12", "OFF-13", "OFF-14", "OFF-15", "OFF-16",
            "OFF-17", "OFF-18",
        )
    },
}

# Where each parameter records how many things it actually scored, best key first. The counts
# come from the handler, never from this file, so they always describe the real run.
_TOTAL_KEYS = (
    "service_pages_total", "eligible_headings", "faq_pages_total", "eligible_pages_total",
    "lists_total", "claims_total", "major_pages", "answers_total", "dated_pages",
    "pages_compared", "pages_assessed", "author_signals_total", "concepts_found_on_site",
    "sampled", "blocks", "images", "page_count", "pages",
)

_UNITS = {
    "eligible_headings": "headings", "lists_total": "lists", "claims_total": "claims",
    "answers_total": "answers", "faq_pages_total": "pages carrying an FAQ",
    "pages_compared": "pages", "author_signals_total": "author signals",
    "concepts_found_on_site": "concepts", "sampled": "links", "blocks": "structured-data blocks",
    "images": "images",
}


# What the MODEL judged, where that differs from what was scored. Left unset means "of them",
# which reads correctly whenever the two units are the same.
_JUDGED_UNITS = {
    "ON-01": "distinct headings",
    "ON-03": "concepts",
    "ON-08": "lists",
    "ON-12": "pages carrying a statistic",
    "ON-13": "bylines",
    "ON-15": "claims",
    "ON-20": "candidate pairs",
}


def _first_count(evidence: dict) -> tuple[Optional[int], str]:
    """The number this parameter actually scored, and what it is a count of."""
    for key in _TOTAL_KEYS:
        value = evidence.get(key)
        if isinstance(value, int):
            return value, _UNITS.get(key, "pages")
    return None, "items"


def link_reason(parameter_id: str, evidence: dict | None) -> str:
    """The 'Why These Pages' cell for one parameter.

    Answers, in order: what this check reads, how much of it there was on this scan, what was
    left out and why, and -- when a model pass was involved -- whether it saw all of it.
    """
    evidence = evidence or {}
    scope, why_not = SCOPES.get(parameter_id, (
        "See the metric column for what this check reads.",
        "No scope description is recorded for this parameter.",
    ))

    parts = [f"WHAT THIS CHECK READS: {scope}"]

    # Off-page parameters measure what the rest of the web says about the company, so the
    # crawl's page counts are not their denominator and quoting them here would imply this
    # check read pages it never opened.
    reads_site_pages = not parameter_id.startswith("OFF-")

    if reads_site_pages:
        count, unit = _first_count(evidence)
        crawled = evidence.get("pages_crawled")
        if count is not None and isinstance(crawled, int) and crawled:
            parts.append(f"ON THIS SCAN: {count:,} {unit} were scored, out of {crawled:,} pages crawled.")
        elif count is not None:
            parts.append(f"ON THIS SCAN: {count:,} {unit} were scored.")

    parts.append(f"WHY THE REST ARE NOT INCLUDED: {why_not}")
    if reads_site_pages:
        parts.append(
            "NOTHING WAS CAPPED: this check has no limit on how many pages it will read. "
            "Every page matching the rule above was scored, and the sample listed in the "
            "previous column is only what fits in a spreadsheet cell."
        )

    # Pages the crawl lost, as opposed to pages this check's rule excludes. Worth separating:
    # one is a scoping decision, the other is a gap in the evidence.
    lost = []
    if reads_site_pages and evidence.get("pages_excluded_fetch_failed"):
        lost.append(f"{evidence['pages_excluded_fetch_failed']:,} could not be fetched")
    if reads_site_pages and evidence.get("pages_excluded_bot_blocked"):
        lost.append(f"{evidence['pages_excluded_bot_blocked']:,} returned a bot-challenge page")
    if lost:
        parts.append("EXCLUDED BY THE CRAWL, NOT BY THIS CHECK: " + " and ".join(lost) +
                     ". These are listed in the failed-links file for this scan.")

    llm = evidence.get("llm")
    if isinstance(llm, dict) and isinstance(llm.get("population"), int):
        population, assessed = llm["population"], llm.get("assessed", 0)
        # Named, because what the model read is not always the same unit as what was scored:
        # ON-20 compares pages but judges PAIRS of them, and "520 of 2,012" beside "1,013
        # pages" reads like a contradiction without it.
        judged_unit = _JUDGED_UNITS.get(parameter_id, "of them")
        if llm.get("complete"):
            parts.append(f"AI READ: all {population:,} {judged_unit}.")
        else:
            detail = f"AI READ: {assessed:,} of {population:,} {judged_unit}."
            if llm.get("capped_at"):
                detail += f" Capped at {llm['capped_at']:,} to bound the cost of this check."
            if llm.get("batches_failed"):
                detail += f" {llm['batches_failed']} batch(es) failed, so this is a partial reading."
            parts.append(detail)

    return "\n".join(parts)

"""Frozen logic for the 22 Technical parameters.

Prose only. The executable rule lives in backend/app/parameters/technical.py; this file
describes it and is the source the parameter workbook is rendered from.
"""

TECHNICAL = {
    "TECH-01": dict(llm=False, metric=(
        "Fetch /robots.txt once per scan and parse it with Protego. For each of the five AI crawlers "
        "(GPTBot, ChatGPT-User, ClaudeBot, Google-Extended, PerplexityBot) evaluate the Allow/Disallow "
        "rules for path \"/\", falling back to the wildcard group when no bot-specific group exists.\n"
        "Score = allowed bots / bots with a definite ALLOWED-or-BLOCKED decision x 100.\n"
        "UNKNOWN if robots.txt cannot be fetched, or if it is non-empty but fails to parse."),
        why=(
        "robots.txt is a formal grammar with one correct interpretation, and Protego implements it. "
        "A language model would add latency, cost and run-to-run variation to a question that already "
        "has an exact, verifiable answer.")),

    "TECH-02": dict(llm=False, metric=(
        "Fetch the homepage once with a normal browser user-agent to establish a baseline response size, "
        "then fetch it again once per AI-crawler user-agent. A bot counts as blocked if it receives HTTP "
        "403, 406 or 429, or if its response body is under 20% of the baseline size (content starvation) "
        "while the baseline itself exceeded 500 bytes.\n"
        "Score = (5 - blocked bots) / 5 x 100.   Confidence 0.6."),
        why=(
        "The evidence is HTTP status codes and response byte counts, both measured directly. Confidence "
        "is held at 0.6 because a site legitimately serving different content per user-agent is "
        "indistinguishable from a block by size alone - a data limitation, not a judgment an LLM could "
        "resolve.")),

    "TECH-03": dict(llm=False, metric=(
        "Fetch /llms.txt. Missing or empty gives 0. Otherwise: 40 base, +20 if the file holds more than 40 "
        "characters of content, +20 x the share of up to 3 sampled markdown links that resolve to a "
        "working URL (or a flat +10 if the file contains no links at all), +20 if a date appears anywhere "
        "in it.\n"
        "Score capped at 100."),
        why=(
        "File presence, HTTP resolution of the links it lists, and a date pattern are all deterministic "
        "checks against a fetched file.")),

    "TECH-04": dict(llm=False, metric=(
        "Scan the rendered homepage HTML for overlay markers (cookie, consent, gdpr, onetrust, cookiebot, "
        "trustarc, cookieyes, modal, overlay, paywall) and for a login form or password input.\n"
        "Score = 100 if neither is present; 70 if markers are present but at least 150 rendered words "
        "still survive; 20 otherwise. Capped at 20 when a login form is present and the page yields "
        "under 80 words.\n"
        "Confidence 0.55."),
        why=(
        "Decided from DOM markers plus the rendered word count. The low confidence reflects that a marker "
        "in the HTML does not prove an overlay actually covers content; proving that needs visual or "
        "viewport analysis, not a language model.")),

    "TECH-05": dict(llm=False, metric=(
        "Read the XML sitemap(s) discovered from robots.txt and the well-known locations. Fetch the first "
        "15 sitemap URLs to test whether they resolve, and compare the set of crawled pages against the "
        "set of sitemap URLs.\n"
        "Validity = resolving sampled URLs / sampled URLs x 100.\n"
        "Coverage = crawled pages that appear in the sitemap / crawled pages x 100.\n"
        "Score = 25 + Validity x 0.45 + Coverage x 0.30, capped at 100.\n"
        "Score 0 if no sitemap URLs were found at all."),
        why="XML parsing, HTTP resolution and set comparison are exact operations."),

    "TECH-06": dict(llm=False, metric=(
        "Take a deterministic sample of 15 internal links from the crawl and fetch each one. A link is "
        "broken if the fetch fails or returns a non-2xx status; it is a redirect chain if it takes 3 or "
        "more hops.\n"
        "Score = (sampled - broken) / sampled x 100 - min(20, chains x 5), floored at 0.\n"
        "Confidence 0.7."),
        why="HTTP status codes and redirect hop counts are facts recorded by the fetcher."),

    "TECH-07": dict(llm=False, metric=(
        "Measure server response time and transferred HTML size across up to 12 successfully crawled "
        "pages, then band each mean:\n"
        "   Latency:  <=800ms -> 100,  <=1800ms -> 70,  <=3000ms -> 40,  else 20\n"
        "   Weight:   <=300KB -> 100,  <=800KB  -> 70,  <=1.5MB  -> 40,  else 20\n"
        "Score = Latency x 0.6 + Weight x 0.4.   Confidence 0.4."),
        why=(
        "Timing and byte size are measured directly. The confidence is deliberately low: this is a proxy, "
        "not Core Web Vitals. Real LCP, INP and CLS require a rendering or field-data source (Lighthouse "
        "or CrUX). That is a missing integration, not something a language model could supply.")),

    "TECH-08": dict(llm=False, metric=(
        "Score = 50 if the homepage final URL is https, + 50 if a viewport meta tag containing \"width\" "
        "is present.\n"
        "Capped at 80 if the HTML contains both \"min-width:\" and \"1200px\", a weak signal of a "
        "fixed-width layout that may overflow on mobile."),
        why="URL scheme and meta-tag presence are read directly from the response and the DOM."),

    "TECH-09": dict(llm=False, metric=(
        "For each of the first 10 pages, strip <script> and <style> from the RAW (pre-JavaScript) HTML, "
        "strip the remaining tags, and count the words left. Compare that with the rendered word count.\n"
        "Per-page ratio = raw words / rendered words x 100, capped at 100 per page.\n"
        "Score = mean of the per-page capped ratios."),
        why=(
        "A word count on two versions of the same document. The per-page cap matters: raw HTML routinely "
        "carries more words than the rendered text (hidden navigation, inline JSON), so an uncapped page "
        "can exceed 100 and conceal a genuinely JavaScript-only page inside the average.")),

    "TECH-10": dict(llm=False, metric=(
        "Per page, start at 100. Subtract 40 if there is no H1, or 25 if there is more than one. Subtract "
        "min(30, skips x 10), where a skip is a jump of more than one heading level between consecutive "
        "headings. Floor each page at 0.\n"
        "Score = mean of per-page scores.\n"
        "Score 0 outright if no headings were found on any page."),
        why="Counting H1 tags and comparing consecutive heading levels is exact."),

    "TECH-11": dict(llm=True, metric=(
        "Heuristic baseline per page: start at 100, -30 if mean words per heading exceeds 220, -20 if "
        "heading density is under 1.5 per 1000 words, or a flat 30 if the page has no H1-H3 at all.\n"
        "LLM pass (authoritative when available): up to 40 real (heading, word count of the text under "
        "that heading up to the next heading) pairs go to the model, which returns how many of those "
        "sections an assistant could lift whole.\n"
        "Score = liftable sections / sections assessed x 100."),
        why=(
        "YES. Whether a section can be lifted as a clean, self-contained passage is a judgment about "
        "whether its heading actually scopes its content. Word counts alone cannot distinguish a focused "
        "200-word section from a rambling one of the same length.\n"
        "The heuristic stays as the fallback when the model is unavailable, so a scan never fails for "
        "lack of an API key.")),

    "TECH-12": dict(llm=False, metric=(
        "Count <table> elements and <ul>/<ol> elements across every crawled page.\n"
        "Score = 100 if tables + lists >= 8; 70 if >= 3; else 40."),
        why=(
        "Element counting is exact.\n"
        "Known limitation: this confirms semantic markup exists but does not detect the failure mode in "
        "the parameter name - a comparison table published as an image. Catching that needs image "
        "analysis (a vision model or OCR), which is not wired into this build.")),

    "TECH-13": dict(llm=False, metric=(
        "Per page:\n"
        "   Text-to-code = visible text characters / total HTML characters x 100, then banded, because a "
        "byte ratio is not a score (a content-rich page sits near 20-25% and never approaches 100):\n"
        "      >=25% -> 100,  >=15% -> 85,  >=10% -> 70,  >=5% -> 45,  else 20\n"
        "   Depth = min(100, words / 400 x 100)\n"
        "   Per-page score = banded text-to-code x 0.4 + Depth x 0.6\n"
        "Score = mean across pages."),
        why="Character ratios and word counts are measured directly from the fetched HTML."),

    "TECH-14": dict(llm=False, metric=(
        "Collect every <img> across the crawl. Alt text counts as usable if it is non-empty, longer than "
        "3 characters, and not one of the placeholder values image/img/logo. Count <video>/<iframe> "
        "elements and pages whose text mentions a transcript.\n"
        "Image score = usable alt / images x 100.\n"
        "Video score = 100 if there are no videos, else transcript signals / videos x 100 (capped 100).\n"
        "Score = Image x 0.7 + Video x 0.3.   Flat 80 if the site has no images at all."),
        why=(
        "Attribute presence and a placeholder blocklist are deterministic.\n"
        "Known limitation: this measures whether alt text exists and is non-trivial, not whether it "
        "actually describes the image. Judging descriptiveness would need a vision model - a deliberate "
        "scope boundary, not an oversight.")),

    "TECH-15": dict(llm=False, metric=(
        "A page counts as dated if it exposes a published or modified date through "
        "article:published_time, article:modified_time or a <time> element, or through "
        "datePublished/dateModified in its JSON-LD.\n"
        "Score = 60 + (dated pages / pages) x 40.\n"
        "The 60 floor is deliberate: dates matter most on articles, so their absence on home and service "
        "pages is treated as partial rather than a total failure."),
        why="Metadata and JSON-LD field presence are read directly."),

    "TECH-16": dict(llm=False, metric=(
        "Find an Organization node in the homepage JSON-LD. Expected fields are name, url, logo and "
        "sameAs; address/location and contactPoint/email/telephone count as a bonus group.\n"
        "Score = (expected fields present / 4) x 80 + 20 if any address or contact field is present.\n"
        "Score 0 if no Organization node exists."),
        why="Checking for named fields in parsed JSON-LD is exact."),

    "TECH-17": dict(llm=False, metric=(
        "Map each page detected type to the schema.org types that genuinely satisfy it:\n"
        "   article     -> Article, BlogPosting, NewsArticle, TechArticle, Report\n"
        "   service     -> Service, Product, Offer, ProfessionalService\n"
        "   about       -> Organization, Corporation, LocalBusiness, AboutPage\n"
        "   home        -> Organization, Corporation, LocalBusiness, WebSite\n"
        "   case_study  -> Article, CaseStudy, CreativeWork\n"
        "Pages of other types are not applicable and are excluded.\n"
        "Score = matched pages / applicable pages x 100, or 50 if no page is applicable."),
        why=(
        "Matching type strings against an explicit allowlist is deterministic. The allowlist is the "
        "point: it replaces a rule that accepted FAQPage as satisfying any expected type, which let a "
        "blog post carrying only FAQPage count as correctly marked-up Article content.")),

    "TECH-18": dict(llm=False, metric=(
        "Collect author signals across the crawl: Person nodes or author fields in JSON-LD, plus visible "
        "[rel=author], .author or .byline elements.\n"
        "Score = 25 if no signal is found; 80 if any signal is found; 95 if any author node carries a "
        "jobTitle.\n"
        "Confidence 0.65."),
        why=(
        "Schema nodes and CSS selectors are read directly.\n"
        "Known limitation: this confirms an author is named and titled, not that the credentials are "
        "real. Verifying that needs external identity sources, not a language model.")),

    "TECH-19": dict(llm=False, metric=(
        "Count every JSON-LD block found across the crawl and how many parsed successfully.\n"
        "Score = valid blocks / total blocks x 100.\n"
        "UNKNOWN when the site has no JSON-LD at all: nothing to validate is not the same as 40% valid, "
        "and whether markup should exist is already scored by TECH-16 and TECH-17."),
        why=(
        "JSON parse success is binary and exact.\n"
        "Known limitation: the \"matches visible content\" half of this parameter name is not currently "
        "evaluated - only syntactic validity is.")),

    "TECH-20": dict(llm=False, metric=(
        "Per page, flag a problem when: no canonical link is present; or an absolute canonical points to "
        "a path that differs from the page own final path (excluding the homepage); or the page took 3 or "
        "more redirect hops.\n"
        "Score = pages with no problem / pages x 100."),
        why="URL comparison and redirect hop counts are exact."),

    "TECH-21": dict(llm=False, metric=(
        "Collect every <link rel=alternate hreflang> across the crawl.\n"
        "UNKNOWN when none is found: a single-market site is not applicable to this check, so it is "
        "excluded from the pillar average rather than passed or failed.\n"
        "When tags are present, a tag is valid if it carries both a lang and an href.\n"
        "Score = valid tags / tags x 100."),
        why=(
        "Attribute presence is exact.\n"
        "Known limitation: reciprocity between pages and the validity of each locale code are not "
        "verified.")),

    "TECH-22": dict(llm=False, metric=(
        "Across every crawled page, measure four things:\n"
        "   Presence               = pages with both a title and a meta description / pages\n"
        "   Title uniqueness       = distinct non-empty titles / non-empty titles\n"
        "   Description uniqueness = distinct non-empty descriptions / non-empty descriptions\n"
        "   Length quality         = pages with a 20-70 char title AND a 70-170 char description / pages\n"
        "Score = Presence x 50 + Title uniqueness x 15 + Description uniqueness x 10 "
        "+ Length quality x 25."),
        why="String lengths and set uniqueness are exact measurements."),
}

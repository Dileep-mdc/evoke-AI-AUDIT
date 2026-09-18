"""Plain-language copy for the parameter workbook.

Two fields per parameter, both written for a reader who is not going to read the code:

  definition  What the parameter is asking about, and why it matters for AI visibility.
              Where a check is a proxy for something it cannot measure directly, the
              definition says so -- the document must not imply coverage we do not have.
  brief       How the score is actually produced, in two or three sentences.

The exact, frozen formula stays in spec_technical/spec_onpage/spec_offpage and is what
backend/app/parameters/frozen_spec.json and formulas.json are built from. This file is the
readable summary of it, and build_parameter_workbook.py fails if the two ever cover
different parameters.
"""
from __future__ import annotations

DEFINITIONS = {

    # --- Technical -----------------------------------------------------------------------

    "TECH-01": dict(
        definition=(
            "Whether robots.txt actually lets the major AI crawlers in. If GPTBot, ClaudeBot or "
            "PerplexityBot are disallowed, the site cannot be read or cited by those assistants "
            "however good its content is."),
        brief=(
            "Fetch robots.txt once per scan and evaluate the Allow/Disallow rules for five named "
            "AI crawlers at the site root, falling back to the wildcard group where no bot-specific "
            "group exists. Score is the share of those crawlers that are allowed. UNKNOWN if "
            "robots.txt cannot be fetched or fails to parse.")),

    "TECH-02": dict(
        definition=(
            "Whether the CDN or firewall blocks AI crawlers even when robots.txt permits them. A "
            "bot-specific 403 or a stripped-down response is a silent block: permission is granted "
            "on paper but the content never reaches the assistant."),
        brief=(
            "Fetch the homepage once with a browser user-agent to set a baseline, then once per AI "
            "crawler user-agent. A crawler counts as blocked on HTTP 403, 406 or 429, or when its "
            "response body is under 20% of the baseline. Score is the share of the five crawlers "
            "not blocked.")),

    "TECH-03": dict(
        definition=(
            "Whether the site publishes an /llms.txt file, the emerging convention for telling AI "
            "systems which pages matter and how to use them. Its presence, working links and a "
            "visible date signal a site actively curated for AI consumption."),
        brief=(
            "Fetch /llms.txt. Missing or empty scores 0. Otherwise 40 base, plus 20 for real "
            "content, up to 20 for the share of sampled markdown links that resolve, and 20 for a "
            "date appearing anywhere in the file. Capped at 100.")),

    "TECH-04": dict(
        definition=(
            "Whether the main content is reachable without dismissing a consent banner, modal or "
            "login. Crawlers do not click Accept, so anything behind an overlay is effectively "
            "invisible to them."),
        brief=(
            "Scan the raw homepage HTML for overlay markers (cookie, consent, GDPR vendors, modal, "
            "overlay, paywall) and for a login or password form. 100 when neither is present, 70 "
            "when markers exist but 150 or more words are still present, 20 otherwise, and capped "
            "at 20 for a login form on a near-empty page.")),

    "TECH-05": dict(
        definition=(
            "Whether an XML sitemap exists, lists URLs that actually resolve, and covers the pages "
            "the site really has. A sitemap is how a crawler finds pages it would not reach by "
            "following links."),
        brief=(
            "Read the sitemaps discovered from robots.txt and the well-known locations, fetch the "
            "first 15 listed URLs to test that they resolve, and compare the crawled page set "
            "against the sitemap. Score is a 25-point base plus weighted validity (45) and coverage "
            "(30). 0 when no sitemap is found at all.")),

    "TECH-06": dict(
        definition=(
            "Whether internal links lead somewhere. Broken links and long redirect chains waste "
            "crawl budget and break the path an assistant follows to reach supporting content."),
        brief=(
            "Take a deterministic sample of 15 internal links and fetch each one. A link is broken "
            "on failure or a non-2xx status, and counts as a chain at 3 or more hops. Score is the "
            "share that resolve, less 5 points per chain up to a 20-point maximum.")),

    "TECH-07": dict(
        definition=(
            "How quickly and cheaply a page delivers its content; slow, heavy pages are crawled "
            "less often and less completely. This is a latency and page-weight proxy: no "
            "Lighthouse or Chromium performance run is wired into this build, so real LCP, INP and "
            "CLS are not measured."),
        brief=(
            "Measure server response time and transferred HTML size across up to 12 crawled pages, "
            "band each mean into four tiers, and combine as latency x 0.6 plus weight x 0.4. "
            "Confidence is held at 0.4 because this is a proxy, not a measured vitals run.")),

    "TECH-08": dict(
        definition=(
            "Whether the site is served over HTTPS and declares a mobile viewport. Both are "
            "baseline trust and accessibility signals, and an insecure or non-responsive page is "
            "discounted by every major crawler."),
        brief=(
            "50 points if the homepage's final URL is HTTPS, plus 50 if a viewport meta tag "
            "containing width is present. Capped at 80 when the HTML carries a fixed-width layout "
            "signal, a weak indication the page may overflow on mobile.")),

    "TECH-09": dict(
        definition=(
            "Whether the content exists in the HTML the server sends or only appears once "
            "JavaScript has run. Many AI crawlers do not execute JavaScript, so client-rendered "
            "copy is invisible to them."),
        brief=(
            "Intended to compare the raw pre-JavaScript word count against a JavaScript-rendered "
            "one. This build has no browser-rendering pass (httpx + lxml + Crawlee only), so there "
            "is no rendered copy to compare against and this parameter always reports UNKNOWN.")),

    "TECH-10": dict(
        definition=(
            "Whether headings form a clean, single-rooted outline. The heading tree is how an "
            "assistant segments a page into passages it can quote, so a missing or duplicated H1 "
            "and skipped levels blur those boundaries."),
        brief=(
            "Per page start at 100, subtract 40 for no H1 or 25 for more than one, and 10 per "
            "skipped heading level up to 30. Score is the mean across pages, or 0 outright if no "
            "page has any headings.")),

    "TECH-11": dict(
        definition=(
            "Whether sections are short and self-contained enough that an assistant can lift one "
            "whole as an answer. A two-thousand-word stretch under a single heading offers no clean "
            "extraction boundary."),
        brief=(
            "The heuristic penalises a high mean words-per-heading and a low heading density per "
            "page. When the model is available it receives up to 40 real heading and "
            "section-length pairs and returns how many of those sections an assistant could lift "
            "whole; the score is liftable sections over sections assessed.")),

    "TECH-12": dict(
        definition=(
            "Whether comparison and specification content is real HTML markup rather than a "
            "screenshot. Text inside an image cannot be read, quoted or cited."),
        brief=(
            "Count <table> elements and <ul>/<ol> elements across every crawled page. 100 at 8 or "
            "more combined, 70 at 3 or more, else 40.")),

    "TECH-13": dict(
        definition=(
            "How much of a page is prose rather than markup and scaffolding, and whether it carries "
            "enough substance to answer anything. A thin page wrapped in a heavy template gives an "
            "assistant little to work with."),
        brief=(
            "Per page, band the visible-text-to-HTML character ratio into five tiers -- a "
            "content-rich page sits near 20-25%, so the raw ratio is not itself a score -- and "
            "compute depth against a 400-word target. Per-page score is 40% ratio band plus 60% "
            "depth; the score is the mean across pages.")),

    "TECH-14": dict(
        definition=(
            "Whether non-text media carries a text equivalent. Alt text and transcripts are the "
            "only route by which the content of an image or video enters an assistant's index."),
        brief=(
            "Alt text counts as usable when it is non-empty, longer than 3 characters and not a "
            "placeholder such as image, img or logo. The image score is the usable share; the video "
            "score is transcript signals over video embeds, or 100 where there are no videos. Score "
            "is image x 0.7 plus video x 0.3.")),

    "TECH-15": dict(
        definition=(
            "Whether pages expose when they were published and last changed. Assistants weight "
            "recency heavily, and an undated page is treated as being of unknown age."),
        brief=(
            "A page counts as dated if it exposes article:published_time, article:modified_time, a "
            "<time> element, or datePublished/dateModified in its JSON-LD. Score is 60 plus 40 "
            "times the dated share; the 60 floor reflects that dates matter most on articles, not "
            "on home and service pages.")),

    "TECH-16": dict(
        definition=(
            "Whether the homepage carries Organization schema naming the company, its logo, contact "
            "details and official profiles. This is the record that lets an assistant resolve the "
            "site to a real-world entity rather than treat it as loose text."),
        brief=(
            "Find an Organization node in the homepage JSON-LD and check for name, url, logo and "
            "sameAs, with address or contact fields as a bonus group. Score is the share of the "
            "four expected fields x 80, plus 20 for any address or contact field. 0 if no "
            "Organization node exists.")),

    "TECH-17": dict(
        definition=(
            "Whether each page's schema type matches what the page actually is -- Article, Service, "
            "Organization and so on. Correct type markup tells an assistant how to read the page "
            "instead of inferring it from layout."),
        brief=(
            "Map each detected page type to the schema.org types that genuinely satisfy it, and "
            "exclude page types the check does not apply to. Score is matched pages over applicable "
            "pages, or 50 when no page is applicable.")),

    "TECH-18": dict(
        definition=(
            "Whether content is attributed in markup to an identifiable person with a stated role. "
            "Author identity is a primary expertise signal for anything an assistant treats as "
            "advice."),
        brief=(
            "Collect author signals across the crawl: Person nodes or author fields in JSON-LD, "
            "plus visible [rel=author], .author or .byline elements. 25 when no signal is found, 80 "
            "when any is found, and 95 when an author node also carries a jobTitle.")),

    "TECH-19": dict(
        definition=(
            "Whether the structured data on the site actually parses. Malformed JSON-LD is "
            "discarded silently, so a site can carry extensive markup and get no benefit from any "
            "of it."),
        brief=(
            "Count every JSON-LD block found across the crawl and how many parsed successfully; the "
            "score is the valid share. UNKNOWN when the site has no JSON-LD at all, since whether "
            "markup should exist is already scored by TECH-16 and TECH-17.")),

    "TECH-20": dict(
        definition=(
            "Whether each page has one canonical address. Duplicate URLs for the same content split "
            "the signals an assistant uses to decide which version to cite."),
        brief=(
            "Flag a page when it carries no canonical link, when an absolute canonical points at a "
            "different path than the page's own final path (the homepage excepted), or when it took "
            "3 or more redirect hops. Score is the share of pages with no problem.")),

    "TECH-21": dict(
        definition=(
            "Whether a multi-market site declares which language and region each page version "
            "serves. Without hreflang, assistants can surface the wrong regional page or treat the "
            "variants as duplicates."),
        brief=(
            "Collect every <link rel=alternate hreflang> across the crawl; a tag is valid when it "
            "carries both a lang and an href, and the score is the valid share. UNKNOWN when none "
            "is found: a single-market site is not applicable, so it is excluded from the pillar "
            "average rather than passed or failed.")),

    "TECH-22": dict(
        definition=(
            "Whether every page has a title and a meta description, and whether they are distinct "
            "from one another. These are the short summaries an assistant reads to decide what a "
            "page is about before reading the page itself."),
        brief=(
            "Across the crawl, measure presence of both fields, title uniqueness, description "
            "uniqueness, and length quality (a 20-70 character title and a 70-170 character "
            "description). Score is presence x 50 plus title uniqueness x 15 plus description "
            "uniqueness x 10 plus length quality x 25.")),

    # --- On-Page -------------------------------------------------------------------------

    "ON-01": dict(
        definition=(
            "Whether headings are written as the questions buyers actually ask rather than as "
            "labels. A question heading matches the shape of a prompt, so the section beneath it is "
            "far more likely to be retrieved as an answer."),
        brief=(
            "Eligible headings are H1-H3 on service, home and case-study pages with at least 3 "
            "words that are not bare navigation labels. The heuristic counts interrogative "
            "openers and question marks; when available the model reads up to 40 of them and "
            "decides which are questions a buyer would actually ask. The resulting share is "
            "banded into five tiers; 30 when there are no eligible headings at all.")),

    "ON-02": dict(
        definition=(
            "Whether each question heading is followed by a complete answer that stands on its own. "
            "An assistant lifts a passage without its surroundings, so an answer that depends on "
            "the paragraph before it is unusable."),
        brief=(
            "For every question-phrased heading, take the text up to the next heading as the "
            "answer. 70 base at 20 or more words, plus 30 in the 40-60 word target window or 15 in "
            "a 25-90 word band, capped at 100 per answer. Score is the mean; 25 with a diagnostic "
            "when there are no question headings to score.")),

    "ON-03": dict(
        definition=(
            "Whether the site plainly defines the concepts it is built around instead of assuming "
            "the reader knows them. Definition sentences are the passages assistants quote most "
            "readily when asked what something is."),
        brief=(
            "Concepts are derived from the site's own navigation and service-page titles, never a "
            "fixed keyword list. The heuristic tests a 200-character window around each first "
            "mention for a definition pattern; when available the model judges whether that window "
            "is a clear, complete definition. Score is concepts defined over concepts assessed.")),

    "ON-04": dict(
        definition=(
            "Whether a page states its answer up front rather than building to it. Assistants read "
            "from the top and weight the opening heavily, so a page that opens with throat-clearing "
            "gives away its best extraction."),
        brief=(
            "Take the opening of up to 12 pages: the first two substantial sentences, up to 400 "
            "characters. The heuristic bands on whether the opening names a site-specific term and "
            "avoids filler openings; when available the model judges whether each opening leads "
            "with something concrete. Score is openings leading with substance over openings "
            "assessed.")),

    "ON-05": dict(
        definition=(
            "Whether service and solution pages carry an FAQ. FAQ blocks are the densest question "
            "and answer format on a site and map directly onto the way buyers prompt an "
            "assistant."),
        brief=(
            "Identify service and solution pages by page type or a service/solution URL. A page "
            "qualifies with an FAQ heading or FAQPage schema, and the score is the qualifying "
            "share. UNKNOWN when the site has no service or solution pages, since there is nothing "
            "for the check to apply to.")),

    "ON-5.1": dict(
        definition=(
            "Whether FAQ content is also marked up as FAQPage structured data, so the questions and "
            "answers are machine-readable rather than only visually formatted. The answer-"
            "consistency half of this parameter -- whether the schema answers match the visible "
            "Q&A -- is semantic and is not currently verified."),
        brief=(
            "Consider only pages that have FAQ schema or a visible FAQ mention. Award 50 for valid "
            "FAQPage JSON-LD and 50 for a visible FAQ, and take the mean across those pages. 20 "
            "when neither appears anywhere on the site.")),

    "ON-07": dict(
        definition=(
            "Whether pages where a buyer is actively comparing options present that comparison as a "
            "real table. A table is the cleanest structure for an assistant to read "
            "attribute-by-attribute differences from."),
        brief=(
            "The heuristic marks a page as evaluation-intent on vs, compare, alternative, pricing, "
            "feature or capability signals, or on being a service page. When available the model "
            "decides which pages are genuinely evaluation pages -- it sets the denominator -- while "
            "the numerator stays the measured fact of whether a real HTML table exists. 40 when "
            "none qualify.")),

    "ON-08": dict(
        definition=(
            "Whether lists carry real procedural or evaluative content rather than acting as "
            "decoration. A genuine step or criteria list is directly liftable as a structured "
            "answer."),
        brief=(
            "Collect <ul>/<ol> with 2 or more items that sit outside nav, header and footer, so "
            "repeated menus are excluded. The heuristic marks a list useful at 3 or more items "
            "mentioning step, how, benefit, includes, deliver or process; when available the model "
            "judges genuine usefulness across up to 30 lists. 35 when no content lists exist.")),

    "ON-10": dict(
        definition=(
            "Whether copy repeats one term far beyond what natural prose does. Heavy repetition "
            "reads as manipulation and degrades the quality of any passage lifted from the page."),
        brief=(
            "For each page with 40 or more content words, compute the density of its single most "
            "frequent content word, then average across pages and band at under 3.5% to 100, under "
            "6% to 75, else 45. Function words are removed first, without which \"the\" would be the "
            "peak term on almost every English page. UNKNOWN if no page has enough copy.")),

    "ON-11": dict(
        definition=(
            "Whether a service page actually covers what a buyer needs in order to decide, or only "
            "asserts that it does. A gap in coverage is where an assistant turns to a competitor's "
            "page to complete the answer."),
        brief=(
            "The rubric is 8 points a complete service page should cover: problem, approach, what "
            "is included, outcomes, who it is for, commercial model, proof and next step. The "
            "heuristic tests keyword presence; when available the model counts how many points each "
            "page actually covers across a 12-page spread. Score is the mean of points covered over "
            "8.")),

    "ON-12": dict(
        definition=(
            "Whether important pages carry a concrete statistic worth citing. A specific number is "
            "what makes a page quotable rather than merely paraphrasable."),
        brief=(
            "Major pages are home, service, case-study and about pages, not every blog post. A "
            "statistic is a percentage, an N+ figure, a currency amount, a multiplier or a scaled "
            "number; bare four-digit years are excluded so a copyright line does not count. The "
            "heuristic scores the share of major pages carrying one; when available the model "
            "judges whether those statistics are original to the company rather than quoted, and "
            "that rate scales the score.")),

    "ON-13": dict(
        definition=(
            "Whether content carries a named author with a stated role and a real profile. This is "
            "the on-page half of the expertise signal that TECH-18 checks in markup. The linked "
            "profile is not fetched, so that the person is real and reachable is still not "
            "independently verified."),
        brief=(
            "Collect visible [rel=author], .author or .byline elements and Person nodes in JSON-LD, "
            "keeping the byline text. Without the model, 80 if any author signal exists anywhere, "
            "else 30. With it, up to 20 bylines are read and scored half for naming a real person "
            "and half for stating that person's role, floored at 30.")),

    "ON-14": dict(
        definition=(
            "Whether case-study and proof content names who the work was for, what it measurably "
            "achieved and how it was delivered. An unattributed claim is not citable evidence."),
        brief=(
            "The rubric is 25 for a client mention, 30 for a quantified figure, 25 for deployment "
            "language, and a 20 bonus when both appear. The heuristic applies it by regex across "
            "all pages; when available the model applies the same rubric by reading a 20-page "
            "spread, deciding whether a specific client is actually named rather than the generic "
            "word used.")),

    "ON-15": dict(
        definition=(
            "Whether factual claims sit next to a citation. An unsourced statistic is a claim an "
            "assistant has no reason to repeat. Without the model the check establishes only that "
            "something source-like sits near the claim, not that a reader could check it."),
        brief=(
            "A claim is a sentence of more than 6 words containing a statistic, using the ON-12 "
            "pattern with years excluded. The heuristic counts a claim as sourced on nearby link "
            "anchor text or the words source, according to, or report; when available the model "
            "reads up to 24 claims and decides which are attributed to a source a reader could "
            "actually check. 35 when no claims are found.")),

    "ON-17": dict(
        definition=(
            "Whether pages show they have been kept current. Stale content is discounted, and on "
            "fast-moving topics it is actively misleading. The check measures date presence only: "
            "no age comparison against topic shelf life is made, so a page dated 2009 scores as a "
            "page dated last week."),
        brief=(
            "Score is 50 plus 50 times the share of pages exposing a published or modified date.")),

    "ON-18": dict(
        definition=(
            "Whether the site has content for every stage a buyer moves through, not only the "
            "bottom of the funnel. A missing stage is a point at which the buyer's question gets "
            "answered by somebody else."),
        brief=(
            "The six stages are awareness, qualification, comparison, objection, trust and "
            "decision. The heuristic keyword-matches page titles, opening text and URLs; when "
            "available the model reads the titles and URLs of a 60-page spread and decides which "
            "stages have content. Score is stages covered over 6.")),

    "ON-19": dict(
        definition=(
            "Whether the site has pages built for the high-intent comparison queries buyers "
            "actually run. These are the queries an assistant is most often asked to answer "
            "directly. Whether those pages cover the specific industries and markets that matter "
            "to this business is not measured -- that needs a target list the audit is not given."),
        brief=(
            "The heuristic matches page URLs and titles against best ... for, vs, versus and "
            "alternative; when available the model reads the titles and URLs of a 30-page spread "
            "and decides which genuinely serve a high-intent comparison query. 80 at 3 or more "
            "such pages, 45 at one or more, else 15.")),

    "ON-20": dict(
        definition=(
            "Whether two pages target the same question. When they do, neither becomes the "
            "definitive answer and an assistant has no clear page to cite."),
        brief=(
            "Title token sets are compared by Jaccard similarity to find candidate pairs at 0.7 or "
            "above; an exact prefix filter gives a result identical to comparing every pair but "
            "runs in seconds at the 2,500-page crawl ceiling. When available the model confirms "
            "which candidates really answer the same buyer question, and only confirmed pairs "
            "count. The clean share is banded into five tiers.")),

    "ON-21": dict(
        definition=(
            "Whether the site carries pages too thin or too similar to stand on their own. They "
            "dilute the topical signal of the pages that do deserve to be cited."),
        brief=(
            "Thin pages are non-utility pages under 180 words; near-duplicates are pages whose "
            "first 400 characters overlap at Jaccard 0.72 or above, found with the same prefix "
            "filter as ON-20. The clean share is banded into five tiers. UNKNOWN when there are no "
            "content pages.")),

    "ON-22": dict(
        definition=(
            "Whether the brand is stated together with what it does and where it operates. That "
            "co-occurrence is how an assistant learns to associate the company with a category and "
            "a market."),
        brief=(
            "Category terms are derived from the site's own navigation and service-page titles, and "
            "geographies from its structured data, address text and a generic reach list. A page "
            "qualifies when its copy contains a brand term, a category term and a geography term; "
            "the score is the qualifying share.")),

    # --- Off-Page ------------------------------------------------------------------------

    "OFF-01": dict(
        definition=(
            "Whether the company exists as a structured entity in Wikidata. Wikidata is a primary "
            "source for entity resolution: it is how an assistant confirms the company is a real, "
            "distinct organisation rather than a name on a page."),
        brief=(
            "Query the Wikidata search API for the company name and pick the hit whose label or "
            "description contains the brand term or an organisation word. 0 with no hits; otherwise "
            "70 base, plus 20 if the brand appears in the label and 10 if the entity has a "
            "description. UNKNOWN if the API is unreachable.")),

    "OFF-02": dict(
        definition=(
            "Whether an independent reference page describes the company. Reference sources carry "
            "disproportionate weight because assistants treat them as corroboration rather than "
            "marketing."),
        brief=(
            "Query the Wikipedia search API. The heuristic scores 80 when a result title contains "
            "the company's first word, else 20. When available the model decides whether any result "
            "is genuinely about this company rather than a same-named one or a disambiguation page, "
            "and rates entity accuracy and source authority. UNKNOWN if the API is unreachable.")),

    "OFF-03": dict(
        definition=(
            "Whether the company has a knowledge-graph entity behind search. The Google Knowledge "
            "Panel itself cannot be scraped reliably, so a confidently brand-matched Wikidata "
            "entity stands in as a proxy signal and confidence is held at 0.4."),
        brief=(
            "Reuse the Wikidata entity resolved for OFF-01. UNKNOWN when no entity confidently "
            "matches the brand or the API is unreachable; otherwise 50 base, plus 30 if the entity "
            "has a description and 20 if the brand appears in its label.")),

    "OFF-04": dict(
        definition=(
            "Whether the company is discoverable on the major business-data platforms, a common "
            "corroboration path for firmographic facts. Field-level profile completeness is not "
            "verified -- that needs a licensed company-data API -- so this is scored and reported "
            "as a presence signal only."),
        brief=(
            "One search for the exact company name scoped to linkedin.com, crunchbase.com, "
            "bloomberg.com and zoominfo.com, matching the hostnames behind the result links. Score "
            "is platforms with at least one result over 4. UNKNOWN when the search endpoint returns "
            "anything other than a clean HTTP 200, since a 202 shell page is a bot challenge rather "
            "than zero results.")),

    "OFF-05": dict(
        definition=(
            "Whether the company's identity details agree wherever they appear; conflicting names "
            "or addresses make an entity harder to resolve confidently. Only one external record is "
            "available to compare against, so consistency ACROSS sources -- what the parameter "
            "asks for -- cannot currently be established."),
        brief=(
            "Compare the site's own name, domain and derived office locations against the top "
            "Wikidata hit. 70 when the brand term appears in the Wikidata entity label, else 40.")),

    "OFF-06": dict(
        definition=(
            "Whether certification and partner-tier claims can be confirmed against the body that "
            "issued them. Issuer registries are not queried, so this reports claims found on the "
            "site as claimed-but-unverified rather than asserting that they are real."),
        brief=(
            "Scan the site copy for certification claims: certified or accredited, compliance, "
            "licensed, an ISO standard number, or a partner tier such as premier, gold or platinum "
            "partner. 40 when no claim is found, 55 when claims are found, the cap reflecting that "
            "they are unverified.")),

    "OFF-07": dict(
        definition=(
            "Whether the company has a discoverable profile on the major B2B review platforms. "
            "These are among the most frequently cited third-party sources for vendor comparisons. "
            "Completeness within each platform would need that platform's API; presence is what is "
            "observable here."),
        brief=(
            "One search for the exact company name scoped to g2.com, clutch.co, gartner.com and "
            "trustradius.com, matching the hostnames behind the result links. Score is platforms "
            "found over 4. UNKNOWN when the search endpoint does not return a clean 200.")),

    "OFF-08": dict(
        definition=(
            "Whether the company has a healthy, current review footprint relative to its market. No "
            "review API is connected, so per-review dates, velocity and a named competitor "
            "benchmark are not measured -- only a coarse volume proxy from search hits."),
        brief=(
            "Count matching result URLs across the same four review platforms. Score is min(60, 15 "
            "+ hits x 15). The 60 cap is deliberate: with recency, velocity and a competitor set "
            "unmeasured this parameter can never return PASS, which is an accurate statement of how "
            "far the measurement reaches.")),

    "OFF-09": dict(
        definition=(
            "Whether third parties file the company under the same category it claims for itself. A "
            "mismatch means assistants retrieve it for the wrong set of questions."),
        brief=(
            "Search the four review platforms and extract result titles and snippets, and derive "
            "the site's own categories from its navigation and service pages. The heuristic tests "
            "for a literal category match; when available the model decides whether the snippets "
            "place the company in the same or a clearly equivalent category. 85 on a match, 30 "
            "otherwise.")),

    "OFF-10": dict(
        definition=(
            "Whether the company is discussed in community venues. Assistants draw heavily on forum "
            "content because it carries unprompted, practitioner-level opinion. Whether each "
            "mention is substantive or spam is not judged."),
        brief=(
            "One search for the exact company name scoped to reddit.com, stackoverflow.com and "
            "quora.com, counting matching result URLs. 15 for none, 40 for one or two, 65 for three "
            "or more.")),

    "OFF-11": dict(
        definition=(
            "Whether the company has a discoverable video footprint. Caption and transcript "
            "availability, which is part of the parameter's intent, is not verified -- that would "
            "need the YouTube API."),
        brief=(
            "One search for the exact company name scoped to youtube.com, counting result URLs. 15 "
            "for none, 45 for one or two, 65 for three or more.")),

    "OFF-12": dict(
        definition=(
            "Whether the company's experts appear in venues that produce indexable, transcribed "
            "content. Whether those appearances are actually indexed and transcribed would need the "
            "source platforms and is not verified here."),
        brief=(
            "One search for the company name together with podcast, webinar or conference, counting "
            "the approximate number of results. 60 at three or more, 30 at one or more, else 15.")),

    "OFF-13": dict(
        definition=(
            "Whether links to the company come from relevant, authoritative sites. No backlink "
            "provider (Ahrefs, Moz, Majestic) is connected, so link authority, topical relevance "
            "and follow status are not measured -- only a count of pages that mention the domain."),
        brief=(
            "Search for the domain while excluding the site's own host, and count distinct external "
            "pages mentioning it. Score is min(55, 10 + pages x 9). Capped at 55, this parameter "
            "cannot return PASS until a backlink index is connected.")),

    "OFF-14": dict(
        definition=(
            "Whether the brand is mentioned on third-party pages, with or without a link. "
            "Assistants register the mention itself, so unlinked coverage still builds entity "
            "association. Whether each mention omits a link back is not individually verified."),
        brief=(
            "Search for the exact company name excluding the site's own domain, and count distinct "
            "external mentioning pages. 15 for none, 40 for one to three, 65 for four or more.")),

    "OFF-15": dict(
        definition=(
            "Which independent sources an assistant reaches for when answering a buyer question in "
            "the company's category, and whether this company is among them. This is the closest "
            "direct read on AI visibility in the audit; it approximates one assistant's behaviour, "
            "not a multi-assistant citation study."),
        brief=(
            "Derive the site's primary topic from its own navigation and service pages, then ask "
            "the model which real, specific websites an assistant would reference for a buyer "
            "question on that topic and whether this site would plausibly be among them. 85 if "
            "included, 30 if sources are named without it, 15 if none are named. UNKNOWN when LLM "
            "scoring is disabled or the call fails.")),

    "OFF-16": dict(
        definition=(
            "Whether the company appears on the roundup and best-of pages that buyers and "
            "assistants consult. The check detects the brand appearing in the results, not "
            "confirmed inclusion on an authoritative list, which would mean fetching and reading "
            "each candidate page."),
        brief=(
            "Search for best {primary topic} together with the company name, and test whether the "
            "brand term appears in the returned results HTML. 50 if mentioned, else 20.")),

    "OFF-17": dict(
        definition=(
            "Whether the company shows up on alternatives-to pages, which is where buyers already "
            "in market look. No competitor list is configured, so this measures inclusion on "
            "alternatives-style pages generally rather than against named competitors."),
        brief=(
            "Search for the company name together with alternatives or alternative to, and count "
            "result URLs whose address contains alternative. 90 at two or more, 60 at one or more, "
            "else 20.")),

    "OFF-18": dict(
        definition=(
            "Whether analysts and trade press cover the company, and whether what they say about it "
            "is correct. An inaccurate third-party description is worse than none, because it "
            "propagates."),
        brief=(
            "Search for the company name with gartner, forrester, idc, analyst report or industry "
            "report, and extract result titles and snippets. The heuristic scores 55 when snippets "
            "are returned and 20 when none are; when available the model judges whether they "
            "describe this company accurately. 80 if accurate, 40 if not.")),
}

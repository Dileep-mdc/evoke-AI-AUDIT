"""Frozen logic for the 22 On-Page parameters.

Prose only. The executable rule lives in backend/app/parameters/onpage.py.
"""

ONPAGE = {
    "ON-01": dict(llm=False, metric=(
        "Eligible headings are H1-H3 on service, home and case-study pages that have at least 3 words and "
        "are not a bare navigation label (home, about, services, blog, pricing ...). A heading counts as "
        "a question if it opens with how/what/why/when/where/which/who/can/should/is/are/do/does/will or "
        "ends with a question mark.\n"
        "Question share = question headings / eligible headings, then banded:\n"
        "   >=33% -> 100,  >=20% -> 85,  >=10% -> 65,  >=3% -> 45,  else 25\n"
        "Score 30 if the site has no eligible headings at all.   Confidence 0.75."),
        why=(
        "A regex decides interrogative form, which is exact and reproducible. Banding replaces a raw "
        "share, which required about 90% of every heading to be a question for full marks - no "
        "well-written page does that, so the check could only ever return FAIL.\n"
        "Known limitation: the regex detects question FORM, not whether it is a question a BUYER would "
        "ask. Tightening that to intent is the clearest candidate for a future model pass.")),

    "ON-02": dict(llm=False, metric=(
        "For every question-phrased H1-H3, take the text between it and the next heading as the answer.\n"
        "   Base    = 70 if the answer runs to 20 words or more, else 30\n"
        "   Bonus   = +30 if the answer is 40-60 words (the target window), or +15 if it is 25-90 words\n"
        "   Per-answer score capped at 100\n"
        "Score = mean across all answers.\n"
        "Score 25, with a diagnostic naming the reason, when the site has no question headings to score."),
        why=(
        "NO - and this is a known gap, recorded here rather than papered over. Word-count windows are "
        "deterministic, but the parameter asks whether the answer is SELF-CONTAINED, which is a "
        "comprehension judgment a word count cannot make: a 50-word answer that begins \"As mentioned "
        "above...\" scores full marks today.\n"
        "The registry previously claimed this parameter was AI-assisted; no prompt was ever written for "
        "it. The registry has been corrected to match the code, and this is the highest-value place to "
        "add a model pass next.")),

    "ON-03": dict(llm=True, metric=(
        "Derive the core concepts from the site own navigation and service-page titles (never a fixed "
        "keyword list - every audited site is a different business).\n"
        "Heuristic baseline: for each concept, look at a 200-character window around its first mention "
        "and test for a definition pattern (concept + is/are/means/refers to/helps).\n"
        "LLM pass (authoritative when available): the model receives each (concept, surrounding window) "
        "pair and decides whether the text gives a clear, complete definition rather than a passing "
        "mention.\n"
        "Score = concepts defined / concepts assessed x 100.   Score 30 if no concept could be derived."),
        why=(
        "YES. Whether a passage truly DEFINES a concept or merely mentions it is reading comprehension. "
        "The regex recognises exactly one sentence shape and misses every definition written another "
        "way, while also passing text that happens to contain \"is\" near the term.")),

    "ON-04": dict(llm=True, metric=(
        "Take the opening (first two substantial sentences, up to 400 characters) of up to 12 pages.\n"
        "Heuristic baseline: 85 if the opening names a site-specific term and avoids filler openings "
        "(\"welcome to\", \"in today's\", \"we are a leading\"), 55 if it names a term but also opens with "
        "filler, 35 otherwise.\n"
        "LLM pass (authoritative when available): the model judges per opening whether it leads with a "
        "specific, concrete statement rather than boilerplate.\n"
        "Score = openings leading with substance / openings assessed x 100."),
        why=(
        "YES. \"Leads with the conclusion\" is a statement about meaning, not vocabulary. A page can open "
        "with a brand term and still say nothing, or open with none of the keywords and be perfectly "
        "direct.")),

    "ON-05": dict(llm=False, metric=(
        "Identify service and solution pages (page type service or home, or a URL containing "
        "service/solution). A page satisfies the check if it has a heading matching FAQ / frequently "
        "asked, or carries FAQPage schema.\n"
        "Score = qualifying pages / service pages x 100.\n"
        "UNKNOWN when the site has no service or solution pages, since there is nothing the check "
        "applies to."),
        why=(
        "Heading regex and schema type presence are exact.\n"
        "The UNKNOWN path replaces a fallback that graded EVERY crawled page when no service page was "
        "found, which demanded an FAQ section on contact and careers pages too.")),

    "ON-06": dict(llm=False, metric=(
        "Consider only pages that have FAQ schema or a visible FAQ mention.\n"
        "Per page: 50 if valid FAQPage JSON-LD is present + 50 if a visible FAQ is present.\n"
        "Score = mean across those pages.   Score 20 if neither appears anywhere on the site."),
        why=(
        "Schema presence and a text match are deterministic.\n"
        "Known limitation: the \"answer consistency\" half of the parameter - whether the schema answers "
        "match the visible Q&A - is not verified. That comparison is semantic and would need a model.")),

    "ON-07": dict(llm=True, metric=(
        "Heuristic baseline: a page shows evaluation intent if its title, opening text or URL contains "
        "vs / compar / alternative / pricing / feature / capability, or it is a service page.\n"
        "LLM pass (authoritative when available): a page-type-spread sample of 24 pages goes to the "
        "model, which decides which are pages where a buyer is actively evaluating or comparing options. "
        "The numerator stays the measured fact - does that page carry a real HTML <table>.\n"
        "Score = evaluation pages with a table / evaluation pages x 100.   Score 40 if none qualify."),
        why=(
        "YES. Which pages are evaluation pages is a judgment about purpose: a pricing page qualifies "
        "without containing \"vs\", and a blog post titled \"X vs Y\" often does not. The keyword list "
        "both over- and under-selects, and it sets the DENOMINATOR, so getting it wrong skews the whole "
        "score.")),

    "ON-08": dict(llm=True, metric=(
        "Collect content lists - <ul>/<ol> with 2 or more items that are not inside <nav>, <header> or "
        "<footer>, so repeated menus are excluded.\n"
        "Heuristic baseline: a list is useful if it has 3 or more items and mentions "
        "step/how/benefit/includ/deliver/process.\n"
        "LLM pass (authoritative when available): up to 30 lists go to the model, which judges whether "
        "each is genuinely useful for answering a question rather than decorative filler.\n"
        "Score = useful lists / lists assessed x 100.   Score 35 if no content lists exist."),
        why=(
        "YES. Usefulness of a list is a reading judgment. The keyword test only recognises a list that "
        "happens to contain one of six words, so it misses a genuine specification or evaluation-criteria "
        "list written without them.")),

    "ON-09": dict(llm=True, metric=(
        "Lexical half: for each page with at least 40 content words, compute the density of its single "
        "most frequent CONTENT word (function words removed). Average across pages and band:\n"
        "   <3.5% -> 100,  <6% -> 75,  else 45\n"
        "LLM half: excerpts from up to 8 pages go to the model, which returns a naturalness rating "
        "0-100.\n"
        "Score = lexical x 0.4 + naturalness x 0.6 when the model is available, else the lexical band "
        "alone.   UNKNOWN if no page has enough copy."),
        why=(
        "YES. Fluency is inherently a language judgment. Density can show that a term repeats, but not "
        "whether the prose reads naturally - stiff, machine-written copy can have perfectly ordinary "
        "term frequencies.")),

    "ON-10": dict(llm=False, metric=(
        "The same measurement as the lexical half of ON-09: peak single CONTENT-word density per page "
        "(function words removed), averaged across pages, banded:\n"
        "   <3.5% -> 100,  <6% -> 75,  else 45\n"
        "UNKNOWN if no page has at least 40 content words."),
        why=(
        "NO, deliberately. Keyword stuffing is a measurable statistical property, so this half of the "
        "pair is kept fully deterministic and reproducible: it cannot drift with model behaviour, and "
        "ON-09 and ON-10 do not both move on the same model call.\n"
        "The stopword filter is what makes it work at all. Counting function words made \"the\" the peak "
        "term on essentially every English page at 4-7%, which sat above the worst band - so every site "
        "scored the same floor and the check reported nothing.")),

    "ON-11": dict(llm=True, metric=(
        "The rubric is 8 points a complete service page should cover: problem/need, approach or "
        "methodology, what is included, outcomes or benefits, who it is for, pricing or commercial model, "
        "proof, and a clear next step.\n"
        "Heuristic baseline: keyword presence per rubric point.\n"
        "LLM pass (authoritative when available): excerpts from a page-type-spread sample of 12 pages go "
        "to the model, which counts how many rubric points each page ACTUALLY covers. The denominator is "
        "unchanged - the same pages, out of the same 8 points.\n"
        "Score = mean(points covered / 8) x 100."),
        why=(
        "YES. Whether a page really covers \"outcomes\" or \"proof\" is comprehension, not keyword "
        "presence. A case study that describes a measured result covers \"outcomes\" without using the "
        "word; a page with a \"Benefits\" heading and no substance does not.")),

    "ON-12": dict(llm=False, metric=(
        "Major pages are home, service, case-study and about pages (not every blog post).\n"
        "A statistic is a percentage, an N+ figure, a currency amount, a multiplier (3x), a "
        "thousands-separated number, or a scaled figure (2.5 million). Bare four-digit years are "
        "explicitly excluded.\n"
        "Score = major pages carrying at least one statistic / major pages x 100."),
        why=(
        "Pattern detection is exact, and the pattern is the fix: the previous rule matched any 2+ digit "
        "number, so a copyright year, street number or phone fragment satisfied \"has an original "
        "statistic\".\n"
        "Known limitation: this cannot tell an ORIGINAL statistic from one quoted off a third party. "
        "That distinction needs a model, or a source check.")),

    "ON-13": dict(llm=False, metric=(
        "Collect visible [rel=author], .author or .byline elements and Person nodes in JSON-LD across the "
        "crawl.\n"
        "Score = 80 if any author signal exists anywhere, else 30."),
        why=(
        "Selector and schema presence are exact.\n"
        "Known limitation: this is a site-level yes/no. It does not check per-page coverage, nor that "
        "the named person has real credentials or a reachable profile, which is what the parameter "
        "actually asks for.")),

    "ON-14": dict(llm=False, metric=(
        "Per page: 25 if the copy mentions a client/customer/partner, + 30 if it contains a quantified "
        "figure, + 25 if it describes deployment (deploy/implementation/production/rolled out), + 20 "
        "bonus when both a client mention and a figure are present.\n"
        "Score = mean across pages.   Confidence 0.65."),
        why=(
        "Regex signals are deterministic.\n"
        "Known limitation: this cannot distinguish a NAMED client from the generic word \"client\", "
        "which is precisely what the parameter asks for. A model reading the passage would do this "
        "materially better and is the natural next upgrade.")),

    "ON-15": dict(llm=False, metric=(
        "A claim is a sentence of more than 6 words containing a statistic (same pattern as ON-12, years "
        "excluded). A claim counts as sourced if the sentence contains link anchor text from that page, "
        "or the words source / according to / report.\n"
        "Score = sourced claims / claims x 100.   Score 35 if no claims were found.   Confidence 0.55."),
        why=(
        "Regex and anchor-text matching are deterministic.\n"
        "Known limitation: this detects that SOMETHING source-like sits near the claim. It does not "
        "verify the source is primary, is dated, or actually supports the claim - all of which the "
        "parameter name asks for and none of which a regex can establish.")),

    "ON-16": dict(llm=False, metric=(
        "Applicable pages are article and service pages. A page qualifies if its copy matches reviewed by "
        "/ technically reviewed / approver / medically reviewed / fact-checked.\n"
        "Score = applicable pages with a reviewer / applicable pages x 100.\n"
        "UNKNOWN when the site has no article or service pages."),
        why=(
        "Regex presence over an explicitly defined page set is exact.\n"
        "Numerator and denominator are now the same set of pages. Previously reviewer signals were "
        "counted site-wide but divided by article/service pages only, so a byline elsewhere could push "
        "the ratio past 100% - visible only because the result was clamped on the way out.")),

    "ON-17": dict(llm=False, metric=(
        "Score = 50 + (pages exposing a published or modified date / pages) x 50.\n"
        "Confidence 0.6."),
        why=(
        "Date metadata presence is exact.\n"
        "Known limitation: the parameter asks for updates WITHIN TOPIC SHELF LIFE, and no age comparison "
        "is made at all - a page dated 2009 scores exactly like one dated last week. Closing that needs "
        "date arithmetic plus a per-topic shelf-life judgment, not just presence.")),

    "ON-18": dict(llm=True, metric=(
        "The six buyer-journey stages are awareness, qualification, comparison, objection, trust and "
        "decision.\n"
        "Heuristic baseline: keyword matching over each page title, opening text and URL.\n"
        "LLM pass (authoritative when available): the titles and URLs of a page-type-spread sample of 60 "
        "pages go to the model, which decides which of the six stages the site has content for.\n"
        "Score = stages covered / 6 x 100."),
        why=(
        "YES. What stage a page serves is a judgment about purpose. A pricing page aids comparison "
        "whether or not it contains the word \"vs\"; a security page handles objections without using "
        "the word \"objection\".")),

    "ON-19": dict(llm=False, metric=(
        "Match each page URL and title against best ... for / vs / versus / alternative.\n"
        "Score = 80 if 3 or more such pages exist; 45 if at least one; else 15.   Confidence 0.7."),
        why=(
        "URL and title pattern matching is exact.\n"
        "Known limitation: this counts whether such pages EXIST, not whether they cover the industries "
        "and markets that matter to this business.")),

    "ON-20": dict(llm=False, metric=(
        "Compare page titles as token sets (4+ character tokens) using Jaccard similarity. Two pages "
        "compete when similarity >= 0.7 and the titles are not identical strings.\n"
        "Comparison uses an exact prefix filter, not all pairs, so the result is identical to comparing "
        "every pair but runs in seconds at the 2500-page crawl ceiling.\n"
        "Clean share = 1 - (pages involved in a competing pair / pages compared), then banded:\n"
        "   100% -> 100,  >=95% -> 90,  >=85% -> 75,  >=70% -> 60,  else 40\n"
        "UNKNOWN if no page has a title.   Confidence 0.6."),
        why=(
        "NO. Set similarity is exact, reproducible and cheap. Sending thousands of title pairs to a model "
        "would be slow, costly and non-deterministic for a measurement that has a precise mathematical "
        "definition.\n"
        "Banding by share rather than a flat penalty per pair is the fix: 12 points per pair meant nine "
        "overlapping titles bottomed out the check whether the site had 20 pages or 2000.")),

    "ON-21": dict(llm=False, metric=(
        "Thin pages are non-utility pages under 180 words. Near-duplicates are pages whose first 400 "
        "characters overlap at Jaccard >= 0.72, found with the same exact prefix filter as ON-20.\n"
        "Clean share = 1 - (pages that are thin or duplicated / pages assessed), then banded:\n"
        "   >=95% -> 100,  >=85% -> 85,  >=70% -> 70,  >=50% -> 55,  else 40\n"
        "UNKNOWN if there are no content pages."),
        why=(
        "NO. Word counts and set similarity are exact.\n"
        "Scoring by share is the fix: the old absolute penalty (8 points per thin page, 10 per duplicate "
        "pair) hit its cap at eight thin pages, so a 2000-page site with eight thin pages scored "
        "identically to a 10-page site that was almost entirely thin.")),

    "ON-22": dict(llm=False, metric=(
        "Derive the site own category terms from its navigation and service-page titles, and its "
        "geographies from its structured data and address text, plus a generic reach list (global, "
        "worldwide, United States, Europe ...).\n"
        "A page qualifies when its copy contains a brand term AND a category term AND a geography term.\n"
        "Score = qualifying pages / pages x 100.   Confidence 0.7."),
        why=(
        "Term presence checked against vocabularies derived from the site itself is deterministic, and "
        "deriving them per scan keeps the check valid for any business rather than hard-coding one "
        "company terms.\n"
        "Known limitation: co-occurrence anywhere on the page is weaker than the parameter intent, which "
        "is the brand named TOGETHER WITH its category and geography in the same statement.")),
}

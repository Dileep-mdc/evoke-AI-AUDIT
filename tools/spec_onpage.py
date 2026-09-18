"""Frozen logic for the 20 On-Page parameters.

Prose only. The executable rule lives in backend/app/parameters/onpage.py.
"""

ONPAGE = {
    "ON-01": dict(llm=True, metric=(
        "Eligible headings are H1-H3 on service, home and case-study pages that have at least 3 words and "
        "are not a bare navigation label (home, about, services, blog, pricing ...).\n"
        "Heuristic baseline: a heading counts as a question if it opens with "
        "how/what/why/when/where/which/who/can/should/is/are/do/does/will or ends with a question mark.\n"
        "LLM pass (authoritative when available): up to 40 headings, sampled at a stride across the "
        "eligible set so one heading-heavy page cannot fill the sample, go to the model, which decides "
        "which are questions a BUYER would actually ask.\n"
        "Question share = buyer questions / headings assessed, then banded:\n"
        "   >=33% -> 100,  >=20% -> 85,  >=10% -> 65,  >=3% -> 45,  else 25\n"
        "Score 30 if the site has no eligible headings at all."),
        why=(
        "YES. The parameter asks for headings phrased as the questions buyers ask, which is a statement "
        "about intent, not grammar. \"What We Do\" has interrogative form and no buyer has ever typed "
        "it; \"Pricing for mid-market teams\" has none and answers a question buyers ask constantly. "
        "The regex scores both exactly backwards.\n"
        "The banding is kept either way: a raw share required about 90% of every heading to be a "
        "question for full marks - no well-written page does that, so the check could only ever "
        "return FAIL.")),

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

    "ON-5.1": dict(llm=False, metric=(
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

    "ON-10": dict(llm=False, metric=(
        "Peak single CONTENT-word density per page (function words removed), averaged across pages, "
        "banded:\n"
        "   <3.5% -> 100,  <6% -> 75,  else 45\n"
        "UNKNOWN if no page has at least 40 content words."),
        why=(
        "NO, deliberately. Keyword stuffing is a measurable statistical property, so the check is kept "
        "fully deterministic and reproducible: it cannot drift with model behaviour.\n"
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

    "ON-12": dict(llm=True, metric=(
        "Major pages are home, service, case-study and about pages (not every blog post).\n"
        "A statistic is a percentage, an N+ figure, a currency amount, a multiplier (3x), a "
        "thousands-separated number, or a scaled figure (2.5 million). Bare four-digit years are "
        "explicitly excluded.\n"
        "Heuristic baseline: Score = major pages carrying at least one statistic / major pages x 100.\n"
        "LLM pass (authoritative when available): up to 24 major pages that carry a statistic go to the "
        "model with their figures and an excerpt, and it decides whether at least one of them is "
        "ORIGINAL to this company rather than quoted off a third party.\n"
        "Score = (major pages with a statistic / major pages) x (pages judged original / pages "
        "assessed) x 100, so the denominator stays every major page and only the numerator moves."),
        why=(
        "YES. The pattern establishes that a statistic is PRESENT; the parameter asks for an ORIGINAL, "
        "citable one. A figure quoted off an analyst firm satisfies the regex and fails the parameter, "
        "and telling the two apart means reading the sentence around the number.\n"
        "The pattern itself stays deterministic and is the other half of the fix: the previous rule "
        "matched any 2+ digit number, so a copyright year, street number or phone fragment satisfied "
        "\"has an original statistic\".")),

    "ON-13": dict(llm=True, metric=(
        "Collect visible [rel=author], .author or .byline elements and Person nodes in JSON-LD across the "
        "crawl, capturing the byline TEXT rather than only the fact that an element matched.\n"
        "Heuristic baseline: Score = 80 if any author signal exists anywhere, else 30.\n"
        "LLM pass (authoritative when available): up to 20 bylines go to the model, which decides for "
        "each whether it names a specific real person - not a company, and not a generic label such as "
        "\"Admin\" or \"Editorial Team\" - and whether it states that person's role or credentials.\n"
        "Score = named people / assessed x 50 + named people who also state a role / assessed x 50, "
        "floored at 30: markup that names nobody is no worse than no markup at all."),
        why=(
        "YES. The selector proves a byline element exists, which is not what the parameter asks for. "
        "\"Posted by Admin\" and \"Dr Jane Roe, Principal Data Engineer\" both match it and are not "
        "remotely the same expertise signal; separating them means reading the text.\n"
        "Known limitation that remains: the linked profile is still not fetched to confirm the person "
        "is real and reachable. That is a data-collection gap, not a judgment a model can close.")),

    "ON-14": dict(llm=True, metric=(
        "Heuristic baseline, per page: 25 if the copy mentions a client/customer/partner, + 30 if it "
        "contains a quantified figure, + 25 if it describes deployment (deploy/implementation/"
        "production/rolled out), + 20 bonus when both a client mention and a figure are present. "
        "Score = mean across pages.\n"
        "LLM pass (authoritative when available): a page-type-spread sample of 20 pages goes to the "
        "model, which judges the same three signals by reading them - does the page NAME a specific "
        "client organisation, is the outcome genuinely quantified, is deployment actually described.\n"
        "Score = mean of the same 25/30/25/+20 rubric applied to the model's verdicts, so the rubric is "
        "unchanged and only the reading of each signal improves."),
        why=(
        "YES. The regex cannot distinguish a NAMED client from the generic word \"client\", which is "
        "precisely what the parameter asks for, and it counts any two-digit number as a quantified "
        "outcome - so a phone number or a street number scores the same as a measured result.")),

    "ON-15": dict(llm=True, metric=(
        "A claim is a sentence of more than 6 words containing a statistic (same pattern as ON-12, years "
        "excluded).\n"
        "Heuristic baseline: a claim counts as sourced if the sentence contains link anchor text from "
        "that page, or the words source / according to / report. Score = sourced claims / claims x 100.\n"
        "LLM pass (authoritative when available): up to 24 claims go to the model together with any "
        "adjacent link anchor text, and it decides whether each is attributed to a source a reader "
        "could actually check - a named organisation, publication, study or dated report.\n"
        "Score = attributed claims / claims assessed x 100.   Score 35 if no claims were found."),
        why=(
        "YES. The regex establishes only that something source-LIKE sits near the claim: the bare word "
        "\"report\" anywhere in the sentence satisfies it, as does a link whose anchor text happens to "
        "appear in the text. Whether the source is identifiable and checkable - which is the "
        "parameter - requires reading the sentence.")),

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

    "ON-19": dict(llm=True, metric=(
        "Heuristic baseline: match each page URL and title against best ... for / vs / versus / "
        "alternative.\n"
        "LLM pass (authoritative when available): the titles and URLs of a page-type-spread sample of 30 "
        "pages go to the model, which decides which are genuinely built to answer a high-intent "
        "comparison query rather than merely containing the words.\n"
        "Score = 80 if 3 or more such pages are found; 45 if at least one; else 15."),
        why=(
        "YES. The token match both over- and under-selects, and it is the whole measurement. A blog post "
        "titled \"Docker vs Podman\" matches and is not a buyer comparison page; \"Choosing an ERP for "
        "manufacturers\" is one and matches nothing. Which pages serve a high-intent comparison query is "
        "a judgment about purpose.\n"
        "Known limitation that remains: this counts whether such pages EXIST, not whether they cover the "
        "industries and markets that matter to this business - that needs a target list the audit is "
        "not given.")),

    "ON-20": dict(llm=True, metric=(
        "Compare page titles as token sets (4+ character tokens) using Jaccard similarity. Two pages are "
        "CANDIDATES to compete when similarity >= 0.7 and the titles are not identical strings.\n"
        "Comparison uses an exact prefix filter, not all pairs, so the result is identical to comparing "
        "every pair but runs in seconds at the 2500-page crawl ceiling.\n"
        "LLM pass (authoritative when available): up to 20 candidate pairs go to the model, which "
        "confirms which of them really answer the same buyer question. Unconfirmed pairs stop counting "
        "against the site; candidates past the sample keep their heuristic verdict rather than being "
        "silently dropped.\n"
        "Clean share = 1 - (pages in a competing pair / pages compared), then banded:\n"
        "   100% -> 100,  >=95% -> 90,  >=85% -> 75,  >=70% -> 60,  else 40\n"
        "UNKNOWN if no page has a title."),
        why=(
        "YES. Set similarity is exact, but it is a CANDIDATE FILTER, not the finding. A service page and "
        "the case study about that service share most of their tokens without competing, and so does the "
        "same service written for two different industries. Whether two pages really compete for one "
        "buyer question is a judgment the similarity score cannot make, and it sets the whole penalty.\n"
        "The model only ever narrows the candidate set, so the check cannot invent cannibalisation that "
        "the deterministic filter did not find.\n"
        "Banding by share rather than a flat penalty per pair stays: 12 points per pair meant nine "
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

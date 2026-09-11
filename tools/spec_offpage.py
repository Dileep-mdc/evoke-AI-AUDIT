"""Frozen logic for the 18 Off-Page parameters.

Prose only. The executable rule lives in backend/app/parameters/offpage.py.

A note that applies to most of this pillar: off-page signals are measured through public
endpoints (the Wikidata and Wikipedia APIs, and DuckDuckGo HTML search). Where a parameter
genuinely needs licensed data - a backlink index, a review-platform API, a company-data
provider - the check is scored as an explicitly capped proxy and says so in its evidence.
That cap is a statement about data availability, not about the site.
"""

OFFPAGE = {
    "OFF-01": dict(llm=False, metric=(
        "Query the Wikidata wbsearchentities API with the company name. Pick the hit whose label or "
        "description contains the brand term or an organisation word (company, business, corporation, "
        "firm ...); otherwise fall back to the first hit.\n"
        "Score = 0 if there are no hits at all; else 70 base, +20 if the brand appears in the entity "
        "label, +10 if the entity has a description. Capped at 100.\n"
        "UNKNOWN if the Wikidata API is unreachable."),
        why=(
        "NO. The response is structured JSON with explicit label and description fields, so matching is "
        "a direct field comparison.\n"
        "The registry previously declared this AI-assisted; no model call was ever wired. The registry "
        "now matches the code. Entity disambiguation here is simpler than in OFF-02 because Wikidata "
        "returns an explicit entity type.")),

    "OFF-02": dict(llm=True, metric=(
        "Query the Wikipedia search API with the company name.\n"
        "Heuristic baseline: 80 if a result title contains the company first word, else 20.\n"
        "LLM pass (authoritative when available): the model decides whether any result is genuinely a "
        "reference page about THIS specific company rather than a different company or a disambiguation "
        "page, and rates entity accuracy and source authority 0-100.\n"
        "Score = 50 + source authority x 0.3 + entity accuracy x 0.2 when the page is about the company; "
        "20 when it is not. Capped at 100.\n"
        "UNKNOWN if the Wikipedia API is unreachable."),
        why=(
        "YES. Telling a page genuinely about this company from a same-named company, a disambiguation "
        "page or an unrelated topic is exactly what substring matching gets wrong, and it flips the "
        "score between 80 and 20.")),

    "OFF-03": dict(llm=False, metric=(
        "Google Knowledge Panel UI cannot be scraped reliably, so a confidently brand-matched Wikidata "
        "entity is used as the proxy signal.\n"
        "UNKNOWN when no Wikidata entity confidently matches the brand, or the API is unreachable.\n"
        "Otherwise Score = 50 + 30 if the entity has a description + 20 if the brand appears in its "
        "label. Capped at 100.   Confidence 0.4."),
        why=(
        "NO. The check reads a structured API. The real limitation is the DATA SOURCE - the live "
        "Knowledge Panel is not accessible - and a language model cannot observe a page it has no "
        "access to. Asking one whether a panel exists would produce a confident guess, not a "
        "measurement.")),

    "OFF-04": dict(llm=False, metric=(
        "Run a DuckDuckGo HTML search for the exact company name restricted to linkedin.com, "
        "crunchbase.com, bloomberg.com and zoominfo.com, then match the hostnames behind the result "
        "redirect links.\n"
        "Score = platforms with at least one result / 4 x 100.   Confidence 0.4.\n"
        "UNKNOWN when DuckDuckGo returns anything other than a clean HTTP 200 (a 202 shell page is a "
        "bot-detection challenge, not zero results)."),
        why=(
        "NO. Hostname matching on result URLs is exact.\n"
        "The parameter asks for profile COMPLETENESS, which needs a licensed company-data API. A model "
        "cannot substitute for data the system does not have - it would infer field completeness it "
        "never saw. The check is therefore scored and labelled as a presence signal only.")),

    "OFF-05": dict(llm=False, metric=(
        "Compare the site own name, domain and derived office locations against the top Wikidata hit.\n"
        "Score = 70 if the brand term appears in the Wikidata entity label, else 40.   Confidence 0.5."),
        why=(
        "NO. The blocker is missing directory records to compare against, not the comparison itself. "
        "With only one external record available, name/address/detail consistency ACROSS SOURCES - what "
        "the parameter actually asks for - cannot be established by any means, model included.")),

    "OFF-06": dict(llm=False, metric=(
        "Scan the site copy for certification claims: certified/accredited, compliance, licensed, an ISO "
        "standard number, or a partner tier (certified/authorized/premier/gold/platinum/elite partner).\n"
        "Score = 40 if no claim is found; 55 if claims are found, reflecting claimed-but-unverified.\n"
        "Confidence 0.4."),
        why=(
        "NO, and deliberately so. The parameter asks whether certifications are VERIFIABLE OFF-SITE. "
        "Issuer registries are not queried, and asking a language model to confirm a certification is "
        "the textbook way to manufacture a false positive - it would produce a plausible answer with no "
        "underlying record. The honest result is a capped score that states the claim is unverified.")),

    "OFF-07": dict(llm=False, metric=(
        "DuckDuckGo HTML search for the exact company name restricted to g2.com, clutch.co, gartner.com "
        "and trustradius.com; match hostnames behind the result links.\n"
        "Score = platforms found / 4 x 100.   Confidence 0.4.\n"
        "UNKNOWN when DuckDuckGo does not return a clean 200."),
        why=(
        "NO. Hostname matching is exact. Profile completeness within each platform needs that platform "
        "API; presence is what is actually observable here, and the score says so.")),

    "OFF-08": dict(llm=False, metric=(
        "DuckDuckGo HTML search for company reviews across the same four review platforms; count "
        "matching result URLs.\n"
        "Score = min(60, 15 + hits x 15) - capped at 60 because recency, velocity and a competitor "
        "benchmark cannot be measured from a search-result count.   Confidence 0.3."),
        why=(
        "NO. What is missing is licensed review data (counts, dates, trend, competitor set). A model "
        "cannot invent it.\n"
        "Note the consequence: this parameter cannot exceed 60, so it can never return PASS. That is an "
        "accurate statement about measurement coverage, and it does structurally limit the Off-Page "
        "pillar until a review API is connected.")),

    "OFF-09": dict(llm=True, metric=(
        "DuckDuckGo HTML search across the four review platforms; extract result titles and snippets. "
        "Derive the site own business categories from its navigation and service pages.\n"
        "Heuristic baseline: 65 if any derived category string appears literally in the snippets, else "
        "35.\n"
        "LLM pass (authoritative when available): the model decides whether the third-party snippets "
        "place the company in the same or a clearly equivalent category.\n"
        "Score = 85 if it matches, 30 if it does not.   Score 30 if no snippets are returned."),
        why=(
        "YES. Category equivalence is semantic. \"Custom software development\" and \"bespoke software "
        "engineering services\" are the same category and share almost no literal tokens, so substring "
        "matching reports a mismatch that is not real.")),

    "OFF-10": dict(llm=False, metric=(
        "DuckDuckGo HTML search for the exact company name restricted to reddit.com, stackoverflow.com "
        "and quora.com; count matching result URLs.\n"
        "Score = 15 if no hits; 40 if 1-2; 65 if 3 or more.   Confidence 0.35."),
        why=(
        "NO. This counts results, which is exact.\n"
        "Known limitation: whether each mention is substantive or spam is not judged. A model could "
        "assess the mentions it can see, but the search endpoint returns only titles and snippets, so "
        "the input needed for that judgment is not available.")),

    "OFF-11": dict(llm=False, metric=(
        "DuckDuckGo HTML search for the exact company name restricted to youtube.com; count result "
        "URLs.\n"
        "Score = 15 if none; 45 if 1-2; 65 if 3 or more.   Confidence 0.35."),
        why=(
        "NO. Counting results is exact.\n"
        "Known limitation: transcript and caption availability - part of the parameter intent - is not "
        "verified, which would need the YouTube API.")),

    "OFF-12": dict(llm=False, metric=(
        "DuckDuckGo HTML search for the company name together with podcast, webinar or conference; count "
        "the approximate number of results.\n"
        "Score = 60 if 3 or more; 30 if at least one; else 15.   Confidence 0.35."),
        why=(
        "NO. Result counting is exact.\n"
        "The registry previously declared this AI-assisted; no model call exists and the registry now "
        "matches the code. Whether appearances are indexed and transcribed would need the source "
        "platforms, not a model.")),

    "OFF-13": dict(llm=False, metric=(
        "DuckDuckGo HTML search for the domain excluding the site own host; count distinct external "
        "pages that mention it.\n"
        "Score = min(55, 10 + external pages x 9) - capped at 55 because link authority, topical "
        "relevance and follow/nofollow status require a backlink index.   Confidence 0.25."),
        why=(
        "NO. The parameter asks about QUALITY and RELEVANCE of linking sites, which requires a backlink "
        "index (Ahrefs, Moz, Majestic). A model has no access to link graphs and would be guessing.\n"
        "Note the consequence: capped at 55, this parameter can never return PASS until a backlink "
        "provider is connected.")),

    "OFF-14": dict(llm=False, metric=(
        "DuckDuckGo HTML search for the exact company name excluding the site own domain; count distinct "
        "external mentioning pages.\n"
        "Score = 15 if none; 40 if 1-3; 65 if 4 or more.   Confidence 0.3."),
        why=(
        "NO. Counting off-domain mentions is exact.\n"
        "Known limitation: whether each mention OMITS a link back - the \"unlinked\" half of the "
        "parameter - is not individually verified, which would require fetching and parsing each "
        "mentioning page.")),

    "OFF-15": dict(llm=True, metric=(
        "Derive the site primary topic from its own navigation and service pages. Ask the model which "
        "real, specific websites an AI assistant would most likely reference when answering a buyer "
        "question about that topic, and whether this site would plausibly be among them.\n"
        "Score = 85 if the site is included; 30 if sources are named without it; 15 if none are named.\n"
        "UNKNOWN when LLM scoring is disabled or the call fails.   Confidence 0.4."),
        why=(
        "YES, necessarily. The parameter is literally \"which third-party sites do AI assistants cite "
        "for these topics\", and the only way to approximate that is to ask an assistant. There is no "
        "deterministic data source for it.\n"
        "Stated limitation: this is a single-model approximation using this tool own LLM, not a "
        "multi-assistant citation audit, which is why confidence is held at 0.4.")),

    "OFF-16": dict(llm=False, metric=(
        "DuckDuckGo HTML search for best {primary topic} together with the company name; test whether "
        "the brand term appears in the returned SERP HTML.\n"
        "Score = 50 if mentioned, else 20.   Confidence 0.35."),
        why=(
        "NO. A substring test over returned HTML is exact.\n"
        "Known limitation: this detects that the brand appears somewhere in the results, not that it is "
        "listed ON an authoritative best/top list. Confirming the latter means fetching and reading each "
        "candidate page - a data-collection gap first, and only then a judgment a model could help "
        "with.")),

    "OFF-17": dict(llm=False, metric=(
        "DuckDuckGo HTML search for the company name together with alternatives / alternative to; count "
        "result URLs whose address contains \"alternative\".\n"
        "Score = 90 if 2 or more; 60 if at least one; else 20.   Confidence 0.35."),
        why=(
        "NO. URL pattern matching is exact.\n"
        "Known limitation: no competitor list is configured, so this measures inclusion on "
        "alternatives-style pages generally rather than against named competitors.")),

    "OFF-18": dict(llm=True, metric=(
        "DuckDuckGo HTML search for the company name together with gartner / forrester / idc / analyst "
        "report / industry report; extract result titles and snippets.\n"
        "Heuristic baseline: 55 when any snippets are returned, 20 when none are.\n"
        "LLM pass (authoritative when available): the model judges whether the snippets describe THIS "
        "company accurately, rather than confusing it with another or repeating outdated facts.\n"
        "Score = 80 if accurate, 40 if not.   Confidence 0.55 with the model, 0.35 without."),
        why=(
        "YES. Detecting a snippet that describes a different company with a similar name, or repeats a "
        "stale fact, requires reading and understanding the text. Counting results can only establish "
        "that coverage exists, not that it is correct - and \"description accuracy\" is half the "
        "parameter.")),
}

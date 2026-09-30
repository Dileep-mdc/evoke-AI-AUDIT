from __future__ import annotations

import json

SYSTEM = (
    "You are a strict, careful grader for a website audit tool. You are given extracted "
    "website content and must make a specific judgment call about it. Reply with ONLY a "
    "single JSON object matching the requested shape -- no prose, no markdown fences."
)


def on03_prompt(concepts: list[dict]) -> str:
    return (
        "For each core concept below, decide whether the surrounding text on the page gives "
        "the reader a clear, complete definition of that concept (not just a passing mention).\n\n"
        f"Concepts and surrounding text (JSON array of {{concept, window}}):\n{json.dumps(concepts)}\n\n"
        'Reply as JSON: {"results": [{"concept": <string>, "defined": <bool>}, ...]} '
        "with one entry per concept given, in the same order."
    )


def category_prompt(company_name: str, categories: list[str], snippets: list[dict]) -> str:
    return (
        f'Given the company "{company_name}", whose own site describes its business using these '
        f"categories: {json.dumps(categories)}, decide whether the third-party search snippets "
        "below place the company in the same or a clearly equivalent category (not a mismatched "
        "or unrelated one).\n\n"
        f"Search snippets (JSON array of {{title, snippet}}):\n{json.dumps(snippets)}\n\n"
        'Reply as JSON: {"matches": <bool>, "confidence": <float 0-100>}'
    )


def analyst_prompt(company_name: str, snippets: list[dict]) -> str:
    return (
        f'Given the company "{company_name}", judge whether the analyst/trade-press search '
        "snippets below describe the company accurately (not confusing it with a different "
        "company, and not repeating outdated/incorrect facts).\n\n"
        f"Search snippets (JSON array of {{title, snippet}}):\n{json.dumps(snippets)}\n\n"
        'Reply as JSON: {"accurate": <bool>, "confidence": <float 0-100>}'
    )


# The rubrics below are the audit's own definitions of what a page should cover, taken from
# the parameter descriptions in registry.json. They are the question put to the model, not
# keywords matched against the page -- the model decides whether each point is actually met.
SERVICE_PAGE_RUBRIC = (
    "the problem or need it addresses",
    "the approach, process or methodology",
    "what is included (features or capabilities)",
    "the outcomes or benefits",
    "who it is for (industry, audience or market)",
    "pricing or commercial model",
    "proof (clients, case studies, testimonials or ratings)",
    "a clear next step or way to get in touch",
)

JOURNEY_RUBRIC = (
    "awareness - explains the problem space to someone new to it",
    "qualification - explains what the company actually offers",
    "comparison - helps a buyer compare options or alternatives",
    "objection - answers concerns such as security, compliance or risk",
    "trust - offers proof via clients, case studies, testimonials or credentials",
    "decision - gives a clear way to start, buy or make contact",
)


def on04_prompt(openings: list[dict]) -> str:
    return (
        "Each item below is the opening text of a web page. For each one, judge whether it "
        "leads with a specific, concrete statement of what the business does or what the page "
        "is about, rather than opening with generic filler, boilerplate or throat-clearing.\n\n"
        f"Openings (JSON array of {{url, opening}}):\n{json.dumps(openings)}\n\n"
        'Reply as JSON: {"results": [{"url": <string>, "leads_with_substance": <bool>}, ...]} '
        "with one entry per opening given, in the same order."
    )


def on07_prompt(pages: list[dict]) -> str:
    return (
        "Each item below is a web page. Decide, for each, whether it is a page where a buyer "
        "would be actively evaluating or comparing options -- comparing products, weighing "
        "alternatives, reviewing capabilities or checking pricing -- as opposed to general "
        "marketing, news or company background.\n\n"
        f"Pages (JSON array of {{url, title, excerpt}}):\n{json.dumps(pages)}\n\n"
        'Reply as JSON: {"results": [{"url": <string>, "evaluation_intent": <bool>}, ...]} '
        "with one entry per page given, in the same order."
    )


def on08_prompt(lists: list[dict]) -> str:
    return (
        "Each item below is a list found on a web page. Judge whether each is genuinely useful "
        "for answering a question -- a procedure, a set of benefits, evaluation criteria, "
        "specifications -- as opposed to decorative or navigational filler.\n\n"
        f"Lists (JSON array of {{url, items}}):\n{json.dumps(lists)}\n\n"
        'Reply as JSON: {"results": [{"url": <string>, "useful": <bool>}, ...]} '
        "with one entry per list given, in the same order."
    )


def on11_prompt(pages: list[dict]) -> str:
    rubric = "\n".join(f"- {point}" for point in SERVICE_PAGE_RUBRIC)
    return (
        "A complete service page should cover all of the following:\n"
        f"{rubric}\n\n"
        "For each page excerpt below, decide which of those points the page actually covers. "
        "Judge on substance, not on whether a particular word appears.\n\n"
        f"Pages (JSON array of {{url, excerpt}}):\n{json.dumps(pages)}\n\n"
        f'Reply as JSON: {{"results": [{{"url": <string>, "covered": <int 0-{len(SERVICE_PAGE_RUBRIC)}, '
        "how many of the points above this page covers>}, ...]} with one entry per page given, "
        "in the same order."
    )


def on18_prompt(pages: list[dict]) -> str:
    rubric = "\n".join(f"- {stage}" for stage in JOURNEY_RUBRIC)
    return (
        "A site that serves a buyer through their whole journey has content for each of these "
        f"six stages:\n{rubric}\n\n"
        "Given the pages below (title and URL only), decide which of the six stages the site "
        "has content for. Judge by what each page is evidently for, not by keywords.\n\n"
        f"Pages (JSON array of {{url, title}}):\n{json.dumps(pages)}\n\n"
        'Reply as JSON: {"stages": {"awareness": <bool>, "qualification": <bool>, '
        '"comparison": <bool>, "objection": <bool>, "trust": <bool>, "decision": <bool>}}'
    )


def on01_prompt(headings: list[dict]) -> str:
    return (
        "For each heading below, decide whether it is a question a real BUYER researching this "
        "kind of purchase would actually ask -- not merely whether it has the grammatical form of "
        "a question. \"What We Do\" has the form and is not a buyer question. \"How long does "
        "implementation take?\" is one. \"Pricing for mid-market teams\" answers one without being "
        "phrased as a question, and also counts.\n\n"
        f"Headings (JSON array of {{url, heading}}):\n{json.dumps(headings)}\n\n"
        'Reply as JSON: {"results": [{"heading": <string>, "buyer_question": <bool>}, ...]} '
        "with one entry per heading given, in the same order."
    )


def on12_prompt(pages: list[dict]) -> str:
    return (
        "Each page below contains one or more statistics. For each page, decide whether at least "
        "one of them is ORIGINAL to the company whose website this is -- its own research, survey, "
        "benchmark, or a result it measured for a client -- rather than a figure quoted from a "
        "third party such as an analyst firm, a news outlet or an industry report.\n\n"
        f"Pages (JSON array of {{url, statistics, excerpt}}):\n{json.dumps(pages)}\n\n"
        'Reply as JSON: {"results": [{"url": <string>, "original": <bool>}, ...]} '
        "with one entry per page given, in the same order."
    )


def on13_prompt(bylines: list[dict]) -> str:
    return (
        "Each entry below is author or byline text taken from a page. For each, decide whether it "
        "names a specific real person -- not a company name, and not a generic label such as "
        "\"Admin\", \"Staff Writer\" or \"Editorial Team\" -- and whether it also states that "
        "person's role, job title or professional credentials.\n\n"
        f"Bylines (JSON array of {{url, byline}}):\n{json.dumps(bylines)}\n\n"
        'Reply as JSON: {"results": [{"url": <string>, "named_person": <bool>, '
        '"has_role_or_credentials": <bool>}, ...]} with one entry per byline given, in the '
        "same order."
    )


def on14_prompt(pages: list[dict]) -> str:
    return (
        "For each page excerpt below, judge the strength of the proof it offers on three points:\n"
        "  names_client       - does it name a SPECIFIC client organisation, rather than only "
        "using the generic words client, customer or partner?\n"
        "  quantified_outcome - does it state a measured result, a number tied to an outcome, "
        "rather than an unquantified claim?\n"
        "  deployment_detail  - does it describe how the work was actually delivered, deployed or "
        "run in production?\n\n"
        f"Pages (JSON array of {{url, excerpt}}):\n{json.dumps(pages)}\n\n"
        'Reply as JSON: {"results": [{"url": <string>, "names_client": <bool>, '
        '"quantified_outcome": <bool>, "deployment_detail": <bool>}, ...]} '
        "with one entry per page given, in the same order."
    )


def on15_prompt(claims: list[dict]) -> str:
    return (
        "Each entry below is a sentence from a website stating a factual claim that contains a "
        "statistic, together with any link anchor text found beside it. For each, decide whether "
        "the claim is genuinely attributed to a source a reader could go and check -- a named "
        "organisation, publication, study or dated report -- rather than merely sitting near the "
        "words \"source\", \"according to\" or \"report\" with nothing identifiable behind them.\n\n"
        f"Claims (JSON array of {{sentence, nearby_links}}):\n{json.dumps(claims)}\n\n"
        'Reply as JSON: {"results": [{"sentence": <string>, "sourced": <bool>}, ...]} '
        "with one entry per claim given, in the same order."
    )


def on19_prompt(pages: list[dict]) -> str:
    return (
        "Decide which of these pages are built to answer a HIGH-INTENT comparison query -- the "
        "kind of \"best X for Y\", \"X vs Y\" or \"alternatives to X\" page a buyer already "
        "evaluating options would search for. A general service page does not qualify, and "
        "neither does an article that merely mentions a competitor in passing.\n\n"
        f"Pages (JSON array of {{url, title}}):\n{json.dumps(pages)}\n\n"
        'Reply as JSON: {"results": [{"url": <string>, "comparison_page": <bool>}, ...]} '
        "with one entry per page given, in the same order."
    )


def on20_prompt(pairs: list[dict]) -> str:
    return (
        "Each pair below is two pages from the same website whose titles are textually similar. "
        "For each pair, decide whether the two pages would genuinely COMPETE for the same buyer "
        "question, so that a search engine or AI assistant would have no clear reason to prefer "
        "one over the other. Pages that share wording but answer different questions -- a service "
        "page and a case study about that service, or the same service for two different "
        "industries -- do NOT compete.\n\n"
        f"Pairs (JSON array of {{a, b, title_a, title_b}}):\n{json.dumps(pairs)}\n\n"
        'Reply as JSON: {"results": [{"a": <string>, "b": <string>, "competing": <bool>}, ...]} '
        "with one entry per pair given, in the same order."
    )


def tech11_prompt(sections: list[dict]) -> str:
    return (
        "For each section below (a heading plus the word count of the text under it before "
        "the next heading), judge whether an AI assistant could \"lift\" that section as a "
        "clean, self-contained passage to answer a question -- i.e. it is reasonably short, "
        "focused, and does not run on without a subheading.\n\n"
        f"Sections (JSON array of {{heading, word_count}}):\n{json.dumps(sections)}\n\n"
        'Reply as JSON: {"liftable_count": <int>, "total": <int>}'
    )

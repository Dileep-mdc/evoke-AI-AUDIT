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


def off02_prompt(company_name: str, hits: list[dict]) -> str:
    return (
        f'Given the company "{company_name}", decide which (if any) of these Wikipedia search '
        "results is genuinely a reference page about that specific company (not a different "
        "company or a generic/disambiguation page), and rate the source's authority as a "
        "reference for factual claims about the company.\n\n"
        f"Search results (JSON array of {{title, snippet}}):\n{json.dumps(hits)}\n\n"
        'Reply as JSON: {"about_company": <bool>, "best_match_title": <string or null>, '
        '"entity_accuracy": <float 0-100, how confident this is the right entity>, '
        '"source_authority": <float 0-100, how authoritative this source is>}'
    )


def on09_prompt(samples: list[dict]) -> str:
    return (
        "Read these excerpts of website copy and judge, overall, whether the writing reads as "
        "natural, fluent, conversational language versus stiff, repetitive or stitched-together "
        "keyword strings (e.g. \"best web design company web design services web design agency\").\n\n"
        f"Excerpts (JSON array of {{url, excerpt}}):\n{json.dumps(samples)}\n\n"
        'Reply as JSON: {"naturalness": <float 0-100, 100 = fully natural and fluent>}'
    )


def off09_prompt(company_name: str, categories: list[str], snippets: list[dict]) -> str:
    return (
        f'Given the company "{company_name}", whose own site describes its business using these '
        f"categories: {json.dumps(categories)}, decide whether the third-party search snippets "
        "below place the company in the same or a clearly equivalent category (not a mismatched "
        "or unrelated one).\n\n"
        f"Search snippets (JSON array of {{title, snippet}}):\n{json.dumps(snippets)}\n\n"
        'Reply as JSON: {"matches": <bool>, "confidence": <float 0-100>}'
    )


def off18_prompt(company_name: str, snippets: list[dict]) -> str:
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


def tech11_prompt(sections: list[dict]) -> str:
    return (
        "For each section below (a heading plus the word count of the text under it before "
        "the next heading), judge whether an AI assistant could \"lift\" that section as a "
        "clean, self-contained passage to answer a question -- i.e. it is reasonably short, "
        "focused, and does not run on without a subheading.\n\n"
        f"Sections (JSON array of {{heading, word_count}}):\n{json.dumps(sections)}\n\n"
        'Reply as JSON: {"liftable_count": <int>, "total": <int>}'
    )

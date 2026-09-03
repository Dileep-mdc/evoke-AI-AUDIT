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


def tech11_prompt(sections: list[dict]) -> str:
    return (
        "For each section below (a heading plus the word count of the text under it before "
        "the next heading), judge whether an AI assistant could \"lift\" that section as a "
        "clean, self-contained passage to answer a question -- i.e. it is reasonably short, "
        "focused, and does not run on without a subheading.\n\n"
        f"Sections (JSON array of {{heading, word_count}}):\n{json.dumps(sections)}\n\n"
        'Reply as JSON: {"liftable_count": <int>, "total": <int>}'
    )

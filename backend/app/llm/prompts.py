from __future__ import annotations

import json

SYSTEM = (
    "You are a strict, careful grader for a website audit tool. You are given extracted "
    "website content and must make a specific judgment call about it. Reply with ONLY a "
    "single JSON object matching the requested shape -- no prose, no markdown fences."
)


def on01_prompt(headings: list[str]) -> str:
    return (
        "Classify each heading below as phrased the way a buyer would ask a question "
        "(How/What/Why/When/Where, or a direct question) versus purely informational or "
        "navigational phrasing.\n\n"
        f"Headings (JSON array):\n{json.dumps(headings)}\n\n"
        'Reply as JSON: {"question_style_count": <int, how many are buyer-question-phrased>, '
        '"total": <int, total headings given>}'
    )


def on03_prompt(concepts: list[dict]) -> str:
    return (
        "For each core concept below, decide whether the surrounding text on the page gives "
        "the reader a clear, complete definition of that concept (not just a passing mention).\n\n"
        f"Concepts and surrounding text (JSON array of {{concept, window}}):\n{json.dumps(concepts)}\n\n"
        'Reply as JSON: {"results": [{"concept": <string>, "defined": <bool>}, ...]} '
        "with one entry per concept given, in the same order."
    )


def on09_prompt(text_sample: str) -> str:
    return (
        "Rate how natural and conversational this website copy sounds, as opposed to "
        "keyword-stuffed or written for search engines rather than people.\n\n"
        f"Text sample:\n{text_sample[:4000]}\n\n"
        'Reply as JSON: {"naturalness_score": <float 0-100, 100=fully natural>, '
        '"stuffed": <bool, true if it reads as keyword-stuffed>}'
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


def tech11_prompt(sections: list[dict]) -> str:
    return (
        "For each section below (a heading plus the word count of the text under it before "
        "the next heading), judge whether an AI assistant could \"lift\" that section as a "
        "clean, self-contained passage to answer a question -- i.e. it is reasonably short, "
        "focused, and does not run on without a subheading.\n\n"
        f"Sections (JSON array of {{heading, word_count}}):\n{json.dumps(sections)}\n\n"
        'Reply as JSON: {"liftable_count": <int>, "total": <int>}'
    )


def tech01_review_prompt(raw_robots_txt: str, decisions: list[dict]) -> str:
    return (
        "A deterministic parser read this robots.txt and produced these allow/block decisions "
        "for AI crawlers. Sanity-check whether the decisions look consistent with the raw text "
        "(watch for wildcard rules, rule ordering, and User-agent grouping), and write a short "
        "plain-English explanation suitable for a non-technical reader. Do not recompute a "
        "score -- this is a review/explanation pass only.\n\n"
        f"Raw robots.txt (may be truncated):\n{raw_robots_txt[:2000]}\n\n"
        f"Computed decisions (JSON array):\n{json.dumps(decisions)}\n\n"
        'Reply as JSON: {"looks_consistent": <bool>, "explanation": <string, 1-3 sentences>}'
    )

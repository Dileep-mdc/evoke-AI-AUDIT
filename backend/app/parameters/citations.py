"""The sources behind a parameter's score, as a list a reader can click through.

Every score the audit produces must be traceable to what it was computed from. The evidence a
handler returns already holds that -- the pages it graded, the links it re-fetched, the files
it parsed, the third-party pages it queried -- but scattered across differently-shaped dicts.
This collects every URL the evidence rests on into one list, each with a short note of what was
found there, so the report can cite its sources the same way for all 60 parameters.

A score is never left uncited: when the evidence names no URL at all, the citation is the
source the check read (`checked_url_or_source`), which every handler is required to set.
"""
from __future__ import annotations

import re
from typing import Any

_URL_RE = re.compile(r"^https?://\S+$", re.I)
# Keys whose value is the location of a thing, rather than a fact about it.
_URL_KEYS = {"url", "href", "src", "link", "page", "probe_url", "source", "final_url", "a", "b", "canonical"}
# Free text the model or the summary wrote: it may mention URLs, but it is not a source.
_PROSE_KEYS = {"curated_input", "model_reasoning", "explanation", "summary", "rules_based_summary",
               "llm_error", "error_detail", "note", "reason"}
_DETAIL_MAX = 160


def _detail(item: dict, skip: set[str]) -> str:
    """The facts recorded beside a URL, compacted: "status: 404; hops: 2"."""
    parts = []
    for key, value in item.items():
        if key in skip or key in _URL_KEYS or key in _PROSE_KEYS:
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            parts.append(f"{key.replace('_', ' ')}: {value}")
        elif isinstance(value, str) and value and not _URL_RE.match(value) and len(value) <= 90:
            parts.append(f"{key.replace('_', ' ')}: {value}")
        elif isinstance(value, list) and value and all(isinstance(v, str) for v in value) and len(value) <= 6:
            joined = ", ".join(v for v in value if not _URL_RE.match(v))
            if joined:
                parts.append(f"{key.replace('_', ' ')}: {joined}")
    text = "; ".join(parts)
    return text if len(text) <= _DETAIL_MAX else text[: _DETAIL_MAX - 1] + "…"


def _walk(value: Any, context: str, out: dict[str, str]) -> None:
    if isinstance(value, dict):
        urls = {k: v for k, v in value.items()
                if isinstance(v, str) and _URL_RE.match(v.strip()) and (k in _URL_KEYS or k.endswith("_url"))}
        detail = _detail(value, set(urls)) if urls else ""
        for key, url in urls.items():
            note = detail or context
            if key in {"a", "b"}:
                note = f"pair with {value.get('b' if key == 'a' else 'a', '')}".strip()
            out.setdefault(url.strip(), note)
        for key, child in value.items():
            if key in _PROSE_KEYS or key in urls:
                continue
            _walk(child, key.replace("_", " "), out)
    elif isinstance(value, list):
        for child in value:
            _walk(child, context, out)
    elif isinstance(value, str) and _URL_RE.match(value.strip()):
        out.setdefault(value.strip(), context)


def build_citations(row: dict) -> list[dict]:
    """[{"source": url-or-source-name, "detail": what was found there}, ...], never empty."""
    found: dict[str, str] = {}
    checked = (row.get("checked_url_or_source") or "").strip()
    if checked and _URL_RE.match(checked):
        found[checked] = "source checked"
    _walk(row.get("evidence") or {}, "evidence", found)
    citations = [{"source": url, "detail": detail} for url, detail in found.items()]
    if not citations:
        citations = [{"source": checked or "not recorded", "detail": "source checked"}]
    return citations

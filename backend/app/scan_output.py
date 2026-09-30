"""Persists the three artifacts of every audit to backend/data/scan_output/, named after
the site so it is obvious at a glance which audit they belong to:

    {company}-scraped-content.json      -- the full end-to-end scraped output for every page
                                           the crawler visited: the extracted fields (title,
                                           meta description, headings, links, images, schema,
                                           visible text), each with its scrape-confidence score
                                           and label, AND the source documents those fields were
                                           read from -- see {company}-raw/ below.
    {company}-raw/                      -- one gzipped file per page: the server's raw HTML, plus
                                           the rendered DOM for pages that were rendered. Kept
                                           because a list of links and a word count cannot be
                                           audited against anything; the document they came from
                                           can. Stored beside the index rather than inside it, so
                                           the index stays small enough to open.
    {company}-failed-links.json         -- every link that could not be scraped cleanly, with
                                           the reason, kept separate so the failures are
                                           readable without opening the big file.
    {company}-parameter-new-logic.xlsx  -- one row per parameter, following the audit end to
                                           end: what the parameter means, how its metric is
                                           calculated, the curated data that was graded, the
                                           score and who produced it, the explanation, and
                                           the recommendation.

Every name is deterministic per domain, so re-auditing a site refreshes that site's one set
of three files rather than leaving a trail of near-identical output behind. The workbook is
also rebuilt on demand, straight from the stored report, by GET /scans/{id}/download.xlsx --
the files on disk are a convenience copy, never the only copy.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .config import DATA_DIR
from .crawler.http import is_html
from .page_selection import link_reason
from .parameters.scoring import scrape_confidence
from .parameters.spec import spec_for

FORMULAS_PATH = Path(__file__).resolve().parent / "parameters" / "formulas.json"
FORMULAS: dict[str, str] = json.loads(FORMULAS_PATH.read_text(encoding="utf-8"))

OUTPUT_DIR = DATA_DIR / "scan_output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _friendly_timestamp(value: str | None) -> str:
    """'2026-08-24T14:56:23.435018+00:00' -> '24 Aug 2026, 02:56:23 PM UTC'."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.strftime("%d %b %Y, %I:%M:%S %p UTC")


def _slug(name: str) -> str:
    """A filesystem-safe, readable stem: 'www.example.com' -> 'example-com'."""
    name = (name or "site").strip().lower().removeprefix("www.")
    name = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return name or "site"


def _page_failure(p) -> dict | None:
    """None if this page's crawl succeeded; otherwise why the crawler could not scrape it
    cleanly -- a hard fetch failure (timeout, DNS, connection refused), a non-2xx HTTP
    status, or a bot-detection block. Deliberately narrower than scrape_confidence(): thin
    content on an otherwise clean 2xx page lowers confidence but isn't a crawl failure."""
    result = p.result
    status = result.status_code if result else None
    error = result.error if result else "No response received"
    if status is None or error:
        return {
            "url": p.url,
            "final_url": result.final_url if result else p.url,
            "status_code": status,
            "reason": error or "No response received",
        }
    if not (200 <= status < 300):
        return {
            "url": p.url,
            "final_url": result.final_url,
            "status_code": status,
            "reason": f"non-2xx HTTP status ({status})",
        }
    if p.blocked_reason:
        return {
            "url": p.url,
            "final_url": result.final_url,
            "status_code": status,
            "reason": p.blocked_reason,
        }
    return None


def save_crawl_failures(scan_id: str, ctx) -> Path:
    """Every link the crawler could not scrape cleanly for this run, in its own file.

    Kept separate from the full scrape output so a failed-link list is available for every
    audit without digging through the big file. Covers both pages that were fetched but came
    back broken or blocked, and targets that returned no response at all and so never became
    a Page (those are only visible via ctx.crawl_errors).
    """
    failures: list[dict] = []
    seen_urls: set[str] = set()
    for p in ctx.pages or []:
        failure = _page_failure(p)
        if failure:
            failures.append(failure)
            seen_urls.add(p.url)
    for entry in ctx.crawl_errors or []:
        url, _, reason = entry.partition(": ")
        if url not in seen_urls:
            failures.append({"url": url, "final_url": url, "status_code": None, "reason": reason or entry})
            seen_urls.add(url)
    payload = {
        "scan_id": scan_id,
        "domain": ctx.domain,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pages_crawled": len(ctx.pages or []),
        "failed_count": len(failures),
        "failures": failures,
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{_slug(ctx.domain)}-failed-links.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# How hard to compress the stored documents. HTML is extremely redundant, so even level 1
# gets several-fold; 6 buys a meaningfully smaller store for CPU that is spent in zlib, which
# releases the GIL and so actually parallelises across the writer pool below.
_GZIP_LEVEL = 6
# Writing is I/O-bound with a compression step attached, which is the case threads are for.
_RAW_WRITE_WORKERS = 8


def _page_filename(url: str, seen: set[str]) -> str:
    """A readable, unique, filesystem-safe name for one page's stored document.

    Readable because the whole point of a directory of files is that a person can find the
    page they care about in it; unique because a name collision would silently overwrite one
    page's document with another's, which is the one failure that would make the store lie.
    The short hash is what guarantees the second property without giving up the first -- '/'
    and '/?page=2' both slug to 'index', and only the hash separates them.
    """
    parsed = urlparse(url)
    stem = re.sub(r"[^a-z0-9]+", "-", (parsed.path or "/").lower()).strip("-") or "index"
    stem = stem[:80]
    digest = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:8]
    name = f"{stem}-{digest}"
    # Belt and braces: the hash is of the full URL, so this can only fire if the same URL
    # appears twice in ctx.pages, but an overwrite here is silent data loss and cheap to rule out.
    while name in seen:
        digest = hashlib.sha1((name + "x").encode()).hexdigest()[:8]
        name = f"{stem}-{digest}"
    seen.add(name)
    return name


def _write_documents(raw_dir: Path, jobs: list[tuple[Path, str]]) -> None:
    """Write every stored document, compressed, in parallel.

    gzip rather than plain .html: these are the bulk of everything this audit stores -- a
    single homepage measured 735 KB of HTML against 2.5 KB of extracted text -- and HTML
    compresses several-fold, so this is the difference between a store that is routine to
    keep and one nobody can afford to. Anything can open a .html.gz.
    """
    if not jobs:
        return
    raw_dir.mkdir(parents=True, exist_ok=True)

    def write(job: tuple[Path, str]) -> None:
        path, text = job
        # mtime=0: the archive header otherwise embeds the write time, so re-auditing an
        # unchanged page produces different bytes for identical content.
        with gzip.GzipFile(filename="", mode="wb", compresslevel=_GZIP_LEVEL,
                           fileobj=path.open("wb"), mtime=0) as fh:
            fh.write(text.encode("utf-8", "replace"))

    with ThreadPoolExecutor(max_workers=_RAW_WRITE_WORKERS) as pool:
        list(pool.map(write, jobs))


def save_crawl_output(scan_id: str, ctx) -> Path:
    """Save the complete scraped-website output for this run: every page the crawler
    visited, end to end, each with its scrape-confidence score, label and reasons.

    The source documents do not go in this file. They go beside it, one compressed file per
    page under {company}-raw/, and each page record points at its own. Inlining them was
    measured at 400 MB - 1 GB of single-line JSON for a 1,064-page site, which has to be
    parsed whole to read any part of it; split out and gzipped, the same documents are a
    fraction of that and the index stays small enough to open.
    """
    slug = _slug(ctx.domain)
    raw_dir = OUTPUT_DIR / f"{slug}-raw"
    # Names are deterministic per domain, so a re-audit must clear the old store first --
    # otherwise a page that has since been removed from the site keeps its document here
    # forever and the directory slowly fills with pages the audit no longer covers.
    if raw_dir.exists():
        shutil.rmtree(raw_dir, ignore_errors=True)

    pages = []
    documents: list[tuple[Path, str]] = []
    seen_names: set[str] = set()
    for p in ctx.pages or []:
        confidence = scrape_confidence(p)
        # The server's HTML, and the post-JavaScript DOM when this page was rendered. Both
        # are kept: the derived fields below are only auditable against the document they
        # were read from, and on a rendered page that is a different document from the one
        # the HTTP response carried.
        name = _page_filename(p.url, seen_names)
        raw_html = (p.result.text or "") if (p.result and is_html(p.result)) else ""
        rendered_html = getattr(p, "rendered_html", "") or ""
        raw_file = rendered_file = None
        if raw_html:
            raw_file = f"{slug}-raw/{name}.html.gz"
            documents.append((raw_dir / f"{name}.html.gz", raw_html))
        if rendered_html:
            rendered_file = f"{slug}-raw/{name}.rendered.html.gz"
            documents.append((raw_dir / f"{name}.rendered.html.gz", rendered_html))
        pages.append({
            "url": p.url,
            "final_url": p.result.final_url if p.result else p.url,
            "status_code": p.result.status_code if p.result else None,
            "content_type": p.result.content_type if p.result else "",
            "raw_html_bytes": len(p.result.content) if p.result else 0,
            "fetch_error": p.result.error if p.result else None,
            "blocked_reason": p.blocked_reason,
            "scrape_confidence_score": confidence["score"],
            "scrape_confidence_label": confidence["label"],
            "scrape_confidence_reasons": confidence["reasons"],
            "page_type": p.page_type,
            # Where this page's CONTENT came from -- the server's HTML, or the DOM after
            # JavaScript ran -- and what the browser added. Recorded per page because the
            # answer varies within one crawl: the render budget covers a subset, so a reader
            # of this file can tell which pages were read which way rather than assuming.
            "content_source": (p.render or {}).get("content_source", "raw"),
            "render": p.render or {},
            "title": p.title,
            "meta_description": p.meta_description,
            "canonical": p.canonical,
            "word_count": p.word_count,
            "headings": p.headings,
            "links": p.links,
            "images": p.images,
            "schema_blocks": p.schema_blocks,
            "hreflang": p.hreflang,
            "dates": p.dates,
            "text": p.text,
            # Where this page's source documents are stored, relative to this file.
            #
            # Everything above is DERIVED -- trafilatura's idea of the body copy, the headings
            # that survived the chrome filter, the links as they were resolved. Saving only the
            # derivation means the audit's own reading is the only reading anyone can ever
            # check, so when a parameter scores a page thin there is no way to tell a thin page
            # from a failed extraction. These two files are the evidence that settles it.
            #
            # raw_html_file      what the server sent, exactly as the crawler read it (bounded
            #                    at fetch time by MAX_BODY_BYTES, so it is the whole document
            #                    the audit saw, not a sample). null for anything that is not
            #                    HTML -- a PDF decoded to text is mojibake, not raw data.
            # rendered_html_file the DOM after JavaScript ran, on the budgeted subset of pages
            #                    that were rendered. There, it and not the raw file is what the
            #                    fields above were read from -- content_source says which.
            "raw_html_file": raw_file,
            "rendered_html_file": rendered_file,
        })
    avg_confidence = round(sum(pg["scrape_confidence_score"] for pg in pages) / len(pages), 1) if pages else None
    payload = {
        "scan_id": scan_id,
        "input_url": ctx.input_url,
        "normalized_url": ctx.normalized_url,
        "origin": ctx.origin,
        "domain": ctx.domain,
        "company_name": ctx.company_name,
        "brand_terms": ctx.brand_terms,
        "sitemap_url_count": len(ctx.sitemap.get("urls", [])) if ctx.sitemap else 0,
        "pages_crawled": len(pages),
        "average_scrape_confidence_score": avg_confidence,
        "crawl_errors": ctx.crawl_errors,
        "render": getattr(ctx, "render", None) or {},
        "snapshot": getattr(ctx, "snapshot", None) or {},
        # Where the source documents went, stated up front so this file explains itself:
        # anyone opening it can see the documents exist and where, without knowing to look.
        "raw_document_store": {
            "directory": raw_dir.name,
            "format": "gzip-compressed UTF-8 HTML (.html.gz)",
            "documents": len(documents),
            "note": "Each page's raw_html_file / rendered_html_file is a path relative to this file.",
        },
        "pages": pages,
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _write_documents(raw_dir, documents)
    path = OUTPUT_DIR / f"{slug}-scraped-content.json"
    # Streamed to the file handle rather than json.dumps()'d into a string first. The index
    # is tens of megabytes on a large site, and building it whole in memory before writing a
    # byte of it doubles the peak for no benefit -- the crawl is already holding every page.
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    return path


# --- Making the graded data readable ----------------------------------------------
# The model is handed the curated evidence as compact JSON, because structure is exactly
# what it parses best. A JSON blob in a spreadsheet cell, though, is unreadable to the
# person the report is actually written for -- a 2,000-character one-liner of braces and
# quotes. These helpers re-present the same payload as labelled lines: same keys, same
# order, same numbers, nothing added and nothing dropped, only the punctuation changed.
_ACRONYMS = {
    "ai", "cdn", "css", "cta", "faq", "gsc", "html", "http", "https", "id", "ids", "json",
    "jsonld", "nap", "ocr", "seo", "sku", "ssl", "svg", "tld", "ttfb", "ui", "url", "urls",
    "utm", "xml", "h1", "h2", "h3", "h4", "kb", "mb", "ms",
}


def _label(key) -> str:
    """'baseline_bytes' -> 'Baseline bytes'; 'llms_txt_url' -> 'Llms txt URL'."""
    words = str(key).replace("_", " ").split()
    if not words:
        return str(key)
    words = [w.upper() if w.lower() in _ACRONYMS else w for w in words]
    head = words[0]
    if not head.isupper():
        words[0] = head[:1].upper() + head[1:]
    return " ".join(words)


def _readable_value(value) -> str:
    """A scalar as a reader would say it, not as JSON spells it."""
    if isinstance(value, bool):          # before int: bool is an int in Python
        return "yes" if value else "no"
    if value is None:
        return "not available"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, int) and abs(value) >= 10_000:
        return f"{value:,}"              # 735542 -> 735,542
    text = str(value)
    return text if text.strip() else "(empty)"


def _omission_note(item) -> str | None:
    """The marker the curation step leaves behind when it drops entries, if this is one.

    curate() caps long lists and wide dicts and records what it dropped in place, as
    '...[3 more of 15 total omitted]'. Left as-is that reads as one more sampled page and
    inflates every count beside it, so it is pulled out and shown as a note instead.
    """
    if isinstance(item, str):
        text = item.strip()
        if text.startswith("...[") and text.endswith("omitted]"):
            return text.removeprefix("...[").removesuffix("]")
    return None


def _countable(value: list) -> int:
    """How many real entries a list holds, ignoring any omission marker."""
    return sum(1 for item in value if _omission_note(item) is None)


def _render_curated(node, depth: int = 0) -> list[str]:
    """One line per fact, nested lists numbered so long samples stay countable."""
    pad = "    " * depth
    lines: list[str] = []
    if isinstance(node, dict):
        # A handler's own summary is the headline, so it leads regardless of key order.
        # sorted() is stable, so everything else keeps the order the handler wrote it in.
        for key in sorted(node, key=lambda k: str(k) != "summary"):
            value = node[key]
            if str(key) == "..." and not isinstance(value, (dict, list)):
                # curate()'s dict-level marker, whose key carries no meaning of its own.
                lines.append(f"{pad}... {str(value).strip('[]')}")
            elif isinstance(value, dict):
                lines.append(f"{pad}{_label(key)}:")
                lines.extend(_render_curated(value, depth + 1))
            elif isinstance(value, list):
                lines.append(f"{pad}{_label(key)} ({_countable(value)}):")
                lines.extend(_render_curated(value, depth + 1))
            else:
                lines.append(f"{pad}{_label(key)}: {_readable_value(value)}")
    elif isinstance(node, list):
        i = 0
        for item in node:
            note = _omission_note(item)
            if note:
                lines.append(f"{pad}... and {note}")
                continue
            i += 1
            if isinstance(item, dict):
                # Each record on one line -- 15 sampled pages read as 15 lines, not 90.
                flat = " | ".join(
                    f"{_label(k)}: {_readable_value(v)}"
                    for k, v in item.items()
                    if not isinstance(v, (dict, list))
                )
                lines.append(f"{pad}{i}. {flat}" if flat else f"{pad}{i}.")
                for k, v in item.items():
                    if isinstance(v, (dict, list)):
                        count = f" ({_countable(v)})" if isinstance(v, list) else ""
                        lines.append(f"{pad}    {_label(k)}{count}:")
                        lines.extend(_render_curated(v, depth + 2))
            else:
                lines.append(f"{pad}{i}. {_readable_value(item)}")
    else:
        lines.append(f"{pad}{_readable_value(node)}")
    return lines


def readable_curated(raw) -> str:
    """The curated data the model graded, as lines a client can read.

    Anything that is not the JSON this pipeline produces -- prose from an older scan, or a
    payload the character cap cut mid-object -- is passed through untouched rather than
    mangled into a guess.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "No data was captured for this check."
    data = raw
    if isinstance(raw, str):
        try:
            # strict=False: scraped text carries stray control characters often enough, and
            # a tab inside a page title should not cost the whole cell its formatting.
            data = json.loads(raw, strict=False)
        except (ValueError, TypeError):
            return raw
    if not isinstance(data, (dict, list)):
        return _readable_value(data)
    lines = _render_curated(data)
    return "\n".join(lines) if lines else "No data was captured for this check."


# --- Look & feel -----------------------------------------------------------------
# Deliberately monochrome. This report is printed, photocopied and pasted into decks, so
# every distinction it makes is carried by type weight, rules and whitespace rather than by
# fill colors -- which also means nothing is lost on a black-and-white printer or to a
# reader who cannot rely on color. No fills, no colored text, anywhere in the workbook.
TITLE_FONT = Font(bold=True, size=13)
SUBTITLE_FONT = Font(italic=True, size=9)
HEADER_FONT = Font(bold=True, size=10)
LABEL_FONT = Font(bold=True, size=10)
SECTION_FONT = Font(bold=True, size=11)
STATUS_FONT = Font(bold=True, size=10)
WRAP = Alignment(wrap_text=True, vertical="top")
WRAP_CENTER = Alignment(wrap_text=True, vertical="center", horizontal="center")
INDENTED = Alignment(vertical="center", horizontal="left", indent=1)
THIN = Side(style="thin")
MEDIUM = Side(style="medium")
CELL_BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
# A heavier rule under the header row is what separates it from the data once the fill is
# gone -- the one job the dark header band used to do.
HEADER_BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=MEDIUM)
TITLE_RULE = Border(bottom=MEDIUM)

STATUS_MEANING = {
    "PASS": "Requirement is fully met.",
    "PARTIAL": "Requirement is partially met - some, but not all, criteria were satisfied.",
    "FAIL": "Requirement is not met and needs attention.",
    "UNKNOWN": "Could not be determined automatically (e.g. blocked page, missing data, or manual check needed).",
}
SCORE_BANDS = [
    ("Excellent", "90-100"),
    ("Good", "75-89"),
    ("Fair", "60-74"),
    ("Poor", "40-59"),
    ("Critical", "0-39"),
    ("Unknown", "Not enough data to score"),
]

# One row per parameter, in the order the audit actually works: what the check is, what it
# means, how it is scored, the curated data that was graded, the verdict, and what to do
# about it. The third number is how many characters fit on one wrapped line at that width,
# which is what lets _row_height below size a row to the prose it holds -- Excel does not
# auto-fit wrapped cells, so without it the long columns are clipped to one line.
COLUMNS = [
    ("Parameter ID", 12, 11),
    ("Parameter", 30, 28),
    ("What This Parameter Means", 52, 50),
    ("How the Metric Is Calculated", 56, 54),
    ("What Was Found on Your Site (the exact data scored)", 58, 56),
    # Immediately after the data it explains. Reading order is: here is what was found, and
    # here is which pages that came from and why not the rest -- which is the question the
    # previous column provokes the moment anyone sees it list a dozen URLs.
    ("Which Pages Were Used, and Why the Others Were Not", 58, 56),
    ("Score (%)", 9, 8),
    ("Status", 11, 10),
    ("Scored By", 15, 13),
    ("Explanation", 56, 54),
    ("Recommendation", 52, 50),
]
STATUS_COL = 8
SCORED_BY_LABEL = {"rules_based": "Rules-based", "llm": "Rules-based", "deterministic": "Rules-based (no AI review)"}
MAX_ROW_HEIGHT = 320.0
LINE_HEIGHT = 13.2
PILLARS = [("technical", "Technical"), ("on_page", "On-Page"), ("off_page", "Off-Page")]
PILLAR_BLURB = {
    "Technical": "Crawlability, indexability, structured data and other machine-readable signals "
                 "that determine whether AI systems and search engines can access and parse the site.",
    "On-Page": "Content quality, clarity and structure on the pages themselves - the signals that "
               "help AI assistants understand and accurately summarize what the business offers.",
    "Off-Page": "External signals such as citations, reviews and third-party mentions that build the "
                "site's authority and trustworthiness in AI-generated answers.",
}


def _title_bar(ws: Worksheet, text: str, subtitle: str, span: int) -> int:
    """Writes a merged title row (and an optional subtitle row) at the top of a sheet and
    returns the next free row number.

    A bold heading underlined by a single rule across the sheet's full width, which reads as
    a masthead in print without any fill behind it.
    """
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=span)
    cell = ws.cell(row=1, column=1, value=_safe(text))
    cell.font = TITLE_FONT
    cell.alignment = INDENTED
    ws.row_dimensions[1].height = 26
    next_row = 2
    if subtitle:
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=span)
        sub_cell = ws.cell(row=2, column=1, value=subtitle)
        sub_cell.font = SUBTITLE_FONT
        sub_cell.alignment = INDENTED
        next_row = 3
    # The rule sits under whichever row ends the masthead, and must span every column or it
    # stops short of the table it is introducing.
    for col in range(1, span + 1):
        ws.cell(row=next_row - 1, column=col).border = TITLE_RULE
    return next_row


# Excel rejects most C0 control characters outright, and openpyxl raises rather than
# dropping them. Scraped markup carries them more often than it looks -- a stray vertical
# tab or form feed in a page title is enough -- and since the curated-data column now
# carries raw page content, one such character anywhere on the site would otherwise raise
# IllegalCharacterError inside build_excel and fail the scan at the very last step, after
# the whole crawl and every model call had already succeeded.
_ILLEGAL_XLSX_CHARS = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")
# Hard limit imposed by the format itself; a longer string makes the file unopenable.
MAX_CELL_CHARS = 32_000


def _safe(value):
    """A value Excel will actually accept, without losing what it says."""
    if not isinstance(value, str):
        return value
    cleaned = _ILLEGAL_XLSX_CHARS.sub(" ", value)
    if len(cleaned) > MAX_CELL_CHARS:
        cleaned = cleaned[:MAX_CELL_CHARS] + " ...[truncated]"
    return cleaned


def _row_height(values: list) -> float:
    """How tall this row needs to be for its longest wrapped cell to stay readable.

    Excel wraps text but never grows the row to fit it, so a 600-character explanation in a
    50-character column renders as one clipped line. Measuring the wrapped line count per
    column -- honouring explicit newlines -- and taking the tallest is what keeps the prose
    columns legible in a workbook meant to be read rather than filtered.
    """
    tallest = 1
    for value, (_, _, per_line) in zip(values, COLUMNS):
        text = "" if value is None else str(value)
        lines = sum(max(1, math.ceil(len(part) / max(1, per_line))) for part in text.split("\n"))
        tallest = max(tallest, lines)
    return min(MAX_ROW_HEIGHT, LINE_HEIGHT * tallest)


def _write_pillar_sheet(wb: Workbook, sheet_name: str, rows: list[dict], reg_by_id: dict, domain: str, generated_at: str) -> None:
    ws = wb.create_sheet(sheet_name)
    header_row = _title_bar(
        ws,
        f"{sheet_name} Parameters - {domain or ''}".strip(" -"),
        f"{PILLAR_BLURB.get(sheet_name, '')}  |  Generated {generated_at}",
        len(COLUMNS),
    )
    for col, (name, width, _) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=header_row, column=col, value=name)
        cell.font = HEADER_FONT
        cell.alignment = WRAP_CENTER
        cell.border = HEADER_BORDER
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[header_row].height = 34
    ws.freeze_panes = f"C{header_row + 1}"

    for offset, p in enumerate(rows):
        r = header_row + 1 + offset
        pid = p.get("parameter_id")
        reg = reg_by_id.get(pid, {})
        frozen = spec_for(pid, reg)
        evidence = p.get("evidence") or {}
        score = p.get("score")
        status = (p.get("status") or "UNKNOWN").upper()
        explanation = evidence.get("explanation") or evidence.get("summary") or p.get("error") or "No output recorded."
        method = evidence.get("scoring_method") or "deterministic"
        # Only a PASS may read as "nothing to do here". result() strips the recommendation
        # from a passing row, so a row the model later downgraded arrives with an empty one --
        # and telling a client no change is required on a parameter that just failed is the
        # one thing this column must never do.
        recommendation = p.get("recommendation") or (
            "No change required." if status == "PASS"
            else "No specific recommendation was generated - see the explanation."
        )
        row = [
            pid,
            p.get("name"),
            frozen.get("definition") or reg.get("check_logic") or "",
            # formulas.json is generated from the same frozen metric the model grades
            # against, so the sheet, the prompt and the frozen spec cannot disagree.
            FORMULAS.get(pid) or frozen.get("metric") or "",
            readable_curated(evidence.get("curated_input")),
            link_reason(pid, evidence),
            score if score is not None else "N/A",
            status,
            SCORED_BY_LABEL.get(method, method),
            explanation,
            recommendation,
        ]
        row = [_safe(v) for v in row]
        for c, value in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.alignment = WRAP
            cell.border = CELL_BORDER
        # The verdict has to stand out without a fill behind it, so it is the one cell set
        # in bold and centred; the word itself carries the meaning.
        status_cell = ws.cell(row=r, column=STATUS_COL)
        status_cell.font = STATUS_FONT
        status_cell.alignment = WRAP_CENTER
        ws.row_dimensions[r].height = _row_height(row)
    ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(COLUMNS))}{max(ws.max_row, header_row)}"

    # Printed and projected as often as it is filtered, so it is set up to survive both:
    # landscape, scaled to one page wide, with the header repeating on every printed page.
    ws.print_title_rows = f"{header_row}:{header_row}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True


def _write_how_to_read_sheet(wb: Workbook) -> None:
    """A plain-language front page so anyone opening this workbook - not just the
    person who ran the scan - understands what they're looking at before they read
    a single score."""
    ws = wb.create_sheet("How to Read This Report")
    wb.move_sheet(ws.title, offset=-(len(wb.sheetnames) - 1))
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 95
    _title_bar(ws, "How to Read This Report", "A quick guide to every sheet, column and verdict in this workbook.", 2)

    r = 4

    def section(title: str) -> None:
        nonlocal r
        cell = ws.cell(row=r, column=1, value=title)
        cell.font = SECTION_FONT
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        r += 1

    def line(a: str, b: str = "") -> None:
        nonlocal r
        left = ws.cell(row=r, column=1, value=a)
        left.font = Font(bold=True, size=10)
        left.alignment = WRAP
        right = ws.cell(row=r, column=2, value=b)
        right.alignment = WRAP
        r += 1

    section("What's in this workbook")
    line("How to Read This Report", "This sheet - a plain-language guide to the rest of the workbook.")
    line("Summary", "The overall AI-visibility score for the site, plus a score for each of the three pillars below.")
    for _, sheet_name in PILLARS:
        line(sheet_name, PILLAR_BLURB.get(sheet_name, ""))
    r += 1

    section("How every parameter is scored")
    line("1. Crawl", "The site is crawled and scraped over plain HTTP, and every page is parsed into text, "
                      "headings, links, images and structured data.")
    line("2. Extract", "For each parameter separately, the pages that matter to that check are pulled and "
                        "reduced to a small set of facts -- the curated data.")
    line("3. Score", "The parameter's Score Logic is applied to the measured values; that rules-based score "
                      "is the final score. An AI model reviews the same data and writes the explanation and the "
                      "recommendation, but it does not change the number.")
    line("4. Roll up", "Each parameter is worth fixed points: its pillar's weight (35 Technical / 40 On-Page / "
                        "25 Off-Page) split equally over the pillar's parameters. Points earned = score / 100 x "
                        "its points. Pillar score = pillar points earned / pillar points measurable x 100. "
                        "Overall score = all points earned / all points measurable x 100.")
    line("If a check cannot be measured", "It is marked UNKNOWN and left out of the measurable points: it is "
                                            "not counted as 0 and its points are not given to other parameters.")
    r += 1

    section("Column glossary (Technical / On-Page / Off-Page sheets)")
    line("Parameter ID / Parameter", "The identifier and plain-English name of the specific check.")
    line("What This Parameter Means", "Why this check matters for how AI assistants read and cite the site.")
    line("How the Metric Is Calculated", "The exact rule used to turn the curated data into a score.")
    line("What Was Found on Your Site", "The actual facts pulled from this site and graded for this "
                                          "parameter, listed one per line. This is exactly what was scored "
                                          "- nothing else was considered.")
    line("Score (%)", "This parameter's individual score, 0-100. 'N/A' means it could not be scored.")
    line("Status", "The verdict for this parameter on this scan. See the color legend below.")
    line("Scored By", "'AI model' when the model graded it; 'Rules-based' when the model was unavailable and "
                       "the deterministic score stands in.")
    line("Explanation", "Why the parameter scored what it did, in plain English, citing the curated data.")
    line("Recommendation", "The specific action to take for this site. 'No change required.' when it passes.")
    r += 1

    # Spelled out rather than shown as a color key: the Status column carries the verdict
    # as a word, so the legend only has to say what each word means.
    section("What each Status means")
    for status, meaning in STATUS_MEANING.items():
        line(status, meaning)
    r += 1

    section("Score bands (Summary sheet)")
    for label, band in SCORE_BANDS:
        line(label, band)


def build_excel(report: dict, registry: list[dict]) -> bytes:
    """A professional, self-explanatory workbook: a "How to Read This Report" guide, a
    Summary sheet, and one sheet per pillar (Technical, On-Page, Off-Page) - each row
    explaining not just the score but how it was calculated.

    Entirely monochrome, by design: see the Look & feel section above.
    """
    reg_by_id = {r["parameter_id"]: r for r in registry}
    domain = report.get("domain") or ""
    generated_at = _friendly_timestamp(report.get("generated_at"))
    params = report.get("parameters") or []
    wb = Workbook()
    wb.remove(wb.active)

    summary = wb.create_sheet("Summary")
    header_row = _title_bar(summary, f"AI Visibility Audit - {domain}", f"Generated {generated_at}", 2)
    summary.column_dimensions["A"].width = 24
    summary.column_dimensions["B"].width = 60
    r = header_row + 1

    def kv(label: str, value, bold_value: bool = False) -> int:
        nonlocal r
        summary.cell(row=r, column=1, value=label).font = Font(bold=True, size=10)
        cell = summary.cell(row=r, column=2, value=_safe(value) if value is not None else "")
        if bold_value:
            cell.font = Font(bold=True, size=10)
        row_num = r
        r += 1
        return row_num

    kv("Domain", report.get("domain"))
    kv("Input URL", report.get("input_url"))
    kv("Scan ID", report.get("scan_id"))
    kv("Generated At", generated_at)
    r += 1

    overall_row = kv("Overall Score", report.get("overall_score"), bold_value=True)
    overall_label_row = kv("Overall Label", report.get("overall_label"), bold_value=True)
    r += 1

    category_scores = report.get("category_scores") or {}
    category_labels = report.get("category_labels") or {}
    pillar_label_rows = []
    for section, sheet_name in PILLARS:
        kv(f"{sheet_name} Score", category_scores.get(section))
        label_row = kv(f"{sheet_name} Label", category_labels.get(section))
        pillar_label_rows.append(label_row)
    r += 1

    kv("Parameters Scored", (report.get("coverage") or {}).get("known"))
    kv("Parameters Unknown", (report.get("coverage") or {}).get("unknown"))
    points = report.get("points") or {}
    if points:
        kv("Points Earned / Measurable", f"{points.get('earned', 0):.2f} / {points.get('measurable', 0):.2f}")

    # The overall score is the one number people look for first, so it is set large; the
    # band labels beside it are bold. No fills -- the legend on the How to Read sheet says
    # what each band spans.
    for row_num in (overall_label_row, *pillar_label_rows):
        summary.cell(row=row_num, column=2).font = LABEL_FONT
    summary.cell(row=overall_row, column=2).font = Font(bold=True, size=14)

    _write_how_to_read_sheet(wb)

    for section, sheet_name in PILLARS:
        rows = [p for p in params if p.get("section") == section]
        _write_pillar_sheet(wb, sheet_name, rows, reg_by_id, domain, generated_at)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def save_excel_output(report: dict, registry: list[dict]) -> Path:
    """Write this scan's audit workbook to backend/data/scan_output/.

    The name is derived from the domain alone, so a site has exactly one workbook: a
    re-audit refreshes that file in place instead of leaving a trail of near-identical
    spreadsheets behind. The
    canonical copy is always the stored report -- this file is rebuilt from it on demand by
    GET /scans/{id}/download.xlsx if it is ever missing.
    """
    data = build_excel(report, registry)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{_slug(report.get('domain'))}-parameter-new-logic.xlsx"
    path.write_bytes(data)
    return path

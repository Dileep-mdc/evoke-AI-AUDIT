"""Persists the three artifacts of every audit to backend/data/scan_output/, named after
the site so it is obvious at a glance which audit they belong to:

    {company}-scraped-content.json      -- the full end-to-end scraped output for every page
                                           the crawler visited (title, meta description,
                                           headings, links, images, schema, visible text),
                                           each with its scrape-confidence score and label.
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

import io
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .config import DATA_DIR
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


def save_crawl_output(scan_id: str, ctx) -> Path:
    """Save the complete scraped-website output for this run: every page the crawler
    visited, end to end, each with its scrape-confidence score, label and reasons."""
    pages = []
    for p in ctx.pages or []:
        confidence = scrape_confidence(p)
        pages.append({
            "url": p.url,
            "final_url": p.result.final_url if p.result else p.url,
            "status_code": p.result.status_code if p.result else None,
            "fetch_error": p.result.error if p.result else None,
            "blocked_reason": p.blocked_reason,
            "scrape_confidence_score": confidence["score"],
            "scrape_confidence_label": confidence["label"],
            "scrape_confidence_reasons": confidence["reasons"],
            "page_type": p.page_type,
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
        "pages": pages,
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{_slug(ctx.domain)}-scraped-content.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


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
    ("Curated Input Data (what the model graded)", 50, 48),
    ("Score (%)", 9, 8),
    ("Status", 11, 10),
    ("Scored By", 15, 13),
    ("Explanation", 56, 54),
    ("Recommendation", 52, 50),
]
STATUS_COL = 7
SCORED_BY_LABEL = {"llm": "AI model", "deterministic": "Rules-based (model unavailable)"}
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
            evidence.get("curated_input") or "No curated data was captured for this check.",
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
    line("3. Grade", "That curated data, together with the parameter's definition and its metric, is given to "
                      "an AI model, which returns the score, the explanation and the recommendation.")
    line("4. Roll up", "Pillar score = the weighted average of every scored parameter in that pillar. Overall "
                        "score = the three pillars weighted 35% Technical / 40% On-Page / 25% Off-Page.")
    line("If the model is unavailable", "The parameter keeps the rules-based score computed in step 2, and its "
                                          "'Scored By' column says so. Nothing is left unscored silently.")
    r += 1

    section("Column glossary (Technical / On-Page / Off-Page sheets)")
    line("Parameter ID / Parameter", "The identifier and plain-English name of the specific check.")
    line("What This Parameter Means", "Why this check matters for how AI assistants read and cite the site.")
    line("How the Metric Is Calculated", "The exact rule used to turn the curated data into a score.")
    line("Curated Input Data", "The actual extracted facts from this site that were graded - the model saw "
                                "exactly this and nothing else.")
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
    model_scored = sum(1 for p in params if ((p.get("evidence") or {}).get("scoring_method")) == "llm")
    kv("Graded by AI Model", f"{model_scored} of {len(params)} parameters" if params else "")

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
    re-audit, or the "Re-check Unscored" retry re-scoring an existing scan, refreshes that
    file in place instead of leaving a trail of near-identical spreadsheets behind. The
    canonical copy is always the stored report -- this file is rebuilt from it on demand by
    GET /scans/{id}/download.xlsx if it is ever missing.
    """
    data = build_excel(report, registry)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{_slug(report.get('domain'))}-parameter-new-logic.xlsx"
    path.write_bytes(data)
    return path

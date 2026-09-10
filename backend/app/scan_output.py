"""Persists two artifacts to disk for every scan run, named so that auditing the same
URL multiple times produces clearly distinct, human-readable files instead of overwriting
or opaque UUID folders:

    {company}-scrap-{date}_{time}.json     -- the full end-to-end scraped output for every
                                               page the crawler visited (title, meta
                                               description, headings, links, images,
                                               schema, visible text).
    {company}-metrics-{date}_{time}.xlsx   -- one row per parameter with its name, output,
                                               calculation method, and the timestamp it
                                               was evaluated at.

Each run gets its own timestamped pair of files, so running the same URL five times leaves
five distinct scrape files and five distinct metrics files in backend/data/scan_output/.
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .config import DATA_DIR
from .parameters.scoring import scrape_confidence

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


def _file_stamp() -> str:
    """A filename-safe date/time stamp, e.g. '2026-08-25_14-56-23'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")


def _unique_path(path: Path) -> Path:
    """Guard against two runs landing in the same second for the same domain."""
    if not path.exists():
        return path
    stem, suffix, parent, n = path.stem, path.suffix, path.parent, 2
    while (candidate := parent / f"{stem}-{n}{suffix}").exists():
        n += 1
    return candidate


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
    """Every link the crawler failed to scrape cleanly for this run -- written alongside
    the full scrape output so a failed-link list is available for every audit without
    digging through the big file. Covers both pages that were fetched but came back
    broken/blocked, and targets that returned no response at all and never became a Page
    (only visible via ctx.crawl_errors)."""
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
    path = _unique_path(OUTPUT_DIR / f"{_slug(ctx.domain)}-failed-links-{_file_stamp()}.json")
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def save_crawl_output(scan_id: str, ctx) -> Path:
    """Save the complete scraped-website output (every page, end to end) for this run."""
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
    path = _unique_path(OUTPUT_DIR / f"{_slug(ctx.domain)}-scrap-{_file_stamp()}.json")
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# --- Look & feel -----------------------------------------------------------------
# A small, consistent palette used across every sheet so the workbook reads as one
# professional document rather than a raw data dump.
ACCENT = "1F4E78"          # dark blue - title bars
ACCENT_FONT = Font(bold=True, size=13, color="FFFFFF")
SUBTITLE_FONT = Font(italic=True, size=9, color="595959")
HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(bold=True, size=10, color="FFFFFF")
BAND_FILL = PatternFill(start_color="F2F6FA", end_color="F2F6FA", fill_type="solid")
WRAP = Alignment(wrap_text=True, vertical="top")
WRAP_CENTER = Alignment(wrap_text=True, vertical="center", horizontal="center")
THIN = Side(style="thin", color="D0D7DE")
CELL_BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

# Status and score-label colors are shared everywhere they appear (pillar sheets,
# the Summary dashboard, and the legend on the "How to Read" sheet) so a color
# always means the same thing no matter which tab you're looking at.
STATUS_STYLE = {
    "PASS": ("C6EFCE", "006100"),
    "PARTIAL": ("FFEB9C", "9C6500"),
    "FAIL": ("FFC7CE", "9C0006"),
    "UNKNOWN": ("E0E0E0", "595959"),
}
LABEL_STYLE = {
    "Excellent": ("C6EFCE", "006100"),
    "Good": ("DDF2D0", "375623"),
    "Fair": ("FFEB9C", "9C6500"),
    "Poor": ("FCE4D6", "9C4B00"),
    "Critical": ("FFC7CE", "9C0006"),
    "Unknown": ("E0E0E0", "595959"),
}
STATUS_MEANING = {
    "PASS": "Requirement is fully met.",
    "PARTIAL": "Requirement is partially met - some, but not all, criteria were satisfied.",
    "FAIL": "Requirement is not met and needs attention.",
    "UNKNOWN": "Could not be determined automatically (e.g. blocked page, missing data, or manual check needed).",
}

COLUMNS = [
    ("Parameter ID", 13),
    ("Parameter Name", 34),
    ("Status", 11),
    ("Score (%)", 10),
    ("Weight", 8),
    ("Output", 50),
    ("Calculation Method", 45),
    ("Scoring Formula", 65),
    ("Checked Source", 32),
    ("Evaluated At (Timestamp)", 26),
]
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
    """Writes a merged, colored title row (and an optional subtitle row) at the top of
    a sheet and returns the next free row number."""
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=span)
    cell = ws.cell(row=1, column=1, value=text)
    cell.font = ACCENT_FONT
    cell.fill = PatternFill(start_color=ACCENT, end_color=ACCENT, fill_type="solid")
    cell.alignment = Alignment(vertical="center", horizontal="left", indent=1)
    ws.row_dimensions[1].height = 26
    next_row = 2
    if subtitle:
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=span)
        sub_cell = ws.cell(row=2, column=1, value=subtitle)
        sub_cell.font = SUBTITLE_FONT
        sub_cell.alignment = Alignment(vertical="center", horizontal="left", indent=1)
        next_row = 3
    return next_row


def _write_pillar_sheet(wb: Workbook, sheet_name: str, rows: list[dict], reg_by_id: dict, domain: str, generated_at: str) -> None:
    ws = wb.create_sheet(sheet_name)
    header_row = _title_bar(
        ws,
        f"{sheet_name} Parameters - {domain or ''}".strip(" -"),
        f"{PILLAR_BLURB.get(sheet_name, '')}  |  Generated {generated_at}",
        len(COLUMNS),
    )
    for col, (name, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=header_row, column=col, value=name)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = WRAP_CENTER
        cell.border = CELL_BORDER
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[header_row].height = 30
    ws.freeze_panes = f"A{header_row + 1}"

    for offset, p in enumerate(rows):
        r = header_row + 1 + offset
        reg = reg_by_id.get(p["parameter_id"], {})
        evidence = p.get("evidence") or {}
        output = evidence.get("summary") or p.get("error") or "No output recorded."
        score = p.get("score")
        status = (p.get("status") or "UNKNOWN").upper()
        row = [
            p.get("parameter_id"),
            p.get("name"),
            status,
            score if score is not None else "N/A",
            p.get("weight"),
            output,
            reg.get("check_logic") or "",
            FORMULAS.get(p.get("parameter_id"), reg.get("scoring") or ""),
            p.get("checked_url_or_source") or "",
            _friendly_timestamp(p.get("evaluated_at")),
        ]
        band = BAND_FILL if offset % 2 == 1 else None
        for c, value in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.alignment = WRAP
            cell.border = CELL_BORDER
            if band is not None:
                cell.fill = band
        status_cell = ws.cell(row=r, column=3)
        fill_color, font_color = STATUS_STYLE.get(status, STATUS_STYLE["UNKNOWN"])
        status_cell.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
        status_cell.font = Font(bold=True, color=font_color, size=10)
        status_cell.alignment = WRAP_CENTER
    ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(COLUMNS))}{max(ws.max_row, header_row)}"


def _write_how_to_read_sheet(wb: Workbook) -> None:
    """A plain-language front page so anyone opening this workbook - not just the
    person who ran the scan - understands what they're looking at before they read
    a single score."""
    ws = wb.create_sheet("How to Read This Report")
    wb.move_sheet(ws.title, offset=-(len(wb.sheetnames) - 1))
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 95
    _title_bar(ws, "How to Read This Report", "A quick guide to every sheet, column and color in this workbook.", 2)

    r = 4

    def section(title: str) -> None:
        nonlocal r
        cell = ws.cell(row=r, column=1, value=title)
        cell.font = Font(bold=True, size=11, color=ACCENT)
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

    section("How scores are calculated")
    line("Parameter score", "Each parameter is checked against the site and scored 0-100 based on the rule in its "
                             "'Calculation Method' and 'Scoring Formula' columns.")
    line("Pillar score", "The weighted average of every scored parameter in that pillar (each parameter's "
                          "'Weight' column controls how much it counts).")
    line("Overall score", "The weighted average of the three pillar scores (Technical, On-Page, Off-Page), "
                           "weighted 35% / 40% / 25% respectively.")
    r += 1

    section("Column glossary (Technical / On-Page / Off-Page sheets)")
    line("Parameter ID / Name", "The identifier and plain-English name of the specific check.")
    line("Status", "The verdict for this parameter on this scan. See the color legend below.")
    line("Score (%)", "This parameter's individual score, 0-100. 'N/A' means it could not be scored.")
    line("Weight", "How much this parameter counts toward its pillar's overall score, relative to the others.")
    line("Output", "A plain-English summary of exactly what the crawler found on the site for this check.")
    line("Calculation Method", "How the crawler gathered the evidence for this check (e.g. which HTML tag, "
                                "header or file it inspected).")
    line("Scoring Formula", "The exact rule used to turn that evidence into the Score (%) value.")
    line("Checked Source", "The specific URL, file or endpoint that was inspected for this parameter.")
    line("Evaluated At", "The timestamp when this parameter was evaluated during the scan.")
    r += 1

    section("Status colors")
    for status, meaning in STATUS_MEANING.items():
        fill_color, font_color = STATUS_STYLE[status]
        badge = ws.cell(row=r, column=1, value=status)
        badge.font = Font(bold=True, color=font_color, size=10)
        badge.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
        badge.alignment = WRAP_CENTER
        badge.border = CELL_BORDER
        desc = ws.cell(row=r, column=2, value=meaning)
        desc.alignment = WRAP
        r += 1
    r += 1

    section("Score bands (Summary sheet)")
    bands = [
        ("Excellent", "90-100"), ("Good", "75-89"), ("Fair", "60-74"),
        ("Poor", "40-59"), ("Critical", "0-39"), ("Unknown", "Not enough data to score"),
    ]
    for label, band in bands:
        fill_color, font_color = LABEL_STYLE[label]
        badge = ws.cell(row=r, column=1, value=label)
        badge.font = Font(bold=True, color=font_color, size=10)
        badge.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
        badge.alignment = WRAP_CENTER
        badge.border = CELL_BORDER
        ws.cell(row=r, column=2, value=band).alignment = WRAP
        r += 1


def build_excel(report: dict, registry: list[dict]) -> bytes:
    """A professional, self-explanatory workbook: a "How to Read This Report" guide,
    a Summary dashboard, and one color-coded sheet per pillar (Technical, On-Page,
    Off-Page) - each row explaining not just the score but how it was calculated."""
    reg_by_id = {r["parameter_id"]: r for r in registry}
    domain = report.get("domain") or ""
    generated_at = _friendly_timestamp(report.get("generated_at"))
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
        cell = summary.cell(row=r, column=2, value=value if value is not None else "")
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

    for row_num in (overall_label_row, *pillar_label_rows):
        cell = summary.cell(row=row_num, column=2)
        fill_color, font_color = LABEL_STYLE.get(str(cell.value), LABEL_STYLE["Unknown"])
        cell.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
        cell.font = Font(bold=True, color=font_color, size=10)
    for row_num in (overall_row,):
        summary.cell(row=row_num, column=2).font = Font(bold=True, size=14, color=ACCENT)

    _write_how_to_read_sheet(wb)

    params = report.get("parameters") or []
    for section, sheet_name in PILLARS:
        rows = [p for p in params if p.get("section") == section]
        _write_pillar_sheet(wb, sheet_name, rows, reg_by_id, domain, generated_at)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def save_excel_output(scan_id: str, report: dict, registry: list[dict]) -> Path:
    data = build_excel(report, registry)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = _unique_path(OUTPUT_DIR / f"{_slug(report.get('domain'))}-metrics-{_file_stamp()}.xlsx")
    path.write_bytes(data)
    return path

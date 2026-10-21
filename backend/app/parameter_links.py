"""The per-company "which links did each parameter check" workbook, written after every audit:

    <SITE_FOLDERS_ROOT>/<host>/<company>.xlsx      e.g. AI Audit/www.evoketechnologies.com/evoketechnologies.xlsx

    Checked Links  one row per link a parameter's score was calculated from:
                   Parameter Name | Checked Links | Reason
    Parameters     one row per parameter: Parameter Name | Links Checked | Reason (what the check
                   reads, how much of it this scan scored, and why the rest is left out)

The links are recorded by each handler as it reads them (common.result(links=...)), from the
same lists it scores -- never re-derived here -- so the sheet cannot disagree with the score.
They travel on the result row under CHECKED_LINKS and are taken off it (take()) before the
report is built: they are not part of what the model grades or what the dashboard loads.

A re-check re-runs only some parameters, so every audit's links are also kept in a sidecar file
in the data directory; a re-check merges its new rows into it and rewrites the workbook.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .config import SITE_FOLDERS_ROOT
from .page_selection import link_reason
from .parameters.common import CHECKED_LINKS
from .scan_output import OUTPUT_DIR, _slug
from .site_folder import _folder_name

log = logging.getLogger("parameter_links")

COLUMNS = ("Parameter Name", "Checked Links", "Reason")
_XLSX_MAX_ROWS = 1_048_575
# Second-level labels that sit under a country code ("example.co.uk", "example.com.au"), so the
# company is the label before them.
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc", "or", "ne", "go"}


def company_from_url(url_or_host: str) -> str:
    """The company name as the URL gives it: 'https://www.evoketechnologies.com/' -> 'evoketechnologies'."""
    text = (url_or_host or "").strip()
    host = (urlparse(text if "//" in text else f"//{text}").hostname or "").lower().strip(".")
    labels = [x for x in host.split(".") if x]
    if not labels:
        return "site"
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        name = labels[-3]
    elif len(labels) >= 2:
        name = labels[-2]
    else:
        name = labels[0]
    return re.sub(r'[<>:"/\\|?*]+', "", name) or "site"


def take(results: list[dict]) -> dict[str, list[dict]]:
    """Remove each row's checked links and return them by parameter id."""
    return {row["parameter_id"]: row.pop(CHECKED_LINKS, None) or [] for row in results if row}


def _sidecar(host: str) -> Path:
    return OUTPUT_DIR / f"{_slug(host)}-parameter-links.json"


def _reason_without_links(row: dict) -> str:
    """Why a parameter lists no links: it did not apply, could not run, or reads nothing by design."""
    ev = row.get("evidence") or {}
    if ev.get("does_not_apply"):
        return f"No links: this check does not apply to this site ({ev['does_not_apply']})."
    if ev.get("no_readable_pages"):
        return "No links: no page of the site could be read, so this check had nothing to score."
    if row.get("error"):
        return f"No links: the check could not be completed ({row['error']})."
    return "No links: this check read nothing it could score on this scan."


_SECTION_ORDER = {"technical": 0, "on_page": 1, "off_page": 2}


def _order(row: dict) -> tuple:
    """Technical, then On-Page, then Off-Page, each by number -- the order the audit reports them in."""
    number = re.search(r"(\d+)", row["parameter_id"])
    return (_SECTION_ORDER.get(row.get("section"), 3), int(number.group(1)) if number else 0, row["parameter_id"])


def _name(row: dict) -> str:
    return f"{row['parameter_id']} - {row.get('name') or ''}".strip(" -")


def _write(path: Path, rows: list[dict], links: dict[str, list[dict]]) -> None:
    import xlsxwriter
    from xlsxwriter.exceptions import FileCreateError

    wb = xlsxwriter.Workbook(str(path), {"constant_memory": True, "strings_to_urls": False,
                                         "strings_to_numbers": False, "strings_to_formulas": False})
    head = wb.add_format({"bold": True, "bg_color": "#D9E1F2", "border": 1, "valign": "top"})
    wrap = wb.add_format({"text_wrap": True, "valign": "top"})
    top = wb.add_format({"valign": "top"})

    ws = wb.add_worksheet("Checked Links")
    ws.set_column(0, 0, 48, top)
    ws.set_column(1, 1, 80, top)
    ws.set_column(2, 2, 110, wrap)
    ws.write_row(0, 0, COLUMNS, head)
    ws.freeze_panes(1, 0)
    r = 1
    for row in rows:
        items = links.get(row["parameter_id"]) or []
        if not items:
            if r <= _XLSX_MAX_ROWS:
                ws.write_row(r, 0, (_name(row), "(no links checked)", _reason_without_links(row)))
            r += 1
            continue
        for item in items:
            if r > _XLSX_MAX_ROWS:
                break
            ws.write_row(r, 0, (_name(row), item["url"], item["why"]))
            r += 1
    ws.autofilter(0, 0, max(1, min(r, _XLSX_MAX_ROWS + 1) - 1), len(COLUMNS) - 1)

    summary = wb.add_worksheet("Parameters")
    summary.set_column(0, 0, 48, top)
    summary.set_column(1, 1, 14, top)
    summary.set_column(2, 2, 120, wrap)
    summary.write_row(0, 0, ("Parameter Name", "Links Checked", "Reason"), head)
    summary.freeze_panes(1, 0)
    for i, row in enumerate(rows, 1):
        count = len(links.get(row["parameter_id"]) or [])
        reason = link_reason(row["parameter_id"], row.get("evidence") or {})
        if not count:
            reason = f"{_reason_without_links(row)}\n{reason}"
        summary.write_row(i, 0, (_name(row), count, reason))
    try:
        wb.close()
    except FileCreateError as exc:
        raise PermissionError(str(exc)) from None


def save(scan_id: str, ctx, results: list[dict], links: dict[str, list[dict]],
         merge: bool = False) -> Optional[Path]:
    """Write <company>.xlsx for this audit. With merge=True (a re-check), `links` holds only the
    re-run parameters and the rest come from the audit's sidecar. Never raises: logs and returns None."""
    try:
        host = (urlparse(getattr(ctx, "origin", "") or "").hostname or getattr(ctx, "domain", "") or "").lower()
        if not host:
            # No site address, so no company name to give the file: writing "site.xlsx" would be a guess.
            log.warning("Scan %s: no site address, so no parameter links workbook was written", scan_id)
            return None
        pages = getattr(ctx, "pages", None)
        if pages is not None and not any(getattr(p, "result", None) and p.result.ok for p in pages):
            # The site refused every request (tcs.com: 403): no page was checked, and a folder made
            # only for this workbook would look like a saved copy of a site that was never read.
            log.info("Scan %s: no page of %s could be read; no parameter links workbook written", scan_id, host)
            return None
        sidecar = _sidecar(host)
        if merge:
            try:
                saved = json.loads(sidecar.read_text(encoding="utf-8"))
                if saved.get("scan_id") == scan_id:
                    links = {**saved.get("parameters", {}), **links}
                else:
                    log.warning("Parameter links for %s belong to another audit; re-checked parameters only", host)
            except FileNotFoundError:
                log.warning("No saved parameter links for %s; re-checked parameters only", host)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"scan_id": scan_id, "host": host,
                                       "saved_at": datetime.now(timezone.utc).isoformat(),
                                       "parameters": links}, ensure_ascii=False), encoding="utf-8")
        rows = sorted((r for r in results if r), key=_order)
        folder = SITE_FOLDERS_ROOT / _folder_name(host)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{company_from_url(host)}.xlsx"
        _write(path, rows, links)
        return path
    except PermissionError:
        log.warning("Scan %s: the parameter links workbook is open in another program and was not refreshed", scan_id)
    except Exception:
        log.exception("Scan %s: could not write the parameter links workbook", scan_id)
    return None

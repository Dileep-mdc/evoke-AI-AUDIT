from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..config import ENGINE_VERSION, SCAN_RETRY_DELAY
from ..crawler import snapshot
from ..crawler.discover import crawl_site
from ..crawler.http import normalize_url
from ..db import discard_scan_pages, vacuum
from ..parameters.engine import load_registry, run_all_parameters
from ..parameters.formula_simple import logic_for
from ..parameters.working import working_for
from ..parameters.scoring import SCORING_METHOD, build_report, final_from_rules, prioritize
from ..pdf_export import build_pdf
from ..scan_output import build_excel, save_crawl_failures, save_crawl_output, save_excel_output
from ..storage.repository import ScanRepository

router = APIRouter(prefix="/api")
repo = ScanRepository()
registry = load_registry()
log = logging.getLogger("scans")
_running: set[asyncio.Task] = set()



def _worth_retrying(row: dict | None) -> bool:
    """A check that came back unscored for a reason a second attempt can fix: a timed-out
    model call, a rate-limited search, a network error. A check that does not apply to the
    site, or whose source is not configured, gives the same answer every time."""
    if row is None:
        return True
    if (row.get("evidence") or {}).get("not_applicable"):
        return False
    if "not configured" in str(row.get("error") or ""):
        return False
    return row["status"] == "UNKNOWN" or bool(row.get("error"))


SECTION_TOTALS = {"technical": 0, "on_page": 0, "off_page": 0}
for _spec in registry:
    SECTION_TOTALS[_spec["section"]] = SECTION_TOTALS.get(_spec["section"], 0) + 1


class ScanRequest(BaseModel):
    url: str = Field(..., min_length=1)


def _progress_payload(scan: dict) -> dict:
    return {
        "scan_id": scan["id"],
        "domain": scan["domain"],
        "input_url": scan["input_url"],
        "status": scan["status"],
        "progress_percent": scan.get("progress_percent") or 0,
        "technical_completed": scan.get("technical_completed") or 0,
        "technical_total": scan.get("technical_total") or SECTION_TOTALS["technical"],
        "onpage_completed": scan.get("onpage_completed") or 0,
        "onpage_total": scan.get("onpage_total") or SECTION_TOTALS["on_page"],
        "offpage_completed": scan.get("offpage_completed") or 0,
        "offpage_total": scan.get("offpage_total") or SECTION_TOTALS["off_page"],
        "started_at": scan.get("started_at"),
        "completed_at": scan.get("completed_at"),
        "errors_count": scan.get("errors_count") or 0,
        "overall_score": scan.get("overall_score"),
    }


async def execute_scan(scan_id: str, url: str, folder: str | None = None) -> None:
    log.info("Scan %s starting %s%s", scan_id, url, f" from saved folder {folder}" if folder else "")
    repo.update(scan_id, status="crawling", progress_percent=2)

    def bump(pct: int) -> None:
        repo.update(scan_id, status="crawling", progress_percent=pct)

    try:
        if folder:
            # Every fetch this scan makes -- crawl, robots.txt, sitemap, the link checks in the
            # parameters -- now reads the folder; see crawler/snapshot.py.
            snapshot.activate(snapshot.open_folder(folder))
        ctx = await crawl_site(url, on_progress=bump)
        repo.save_pages(scan_id, ctx.pages)
        # Both crawl artifacts are written before scoring starts, so the scraped content and
        # the failed-link list survive even if the model calls or the workbook later fail.
        crawl_path = await asyncio.to_thread(save_crawl_output, scan_id, ctx)
        # Writing the scraped content is tens of megabytes on a large site and takes real
        # time, so it gets its own step on the bar rather than looking like a stalled crawl.
        repo.update(scan_id, status="crawling", progress_percent=29)
        await asyncio.to_thread(save_crawl_failures, scan_id, ctx)
        # The crawl owns 2-30 of the bar and scoring owns the rest. The crawl used to be
        # squeezed into 2-18, of which fetching, parsing and three rounds of link discovery
        # all landed on 16 -- so the longest phase of a scan looked frozen on one number.
        repo.update(scan_id, status="evaluating", progress_percent=30, domain=ctx.domain, crawl_output_path=str(crawl_path))

        counts = {"technical": 0, "on_page": 0, "off_page": 0}
        errors = 0

        async def on_each(row: dict) -> None:
            nonlocal errors
            repo.save_parameter(scan_id, row)
            counts[row["section"]] = counts.get(row["section"], 0) + 1
            if row.get("error") or row["status"] == "UNKNOWN":
                errors += 1 if row.get("error") else 0
            done = sum(counts.values())
            repo.update(
                scan_id,
                technical_completed=counts["technical"],
                onpage_completed=counts["on_page"],
                offpage_completed=counts["off_page"],
                progress_percent=min(99, 30 + done / len(registry) * 69),
                errors_count=errors,
                status="evaluating",
            )

        results = await run_all_parameters(ctx, registry, on_each)
        # Checks that came back unscored are retried once, straight away, against the same
        # crawl, so the report is complete when the scan finishes and nothing has to be
        # re-run by hand afterwards.
        by_id = {r["parameter_id"]: r for r in results}
        retry = [spec for spec in registry if _worth_retrying(by_id.get(spec["parameter_id"]))]
        if retry:
            log.info("Scan %s retrying %d unscored parameter(s): %s", scan_id, len(retry),
                     ", ".join(s["parameter_id"] for s in retry))
            await asyncio.sleep(SCAN_RETRY_DELAY)

            async def save(row: dict) -> None:
                repo.save_parameter(scan_id, row)

            for row in await run_all_parameters(ctx, retry, save):
                by_id[row["parameter_id"]] = row
            results = [by_id[s["parameter_id"]] for s in registry if s["parameter_id"] in by_id]
            repo.update(scan_id, errors_count=sum(1 for r in results if r.get("error")))
        issues = prioritize(results)
        repo.save_issues(scan_id, issues)
        completed = datetime.now(timezone.utc).isoformat()
        repo.update(scan_id, completed_at=completed, progress_percent=100)
        scan = repo.get(scan_id)
        report = build_report(scan, results, issues)
        # Which copy of the site was audited: the saved folder or the live web. The dashboard
        # shows it, since scores from a snapshot describe the site as of the day it was saved.
        report["snapshot"] = getattr(ctx, "snapshot", None) or {}
        repo.save_report(scan_id, report)
        # The workbook is an artifact of the report, not a precondition for it. Writing it
        # used to sit inside this try, so a locked file on a synced drive -- or any openpyxl
        # complaint about one character of scraped text -- marked a finished scan as "error",
        # and an errored scan refuses to serve the report it had already saved. A whole crawl
        # was thrown away over a spreadsheet. It can still be rebuilt on demand from the
        # stored report by GET /scans/{id}/download.xlsx.
        try:
            excel_path = await asyncio.to_thread(save_excel_output, report, registry)
            repo.update(scan_id, excel_output_path=str(excel_path))
        except Exception:
            log.exception("Scan %s completed but its workbook could not be written", scan_id)
        # Only flip status to "completed" once the report is fully persisted, so a client
        # that sees "completed" and immediately requests the report never hits a window
        # where report_json is still empty.
        repo.update(scan_id, status="completed")
        # The write load is over, so this is the moment to hand back the space deleted rows
        # are still holding. Off the event loop: VACUUM rewrites the database file.
        await asyncio.to_thread(vacuum)
    except Exception:
        log.exception("Scan %s failed", scan_id)
        repo.update(scan_id, status="error", errors_count=1)
        # Pages are saved straight after the crawl, before any parameter runs, so a scan that
        # dies during evaluation has already written a row per crawled page -- 1,064 of them,
        # for a run that produced no report. Nothing can ever read them, so they go.
        try:
            dropped = await asyncio.to_thread(discard_scan_pages, scan_id)
            if dropped:
                log.info("Discarded %d page rows from failed scan %s", dropped, scan_id)
        except Exception:
            log.exception("Could not discard page rows for failed scan %s", scan_id)


@router.post("/scans")
async def create_scan(payload: ScanRequest):
    try:
        if snapshot.looks_like_folder(payload.url):
            # A saved copy of a site on disk (see crawler/snapshot.py): the scan audits the
            # site that folder holds, reading its pages from the folder, not the web.
            snap = snapshot.open_folder(payload.url)
            url = normalize_url(f"https://{snap.host}/")
            folder = str(snap.folder)
        else:
            url = normalize_url(payload.url)
            folder = None
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    domain = urlparse(url).netloc.lower()
    scan_id = str(uuid.uuid4())
    scan = repo.create(
        scan_id, domain, url, ENGINE_VERSION,
        technical_total=SECTION_TOTALS["technical"],
        onpage_total=SECTION_TOTALS["on_page"],
        offpage_total=SECTION_TOTALS["off_page"],
    )
    task = asyncio.create_task(_run(scan_id, url, folder), name=f"scan-{scan_id}")
    _running.add(task)
    task.add_done_callback(_running.discard)
    return _progress_payload(scan)


async def _run(scan_id: str, url: str, folder: str | None = None) -> None:
    try:
        await execute_scan(scan_id, url, folder)
    except Exception:
        log.exception("Scan %s crashed", scan_id)
        repo.update(scan_id, status="error")


def _current_report(scan_id: str) -> dict | None:
    """The stored report, recalculated first if it was saved under an older scoring method.

    Reports are persisted when a scan finishes, so one saved before the current method
    (rules-based final scores, fixed points per parameter) would otherwise keep showing
    the numbers it was saved with. Rebuilding needs only the stored parameter rows, so it
    is done once, written back, and every later read is the plain stored copy.
    """
    report = repo.report(scan_id)
    if not report or report.get("scoring_method") == SCORING_METHOD:
        return report
    scan = repo.get(scan_id)
    results = repo.parameters(scan_id)
    if not scan or not results:
        return report
    issues = prioritize(results)
    fresh = build_report(scan, results, issues)
    fresh["snapshot"] = report.get("snapshot") or {}
    repo.save_issues(scan_id, issues)
    repo.save_report(scan_id, fresh)
    return fresh


def upgrade_saved_reports() -> int:
    """Recalculate every completed scan saved under an older scoring method; returns how many."""
    upgraded = 0
    for scan in repo.list_scans(limit=1000):
        if scan.get("status") != "completed":
            continue
        before = repo.report(scan["id"])
        if before and before.get("scoring_method") != SCORING_METHOD:
            try:
                _current_report(scan["id"])
                upgraded += 1
            except Exception:
                log.exception("Could not recalculate the saved report for scan %s", scan["id"])
    return upgraded


@router.get("/scans")
async def list_scans():
    return [_progress_payload(s) for s in repo.list_scans()]


@router.get("/scans/{scan_id}/progress")
async def get_progress(scan_id: str):
    scan = repo.get(scan_id)
    if not scan:
        raise HTTPException(404, "Scan not found")
    return _progress_payload(scan)


@router.get("/scans/{scan_id}/report")
async def get_report(scan_id: str):
    scan = repo.get(scan_id)
    if not scan:
        raise HTTPException(404, "Scan not found")
    if scan["status"] != "completed":
        raise HTTPException(409, f"Scan is {scan['status']}; report not ready")
    report = _current_report(scan_id)
    if not report:
        raise HTTPException(409, "Report not persisted yet")
    return report


@router.get("/scans/{scan_id}/parameters/{parameter_id}")
async def get_parameter(scan_id: str, parameter_id: str):
    row = repo.parameter(scan_id, parameter_id)
    if not row:
        raise HTTPException(404, "Parameter result not found")
    row = final_from_rules(row)
    row["working"] = working_for(row["parameter_id"], row.get("evidence"), row.get("score"))
    row["logic"] = logic_for(row["parameter_id"])
    return row


@router.get("/scans/{scan_id}/download")
async def download_report(scan_id: str):
    report = _current_report(scan_id)
    if not report:
        raise HTTPException(409, "Report not ready")
    pdf = build_pdf(report)
    filename = f"AI-Visibility-Audit-{report.get('domain','report')}.pdf"
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/scans/{scan_id}/download.xlsx")
async def download_workbook(scan_id: str):
    """This scan's metrics workbook.

    Rebuilt from the stored report rather than streamed off disk: the scan already wrote a
    copy to data/scan_output/, but that file can be missing (a scan from before the export
    existed, a cleaned data directory), and rebuilding is cheap enough that a download
    should not be the thing that fails.
    """
    report = _current_report(scan_id)
    if not report:
        raise HTTPException(409, "Report not ready")
    data = await asyncio.to_thread(build_excel, report, registry)
    filename = f"AI-Visibility-Audit-{report.get('domain', 'report')}.xlsx"
    return Response(
        content=data,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

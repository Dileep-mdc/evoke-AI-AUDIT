from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..config import ENGINE_VERSION
from ..crawler.discover import crawl_site
from ..crawler.http import normalize_url
from ..parameters.engine import load_registry, run_all_parameters
from ..parameters.scoring import build_report, prioritize
from ..pdf_export import build_pdf
from ..scan_output import save_crawl_failures, save_crawl_output, save_excel_output
from ..storage.repository import ScanRepository

router = APIRouter(prefix="/api")
repo = ScanRepository()
registry = load_registry()
log = logging.getLogger("scans")
_running: set[asyncio.Task] = set()

# The crawled site (pages, soups, sitemap, robots...) behind a completed scan, kept in
# memory so "re-run unscored parameters" can re-evaluate against the exact same crawl
# instead of re-crawling the site. Bounded so a long-lived server doesn't accumulate one
# entry per scan forever; a scan that's aged out simply can't be re-run without a fresh
# full scan.
_ctx_cache: "OrderedDict[str, object]" = OrderedDict()
_CTX_CACHE_SIZE = 12


def _cache_ctx(scan_id: str, ctx) -> None:
    _ctx_cache[scan_id] = ctx
    _ctx_cache.move_to_end(scan_id)
    while len(_ctx_cache) > _CTX_CACHE_SIZE:
        _ctx_cache.popitem(last=False)


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


async def execute_scan(scan_id: str, url: str) -> None:
    log.info("Scan %s starting %s", scan_id, url)
    repo.update(scan_id, status="crawling", progress_percent=2)

    def bump(pct: int) -> None:
        repo.update(scan_id, status="crawling", progress_percent=pct)

    try:
        ctx = await crawl_site(url, on_progress=bump)
        _cache_ctx(scan_id, ctx)
        repo.save_pages(scan_id, ctx.pages)
        crawl_path = await asyncio.to_thread(save_crawl_output, scan_id, ctx)
        await asyncio.to_thread(save_crawl_failures, scan_id, ctx)
        repo.update(scan_id, status="evaluating", progress_percent=18, domain=ctx.domain, crawl_output_path=str(crawl_path))

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
                progress_percent=min(99, 18 + done / len(registry) * 80),
                errors_count=errors,
                status="evaluating",
            )

        results = await run_all_parameters(ctx, registry, on_each)
        issues = prioritize(results)
        repo.save_issues(scan_id, issues)
        completed = datetime.now(timezone.utc).isoformat()
        repo.update(scan_id, completed_at=completed, progress_percent=100)
        scan = repo.get(scan_id)
        report = build_report(scan, results, issues)
        repo.save_report(scan_id, report)
        excel_path = await asyncio.to_thread(save_excel_output, scan_id, report, registry)
        repo.update(scan_id, excel_output_path=str(excel_path))
        # Only flip status to "completed" once the report is fully persisted, so a client
        # that sees "completed" and immediately requests the report never hits a window
        # where report_json is still empty.
        repo.update(scan_id, status="completed")
    except Exception:
        log.exception("Scan %s failed", scan_id)
        repo.update(scan_id, status="error", errors_count=1)


@router.post("/scans")
async def create_scan(payload: ScanRequest):
    try:
        url = normalize_url(payload.url)
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
    task = asyncio.create_task(_run(scan_id, url), name=f"scan-{scan_id}")
    _running.add(task)
    task.add_done_callback(_running.discard)
    return _progress_payload(scan)


async def _run(scan_id: str, url: str) -> None:
    try:
        await execute_scan(scan_id, url)
    except Exception:
        log.exception("Scan %s crashed", scan_id)
        repo.update(scan_id, status="error")


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
    report = repo.report(scan_id)
    if not report:
        raise HTTPException(409, "Report not persisted yet")
    return report


@router.get("/scans/{scan_id}/parameters/{parameter_id}")
async def get_parameter(scan_id: str, parameter_id: str):
    row = repo.parameter(scan_id, parameter_id)
    if not row:
        raise HTTPException(404, "Parameter result not found")
    return row


@router.post("/scans/{scan_id}/rerun-unscored")
async def rerun_unscored(scan_id: str):
    """Re-evaluate only the parameters that came back UNKNOWN or errored last time,
    reusing the original crawl (no re-fetching the site) so a flaky check can be retried
    in seconds instead of re-running the whole audit."""
    scan = repo.get(scan_id)
    if not scan:
        raise HTTPException(404, "Scan not found")
    if scan["status"] != "completed":
        raise HTTPException(409, f"Scan is {scan['status']}; wait for it to complete first")

    ctx = _ctx_cache.get(scan_id)
    if ctx is None:
        raise HTTPException(
            409,
            "The original crawl for this scan is no longer available in memory "
            "(the server may have restarted since). Run a new full scan to refresh it.",
        )

    existing = {row["parameter_id"]: row for row in repo.parameters(scan_id)}
    failed_specs = [
        spec for spec in registry
        if (row := existing.get(spec["parameter_id"])) is None
        or row["status"] == "UNKNOWN"
        or row.get("error")
    ]
    if not failed_specs:
        return {"rerun_count": 0, "rerun_parameter_ids": [], **_progress_payload(scan)}

    repo.update(scan_id, status="evaluating")

    async def on_each(row: dict) -> None:
        repo.save_parameter(scan_id, row)

    await run_all_parameters(ctx, failed_specs, on_each)

    all_results = repo.parameters(scan_id)
    issues = prioritize(all_results)
    repo.save_issues(scan_id, issues)
    repo.update(scan_id, completed_at=datetime.now(timezone.utc).isoformat())
    scan = repo.get(scan_id)
    report = build_report(scan, all_results, issues)
    repo.save_report(scan_id, report)
    # Update this scan's one workbook in place -- a rerun re-scores the same scan, it
    # doesn't start a new one, so it must not leave a fresh file behind each time it's clicked.
    excel_path = await asyncio.to_thread(save_excel_output, scan_id, report, registry, existing_path=scan.get("excel_output_path"))
    repo.update(scan_id, excel_output_path=str(excel_path), status="completed")

    return {
        "rerun_count": len(failed_specs),
        "rerun_parameter_ids": [s["parameter_id"] for s in failed_specs],
        **_progress_payload(repo.get(scan_id)),
    }


@router.get("/scans/{scan_id}/download")
async def download_report(scan_id: str):
    report = repo.report(scan_id)
    if not report:
        raise HTTPException(409, "Report not ready")
    pdf = build_pdf(report)
    filename = f"AI-Visibility-Audit-{report.get('domain','report')}.pdf"
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

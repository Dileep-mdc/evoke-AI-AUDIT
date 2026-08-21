from __future__ import annotations

import asyncio
import logging
import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..config import DEFAULT_URL, ENGINE_VERSION
from ..crawler.discover import crawl_site
from ..crawler.http import normalize_url
from ..parameters.engine import load_registry, run_all_parameters
from ..parameters.scoring import build_report, prioritize
from ..pdf_export import build_pdf
from ..storage.repository import ScanRepository

router = APIRouter(prefix="/api")
repo = ScanRepository()
registry = load_registry()
log = logging.getLogger("scans")
_running: set[asyncio.Task] = set()


class ScanRequest(BaseModel):
    url: str = Field(default=DEFAULT_URL)


def _progress_payload(scan: dict) -> dict:
    return {
        "scan_id": scan["id"],
        "domain": scan["domain"],
        "input_url": scan["input_url"],
        "status": scan["status"],
        "progress_percent": scan.get("progress_percent") or 0,
        "technical_completed": scan.get("technical_completed") or 0,
        "technical_total": scan.get("technical_total") or 22,
        "onpage_completed": scan.get("onpage_completed") or 0,
        "onpage_total": scan.get("onpage_total") or 22,
        "offpage_completed": scan.get("offpage_completed") or 0,
        "offpage_total": scan.get("offpage_total") or 18,
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
        repo.save_pages(scan_id, ctx.pages)
        repo.update(scan_id, status="evaluating", progress_percent=18, domain=ctx.domain)

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
                progress_percent=min(99, 18 + done / 62 * 80),
                errors_count=errors,
                status="evaluating",
            )

        results = await run_all_parameters(ctx, registry, on_each)
        issues = prioritize(results)
        repo.save_issues(scan_id, issues)
        scan = repo.get(scan_id)
        from datetime import datetime, timezone
        completed = datetime.now(timezone.utc).isoformat()
        repo.update(scan_id, completed_at=completed, status="completed", progress_percent=100)
        scan = repo.get(scan_id)
        report = build_report(scan, results, issues)
        repo.save_report(scan_id, report)
    except Exception as exc:
        log.exception("Scan %s failed", scan_id)
        repo.update(scan_id, status="error", errors_count=1)


@router.post("/scans")
async def create_scan(payload: ScanRequest):
    try:
        url = normalize_url(payload.url or DEFAULT_URL)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    domain = urlparse(url).netloc.lower()
    scan_id = str(uuid.uuid4())
    scan = repo.create(scan_id, domain, url, ENGINE_VERSION)
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


@router.get("/scans/{scan_id}")
async def get_scan(scan_id: str):
    scan = repo.get(scan_id)
    if not scan:
        raise HTTPException(404, "Scan not found")
    return _progress_payload(scan)


@router.get("/scans/{scan_id}/progress")
async def get_progress(scan_id: str):
    return await get_scan(scan_id)


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


@router.get("/scans/{scan_id}/parameters")
async def get_parameters(scan_id: str):
    if not repo.get(scan_id):
        raise HTTPException(404, "Scan not found")
    return repo.parameters(scan_id)


@router.get("/scans/{scan_id}/parameters/{parameter_id}")
async def get_parameter(scan_id: str, parameter_id: str):
    row = repo.parameter(scan_id, parameter_id)
    if not row:
        raise HTTPException(404, "Parameter result not found")
    return row


@router.get("/scans/{scan_id}/download")
async def download_report(scan_id: str):
    report = repo.report(scan_id)
    if not report:
        raise HTTPException(409, "Report not ready")
    pdf = build_pdf(report)
    filename = f"AI-Visibility-Audit-{report.get('domain','report')}.pdf"
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

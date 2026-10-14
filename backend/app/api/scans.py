from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..config import ENGINE_VERSION, LLM_SCAN_BUDGET_USD, SCAN_RETRY_DELAY
from ..crawler import restore, snapshot
from ..crawler.discover import crawl_site
from ..crawler.http import normalize_url
from ..db import discard_scan_pages, vacuum
from ..llm import usage
from ..parameters.engine import load_registry, run_all_parameters
from ..parameters.common import upgrade_na_row
from ..parameters.formula_simple import logic_for
from ..parameters import crosschecks, identity, industry
from ..parameters.platforms import resolve as resolve_platforms
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
# AI spend of the scans running in this process, for the progress screen: the live meter and the
# estimate made once the crawl's size is known. Dropped when the scan ends; the report keeps both.
_meters: dict[str, usage.Meter] = {}
_estimates: dict[str, dict] = {}
_history_cache: list[dict] | None = None
# Re-checks in progress or just finished, by scan id: {state, total, done, message, parameters}.
_rechecks: dict[str, dict] = {}



def _worth_retrying(row: dict | None) -> bool:
    """A check that came back unscored for a reason a second attempt can fix: a timed-out
    model call, a rate-limited search, a network error. A check that does not apply to the
    site, or whose source is not configured, gives the same answer every time."""
    if row is None:
        return True
    if (row.get("evidence") or {}).get("retry_pointless"):
        return False
    if "not configured" in str(row.get("error") or ""):
        return False
    # A check that ran out its whole PARAMETER_TIMEOUT is slow, not unlucky: re-running it
    # spends that budget a second time (17 minutes of one audit) for the same result.
    if str(row.get("error") or "").startswith("Timed out after"):
        return False
    return row["status"] == "UNKNOWN" or bool(row.get("error"))


SECTION_TOTALS = {"technical": 0, "on_page": 0, "off_page": 0}
for _spec in registry:
    SECTION_TOTALS[_spec["section"]] = SECTION_TOTALS.get(_spec["section"], 0) + 1


class ScanRequest(BaseModel):
    url: str = Field(..., min_length=1)
    # Optional: the industry profile to audit against (parameters/industry_profiles.json; detected
    # from the site when omitted) and a region whose sites the off-page checks add.
    industry: str | None = None
    region: str | None = None


def usage_history(limit: int = 10) -> list[dict]:
    """What recent completed scans spent on AI, for estimating the next one.

    Read from saved reports, newest first, and cached until the next scan completes: the
    estimate is shown on every progress poll and must not re-read reports each time.
    """
    global _history_cache
    if _history_cache is None:
        out = []
        for spent in repo.ai_usage_history(limit=limit * 3):
            if spent.get("calls") and spent.get("pages"):
                entry = {"pages": spent["pages"], "calls": spent["calls"] + spent.get("search_calls", 0),
                         "cost_usd": spent.get("cost_usd") or 0.0}
                if "scaling_calls" in spent:
                    entry.update(scaling_calls=spent["scaling_calls"], scaling_cost_usd=spent.get("scaling_cost_usd") or 0.0)
                out.append(entry)
            if len(out) >= limit:
                break
        _history_cache = out
    return _history_cache


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
        "ai_usage": _live_usage(scan["id"]),
    }


def _live_usage(scan_id: str) -> dict | None:
    meter = _meters.get(scan_id)
    if meter is None:
        return None
    return {**meter.as_dict(), "estimate": _estimates.get(scan_id)}


async def execute_scan(scan_id: str, url: str, folder: str | None = None, choice: dict | None = None) -> None:
    global _history_cache
    log.info("Scan %s starting %s%s", scan_id, url, f" from saved folder {folder}" if folder else "")
    repo.update(scan_id, status="crawling", progress_percent=2)
    # Every model call and web search this scan makes is counted here, and refused once the
    # optional spending cap is reached (llm/usage.py).
    meter = _meters[scan_id] = usage.start(LLM_SCAN_BUDGET_USD)

    def bump(pct: int) -> None:
        repo.update(scan_id, status="crawling", progress_percent=pct)

    try:
        if folder:
            # Every fetch this scan makes -- crawl, robots.txt, sitemap, the link checks in the
            # parameters -- now reads the folder; see crawler/snapshot.py.
            snapshot.activate(snapshot.open_folder(folder))
        ctx = await crawl_site(url, on_progress=bump)
        # Company Identity Agent: who owns the site, before any off-page research searches for it.
        ctx.identity = await identity.resolve(ctx)
        ctx.company_name = ctx.identity["name"]
        ctx.brand_terms = list(dict.fromkeys(
            [t.lower() for t in (ctx.identity["brand"], ctx.identity.get("acronym"), ctx.identity["short_name"], ctx.identity["name"]) if t]
            + list(ctx.brand_terms)))
        log.info("Scan %s organisation: %s (%s, confidence %s)", scan_id, ctx.company_name,
                 ctx.identity["source"], ctx.identity["confidence"])
        # The industry profile: named in the request, or read from the site's own pages.
        choice = choice or {}
        ctx.industry = industry.chosen(choice["industry"]) if choice.get("industry") else industry.detect(ctx)
        ctx.platforms = resolve_platforms(ctx.industry["key"], choice.get("region"))
        log.info("Scan %s audited as %s (%s)", scan_id, ctx.industry["label"],
                 "detected" if ctx.industry["detected"] else "chosen")
        # The crawl's size is now known, so the AI spend can be estimated before scoring starts.
        estimate = usage.estimate(len(ctx.pages), usage_history())
        if estimate:
            _estimates[scan_id] = estimate
        # Thousands of page rows: written on a worker thread so the server keeps answering.
        await asyncio.to_thread(repo.save_pages, scan_id, ctx.pages)
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
        # Read the checks together and explain results that look contradictory. Annotated rows
        # are saved again.
        findings = crosschecks.reconcile(results, ctx)
        for row in results:
            if "cross_check" in (row.get("evidence") or {}):
                repo.save_parameter(scan_id, row)
        issues = prioritize(results)
        repo.save_issues(scan_id, issues)
        completed = datetime.now(timezone.utc).isoformat()
        repo.update(scan_id, completed_at=completed, progress_percent=100)
        scan = repo.get(scan_id)
        report = build_report(scan, results, issues)
        # Which copy of the site was audited: the saved folder or the live web. The dashboard
        # shows it, since scores from a snapshot describe the site as of the day it was saved.
        report["snapshot"] = getattr(ctx, "snapshot", None) or {}
        report["audited_at"] = report["generated_at"]
        report["platforms"] = ctx.platforms
        report["industry"] = ctx.industry
        report["identity"] = ctx.identity
        report["cross_checks"] = findings["cross_checks"]
        report["content_analysis"] = crosschecks.content_analysis(ctx)
        report["ai_usage"] = {**meter.as_dict(), "pages": len(ctx.pages), "estimate": _estimates.get(scan_id)}
        repo.save_report(scan_id, report)
        _history_cache = None
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
    finally:
        _meters.pop(scan_id, None)
        _estimates.pop(scan_id, None)


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
        resolve_platforms(payload.industry, payload.region)   # refuses an unknown industry or region now
        choice = {"industry": payload.industry, "region": payload.region}
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
    task = asyncio.create_task(_run(scan_id, url, folder, choice), name=f"scan-{scan_id}")
    _running.add(task)
    task.add_done_callback(_running.discard)
    return _progress_payload(scan)


async def _run(scan_id: str, url: str, folder: str | None = None, choice: dict | None = None) -> None:
    try:
        await execute_scan(scan_id, url, folder, choice)
    except Exception:
        log.exception("Scan %s crashed", scan_id)
        repo.update(scan_id, status="error")


def unmeasured(rows: list[dict]) -> list[str]:
    """The checks a re-check re-runs: every one that came back unscored."""
    return [r["parameter_id"] for r in rows if r["status"] == "UNKNOWN"]


def _carried(report: dict) -> dict:
    """What a finished audit established about the site, kept when its report is rebuilt."""
    out = {"snapshot": report.get("snapshot") or {}}
    # Recalculating a saved report is not a new report: it keeps the time it was generated.
    for key in ("platforms", "ai_usage", "industry", "identity", "cross_checks", "content_analysis",
                "generated_at", "audited_at"):
        if key in report:
            out[key] = report[key]
    return out


async def execute_recheck(scan_id: str, ids: list[str]) -> None:
    """Re-run only the given checks of a finished audit, against the pages it saved.

    No re-crawl: the pages come back from disk (crawler/restore.py) and only robots.txt, the
    sitemap and llms.txt are fetched again. The other checks keep their results; the report,
    the cross-checks and the top issues are then rebuilt over all of them.
    """
    state = _rechecks[scan_id]
    try:
        scan = repo.get(scan_id)
        old = repo.report(scan_id) or {}
        state["message"] = "Reading the saved website data"
        ctx = await restore.restore(scan_id, scan["domain"], old)
        meter = usage.start(LLM_SCAN_BUDGET_USD)
        specs = [spec for spec in registry if spec["parameter_id"] in ids]
        state["message"] = f"Re-checking {len(specs)} parameter(s)"

        async def on_each(row: dict) -> None:
            repo.save_parameter(scan_id, row)
            state["done"] += 1

        await run_all_parameters(ctx, specs, on_each)
        state["message"] = "Rebuilding the report"
        results = repo.parameters(scan_id)
        # Notes from the first pass are cleared, so none survives about a result that changed.
        noted = set()
        for row in results:
            if "cross_check" in (row.get("evidence") or {}):
                noted.add(row["parameter_id"])
                row["evidence"] = {k: v for k, v in row["evidence"].items() if k != "cross_check"}
        findings = crosschecks.reconcile(results, ctx)
        for row in results:
            if row["parameter_id"] in noted or "cross_check" in (row.get("evidence") or {}):
                repo.save_parameter(scan_id, row)
        issues = prioritize(results)
        repo.save_issues(scan_id, issues)
        report = build_report(repo.get(scan_id), results, issues)
        report.update(_carried(old))
        # A re-check produces a new version of the report: it is stamped now, and keeps the time
        # the website itself was read, which the re-check did not repeat.
        report["audited_at"] = old.get("audited_at") or old.get("generated_at")
        report["generated_at"] = datetime.now(timezone.utc).isoformat()
        report["cross_checks"] = findings["cross_checks"]
        # The re-check's AI spend is kept apart from the audit's, which estimates the next audit.
        report["rechecked"] = {"at": datetime.now(timezone.utc).isoformat(), "parameters": ids, "ai_usage": meter.as_dict()}
        repo.save_report(scan_id, report)
        repo.update(scan_id, errors_count=sum(1 for r in results if r.get("error")))
        try:
            excel_path = await asyncio.to_thread(save_excel_output, report, registry)
            repo.update(scan_id, excel_output_path=str(excel_path))
        except Exception:
            log.exception("Re-check of %s finished but its workbook could not be written", scan_id)
        still = [pid for pid in unmeasured(results) if pid in ids]
        state.update(state="done", message=(f"{len(ids) - len(still)} of {len(ids)} now measured"
                                            + (f"; still unmeasured: {', '.join(still)}" if still else "")),
                     still_unmeasured=still)
        log.info("Re-check of %s done: %s", scan_id, state["message"])
    except restore.NotRestorable as exc:
        state.update(state="error", message=str(exc))
    except Exception as exc:
        log.exception("Re-check of %s failed", scan_id)
        state.update(state="error", message=f"The re-check failed: {exc}")


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
    # Rows saved as N/A before it was retired are scored as what they are now: a no-data FAIL.
    for row in results:
        if upgrade_na_row(row):
            repo.save_parameter(scan_id, row)
    issues = prioritize(results)
    fresh = build_report(scan, results, issues)
    fresh.update(_carried(report))
    if "rechecked" in report:
        fresh["rechecked"] = report["rechecked"]
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


@router.post("/scans/{scan_id}/recheck")
async def start_recheck(scan_id: str):
    """Re-run only this audit's unmeasured checks, against the pages it already read."""
    scan = repo.get(scan_id)
    if not scan:
        raise HTTPException(404, "Scan not found")
    if scan["status"] != "completed":
        raise HTTPException(409, "Only a finished audit can be re-checked")
    if (_rechecks.get(scan_id) or {}).get("state") == "running":
        return _rechecks[scan_id]
    ids = unmeasured(repo.parameters(scan_id))
    if not ids:
        raise HTTPException(409, "Every check in this audit was measured; there is nothing to re-check")
    _rechecks[scan_id] = {"state": "running", "total": len(ids), "done": 0, "parameters": ids,
                          "message": "Starting"}
    task = asyncio.create_task(execute_recheck(scan_id, ids), name=f"recheck-{scan_id}")
    _running.add(task)
    task.add_done_callback(_running.discard)
    return _rechecks[scan_id]


@router.get("/scans/{scan_id}/recheck")
async def recheck_status(scan_id: str):
    state = _rechecks.get(scan_id)
    if state is None:
        raise HTTPException(404, "No re-check has run for this audit since the server started")
    return state


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

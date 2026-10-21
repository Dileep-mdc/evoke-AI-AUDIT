from __future__ import annotations

import json
from datetime import datetime, timezone
from ..db import get_db
from ..parameters.common import parameter_sort_key
from ..parameters.renumbering import RENAMED


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _renamed(text: str) -> str:
    """Saved text with every since-renamed parameter name read as its new name. The names are
    distinctive phrases, so they are replaced wherever a saved scan quoted them -- the row, its
    evidence summaries, and the stored report and issue list built from them."""
    for old, new in RENAMED.items():
        if old in text:
            text = text.replace(old, new)
    return text


def _loaded(row) -> dict:
    item = dict(row)
    # A row keeps the name it was scanned under; a parameter renamed since reads as its new name.
    item["name"] = RENAMED.get(item["name"], item["name"])
    try:
        item["evidence"] = json.loads(_renamed(item["evidence"] or "{}"))
    except Exception:
        item["evidence"] = {"raw": item["evidence"]}
    return item


class ScanRepository:
    def create(
        self,
        scan_id: str,
        domain: str,
        input_url: str,
        version: str,
        technical_total: int,
        onpage_total: int,
        offpage_total: int,
    ) -> dict:
        started = _now()
        with get_db() as conn:
            conn.execute(
                """INSERT INTO scans (id, domain, input_url, status, started_at, crawler_version,
                   technical_total, onpage_total, offpage_total)
                   VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
                (scan_id, domain, input_url, started, version, technical_total, onpage_total, offpage_total),
            )
        return self.get(scan_id)

    @staticmethod
    def _summary_columns(conn) -> str:
        """Every column of a scan except the saved report itself. The report is 6-14 MB of JSON; reading
        it for every row of the Reports list, and on every progress poll of a finished audit, took the
        list 15-22 seconds and froze the server while it did."""
        cols = [r[1] for r in conn.execute("PRAGMA table_info(scans)") if r[1] != "report_json"]
        return ", ".join(cols)

    def get(self, scan_id: str) -> dict | None:
        with get_db() as conn:
            row = conn.execute(f"SELECT {self._summary_columns(conn)} FROM scans WHERE id=?", (scan_id,)).fetchone()
            return dict(row) if row else None

    def list_scans(self, limit: int = 20) -> list[dict]:
        with get_db() as conn:
            rows = conn.execute(f"SELECT {self._summary_columns(conn)} FROM scans ORDER BY started_at DESC LIMIT ?",
                                (limit,)).fetchall()
            return [dict(r) for r in rows]

    def ai_usage_history(self, limit: int = 10) -> list[dict]:
        """The AI-usage record of recent completed scans, read inside SQLite so no report is parsed."""
        with get_db() as conn:
            rows = conn.execute(
                """SELECT json_extract(report_json, '$.ai_usage') FROM scans
                   WHERE status='completed' AND json_extract(report_json, '$.ai_usage') IS NOT NULL
                   ORDER BY started_at DESC LIMIT ?""", (limit,)).fetchall()
            return [json.loads(r[0]) for r in rows if r[0]]

    def update(self, scan_id: str, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with get_db() as conn:
            conn.execute(f"UPDATE scans SET {sets} WHERE id=?", [*fields.values(), scan_id])

    def save_pages(self, scan_id: str, pages) -> None:
        with get_db() as conn:
            for p in pages:
                conn.execute(
                    """INSERT INTO pages (scan_id, url, final_url, status_code, content_type, response_time_ms,
                       html_hash, canonical_url, word_count, title)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        scan_id, p.url, p.result.final_url, p.result.status_code, p.result.content_type,
                        p.result.elapsed_ms, p.result.html_hash, p.canonical, p.word_count, p.title,
                    ),
                )

    def save_parameter(self, scan_id: str, row: dict) -> None:
        with get_db() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO parameter_results
                   (scan_id, parameter_id, section, name, status, score, max_score, weight, confidence,
                    checked_url_or_source, evidence, recommendation, error, duration_ms, evaluated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    scan_id, row["parameter_id"], row["section"], row["name"], row["status"],
                    row.get("score"), row.get("max_score", 100), row.get("weight", 1),
                    row.get("confidence"), row.get("checked_url_or_source"),
                    json.dumps(row.get("evidence") or {}, default=str),
                    row.get("recommendation"), row.get("error"), row.get("duration_ms"),
                    row.get("evaluated_at"),
                ),
            )

    def parameters(self, scan_id: str) -> list[dict]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM parameter_results WHERE scan_id=?", (scan_id,)
            ).fetchall()
        out = [_loaded(r) for r in rows]
        # This is the order the report and the UI table are rendered in, so it sorts here
        # rather than in SQL: SQLite's text collation would put a sub-numbered id such as ON-5.1
        # after ON-20 instead of after ON-05.
        out.sort(key=lambda item: parameter_sort_key(item["parameter_id"]))
        return out

    def parameter(self, scan_id: str, parameter_id: str) -> dict | None:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM parameter_results WHERE scan_id=? AND parameter_id=?",
                (scan_id, parameter_id),
            ).fetchone()
        if not row:
            return None
        return _loaded(row)

    def save_issues(self, scan_id: str, issues: list[dict]) -> None:
        with get_db() as conn:
            conn.execute("DELETE FROM issues WHERE scan_id=?", (scan_id,))
            for iss in issues:
                conn.execute(
                    """INSERT INTO issues (scan_id, issue_id, parameter_id, severity, category, title, score_impact, effort, recommendation)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        scan_id, iss["issue_id"], iss.get("parameter_id"), iss.get("severity"),
                        iss.get("category"), iss.get("title"), iss.get("score_impact"),
                        iss.get("effort"), iss.get("recommendation"),
                    ),
                )

    def save_report(self, scan_id: str, report: dict) -> None:
        with get_db() as conn:
            conn.execute(
                """UPDATE scans SET report_json=?, overall_score=?, technical_score=?, onpage_score=?,
                   offpage_score=?, coverage_known=?, coverage_total=? WHERE id=?""",
                (
                    json.dumps(report, default=str),
                    report.get("overall_score"),
                    (report.get("category_scores") or {}).get("technical"),
                    (report.get("category_scores") or {}).get("on_page"),
                    (report.get("category_scores") or {}).get("off_page"),
                    (report.get("coverage") or {}).get("known"),
                    (report.get("coverage") or {}).get("scorable_parameters", 60),
                    scan_id,
                ),
            )

    # Columns stored BEFORE report_json in the scans row. SQLite reads a row front to back, so any
    # column after the 6-14 MB report (crawl_output_path, the crawl_* timings) costs a walk through
    # it: the full summary row of 9 finished audits took 465 ms. These cost microseconds.
    _LEAN = ("id, domain, input_url, status, progress_percent, technical_completed, technical_total, "
             "onpage_completed, onpage_total, offpage_completed, offpage_total, errors_count, started_at, "
             "completed_at, overall_score, onpage_score")

    def recent_lean(self, limit: int) -> list[dict]:
        """The newest audits, newest first, without touching any column stored after the report."""
        with get_db() as conn:
            rows = conn.execute(f"SELECT {self._LEAN} FROM scans ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def reuse_facts(self, scan_id: str) -> dict | None:
        """The saved report's facts that decide whether this audit can be shown again: whether it read a
        saved folder, and the industry and region it was scored for. None until the report is saved.

        These never change once an audit's report exists (a re-check keeps them), so they are parsed out
        of the report once -- one multi-path json_extract, inside SQLite -- and served from scan_reuse after."""
        with get_db() as conn:
            row = conn.execute("SELECT snapshot_folder, industry, industry_detected, region FROM scan_reuse WHERE scan_id=?",
                               (scan_id,)).fetchone()
            if row is None:
                found = conn.execute(
                    """SELECT json_extract(report_json, '$.snapshot.folder', '$.industry.key',
                              '$.industry.detected', '$.platforms.region')
                       FROM scans WHERE id=? AND report_json IS NOT NULL""", (scan_id,)).fetchone()
                if not found or not found[0]:
                    return None
                folder, industry_key, detected, region = json.loads(found[0])
                row = (folder, industry_key, 1 if detected else 0, region)
                conn.execute("INSERT OR REPLACE INTO scan_reuse VALUES (?, ?, ?, ?, ?)", (scan_id, *row))
        folder, industry_key, detected, region = row
        return {"snapshot_folder": folder, "industry": industry_key, "industry_detected": bool(detected), "region": region}

    def report(self, scan_id: str) -> dict | None:
        with get_db() as conn:
            row = conn.execute("SELECT report_json FROM scans WHERE id=?", (scan_id,)).fetchone()
        if row and row[0]:
            return json.loads(_renamed(row[0]))
        return None

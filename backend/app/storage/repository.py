from __future__ import annotations

import json
from datetime import datetime, timezone

from ..db import get_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def get(self, scan_id: str) -> dict | None:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
            return dict(row) if row else None

    def list_scans(self, limit: int = 20) -> list[dict]:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM scans ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

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
                "SELECT * FROM parameter_results WHERE scan_id=? ORDER BY parameter_id", (scan_id,)
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["evidence"] = json.loads(item["evidence"] or "{}")
            except Exception:
                item["evidence"] = {"raw": item["evidence"]}
            out.append(item)
        return out

    def parameter(self, scan_id: str, parameter_id: str) -> dict | None:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM parameter_results WHERE scan_id=? AND parameter_id=?",
                (scan_id, parameter_id),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["evidence"] = json.loads(item["evidence"] or "{}")
        except Exception:
            item["evidence"] = {"raw": item["evidence"]}
        return item

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
                    (report.get("coverage") or {}).get("scorable_parameters", 62),
                    scan_id,
                ),
            )

    def report(self, scan_id: str) -> dict | None:
        scan = self.get(scan_id)
        if not scan:
            return None
        if scan.get("report_json"):
            return json.loads(scan["report_json"])
        return None

"""Re-checking only an audit's unmeasured checks, and sampling under a low AI rate limit."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

from app.api import scans
from app.llm import batch, client
from app.llm.client import LLMResult


def _run(coro):
    return asyncio.run(coro)


def _fake_judge(seen: list):
    async def judge(prompt, *, system="", lane="classify", **kw):
        seen.append(prompt)
        return LLMResult(ok=True, parsed={"results": [{"ok": True}]})
    return judge


def test_a_low_rate_limit_judges_an_evenly_spread_sample(monkeypatch):
    seen: list = []
    monkeypatch.setattr(batch, "judge", _fake_judge(seen))
    monkeypatch.setattr(client, "_rpm_limit", 10)
    items = list(range(2313))
    picked: list = []

    def prompt(chunk):
        picked.extend(chunk)
        return "p"

    _, cov = _run(batch.judge_all(items, prompt, batch_size=40, full_coverage=True, max_items=0))
    assert cov.batches == 5 == len(seen)                 # rpm // 2 batches, not 58
    assert cov.submitted == len(picked) == 200
    assert picked[0] == 0 and picked[-1] > 2200          # spread over the whole site, not its first pages
    ev = cov.as_evidence()
    assert ev["capped_at"] == 200 and "200 of 2313" in ev["sampled"] and "10 requests per minute" in ev["sampled"]


def test_an_adequate_rate_limit_judges_every_item(monkeypatch):
    seen: list = []
    monkeypatch.setattr(batch, "judge", _fake_judge(seen))
    for limit in (None, 500):
        seen.clear()
        monkeypatch.setattr(client, "_rpm_limit", limit)
        _, cov = _run(batch.judge_all(list(range(400)), lambda c: "p", batch_size=40, full_coverage=True, max_items=0))
        assert cov.batches == 10 == len(seen) and cov.submitted == 400 and "sampled" not in cov.as_evidence()


def test_a_small_population_is_never_sampled(monkeypatch):
    monkeypatch.setattr(batch, "judge", _fake_judge([]))
    monkeypatch.setattr(client, "_rpm_limit", 10)
    _, cov = _run(batch.judge_all(list(range(120)), lambda c: "p", batch_size=40, full_coverage=True, max_items=0))
    assert cov.submitted == 120 and not cov.capped


def _row(pid, section, status="PASS", score=80.0, **evidence):
    return {"parameter_id": pid, "section": section, "name": pid, "status": status, "weight": 1,
            "score": None if status == "UNKNOWN" else score, "evidence": evidence, "error": None}


def test_only_unscored_checks_are_rechecked():
    rows = [_row("TECH-01", "technical"), _row("ON-03", "on_page", "UNKNOWN"),
            _row("ON-06", "on_page", "FAIL", 0.0, no_data=True), _row("OFF-02", "off_page", "FAIL", 0.0)]
    assert scans.unmeasured(rows) == ["ON-03"]


class _Repo:
    def __init__(self, rows, report):
        self.rows = {r["parameter_id"]: r for r in rows}
        self.saved_report = report
        self.saved: list[str] = []

    def get(self, scan_id):
        return {"id": scan_id, "domain": "example.org", "input_url": "https://example.org/", "status": "completed",
                "started_at": None, "completed_at": None, "crawler_version": "x"}

    def report(self, scan_id):
        return copy.deepcopy(self.saved_report)

    def parameters(self, scan_id):
        return [copy.deepcopy(r) for r in self.rows.values()]

    def save_parameter(self, scan_id, row):
        self.rows[row["parameter_id"]] = copy.deepcopy(row)
        self.saved.append(row["parameter_id"])

    def save_issues(self, scan_id, issues):
        pass

    def save_report(self, scan_id, report):
        self.saved_report = report

    def update(self, scan_id, **fields):
        pass


def test_a_recheck_reruns_only_the_unmeasured_checks_and_rebuilds_the_report(monkeypatch):
    rows = [_row("TECH-01", "technical", score=60.0), _row("ON-03", "on_page", "UNKNOWN"),
            _row("ON-06", "on_page", "FAIL", 0.0, no_data=True), _row("ON-07", "on_page", score=40.0),
            _row("OFF-02", "off_page", score=50.0)]
    rows[1]["error"] = "Timed out after 900s"
    old = {"identity": {"name": "Example Org"}, "industry": {"key": "general"}, "snapshot": {}, "ai_usage": {"calls": 9},
           "generated_at": "2026-01-01T10:00:00+00:00"}
    fake = _Repo(rows, old)
    monkeypatch.setattr(scans, "repo", fake)

    async def restored(scan_id, domain, report):
        return SimpleNamespace(pages=[], identity=report["identity"])
    monkeypatch.setattr(scans.restore, "restore", restored)
    ran: list = []

    async def run_all(ctx, specs, on_each):
        out = []
        for spec in specs:
            ran.append(spec["parameter_id"])
            row = _row(spec["parameter_id"], spec["section"], score=70.0)
            await on_each(row)
            out.append(row)
        return out
    monkeypatch.setattr(scans, "run_all_parameters", run_all)
    monkeypatch.setattr(scans, "save_excel_output", lambda report, registry: "x.xlsx")

    scans._rechecks["s1"] = {"state": "running", "total": 1, "done": 0, "parameters": ["ON-03"], "message": ""}
    _run(scans.execute_recheck("s1", ["ON-03"]))

    state = scans._rechecks.pop("s1")
    assert state["state"] == "done" and state["done"] == 1 and state["still_unmeasured"] == []
    assert ran == ["ON-03"]
    assert fake.rows["ON-03"]["status"] == "PASS" and fake.rows["ON-03"]["score"] == 70.0
    assert fake.rows["TECH-01"]["score"] == 60.0 and fake.rows["ON-06"]["evidence"]["no_data"]
    report = fake.saved_report
    assert report["identity"] == {"name": "Example Org"} and report["ai_usage"] == {"calls": 9}
    assert report["rechecked"]["parameters"] == ["ON-03"]
    # A re-check is a new version of the report: stamped now, keeping when the website was read.
    assert report["generated_at"] > "2026-01-01T10:00:00+00:00" and report["audited_at"] == "2026-01-01T10:00:00+00:00"
    assert any(p["parameter_id"] == "ON-03" and p["score"] == 70.0 for p in report["parameters"])


def test_a_replaced_crawl_is_reported_not_crashed(monkeypatch):
    fake = _Repo([_row("ON-03", "on_page", "UNKNOWN")], {})
    monkeypatch.setattr(scans, "repo", fake)

    async def gone(scan_id, domain, report):
        raise scans.restore.NotRestorable("a newer audit of this site has replaced its saved website data")
    monkeypatch.setattr(scans.restore, "restore", gone)
    scans._rechecks["s2"] = {"state": "running", "total": 1, "done": 0, "parameters": ["ON-03"], "message": ""}
    _run(scans.execute_recheck("s2", ["ON-03"]))
    state = scans._rechecks.pop("s2")
    assert state["state"] == "error" and "newer audit" in state["message"]
    assert fake.rows["ON-03"]["status"] == "UNKNOWN"

"""A site audited before is not read again: the request gets its most recent audit (api/scans.py).

Pinned here: the site is matched however it is typed; a running audit is joined; the newest
finished audit is returned only when it actually read the site, audited the live site and used
the same industry and region; and fresh=True always starts a new audit.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import db
from app.api import scans


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway database. init_db() also moves legacy data dirs, so those point at tmp too."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scans.db")
    monkeypatch.setattr(db, "LEGACY_DATA_DIR", tmp_path / "legacy")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "live")
    db.init_db()
    return scans.repo


_n = 0


def _audit(repo, domain, *, status="completed", onpage=72.5, industry="it_services", detected=True,
           region=None, folder=None, started=None):
    global _n
    _n += 1
    scan_id = f"scan-{_n}"
    repo.create(scan_id, domain, f"https://{domain}/", "test", 22, 20, 9)
    report = {"snapshot": {"folder": folder} if folder else {}, "industry": {"key": industry, "detected": detected},
              "platforms": {"region": region}}
    fields = {"status": status, "onpage_score": onpage, "started_at": started or f"2026-10-{_n:02d}T00:00:00+00:00"}
    if status == "completed":
        fields["report_json"] = json.dumps(report)
    repo.update(scan_id, **fields)
    return scan_id


NO_CHOICE = {"industry": None, "region": None}


@pytest.mark.parametrize("typed", ["https://www.acme.com/", "acme.com", "WWW.ACME.COM/about", "http://acme.com"])
def test_the_same_site_is_matched_however_it_is_typed(repo, typed):
    sid = _audit(repo, "www.acme.com")
    assert scans._existing_scan(scans.normalize_url(typed), NO_CHOICE)["id"] == sid


def test_the_most_recent_usable_audit_is_returned(repo):
    _audit(repo, "www.acme.com")
    newest = _audit(repo, "www.acme.com")
    _audit(repo, "www.other.com")
    assert scans._existing_scan("https://www.acme.com/", NO_CHOICE)["id"] == newest


def test_an_audit_that_could_not_read_the_site_is_not_reused(repo):
    """tcs.com answered 403: no page read, no on-page score. Asking again must try again."""
    _audit(repo, "www.tcs.com", onpage=None)
    assert scans._existing_scan("https://www.tcs.com/", NO_CHOICE) is None


def test_an_older_audit_that_read_the_site_beats_a_newer_one_that_did_not(repo):
    good = _audit(repo, "www.acme.com")
    _audit(repo, "www.acme.com", onpage=None)
    assert scans._existing_scan("https://www.acme.com/", NO_CHOICE)["id"] == good


def test_failed_and_saved_folder_audits_are_not_reused(repo):
    _audit(repo, "www.acme.com", status="error")
    _audit(repo, "www.acme.com", folder=r"C:\sites\www.acme.com")
    assert scans._existing_scan("https://www.acme.com/", NO_CHOICE) is None


def test_a_running_audit_is_joined_instead_of_starting_another(repo):
    _audit(repo, "www.acme.com")
    running = _audit(repo, "www.acme.com", status="crawling")
    assert scans._existing_scan("https://www.acme.com/", NO_CHOICE)["id"] == running


def test_a_different_industry_or_region_runs_a_new_audit(repo):
    detected = _audit(repo, "www.acme.com", industry="it_services", detected=True)
    assert scans._existing_scan("https://www.acme.com/", {"industry": "healthcare", "region": None}) is None
    assert scans._existing_scan("https://www.acme.com/", {"industry": None, "region": "india"}) is None
    assert scans._existing_scan("https://www.acme.com/", {"industry": "it_services", "region": None})["id"] == detected


def test_an_audit_scored_for_a_chosen_industry_is_not_reused_for_auto_detect(repo):
    _audit(repo, "www.acme.com", industry="healthcare", detected=False)
    assert scans._existing_scan("https://www.acme.com/", NO_CHOICE) is None


def test_create_scan_returns_the_existing_audit_and_fresh_starts_a_new_one(repo, monkeypatch):
    sid = _audit(repo, "www.acme.com")
    started = []

    async def fake_run(scan_id, url, folder=None, choice=None):
        started.append(scan_id)

    monkeypatch.setattr(scans, "_run", fake_run)

    async def go(fresh):
        out = await scans.create_scan(scans.ScanRequest(url="acme.com", fresh=fresh))
        await asyncio.sleep(0)   # let a started task run
        return out

    out = asyncio.run(go(False))
    assert out["scan_id"] == sid and out["reused"] is True and out["status"] == "completed"
    assert not started

    out = asyncio.run(go(True))
    assert out["scan_id"] != sid and "reused" not in out
    assert started == [out["scan_id"]]

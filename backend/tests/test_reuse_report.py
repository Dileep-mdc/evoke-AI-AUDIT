"""Every request for a site reads it afresh (api/scans.py).

Pinned here: a finished audit is never shown in place of a new one, whatever its outcome; the
only request answered with an existing audit is one for a site whose audit is still running,
which is joined rather than started twice -- matched however the site is typed, and not when
an explicit industry or region was asked for.
"""
from __future__ import annotations

import asyncio

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


def _audit(repo, domain, *, status="completed"):
    global _n
    _n += 1
    scan_id = f"scan-{_n}"
    repo.create(scan_id, domain, f"https://{domain}/", "test", 22, 20, 9)
    fields = {"status": status, "started_at": f"2026-10-{_n:02d}T00:00:00+00:00"}
    if status == "completed":
        fields["report_json"] = "{}"
    repo.update(scan_id, **fields)
    return scan_id


NO_CHOICE = {"industry": None, "region": None}


def test_a_finished_audit_is_not_reused(repo):
    _audit(repo, "www.acme.com")
    _audit(repo, "www.acme.com", status="error")
    assert scans._running_scan("https://www.acme.com/", NO_CHOICE) is None


@pytest.mark.parametrize("typed", ["https://www.acme.com/", "acme.com", "WWW.ACME.COM/about", "http://acme.com"])
def test_a_running_audit_is_joined_however_the_site_is_typed(repo, typed):
    _audit(repo, "www.acme.com")
    running = _audit(repo, "www.acme.com", status="crawling")
    assert scans._running_scan(scans.normalize_url(typed), NO_CHOICE)["id"] == running


def test_a_running_audit_of_another_site_is_not_joined(repo):
    _audit(repo, "www.other.com", status="evaluating")
    assert scans._running_scan("https://www.acme.com/", NO_CHOICE) is None


def test_an_explicit_industry_or_region_starts_its_own_audit(repo):
    _audit(repo, "www.acme.com", status="crawling")
    assert scans._running_scan("https://www.acme.com/", {"industry": "healthcare", "region": None}) is None
    assert scans._running_scan("https://www.acme.com/", {"industry": None, "region": "india"}) is None


def test_create_scan_starts_a_new_audit_of_a_site_audited_before(repo, monkeypatch):
    finished = _audit(repo, "www.acme.com")
    started = []

    async def fake_run(scan_id, url, folder=None, choice=None):
        started.append(scan_id)

    monkeypatch.setattr(scans, "_run", fake_run)

    async def go():
        out = await scans.create_scan(scans.ScanRequest(url="acme.com"))
        await asyncio.sleep(0)   # let a started task run
        return out

    out = asyncio.run(go())
    assert out["scan_id"] != finished
    assert "reused" not in out
    assert started == [out["scan_id"]]

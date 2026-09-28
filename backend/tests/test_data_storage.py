"""Where scan data lives, and what reclaims it.

Three defects, all found by measuring a real install rather than by reading the code:

  * the data directory sat inside a OneDrive-synced folder, holding a WAL-mode SQLite
    database. WAL keeps scans.db, -wal and -shm mutually consistent; a sync client treats
    them as three unrelated files and will upload, lock or dehydrate them independently,
    which is a documented route to silent corruption.
  * 32% of that database (1,899 of 5,981 pages) was freelist -- space deleted rows had
    released and nothing ever handed back.
  * a scan that crashed during evaluation still left 1,064 rows in `pages`, because pages
    are written immediately after the crawl. Nothing can read them and nothing removed them.
"""
from __future__ import annotations

import sqlite3

import pytest

from app import config, db


# --- choosing the directory ---------------------------------------------------------------

@pytest.mark.parametrize("path, synced", [
    (r"C:\Users\x\OneDrive - Evoke Technologies Private Limited\AI Audit\backend\data", True),
    (r"C:\Users\x\OneDrive\proj\data", True),
    ("/home/x/Dropbox/proj/data", True),
    ("/home/x/Google Drive/proj/data", True),
    (r"C:\Users\x\AppData\Local\ai-audit\data", False),
    ("/srv/ai-audit/data", False),
    # A project that merely mentions the word is not synced -- this must not over-match.
    ("/home/x/code/onedrive-exporter/data", False),
])
def test_cloud_synced_detects_only_real_sync_roots(path, synced):
    from pathlib import PureWindowsPath, PurePosixPath
    parsed = PureWindowsPath(path) if "\\" in path else PurePosixPath(path)
    assert config._cloud_synced(parsed) is synced


def test_an_explicit_override_always_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path / "chosen"))
    assert config._default_data_dir() == (tmp_path / "chosen").resolve()


# --- migrating off the synced folder --------------------------------------------------------

def _stage(tmp_path, monkeypatch):
    legacy, new = tmp_path / "legacy", tmp_path / "new"
    legacy.mkdir(); new.mkdir()
    monkeypatch.setattr(db, "LEGACY_DATA_DIR", legacy)
    monkeypatch.setattr(db, "DATA_DIR", new)
    return legacy, new


def test_existing_data_is_moved_rather_than_abandoned(tmp_path, monkeypatch):
    """An upgrade that silently orphans the scan history would look exactly like data loss."""
    legacy, new = _stage(tmp_path, monkeypatch)
    (legacy / "scans.db").write_bytes(b"the real database")
    (legacy / "scan_output").mkdir()
    (legacy / "scan_output" / "acme-scraped-content.json").write_text("{}")

    db.migrate_legacy_data_dir()

    assert (new / "scans.db").read_bytes() == b"the real database"
    assert (new / "scan_output" / "acme-scraped-content.json").exists()
    assert not (legacy / "scans.db").exists()


def test_live_wal_sidecars_are_not_carried_across(tmp_path, monkeypatch):
    """Copying a half-synced -wal onto a database is how the destination gets corrupted."""
    legacy, new = _stage(tmp_path, monkeypatch)
    (legacy / "scans.db").write_bytes(b"db")
    (legacy / "scans.db-wal").write_bytes(b"stale wal")
    (legacy / "scans.db-shm").write_bytes(b"stale shm")

    db.migrate_legacy_data_dir()

    assert (new / "scans.db").exists()
    assert not (new / "scans.db-wal").exists()
    assert not (new / "scans.db-shm").exists()


def test_migration_never_overwrites_what_is_already_there(tmp_path, monkeypatch):
    """The new directory is the live one; a stale copy must not clobber a newer database."""
    legacy, new = _stage(tmp_path, monkeypatch)
    (legacy / "scans.db").write_bytes(b"old")
    (new / "scans.db").write_bytes(b"current")

    db.migrate_legacy_data_dir()

    assert (new / "scans.db").read_bytes() == b"current"


def test_migration_is_a_no_op_when_there_is_nothing_to_move(tmp_path, monkeypatch):
    legacy, new = _stage(tmp_path, monkeypatch)
    db.migrate_legacy_data_dir()
    assert list(new.iterdir()) == []


# --- reclaiming space ------------------------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """A throwaway database, with the startup migration neutralised.

    init_db() calls migrate_legacy_data_dir(), which moves real files on this machine. A test
    that left that pointing at the developer's own directories would quietly relocate their
    scan history as a side effect of running the suite -- which is exactly what happened once.
    """
    path = tmp_path / "scans.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "LEGACY_DATA_DIR", tmp_path / "legacy")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "live")
    db.init_db()
    return path


def test_a_failed_scan_discards_its_own_page_rows(temp_db):
    """1,064 unreachable rows per crashed scan, in a table nothing prunes."""
    with db.get_db() as conn:
        for scan in ("dead", "alive"):
            conn.execute(
                "INSERT INTO scans (id, domain, input_url, status, started_at) VALUES (?,?,?,?,?)",
                (scan, "acme.test", "https://acme.test", "error", "2026-09-21T00:00:00"),
            )
            for i in range(20):
                conn.execute("INSERT INTO pages (scan_id, url) VALUES (?,?)", (scan, f"https://acme.test/{i}"))

    assert db.discard_scan_pages("dead") == 20

    with db.get_db() as conn:
        remaining = conn.execute("SELECT scan_id, COUNT(*) FROM pages GROUP BY scan_id").fetchall()
    assert [tuple(r) for r in remaining] == [("alive", 20)], "only the failed scan's rows may go"


def test_vacuum_returns_freed_space_to_the_file(temp_db):
    """SQLite keeps freed pages on a freelist forever unless VACUUM is asked for."""
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO scans (id, domain, input_url, status, started_at) VALUES (?,?,?,?,?)",
            ("s", "acme.test", "https://acme.test", "completed", "2026-09-21T00:00:00"),
        )
        conn.executemany(
            "INSERT INTO pages (scan_id, url, title) VALUES (?,?,?)",
            [("s", f"https://acme.test/{i}", "x" * 2000) for i in range(2000)],
        )
    with db.get_db() as conn:
        conn.execute("DELETE FROM pages")
    before = sqlite3.connect(temp_db).execute("PRAGMA freelist_count").fetchone()[0]
    assert before > 0, "the deletion should have left reusable pages behind"

    db.vacuum()

    after = sqlite3.connect(temp_db).execute("PRAGMA freelist_count").fetchone()[0]
    assert after < before and after == 0


def test_migration_recurses_into_a_directory_that_already_exists(tmp_path, monkeypatch):
    """scan_output/ is created at import time, so the destination always exists already.

    Skipping it as "already migrated" would strand every saved crawl in the synced folder
    while reporting success -- silent data loss dressed as a clean upgrade.
    """
    legacy, new = _stage(tmp_path, monkeypatch)
    (legacy / "scan_output").mkdir()
    (legacy / "scan_output" / "acme-scraped-content.json").write_text("the crawl")
    (legacy / "scan_output" / "acme-raw").mkdir()
    (legacy / "scan_output" / "acme-raw" / "index.html.gz").write_bytes(b"doc")
    (new / "scan_output").mkdir()  # as scan_output.py does on import

    db.migrate_legacy_data_dir()

    assert (new / "scan_output" / "acme-scraped-content.json").read_text() == "the crawl"
    assert (new / "scan_output" / "acme-raw" / "index.html.gz").read_bytes() == b"doc"

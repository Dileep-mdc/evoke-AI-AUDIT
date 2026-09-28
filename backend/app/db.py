import logging
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import DATA_DIR, DB_PATH, LEGACY_DATA_DIR

log = logging.getLogger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    input_url TEXT NOT NULL,
    status TEXT NOT NULL,
    progress_percent REAL DEFAULT 0,
    -- Section totals are supplied by ScanRepository.create() from the live registry, so no
    -- default is declared here: a baked-in number silently goes stale when a parameter is
    -- added (these read 15/20/6 while the registry held 22/22/18).
    technical_completed INTEGER DEFAULT 0,
    technical_total INTEGER,
    onpage_completed INTEGER DEFAULT 0,
    onpage_total INTEGER,
    offpage_completed INTEGER DEFAULT 0,
    offpage_total INTEGER,
    errors_count INTEGER DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    crawler_version TEXT,
    overall_score REAL,
    technical_score REAL,
    onpage_score REAL,
    offpage_score REAL,
    coverage_known INTEGER,
    coverage_total INTEGER,
    report_json TEXT,
    crawl_output_path TEXT,
    excel_output_path TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    url TEXT NOT NULL,
    final_url TEXT,
    status_code INTEGER,
    content_type TEXT,
    response_time_ms INTEGER,
    html_hash TEXT,
    canonical_url TEXT,
    word_count INTEGER,
    title TEXT,
    FOREIGN KEY(scan_id) REFERENCES scans(id)
);

CREATE TABLE IF NOT EXISTS parameter_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    parameter_id TEXT NOT NULL,
    section TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    score REAL,
    max_score REAL DEFAULT 100,
    weight REAL DEFAULT 1,
    confidence REAL,
    checked_url_or_source TEXT,
    evidence TEXT,
    recommendation TEXT,
    error TEXT,
    duration_ms INTEGER,
    evaluated_at TEXT,
    UNIQUE(scan_id, parameter_id),
    FOREIGN KEY(scan_id) REFERENCES scans(id)
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    parameter_id TEXT,
    severity TEXT,
    category TEXT,
    title TEXT,
    score_impact REAL,
    effort TEXT,
    recommendation TEXT,
    FOREIGN KEY(scan_id) REFERENCES scans(id)
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def migrate_legacy_data_dir() -> None:
    """Move data written by an earlier build, back when DATA_DIR was always backend/data.

    Runs before the schema is touched, so the database that gets opened is the migrated one
    rather than a fresh empty file beside it -- which is what makes the move invisible: the
    scan history and the saved crawl output are simply still there after the upgrade.

    Deliberately conservative. Nothing is overwritten (a name already present in the new
    directory wins), and a failure is logged and swallowed rather than raised: a file that is
    locked -- which, in a synced folder, is the expected failure -- must not stop the server
    from starting. Whatever did not move is left where it is and retried next launch.
    """
    legacy = LEGACY_DATA_DIR
    if legacy.resolve() == DATA_DIR.resolve() or not legacy.is_dir():
        return
    moved = _move_contents(legacy, DATA_DIR)
    if moved:
        log.info("Moved %d file(s) from %s to %s", moved, legacy, DATA_DIR)


def _move_contents(src_dir: Path, dest_dir: Path) -> int:
    """Move everything under src_dir into dest_dir, recursing into directories that already
    exist on the other side. Returns how many files were moved.

    Recursing matters, and skipping a directory that already exists would be a silent
    data-loss bug rather than a cosmetic one: scan_output/ is created at import time by
    scan_output.py, so by the time this runs the destination directory reliably exists and is
    empty. Treating "it exists" as "already migrated" would strand every saved crawl in the
    synced folder forever, while reporting success.
    """
    moved = 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in src_dir.iterdir():
        # -wal and -shm are live SQLite sidecars; they belong to whichever process last had
        # the database open and are rebuilt on demand. Carrying a half-synced pair across is
        # how the destination gets corrupted, so they are skipped and left to expire.
        if src.name.endswith(("-wal", "-shm", "-journal")):
            continue
        dest = dest_dir / src.name
        if src.is_dir():
            moved += _move_contents(src, dest)
            continue
        if dest.exists():
            continue
        try:
            shutil.move(str(src), str(dest))
            moved += 1
        except OSError as exc:
            log.warning("Could not move %s out of the synced folder: %s", src.name, exc)
    return moved


def init_db() -> None:
    migrate_legacy_data_dir()
    with get_db() as conn:
        conn.executescript(SCHEMA)
    fail_interrupted_scans()


def vacuum() -> None:
    """Reclaim the space deleted rows leave behind.

    SQLite never returns freed pages to the filesystem on its own; it keeps them on a
    freelist and reuses them. On this database that reached 32% of the file, because every
    scan writes a multi-megabyte report_json and a row per crawled page, and nothing ever
    shrank it back. Called after a scan finishes, when the write load is over.

    VACUUM cannot run inside a transaction, so this opens its own connection with autocommit
    rather than going through get_db().
    """
    conn = connect()
    try:
        conn.isolation_level = None
        conn.execute("VACUUM")
    except sqlite3.Error as exc:
        log.warning("VACUUM skipped: %s", exc)
    finally:
        conn.close()


def discard_scan_pages(scan_id: str) -> int:
    """Drop the crawled-page rows belonging to one scan, and say how many went.

    Pages are saved immediately after the crawl, before any parameter is evaluated, so a scan
    that dies during evaluation still leaves a full page table behind -- 1,064 rows, for a
    run that produced no report at all. Those rows are unreachable (nothing renders a failed
    scan's pages) and pure weight, so a failed scan clears its own.
    """
    with get_db() as conn:
        return conn.execute("DELETE FROM pages WHERE scan_id=?", (scan_id,)).rowcount


def fail_interrupted_scans() -> None:
    """A process restart leaves crawls stuck at 2%. Mark those so the UI does not hang."""
    with get_db() as conn:
        conn.execute(
            """UPDATE scans SET status='error', errors_count=COALESCE(errors_count, 0) + 1
               WHERE status IN ('queued', 'crawling', 'evaluating')"""
        )

import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    input_url TEXT NOT NULL,
    status TEXT NOT NULL,
    progress_percent REAL DEFAULT 0,
    technical_completed INTEGER DEFAULT 0,
    technical_total INTEGER DEFAULT 22,
    onpage_completed INTEGER DEFAULT 0,
    onpage_total INTEGER DEFAULT 22,
    offpage_completed INTEGER DEFAULT 0,
    offpage_total INTEGER DEFAULT 18,
    errors_count INTEGER DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    crawler_version TEXT,
    overall_score REAL,
    technical_score REAL,
    onpage_score REAL,
    offpage_score REAL,
    coverage_known INTEGER,
    coverage_total INTEGER DEFAULT 62,
    report_json TEXT
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


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(SCHEMA)
    fail_interrupted_scans()


def fail_interrupted_scans() -> None:
    """A process restart leaves crawls stuck at 2%. Mark those so the UI does not hang."""
    with get_db() as conn:
        conn.execute(
            """UPDATE scans SET status='error', errors_count=COALESCE(errors_count, 0) + 1
               WHERE status IN ('queued', 'crawling', 'evaluating')"""
        )

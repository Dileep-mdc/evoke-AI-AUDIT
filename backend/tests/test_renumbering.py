"""The 1..N renumbering of saved scans, and the scan's own retry of unscored parameters."""
import sqlite3

from app.api.scans import _worth_retrying
from app.db import SCHEMA, _renumber_parameters
from app.parameters.engine import load_registry
from app.parameters.renumbering import OLD_TO_NEW


def _db(ids):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    for pid in ids:
        conn.execute("INSERT INTO parameter_results (scan_id, parameter_id, section, name, status) VALUES ('s', ?, 'x', ?, 'PASS')", (pid, pid))
        conn.execute("INSERT INTO issues (scan_id, issue_id, parameter_id) VALUES ('s', ?, ?)", (f"I-{pid}", pid))
    return conn


def _ids(conn, table="parameter_results"):
    return sorted(r[0] for r in conn.execute(f"SELECT parameter_id FROM {table}"))


def test_saved_scans_move_to_the_new_ids_and_drop_the_retired_one():
    conn = _db(["ON-5.1", "ON-10", "ON-11", "ON-22", "OFF-08", "OFF-09", "OFF-10", "TECH-01"])
    _renumber_parameters(conn)
    expected = sorted(["ON-06", "ON-09", "ON-10", "ON-20", "OFF-08", "OFF-09", "TECH-01"])
    assert _ids(conn) == expected and _ids(conn, "issues") == expected
    # the new OFF-08 is the old OFF-09 (best/top lists), not the retired YouTube check
    assert conn.execute("SELECT name FROM parameter_results WHERE parameter_id='OFF-08'").fetchone()[0] == "OFF-09"


def test_the_migration_runs_once():
    conn = _db(["ON-11"])
    _renumber_parameters(conn)
    _renumber_parameters(conn)  # a second startup must not shift ON-10 on to ON-09
    assert _ids(conn) == ["ON-10"]


def test_the_mapping_lands_exactly_on_the_registry():
    ids = {s["parameter_id"] for s in load_registry()}
    assert set(OLD_TO_NEW.values()) <= ids
    assert not set(OLD_TO_NEW) & ids - set(OLD_TO_NEW.values())


def test_only_fixable_unscored_checks_are_retried():
    assert _worth_retrying(None)
    assert _worth_retrying({"status": "UNKNOWN", "evidence": {}, "error": "model call timed out"})
    assert _worth_retrying({"status": "FAIL", "evidence": {}, "error": "rate limited"})
    assert not _worth_retrying({"status": "PASS", "evidence": {}})
    assert not _worth_retrying({"status": "UNKNOWN", "evidence": {"not_applicable": True}})
    assert not _worth_retrying({"status": "UNKNOWN", "evidence": {}, "error": "Web search is not configured: set ..."})


def test_a_scan_from_an_older_parameter_set_is_left_as_it_was():
    conn = _db([])
    rows = [("old", "OFF-08", "Review volume, recency and velocity versus competitors"),
            ("old", "OFF-09", "Category placement matching how the company positions itself"),
            ("old", "OFF-15", "an off-page check of the 18-check set"),
            ("new", "OFF-09", "Presence on best/top service lists in the category")]
    conn.executemany("INSERT INTO parameter_results (scan_id, parameter_id, section, name, status) VALUES (?, ?, 'off_page', ?, 'PASS')", rows)
    _renumber_parameters(conn)
    got = sorted(conn.execute("SELECT scan_id, parameter_id FROM parameter_results").fetchall())
    assert got == [("new", "OFF-08"), ("old", "OFF-08"), ("old", "OFF-09"), ("old", "OFF-15")]

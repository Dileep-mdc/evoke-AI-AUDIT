"""Parameter ids before the renumbering of 2026-09-30 -> the ids they have now.

Retiring checks (ON-06, ON-09, ON-16, OFF-08) and a sub-numbered one (ON-5.1) had left the
numbering with gaps, so each section was renumbered to run 1, 2, 3 ... with no gaps and no
sub-numbers. Only the ids changed; every parameter kept its logic, weight and scoring.

db.py applies this once to every saved scan, so an old report reads in the new numbering.
Ids not listed here did not change.
"""
from __future__ import annotations

OLD_TO_NEW: dict[str, str] = {
    "ON-5.1": "ON-06",
    "ON-10": "ON-09",
    "ON-11": "ON-10",
    "ON-12": "ON-11",
    "ON-13": "ON-12",
    "ON-14": "ON-13",
    "ON-15": "ON-14",
    "ON-17": "ON-15",
    "ON-18": "ON-16",
    "ON-19": "ON-17",
    "ON-20": "ON-18",
    "ON-21": "ON-19",
    "ON-22": "ON-20",
    "OFF-09": "OFF-08",
    "OFF-10": "OFF-09",
}

# Retired before the renumbering, so its old id is dropped from saved scans rather than mapped:
# the new OFF-08 is a different parameter (the old OFF-09).
RETIRED_OLD_IDS = frozenset({"OFF-08"})  # YouTube presence and channel followers

# Every id a scan from just before the renumbering can hold. A saved scan with any other id comes
# from an older parameter set (the 18 off-page checks of 2026-09-28, say), whose numbers meant
# different parameters, so it is left exactly as it was saved.
NEW_TO_OLD = {new: old for old, new in OLD_TO_NEW.items()}


def ids_before(current_ids) -> frozenset:
    return frozenset(NEW_TO_OLD.get(pid, pid) for pid in current_ids) | RETIRED_OLD_IDS


# Parameters renamed without a change of id: saved rows keep the name they were scanned under,
# so the old name is read as the new one.
RENAMED = {"Wikidata/Wikipedia entry present and accurate": "Wikidata/Wikipedia entry present"}

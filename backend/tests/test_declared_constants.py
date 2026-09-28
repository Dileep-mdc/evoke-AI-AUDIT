"""Nothing that decides a score, and nothing we talk to, is a literal buried in a handler.

Two different kinds of constant used to sit inline, and both cost something real:

  * Third-party endpoints (DuckDuckGo, Wikidata, Wikipedia) lived in offpage.py, so the full
    set of external services this tool contacts could only be found by grepping, and pointing
    at a mirror or a proxy meant editing a parameter handler.
  * Scoring bands and thresholds -- `word_count < 180`, `(0.035, 100)`, `near_duplicate_pairs
    (excerpts, 0.72)` -- lived beside whichever function used them, so the audit's methodology
    could not be reviewed without reading three modules, and could drift from the metric text
    in frozen_spec.json that claims to describe it.

Neither can be deleted: a scoring engine has to encode its scoring. Both can be declared once.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import config
from app.parameters import rules

PARAMS = Path(__file__).resolve().parents[1] / "app" / "parameters"
HANDLER_FILES = ("onpage.py", "technical.py", "offpage.py")


def _code_lines(name: str):
    """Source lines with comments and docstring prose stripped, so documentation that mentions
    a number is not mistaken for code that hardcodes one."""
    for num, line in enumerate((PARAMS / name).read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#")[0]
        if code.strip().startswith(('"', "'")):
            continue
        yield num, code


def test_no_third_party_endpoint_is_hardcoded_in_a_handler():
    offenders = []
    for name in HANDLER_FILES:
        for num, code in _code_lines(name):
            if re.search(r"[\"']https?://[a-z]", code):
                offenders.append(f"{name}:{num}: {code.strip()}")
    assert not offenders, "endpoints belong in config.py:\n  " + "\n  ".join(offenders)


def test_the_endpoints_are_configurable():
    """A deployment must be able to point at a mirror or a proxy without a fork."""
    for name in ("DDG_HTML_ENDPOINT", "WIKIDATA_API", "WIKIPEDIA_API"):
        assert getattr(config, name).startswith("http"), name


def test_no_score_band_table_is_defined_inline():
    """A band table is a tuple of (threshold, points) pairs. Finding one in a handler means
    the methodology moved back out of scoring_rules.json."""
    offenders = []
    for name in HANDLER_FILES:
        for num, code in _code_lines(name):
            if re.search(r"=\s*\(\s*\(\s*[\d.]+\s*,\s*[\d.]+\s*\)\s*,", code):
                offenders.append(f"{name}:{num}: {code.strip()}")
    assert not offenders, "band tables belong in scoring_rules.json:\n  " + "\n  ".join(offenders)


@pytest.mark.parametrize("name,expected", [
    ("thin_page_words", 180),
    ("near_duplicate_body_similarity", 0.72),
    ("competing_title_similarity", 0.7),
    ("min_content_words_for_density", 40),
    ("useful_list_min_items", 3),
])
def test_the_declared_thresholds_still_hold_their_audited_values(name, expected):
    """These are the numbers the reports were validated against. Changing one changes every
    client's score, so it should take a deliberate edit here as well as in the JSON."""
    assert rules.threshold(name) == expected


def test_every_band_table_has_a_floor_and_descends():
    """A malformed table would silently misscore rather than raise: band() walks best-first and
    returns the floor if nothing matches, so an out-of-order table just never matches."""
    for name, entry in rules.all_rules()["bands"].items():
        pairs = entry["bands"]
        assert entry.get("floor") is not None, f"{name} has no floor"
        assert pairs, f"{name} has no bands"
        thresholds = [p[0] for p in pairs]
        ascending = thresholds == sorted(thresholds)
        descending = thresholds == sorted(thresholds, reverse=True)
        assert ascending or descending, f"{name} thresholds are not monotonic: {thresholds}"


def test_every_rule_carries_its_reasoning():
    """The point of the file is that a reviewer can read the methodology. A bare number with no
    note is the thing we moved away from, just relocated."""
    undocumented = [
        f"{section}.{name}"
        for section in ("bands", "thresholds")
        for name, entry in rules.all_rules()[section].items()
        if not entry.get("_what")
    ]
    assert not undocumented, f"declared without an explanation: {undocumented}"


def test_the_rules_file_can_be_replaced_wholesale(tmp_path, monkeypatch):
    """SCORING_RULES_PATH is the escape hatch for a deployment that needs different thresholds
    without forking. If it does not actually work it is documentation, not a feature."""
    import importlib
    import json

    doc = json.loads(rules.RULES_PATH.read_text(encoding="utf-8"))
    doc["thresholds"]["thin_page_words"]["value"] = 999
    override = tmp_path / "custom_rules.json"
    override.write_text(json.dumps(doc), encoding="utf-8")

    monkeypatch.setenv("SCORING_RULES_PATH", str(override))
    reloaded = importlib.reload(rules)
    try:
        assert reloaded.threshold("thin_page_words") == 999
    finally:
        monkeypatch.delenv("SCORING_RULES_PATH", raising=False)
        importlib.reload(rules)

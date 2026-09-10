from app.config import WEIGHTS
from app.parameters.scoring import (
    category_score,
    coefficients,
    contributions,
    label_for_score,
    overall_score,
    prioritize,
    status_counts,
)


def test_unknown_excluded_from_category():
    rows = [
        {"section": "technical", "status": "PASS", "score": 100, "weight": 1},
        {"section": "technical", "status": "UNKNOWN", "score": None, "weight": 1.5},
        {"section": "technical", "status": "FAIL", "score": 0, "weight": 1},
    ]
    assert category_score(rows, "technical") == 50.0


def test_overall_renormalizes_missing_category():
    assert overall_score(100, 50, None) == round((100 * 0.35 + 50 * 0.40) / 0.75, 1)


def test_status_counts_and_labels():
    rows = [
        {"status": "PASS"},
        {"status": "PARTIAL"},
        {"status": "FAIL"},
        {"status": "UNKNOWN"},
    ]
    assert status_counts(rows) == {"pass": 1, "partial": 1, "fail": 1, "unknown": 1}
    assert label_for_score(92) == "Excellent"
    assert label_for_score(80) == "Good"
    assert label_for_score(65) == "Fair"
    assert label_for_score(41.2) == "Poor"
    assert label_for_score(28) == "Critical"


# Mirrors the shipped registry: 22 technical, 22 on-page, 18 off-page, every weight 1.0.
SECTION_SIZES = {"technical": 22, "on_page": 22, "off_page": 18}


def _rows(scores: dict[str, float] | None = None, unknown: set[str] | None = None) -> list[dict]:
    """A full 62-parameter result set. Anything in `unknown` comes back UNKNOWN/None,
    anything else scores `scores[id]` (default 50)."""
    scores = scores or {}
    unknown = unknown or set()
    prefix = {"technical": "TECH", "on_page": "ON", "off_page": "OFF"}
    rows = []
    for section, size in SECTION_SIZES.items():
        for i in range(1, size + 1):
            pid = f"{prefix[section]}-{i:02d}"
            if pid in unknown:
                rows.append({"parameter_id": pid, "section": section, "name": pid,
                             "weight": 1.0, "status": "UNKNOWN", "score": None})
            else:
                score = scores.get(pid, 50.0)
                rows.append({"parameter_id": pid, "section": section, "name": pid, "weight": 1.0,
                             "status": "PASS" if score >= 90 else "FAIL" if score < 50 else "PARTIAL",
                             "score": score})
    return rows


def _reported(rows: list[dict]) -> float:
    return overall_score(*(category_score(rows, s) for s in ("technical", "on_page", "off_page")))


def test_coefficients_sum_to_one_hundred():
    coeffs = coefficients(_rows())
    assert len(coeffs) == 62
    assert round(sum(coeffs.values()), 9) == 100.0


def test_coefficients_match_section_weights():
    coeffs = coefficients(_rows())
    for section, size in SECTION_SIZES.items():
        section_total = sum(v for k, v in coeffs.items() if k.startswith(
            {"technical": "TECH", "on_page": "ON-", "off_page": "OFF"}[section]))
        assert round(section_total, 9) == round(WEIGHTS[section] * 100, 9)
        # every parameter in a section carries an equal share of it
        assert round(section_total / size, 4) == round(
            next(v for k, v in coeffs.items() if k.startswith(
                {"technical": "TECH", "on_page": "ON-", "off_page": "OFF"}[section])), 4)


def test_contributions_reproduce_the_reported_score():
    """The whole promise of the ledger: the column adds up to the headline number,
    to within the two round(..., 1) calls applied on the way out."""
    rows = _rows({f"TECH-{i:02d}": i * 4 for i in range(1, 23)})
    exact = sum(c["points_earned"] for c in contributions(rows))
    assert abs(_reported(rows) - exact) <= 0.05


def test_earned_plus_lost_equals_the_ceiling():
    for c in contributions(_rows({"ON-01": 12.5, "OFF-03": 77.0})):
        assert round(c["points_earned"] + c["points_lost"], 9) == round(c["coefficient"], 9)


def test_unknown_rows_hand_their_weight_to_scored_siblings():
    """An UNKNOWN is excluded from the average, not scored zero -- so the survivors in its
    section each get a bigger share. This is the case a naive weight/count breakdown gets
    wrong, and it still has to reconcile."""
    half_off_page_missing = {f"OFF-{i:02d}" for i in range(1, 10)}
    rows = _rows(unknown=half_off_page_missing)
    coeffs = coefficients(rows)

    assert len(coeffs) == 53
    assert round(sum(coeffs.values()), 9) == 100.0
    # 9 of 18 off-page checks left: each survivor is worth double its nominal 1.389
    assert round(coeffs["OFF-10"], 4) == round(WEIGHTS["off_page"] / 9 * 100, 4)
    assert round(coeffs["OFF-10"], 4) == round(2 * WEIGHTS["off_page"] / 18 * 100, 4)
    # untouched sections are unaffected
    assert round(coeffs["TECH-01"], 4) == round(WEIGHTS["technical"] / 22 * 100, 4)
    assert abs(_reported(rows) - sum(c["points_earned"] for c in contributions(rows))) <= 0.05


def test_a_silent_section_renormalises_the_others():
    """overall_score() divides by the weight of sections that scored, so losing a whole
    section promotes the remaining two rather than counting it as zero."""
    rows = _rows(unknown={f"OFF-{i:02d}" for i in range(1, 19)})
    coeffs = coefficients(rows)

    assert not any(k.startswith("OFF") for k in coeffs)
    assert round(sum(coeffs.values()), 9) == 100.0
    surviving = WEIGHTS["technical"] + WEIGHTS["on_page"]
    assert round(coeffs["TECH-01"], 4) == round(WEIGHTS["technical"] / surviving / 22 * 100, 4)
    assert abs(_reported(rows) - sum(c["points_earned"] for c in contributions(rows))) <= 0.05


def test_prioritize_impact_is_the_exact_points_lost():
    rows = _rows({"ON-05": 0.0, "TECH-04": 80.0})
    lost = {c["parameter_id"]: c["points_lost"] for c in contributions(rows)}
    for issue in prioritize(rows):
        assert issue["score_impact"] == -round(lost[issue["parameter_id"]], 3)


def test_prioritize_ranks_by_points_and_bands_severity():
    rows = _rows({"ON-05": 0.0, "TECH-04": 85.0})
    issues = prioritize(rows)

    # a total failure of an on-page check costs its full 1.818-point ceiling
    worst = issues[0]
    assert worst["parameter_id"] == "ON-05"
    assert worst["score_impact"] == -1.818
    assert worst["severity"] == "High Impact"
    assert issues == sorted(issues, key=lambda i: i["score_impact"])

    # 85/100 on a technical check forfeits only 0.239 points
    minor = next(i for i in issues if i["parameter_id"] == "TECH-04")
    assert minor["score_impact"] == -0.239
    assert minor["severity"] == "Low Impact"


BANDS = ["Low Impact", "Medium Impact", "High Impact"]


def test_severity_drift_is_bounded_when_checks_go_unmeasured():
    """Unmeasured checks raise the surviving coefficients in their own section, so an
    identical failure there genuinely costs more. Banding on the mean coefficient keeps
    that from stampeding the whole report into one severity -- which is what fixed point
    thresholds did, since every coefficient can double."""
    intact = prioritize(_rows({"OFF-10": 70.0}))
    degraded = prioritize(_rows({"OFF-10": 70.0}, unknown={f"OFF-{i:02d}" for i in range(1, 10)}))

    before = next(i for i in intact if i["parameter_id"] == "OFF-10")
    after = next(i for i in degraded if i["parameter_id"] == "OFF-10")

    # half its section went unmeasured, so its share -- and its cost -- doubles
    assert abs(after["score_impact"] / before["score_impact"] - 2) < 0.01
    # but the label drifts by at most one band rather than jumping straight to High
    assert BANDS.index(after["severity"]) - BANDS.index(before["severity"]) <= 1


def test_severity_discriminates_across_all_three_bands():
    """A report with a real spread of scores must not land everything in one band."""
    rows = _rows({"ON-05": 0.0, "ON-06": 30.0, "TECH-04": 60.0, "TECH-05": 88.0})
    bands = {i["severity"] for i in prioritize(rows)}
    assert bands == set(BANDS)


def test_prioritize_skips_passing_and_unmeasured_checks():
    rows = _rows({"TECH-01": 100.0}, unknown={"OFF-01"})
    listed = {i["parameter_id"] for i in prioritize(rows)}
    assert "TECH-01" not in listed
    assert "OFF-01" not in listed

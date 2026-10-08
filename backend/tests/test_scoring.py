from app.config import WEIGHTS
from app.parameters.scoring import (
    category_score,
    coefficients,
    contributions,
    final_from_rules,
    label_for_score,
    measurable_points,
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


def test_overall_is_points_earned_over_points_measurable():
    rows = _rows({f"TECH-{i:02d}": 100.0 for i in range(1, 23)}, unknown={f"OFF-{i:02d}" for i in range(1, 10)})
    # Technical 35 points all earned, On-Page 40 points at 50%, Off-Page not measured.
    assert overall_score(rows) == round((35 + 20) / 75 * 100, 1)


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


# Section sizes of the shipped registry: 22 technical, 20 on-page, 9 off-page, every
# weight 1.0. The ids below are generated, so they are the right SHAPE and count rather
# than the exact shipped set -- scoring only ever weights and groups them.
SECTION_SIZES = {"technical": 22, "on_page": 20, "off_page": 9}


def _rows(scores: dict[str, float] | None = None, unknown: set[str] | None = None) -> list[dict]:
    """A full 51-parameter result set. Anything in `unknown` comes back UNKNOWN/None,
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
    return overall_score(rows)


def test_coefficients_sum_to_one_hundred():
    coeffs = coefficients(_rows())
    assert len(coeffs) == 51
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


def test_unmeasured_checks_do_not_shift_the_pillar_weights():
    """The reviewer's case: with unmeasured checks the overall score must still be the stated
    35 / 40 / 25 blend of the pillar scores, not a re-weighting by how many checks ran."""
    rows = _rows({f"TECH-{i:02d}": 60.0 for i in range(1, 23)} | {f"ON-{i:02d}": 30.0 for i in range(1, 21)}
                 | {f"OFF-{i:02d}": 45.0 for i in range(1, 10)},
                 unknown={f"ON-{i:02d}" for i in range(1, 8)} | {"TECH-01", "TECH-06", "OFF-08"})
    tech, onpage, offpage = (category_score(rows, s) for s in ("technical", "on_page", "off_page"))
    blend = 0.35 * tech + 0.40 * onpage + 0.25 * offpage
    assert abs(_reported(rows) - blend) <= 0.05
    for section in ("technical", "on_page", "off_page"):
        assert round(measurable_points(rows, section), 9) == round(WEIGHTS[section] * 100, 9)


def test_points_lost_add_up_to_what_the_overall_score_is_missing():
    rows = _rows({"ON-05": 0.0, "OFF-07": 10.0, "TECH-04": 80.0}, unknown={f"ON-{i:02d}" for i in range(10, 16)})
    lost = sum(c["points_lost"] for c in contributions(rows))
    assert abs((100 - _reported(rows)) - lost) <= 0.05


def test_a_silent_section_drops_out_and_the_others_keep_their_proportions():
    rows = _rows(unknown={f"OFF-{i:02d}" for i in range(1, 10)})
    assert round(measurable_points(rows), 9) == 100.0
    assert not any(c["section"] == "off_page" for c in contributions(rows))
    assert round(coefficients(rows)["TECH-01"], 6) == round(35 / 75 * 100 / 22, 6)
    tech, onpage = category_score(rows, "technical"), category_score(rows, "on_page")
    assert abs(_reported(rows) - (35 * tech + 40 * onpage) / 75) <= 0.05


def test_the_rules_based_score_is_the_final_score_for_old_saved_rows():
    row = {"parameter_id": "ON-10", "section": "on_page", "status": "PARTIAL", "score": 64.1,
           "recommendation": "Cover the rubric.", "evidence": {"rules_based_score": 19.5}}
    out = final_from_rules(row)
    assert out["score"] == 19.5 and out["status"] == "FAIL"
    assert out["evidence"]["model_score"] == 64.1
    assert row["score"] == 64.1, "the stored row must not be mutated"
    upgraded = final_from_rules({**row, "score": 100.0, "status": "PASS", "evidence": {"rules_based_score": 95.0}})
    assert upgraded["status"] == "PASS" and upgraded["recommendation"] is None


def test_prioritize_impact_is_the_exact_points_lost():
    rows = _rows({"ON-05": 0.0, "TECH-04": 80.0})
    lost = {c["parameter_id"]: c["points_lost"] for c in contributions(rows)}
    for issue in prioritize(rows):
        assert issue["score_impact"] == -round(lost[issue["parameter_id"]], 3)


def test_prioritize_ranks_by_points_and_bands_severity():
    rows = _rows({"ON-05": 0.0, "TECH-04": 85.0})
    issues = prioritize(rows)

    # a total failure of an on-page check costs its full 2.0-point ceiling
    worst = issues[0]
    assert worst["parameter_id"] == "ON-05"
    assert worst["score_impact"] == -2.0
    assert worst["severity"] == "High Impact"
    assert issues == sorted(issues, key=lambda i: i["score_impact"])

    # 85/100 on a technical check forfeits only 0.239 points
    minor = next(i for i in issues if i["parameter_id"] == "TECH-04")
    assert minor["score_impact"] == -0.239
    assert minor["severity"] == "Low Impact"


BANDS = ["Low Impact", "Medium Impact", "High Impact"]


def test_a_check_s_cost_is_on_the_same_scale_as_the_overall_score():
    """With siblings unmeasured, a measured check carries more of its pillar, so it costs more --
    exactly as much as it moves the overall score. The severity yardstick stays put."""
    intact = prioritize(_rows({"OFF-08": 70.0}))
    degraded = prioritize(_rows({"OFF-08": 70.0}, unknown={f"OFF-{i:02d}" for i in range(1, 6)}))
    before = next(i for i in intact if i["parameter_id"] == "OFF-08")
    after = next(i for i in degraded if i["parameter_id"] == "OFF-08")
    assert before["score_impact"] == -0.833 and after["score_impact"] == -1.875   # 25/9 vs 25/4 of the pillar, x 0.30
    assert before["severity"] in BANDS and after["severity"] in BANDS


def test_severity_discriminates_across_all_three_bands():
    """A report with a real spread of scores must not land everything in one band."""
    rows = _rows({"ON-05": 0.0, "ON-11": 30.0, "TECH-04": 60.0, "TECH-05": 88.0})
    bands = {i["severity"] for i in prioritize(rows)}
    assert bands == set(BANDS)


def test_prioritize_skips_passing_and_unmeasured_checks():
    rows = _rows({"TECH-01": 100.0}, unknown={"OFF-01"})
    listed = {i["parameter_id"] for i in prioritize(rows)}
    assert "TECH-01" not in listed
    assert "OFF-01" not in listed

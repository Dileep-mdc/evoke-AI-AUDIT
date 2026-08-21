from app.parameters.scoring import category_score, label_for_score, overall_score, status_counts


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

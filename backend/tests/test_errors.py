from app.errors import humanize_error
from app.parameters.common import enrich_evidence, result


def test_humanize_dns_errno():
    msg = humanize_error("[Errno 11001] getaddrinfo failed")
    assert "DNS" in msg
    assert "11001" not in msg
    assert "getaddrinfo" not in msg


def test_humanize_passthrough():
    assert humanize_error("HTTP 403") == "HTTP 403"


def test_result_adds_readable_summary():
    spec = {"parameter_id": "ON-01", "section": "on_page", "name": "Question-style headings", "weight": 1}
    row = result(spec, score=0, evidence={"headings": []}, recommendation="Rewrite headings.", checked="https://example.com")
    assert row["status"] == "FAIL"
    assert row["evidence"]["summary"]
    assert "headings" not in row["evidence"]["summary"].lower() or "nothing matching" in row["evidence"]["summary"].lower() or "Question-style" in row["evidence"]["summary"]


def test_enrich_empty_lists():
    ev = enrich_evidence(
        {"name": "Question-style headings"},
        {"headings": []},
        score=0,
        error=None,
        unknown=False,
    )
    assert "nothing matching" in ev["summary"]

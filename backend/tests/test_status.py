"""GET /api/status describes the server's setup."""
import pytest
from fastapi.testclient import TestClient

from app.api import status
from app.main import app


@pytest.fixture
def get(monkeypatch):
    monkeypatch.setattr(status, "usage_history", lambda: [])

    def call():
        return TestClient(app).get("/api/status").json()
    return call


def test_status_names_the_off_page_checks_that_need_web_search(monkeypatch, get):
    monkeypatch.setattr(status, "search_provider", lambda: "none")
    body = get()
    assert body["search"] == {"provider": "none", "ready": False,
                              "checks": ["OFF-03", "OFF-04", "OFF-05", "OFF-06", "OFF-07", "OFF-08", "OFF-09"]}


def test_status_reports_no_warnings_list(get):
    assert "warnings" not in get()


def test_status_has_no_typical_cost_without_history(get):
    assert get()["cost"]["typical_per_100_pages"] is None

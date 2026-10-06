"""GET /api/status warns before a scan about what this server cannot measure."""
import pytest
from fastapi.testclient import TestClient

from app.api import status
from app.main import app


@pytest.fixture
def get(monkeypatch):
    async def reachable():
        return {"reachable": True, "error": None}
    monkeypatch.setattr(status, "_wikimedia_reachable", reachable)
    monkeypatch.setattr(status, "usage_history", lambda: [])

    def call():
        return TestClient(app).get("/api/status").json()
    return call


def _titles(body):
    return [w["title"] for w in body["warnings"]]


def test_no_search_provider_warns_that_off_page_is_partial(monkeypatch, get):
    monkeypatch.setattr(status, "search_provider", lambda: "none")
    body = get()
    warning = next(w for w in body["warnings"] if w["area"] == "off_page")
    assert warning["level"] == "warning"
    assert "7 of the 9 Off-Page checks" in warning["text"]
    assert body["search"]["checks"] == ["OFF-03", "OFF-04", "OFF-05", "OFF-06", "OFF-07", "OFF-08", "OFF-09"]


def test_a_configured_provider_raises_no_off_page_warning(monkeypatch, get):
    monkeypatch.setattr(status, "search_provider", lambda: "openai")
    assert "Off-Page checks need a web-search key" not in _titles(get())


def test_unreachable_wikimedia_is_named(monkeypatch, get):
    async def down():
        return {"reachable": False, "error": "ConnectError: certificate verify failed"}
    monkeypatch.setattr(status, "_wikimedia_reachable", down)
    body = get()
    assert "Wikidata and Wikipedia are unreachable from this server" in _titles(body)


def test_status_lists_the_site_set_choices(get):
    body = get()
    assert {o["key"] for o in body["platforms"]["industries"]} >= {"software", "it_services"}
    assert {o["key"] for o in body["platforms"]["regions"]} >= {"india", "uk"}

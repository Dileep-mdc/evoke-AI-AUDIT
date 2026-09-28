"""Auditing from a saved folder: responses round-trip exactly, the app's own fetches are served
from disk, and TECH-02's AI-bot probes still reach the live site."""
import asyncio

import pytest

from app.config import BROWSER_UA, CRAWLER_UA
from app.crawler import http, snapshot


def _res(url, status=200, body=b"<html><title>Hi</title></html>", hops=0):
    return http.FetchResult(url=url, final_url=url, status_code=status, headers={"x": "1"},
                            content=body, text=body.decode(), content_type="text/html",
                            elapsed_ms=321, hops=hops)


@pytest.fixture
def saved_site(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "SNAPSHOT_ROOT", tmp_path)
    monkeypatch.setattr(snapshot, "USE_SNAPSHOTS", True)
    snapshot._cache.clear()
    rec = snapshot.Recorder("www.x.com")
    rec.add("https://www.x.com/", _res("https://www.x.com/"))
    rec.add("https://www.x.com/about/", _res("https://www.x.com/about/", hops=1))
    rec.add("https://www.x.com/brochure.pdf", _res("https://www.x.com/brochure.pdf", 404), keep_body=False)
    rec.pages = ["https://www.x.com/", "https://www.x.com/about/"]
    rec.save()
    yield
    snapshot._cache.clear()


def _no_network(monkeypatch):
    calls = []

    async def live(url, *, user_agent, follow, method):
        calls.append(user_agent)
        return _res(url, 403, b"blocked")

    monkeypatch.setattr(http, "_fetch_once", live)
    return calls


def test_saved_response_round_trips(saved_site):
    snap = snapshot.load("https://www.x.com/anything")
    got = snap.result("https://www.x.com/about")  # no trailing slash: same page
    assert (got.status_code, got.hops, got.elapsed_ms, got.headers) == (200, 1, 321, {"x": "1"})
    assert got.text == "<html><title>Hi</title></html>"
    assert snap.result("https://www.x.com/brochure.pdf").status_code == 404
    assert snap.summary()["pages"] == 2


def test_app_fetches_are_served_from_disk(saved_site, monkeypatch):
    calls = _no_network(monkeypatch)
    for ua in (CRAWLER_UA, BROWSER_UA):
        assert asyncio.run(http.fetch("https://www.x.com/", user_agent=ua)).status_code == 200
    assert calls == []


def test_ai_bot_probe_still_goes_live(saved_site, monkeypatch):
    calls = _no_network(monkeypatch)
    res = asyncio.run(http.fetch("https://www.x.com/", user_agent="GPTBot/1.0"))
    assert res.status_code == 403 and calls == ["GPTBot/1.0"]


def test_unsaved_url_falls_through_to_the_network(saved_site, monkeypatch):
    calls = _no_network(monkeypatch)
    asyncio.run(http.fetch("https://www.x.com/new-page/"))
    assert calls  # went live


def test_no_snapshot_when_switched_off(saved_site, monkeypatch):
    monkeypatch.setattr(snapshot, "USE_SNAPSHOTS", False)
    assert snapshot.load("https://www.x.com/") is None


def test_folder_path_scan_reads_only_its_folder(saved_site, tmp_path, monkeypatch):
    """A folder typed into the dashboard is read by that scan; a URL scan stays live."""
    monkeypatch.setattr(snapshot, "USE_SNAPSHOTS", False)
    calls = _no_network(monkeypatch)

    async def folder_scan():
        snapshot.activate(snapshot.open_folder(str(tmp_path / "www.x.com")))
        return (await http.fetch("https://www.x.com/")).status_code

    async def url_scan():
        return (await http.fetch("https://www.x.com/")).status_code

    assert asyncio.run(folder_scan()) == 200 and calls == []
    assert asyncio.run(url_scan()) == 403 and calls  # separate scan: not served from disk


def test_folder_input_is_recognised_and_explained():
    assert snapshot.looks_like_folder(r"C:\Users\me\www.x.com")
    assert snapshot.looks_like_folder(r'"C:\a b\www.x.com"')
    assert not snapshot.looks_like_folder("https://www.x.com/")
    assert not snapshot.looks_like_folder("www.x.com")
    with pytest.raises(ValueError, match="no saved copy"):
        snapshot.open_folder(str(__import__("pathlib").Path(__file__).parent))

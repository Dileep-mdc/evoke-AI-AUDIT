"""The readable site folder every scan writes (app/site_folder.py).

Pinned here: page copy and site furniture are told apart (including the Drupal wrapper whose
class name reads like a popup), every link is kept with its anchor text and where it sat, file
names stay inside the Windows path limit, and a re-scan removes only the files the previous
export wrote -- never the snapshot or anything else a person put in the folder.
"""
from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import site_folder
from app.crawler.http import FetchResult

PAGE = (
    "<!doctype html><html><head><title>Acme Widgets</title><script>var x = 1;</script></head><body>"
    "<header><a href='/'>Acme</a><nav><ul><li><a href='/about/'>About us</a></li></ul></nav></header>"
    "<div class='cookie-banner'>We use cookies <a href='/privacy'>Privacy</a></div>"
    "<main><h1>Industrial widgets</h1><p>Built to last since 1974.</p>"
    "<p>Write to <a href='mailto:hi@acme.test'>hi@acme.test</a> or read "
    "<a href='https://partner.example/x#top' rel='nofollow'>our partner</a>.</p></main>"
    "<footer><p>© Acme</p></footer></body></html>"
)


def _fetch(url: str, body: str, status: int = 200) -> FetchResult:
    return FetchResult(url=url, final_url=url, status_code=status, headers={}, content=body.encode(),
                       text=body, content_type="text/html; charset=utf-8", elapsed_ms=5)


def test_main_content_and_site_furniture_are_separated():
    read = site_folder.read_page(PAGE, "https://acme.test/")
    assert read["main"] == ["# Industrial widgets", "Built to last since 1974.",
                            "Write to hi@acme.test or read our partner."]
    assert "About us" in read["chrome"] and "We use cookies Privacy" in read["chrome"]
    assert "© Acme" in read["chrome"]
    assert not any("var x" in line for line in read["main"] + read["chrome"])


def test_every_link_is_kept_with_text_rel_and_area():
    links = site_folder.read_page(PAGE, "https://acme.test/")["links"]
    assert ("https://acme.test/about/", "About us", "", "chrome") in links
    assert ("mailto:hi@acme.test", "hi@acme.test", "", "main") in links
    # Absolute, fragment dropped, rel kept.
    assert ("https://partner.example/x", "our partner", "nofollow", "main") in links


def test_a_page_wrapper_named_like_a_popup_is_still_the_page():
    """Drupal wraps every page in div.dialog-off-canvas-main-canvas; reading that as a popup put
    an entire nvent.com page under HEADER, MENUS, POPUPS & FOOTER and left MAIN CONTENT empty."""
    html = ("<html><body><div class='dialog-off-canvas-main-canvas'><nav><a href='/a'>Menu</a></nav>"
            "<h1>Electrical Enclosures</h1><p>Enclosures for harsh environments.</p></div></body></html>")
    read = site_folder.read_page(html, "https://nvent.test/")
    assert read["main"] == ["# Electrical Enclosures", "Enclosures for harsh environments."]
    assert read["chrome"] == ["Menu"]


def test_unparseable_input_returns_empty_rather_than_raising():
    assert site_folder.read_page("", "https://acme.test/") == {"main": [], "chrome": [], "links": []}
    assert site_folder.read_page("<?xml version='1.0' encoding='utf-8'?><p>hi</p>", "https://acme.test/")["main"] == ["hi"]


def test_file_names_follow_the_saved_folder_convention():
    seen: set[str] = set()
    assert site_folder._file_name("https://acme.test/", seen) == "home.txt"
    assert site_folder._file_name("https://acme.test/about-us/company-overview/", seen) == "about_us_company_overview.txt"
    # Same slug twice (trailing slash, query) must not overwrite the first page's file.
    assert site_folder._file_name("https://acme.test/about-us/company-overview", seen) == "about_us_company_overview_2.txt"


def test_file_names_fit_the_windows_path_limit(tmp_path):
    folder = tmp_path / ("d" * max(1, 200 - len(str(tmp_path)))) / "www.acme.test"
    folder.mkdir(parents=True)
    budget = site_folder._name_budget(folder)
    name = site_folder._file_name("https://acme.test/" + "very-long-segment/" * 20, set(), budget)
    assert len(str(folder.resolve() / name)) <= site_folder._MAX_PATH


def _ctx(pages):
    return SimpleNamespace(pages=pages, origin="https://acme.test", normalized_url="https://acme.test/",
                           domain="acme.test", company_name="Acme Widgets Ltd")


def _page(url: str, html: str, status: int = 200, title: str = "Acme"):
    return SimpleNamespace(url=url, result=_fetch(url, html, status), rendered_html="", title=title,
                           meta_description="Widgets.", word_count=5)


def test_save_writes_the_whole_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(site_folder, "SITE_FOLDERS_ROOT", tmp_path)
    pages = [_page("https://acme.test/", PAGE), _page("https://acme.test/gone", "<p>Not found</p>", 404, "")]
    folder = asyncio.run(site_folder.save("scan-1", _ctx(pages), crawl_ms=4200))
    site_folder.wait_for_workbooks()

    assert folder == tmp_path / "acme.test"
    rows = list(csv.DictReader((folder / "_index.csv").open(encoding="utf-8")))
    assert [r["URL"] for r in rows] == ["https://acme.test/", "https://acme.test/gone"]
    assert rows[0]["File"] == "home.txt" and rows[0]["HTTP Status"] == "200"

    text = (folder / "home.txt").read_text(encoding="utf-8")
    assert text.startswith("URL: https://acme.test/\nTitle: Acme\nMeta description: Widgets.\n")
    for header in ("===== MAIN CONTENT =====", "===== HEADER, MENUS, POPUPS & FOOTER =====", "===== LINKS ON THIS PAGE (5) ====="):
        assert header in text
    assert "About us -> https://acme.test/about/ [header/menu/footer]" in text

    assert "https://partner.example/x" in (folder / "all_links.txt").read_text(encoding="utf-8").splitlines()
    meta = json.loads((folder / "_site.json").read_text(encoding="utf-8"))
    assert meta["company_name"] == "Acme Widgets Ltd"
    assert meta["scraping_seconds"] == 4.2 and meta["pages_crawled"] == 2
    assert (folder / meta["links_workbook"]).exists()


def test_rescan_removes_only_its_own_previous_files(tmp_path, monkeypatch):
    monkeypatch.setattr(site_folder, "SITE_FOLDERS_ROOT", tmp_path)
    folder = tmp_path / "acme.test"
    (folder / "snapshot").mkdir(parents=True)
    (folder / "snapshot" / "manifest.json").write_text("{}")
    (folder / "notes.txt").write_text("mine")
    asyncio.run(site_folder.save("scan-1", _ctx([_page("https://acme.test/old-page", PAGE)]), None))
    assert (folder / "old_page.txt").exists()

    asyncio.run(site_folder.save("scan-2", _ctx([_page("https://acme.test/", PAGE)]), None))
    site_folder.wait_for_workbooks()
    assert not (folder / "old_page.txt").exists()
    assert (folder / "home.txt").exists()
    assert (folder / "notes.txt").read_text() == "mine"
    assert (folder / "snapshot" / "manifest.json").exists()


def test_a_failure_never_reaches_the_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(site_folder, "SITE_FOLDERS_ROOT", tmp_path)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(site_folder, "_write", boom)
    assert asyncio.run(site_folder.save("scan-1", _ctx([_page("https://acme.test/", PAGE)]), None)) is None


def test_disabled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(site_folder, "SITE_FOLDERS_ROOT", tmp_path)
    monkeypatch.setattr(site_folder, "SAVE_SITE_FOLDER", False)
    assert asyncio.run(site_folder.save("scan-1", _ctx([_page("https://acme.test/", PAGE)]), None)) is None
    assert not any(tmp_path.iterdir())


def test_no_folder_for_a_site_that_could_not_be_read(tmp_path, monkeypatch):
    """tcs.com answered 403: a folder holding only the refusal page would look like a saved site."""
    monkeypatch.setattr(site_folder, "SITE_FOLDERS_ROOT", tmp_path)
    assert asyncio.run(site_folder.save("scan-1", _ctx([_page("https://acme.test/", "<p>Forbidden</p>", 403)]), None)) is None
    assert not any(tmp_path.iterdir())

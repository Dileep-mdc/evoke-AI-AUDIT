"""Every parameter score carries its sources, and a gzip / multi-locale sitemap is read."""
import asyncio
import gzip

from app.crawler import sitemap
from app.crawler.http import FetchResult
from app.parameters.citations import build_citations


def test_pages_behind_a_score_are_cited_with_what_was_found():
    row = {
        "checked_url_or_source": "https://x.com",
        "evidence": {
            "pages": [{"url": "https://x.com/a", "h1_count": 0, "score": 60},
                      {"url": "https://x.com/b", "h1_count": 1, "score": 100}],
            "summary": "see https://ignored.example/prose",
            "curated_input": "https://ignored.example/model-input",
        },
    }
    cites = build_citations(row)
    sources = [c["source"] for c in cites]
    assert sources == ["https://x.com", "https://x.com/a", "https://x.com/b"]
    assert "h1 count: 0" in cites[1]["detail"]


def test_nested_lists_and_pairs_are_cited():
    row = {"checked_url_or_source": "site", "evidence": {
        "stages": {"awareness": ["https://x.com/blog"]},
        "overlapping_pairs": [{"a": "https://x.com/1", "b": "https://x.com/2", "similarity": 0.9}],
    }}
    got = {c["source"]: c["detail"] for c in build_citations(row)}
    assert got["https://x.com/blog"] == "awareness"
    assert got["https://x.com/1"] == "pair with https://x.com/2"


def test_a_score_is_never_left_uncited():
    assert build_citations({"checked_url_or_source": "llm-citation-probe", "evidence": {}}) == [
        {"source": "llm-citation-probe", "detail": "source checked"}]


def _xml_result(url, body: bytes):
    return FetchResult(url=url, final_url=url, status_code=200, headers={}, content=body,
                       text="", content_type="application/xml", elapsed_ms=1)


def test_gzip_sitemaps_and_preferred_locale(monkeypatch):
    index = (b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             b'<sitemap><loc>https://x.com/cs-cz.xml.gz</loc></sitemap>'
             b'<sitemap><loc>https://x.com/en-in.xml.gz</loc></sitemap></sitemapindex>')

    def urlset(locale):
        return gzip.compress(('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                              f'<url><loc>https://x.com/{locale}/p</loc></url></urlset>').encode())

    bodies = {"https://x.com/sitemap.xml": index,
              "https://x.com/cs-cz.xml.gz": urlset("cs-cz"),
              "https://x.com/en-in.xml.gz": urlset("en-in")}

    async def fake_fetch(url, **kw):
        return _xml_result(url, bodies.get(url, b""))

    monkeypatch.setattr(sitemap, "fetch", fake_fetch)
    monkeypatch.setattr(sitemap, "MAX_SITEMAP_URLS", 1)
    got = asyncio.run(sitemap.fetch_sitemaps("https://x.com", ["https://x.com/sitemap.xml"], prefer="en-in"))
    assert got["urls"] == ["https://x.com/en-in/p"] and not got["errors"]
    assert sitemap.locale_of("https://x.com/en-in/") == "en-in"
    assert sitemap.locale_of("https://x.com/blog/post") == ""

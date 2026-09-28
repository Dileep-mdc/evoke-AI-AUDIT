"""The saved crawl output has to carry the documents, not only the audit's reading of them.

Everything the scraped-content file used to hold was DERIVED: trafilatura's idea of the body
copy, the headings that survived the chrome filter, the links as they were resolved. On a real
site that made the file overwhelmingly a link inventory -- one homepage record was 43 KB of
`links` and 35 KB of `images` against 2.5 KB of `text` -- and the source document it was all
read from was never written down anywhere at all.

That is not a cosmetic gap. When a parameter scores a page thin, the derived fields alone
cannot distinguish a thin page from a failed extraction, because the only evidence of what the
page said is the extraction being questioned. These tests pin down that both source documents
now survive the crawl, that they survive VERBATIM, and that the derived fields were not traded
away to make room for them.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import scan_output
from app.crawler.discover import parse_page
from app.crawler.http import FetchResult

RAW_HTML = (
    "<!doctype html><html><head><title>Acme Widgets</title>"
    '<meta name="description" content="We make widgets."></head>'
    "<body><nav><a href='/about'>About us</a></nav>"
    "<h1>Industrial widgets built to last</h1>"
    "<p>Acme has manufactured precision widgets since 1974 for aerospace customers.</p>"
    "<a href='/contact'>Contact</a><img src='/w.png' alt='a widget'></body></html>"
)
RENDERED_HTML = RAW_HTML.replace("</body>", "<p>This paragraph is mounted by JavaScript.</p></body>")


def _fetch(url="https://acme.test/", body=RAW_HTML, content_type="text/html; charset=utf-8"):
    encoded = body.encode()
    return FetchResult(
        url=url, final_url=url, status_code=200, headers={}, content=encoded,
        text=body, content_type=content_type, elapsed_ms=11,
    )


@pytest.fixture(autouse=True)
def _isolated_output_dir(tmp_path, monkeypatch):
    """Point the writer at a throwaway directory, and put the real one back afterwards.

    monkeypatch rather than a bare assignment: OUTPUT_DIR is a module global, so a test that
    reassigned it permanently would silently redirect every later test in the same session --
    and, worse, pass while doing it.
    """
    monkeypatch.setattr(scan_output, "OUTPUT_DIR", tmp_path)


def _read_doc(tmp_path, rel):
    """Read one stored document back, exactly as a consumer of the index would."""
    return gzip.decompress((tmp_path / rel).read_bytes()).decode("utf-8")


def _save(pages, tmp_path):
    """Run the real writer and read the file it produced back off disk."""
    ctx = SimpleNamespace(
        pages=pages, input_url="acme.test", normalized_url="https://acme.test/",
        origin="https://acme.test", domain="acme.test", company_name="Acme",
        brand_terms=["Acme"], sitemap={}, crawl_errors=[], render={},
    )
    path = scan_output.save_crawl_output("scan-1", ctx)
    return json.loads(path.read_text(encoding="utf-8"))["pages"]


def test_raw_html_is_saved_verbatim(tmp_path):
    """Byte-for-byte what the server sent -- a summary of the document is not the document."""
    saved = _save([parse_page(_fetch())], tmp_path)[0]
    assert _read_doc(tmp_path, saved["raw_html_file"]) == RAW_HTML
    assert saved["raw_html_bytes"] == len(RAW_HTML.encode())
    assert saved["content_type"] == "text/html; charset=utf-8"


def test_documents_live_beside_the_index_not_inside_it(tmp_path):
    """The index must stay small enough to open; inlining measured at 400 MB - 1 GB."""
    saved = _save([parse_page(_fetch())], tmp_path)[0]
    assert "raw_html" not in saved, "the document itself must not be inlined into the index"
    rel = saved["raw_html_file"]
    assert rel.startswith("acme-test-raw/index-") and rel.endswith(".html.gz"), rel
    assert (tmp_path / rel).exists(), "the index must point at a file that is really there"
    # Relative, so the index and its documents can be copied or moved as one directory.
    assert not Path(rel).is_absolute()


def test_two_pages_never_share_a_document_file(tmp_path):
    """A name collision would silently overwrite one page's document with another's."""
    a = parse_page(_fetch(url="https://acme.test/a/b", body=RAW_HTML))
    b = parse_page(_fetch(url="https://acme.test/a-b", body=RAW_HTML.replace("Acme", "Beta")))
    rows = _save([a, b], tmp_path)
    assert rows[0]["raw_html_file"] != rows[1]["raw_html_file"]
    assert "Beta" in _read_doc(tmp_path, rows[1]["raw_html_file"])


def test_a_re_audit_clears_documents_for_pages_that_are_gone(tmp_path):
    """Names are deterministic per domain, so a stale store would accumulate dead pages."""
    _save([parse_page(_fetch(url="https://acme.test/old"))], tmp_path)
    stale = next((tmp_path / "acme-test-raw").iterdir())
    _save([parse_page(_fetch(url="https://acme.test/new"))], tmp_path)
    assert not stale.exists(), "a page dropped from the site must not keep its document forever"
    assert len(list((tmp_path / "acme-test-raw").iterdir())) == 1


def test_the_extracted_text_is_saved_alongside_the_links(tmp_path):
    """The complaint that started this: the file listed links and kept no text worth reading."""
    saved = _save([parse_page(_fetch())], tmp_path)[0]
    assert "precision widgets since 1974" in saved["text"]
    assert saved["word_count"] > 5
    # ...and the derived fields were not dropped to make room for the documents.
    assert [link["href"] for link in saved["links"]] == [
        "https://acme.test/about", "https://acme.test/contact",
    ]
    assert saved["images"] and saved["headings"] and saved["title"] == "Acme Widgets"


def test_a_rendered_page_keeps_both_documents(tmp_path):
    """Both copies, so the rendered reading can be checked against what the server actually sent."""
    saved = _save([parse_page(_fetch(), RENDERED_HTML)], tmp_path)[0]
    assert saved["content_source"] == "rendered"
    assert _read_doc(tmp_path, saved["rendered_html_file"]) == RENDERED_HTML
    assert _read_doc(tmp_path, saved["raw_html_file"]) == RAW_HTML,         "the server's HTML must survive a render, not be replaced by it"
    assert "mounted by JavaScript" in saved["text"]


def test_an_unrendered_page_claims_no_rendered_dom(tmp_path):
    """Otherwise every page would appear to have been rendered, and the field would mean nothing."""
    saved = _save([parse_page(_fetch())], tmp_path)[0]
    assert saved["content_source"] == "raw"
    assert saved["rendered_html_file"] is None


def test_a_non_html_response_is_not_written_out_as_raw_html(tmp_path):
    """A PDF decoded as text is mojibake. Saving it as `raw_html` would be saving noise."""
    pdf = FetchResult(
        url="https://acme.test/spec.pdf", final_url="https://acme.test/spec.pdf", status_code=200,
        headers={}, content=b"%PDF-1.4\n\x00\x01binary", text="%PDF-1.4\n\x00\x01binary",
        content_type="application/pdf", elapsed_ms=8,
    )
    saved = _save([parse_page(pdf)], tmp_path)[0]
    assert saved["raw_html_file"] is None
    assert saved["content_type"] == "application/pdf"
    assert saved["raw_html_bytes"] == len(b"%PDF-1.4\n\x00\x01binary")


def test_a_failed_fetch_saves_no_document_and_still_writes_a_row(tmp_path):
    """A page that never responded has no raw data -- but must not vanish from the output."""
    dead = FetchResult(
        url="https://acme.test/gone", final_url="https://acme.test/gone", status_code=None,
        headers={}, content=b"", text="", content_type="", elapsed_ms=0, error="Connection timed out",
    )
    saved = _save([parse_page(dead)], tmp_path)[0]
    assert saved["raw_html_file"] is None and saved["rendered_html_file"] is None
    assert saved["fetch_error"] == "Connection timed out"
    assert saved["raw_html_bytes"] == 0

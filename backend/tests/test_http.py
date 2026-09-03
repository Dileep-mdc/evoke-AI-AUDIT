from app.crawler.http import normalize_url, origin_of


def test_normalize_keeps_https():
    assert normalize_url("example.com") == "https://example.com/"
    assert origin_of("https://www.example.com/blog") == "https://www.example.com"

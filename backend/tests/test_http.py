from app.crawler.http import normalize_url, origin_of


def test_normalize_keeps_https():
    assert normalize_url("evoketechnologies.com") == "https://evoketechnologies.com/"
    assert origin_of("https://www.evoketechnologies.com/blog") == "https://www.evoketechnologies.com"

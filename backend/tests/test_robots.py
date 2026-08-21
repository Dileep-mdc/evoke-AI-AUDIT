from types import SimpleNamespace

from app.crawler.robots import AI_BOTS, RobotsData, bot_decision
from app.crawler.http import FetchResult
from protego import Protego


def _robots(text: str, ok=True) -> RobotsData:
    result = FetchResult(
        url="https://example.com/robots.txt",
        final_url="https://example.com/robots.txt",
        status_code=200 if ok else 404,
        headers={},
        content=text.encode(),
        text=text,
        content_type="text/plain",
        elapsed_ms=10,
        error=None if ok else "missing",
    )
    parser = Protego.parse(text) if ok and text.strip() else None
    return RobotsData(url=result.url, result=result, raw=text, parser=parser, sitemap_urls=[])


def test_wildcard_allows_ai_bots():
    robots = _robots("User-agent: *\nAllow: /\n")
    for bot in AI_BOTS:
        assert bot_decision(robots, bot)["decision"] == "ALLOWED"


def test_explicit_block():
    robots = _robots("User-agent: GPTBot\nDisallow: /\nUser-agent: *\nAllow: /\n")
    assert bot_decision(robots, "GPTBot")["decision"] == "BLOCKED"
    assert bot_decision(robots, "ClaudeBot")["decision"] == "ALLOWED"


def test_missing_robots_is_unknown():
    robots = _robots("", ok=False)
    robots.result = SimpleNamespace(ok=False, status_code=404, error="HTTP 404")
    assert bot_decision(robots, "GPTBot")["decision"] == "UNKNOWN"

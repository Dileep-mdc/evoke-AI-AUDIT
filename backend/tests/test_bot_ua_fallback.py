"""A CDN that refuses the audit bot must not make robots.txt, the sitemap and every link look
missing or broken -- while TECH-02's explicit AI-bot probes must still see the refusal."""
import asyncio

from app.config import BROWSER_UA, CRAWLER_UA
from app.crawler import http


def _result(status):
    return http.FetchResult(url="u", final_url="u", status_code=status, headers={}, content=b"",
                            text="", content_type="", elapsed_ms=0)


def _serve(monkeypatch):
    calls = []

    async def once(url, *, user_agent, follow, method):
        calls.append(user_agent)
        return _result(200 if user_agent == BROWSER_UA else 403)

    monkeypatch.setattr(http, "_fetch_once", once)
    return calls


def test_default_agent_falls_back_to_browser_when_refused(monkeypatch):
    calls = _serve(monkeypatch)
    res = asyncio.run(http.fetch("https://x/robots.txt"))
    assert res.status_code == 200 and res.bot_ua_refused
    assert calls == [CRAWLER_UA, BROWSER_UA]


def test_explicit_agent_sees_the_refusal(monkeypatch):
    calls = _serve(monkeypatch)
    res = asyncio.run(http.fetch("https://x/", user_agent="GPTBot/1.0"))
    assert res.status_code == 403 and not res.bot_ua_refused
    assert calls == ["GPTBot/1.0"]

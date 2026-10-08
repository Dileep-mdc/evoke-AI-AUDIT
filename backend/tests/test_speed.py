"""Audit-speed fixes: AI calls paced to the project's RPM, no re-run of timed-out checks, and
TECH-06 reusing the crawl's own responses instead of requesting every link again."""
import asyncio
from types import SimpleNamespace

import pytest

from app.api.scans import _worth_retrying
from app.llm import client
from app.parameters import technical


@pytest.fixture
def fresh_pacer(monkeypatch):
    monkeypatch.setattr(client, "_rpm_limit", None)
    monkeypatch.setattr(client, "_tat", 0.0)


def test_the_rpm_limit_is_learned_from_openais_refusal(fresh_pacer):
    exc = Exception("Rate limit reached for gpt-4o-mini in project p on requests per min (RPM): Limit 10, Used 10")
    assert client._learn_rpm(exc) and client.rpm_limit() == 10
    assert not client._learn_rpm(Exception("Rate limit reached on tokens per min (TPM)"))


def test_pacing_allows_the_burst_then_one_request_per_interval(fresh_pacer, monkeypatch):
    clock = {"now": 1000.0}
    slept = []
    monkeypatch.setattr(client.time, "monotonic", lambda: clock["now"])

    async def fake_sleep(seconds):
        slept.append(round(seconds, 2))
    monkeypatch.setattr(client.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(client, "_rpm_limit", 10)

    async def twelve():
        for _ in range(12):
            await client.pace()
    asyncio.run(twelve())
    assert slept == [6.0, 12.0]           # 10 admitted at once; the 11th and 12th wait 6s and 12s


def test_no_limit_means_no_waiting(fresh_pacer):
    asyncio.run(client.pace())            # returns at once


def test_a_timed_out_check_is_not_re_run():
    assert not _worth_retrying({"status": "UNKNOWN", "error": "Timed out after 900s", "evidence": {}})
    assert _worth_retrying({"status": "UNKNOWN", "error": "rate limited", "evidence": {}})


def test_tech06_reuses_the_crawls_responses(monkeypatch):
    checked = []

    async def fake_check(url):
        checked.append(url)
        return SimpleNamespace(ok=True, status_code=200, hops=0)
    monkeypatch.setattr(technical, "check", fake_check)
    ok = SimpleNamespace(ok=True, status_code=200, hops=0)
    broken = SimpleNamespace(ok=False, status_code=404, hops=0)
    pages = [SimpleNamespace(url="https://a.com/", result=ok, links=[{"href": "https://a.com/x"}, {"href": "https://a.com/new"}]),
             SimpleNamespace(url="https://a.com/x", result=broken, links=[])]
    ctx = SimpleNamespace(origin="https://a.com", pages=pages)
    monkeypatch.setattr(technical, "internal_links", lambda c: ["https://a.com/x", "https://a.com/new"])
    row = asyncio.run(technical.tech_06({"parameter_id": "TECH-06", "section": "technical", "name": "TECH-06", "weight": 1.0}, ctx))
    assert checked == ["https://a.com/new"]          # only the link the crawl never requested
    assert row["evidence"]["broken"] == 1 and row["evidence"]["from_crawl"] == 1


def test_a_flood_of_requests_never_puts_more_than_the_limit_in_flight(monkeypatch):
    """TECH-06 once fired a check for every uncrawled link at once and froze the server."""
    from contextlib import asynccontextmanager
    from app.crawler import http

    live = {"now": 0, "peak": 0}

    class FakeClient:
        @asynccontextmanager
        async def stream(self, method, url):
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            await asyncio.sleep(0.001)
            live["now"] -= 1
            yield SimpleNamespace(status_code=200, encoding="utf-8", url=url, headers={}, history=[])

    async def body(resp):
        return b"ok"
    monkeypatch.setattr(http, "client", lambda **kw: FakeClient())
    monkeypatch.setattr(http, "_read_capped", body)
    monkeypatch.setattr(http, "CONCURRENCY", 5)
    monkeypatch.setattr(http, "_inflight", __import__("weakref").WeakKeyDictionary())

    async def flood():
        await asyncio.gather(*(http._fetch_once(f"https://x/{i}", user_agent="t", follow=True, method="HEAD") for i in range(400)))
    asyncio.run(flood())
    assert live["peak"] == 5


def test_tech06_samples_uncrawled_links_beyond_the_limit(monkeypatch):
    async def fake_check(url):
        return SimpleNamespace(ok=True, status_code=200, hops=0)
    monkeypatch.setattr(technical, "check", fake_check)
    monkeypatch.setattr(technical, "LINK_RECHECK_LIMIT", 10)
    ok = SimpleNamespace(ok=True, status_code=200, hops=0)
    ctx = SimpleNamespace(origin="https://a.com", pages=[SimpleNamespace(url="https://a.com/", result=ok, links=[])])
    monkeypatch.setattr(technical, "internal_links", lambda c: ["https://a.com/"] + [f"https://a.com/p{i}" for i in range(50)])
    row = asyncio.run(technical.tech_06({"parameter_id": "TECH-06", "section": "technical", "name": "TECH-06", "weight": 1.0}, ctx))
    ev = row["evidence"]
    assert ev["rechecked"] == 10 and ev["uncrawled_links"] == 50 and ev["not_checked"] == 40 and ev["sampled"] == 11

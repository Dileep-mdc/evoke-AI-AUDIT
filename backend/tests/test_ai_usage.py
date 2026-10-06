"""AI spend per scan: metered, estimated from history, and capped when a budget is set."""
import asyncio

import pytest

from app.llm import client, usage


def test_cost_follows_the_configured_token_prices(monkeypatch):
    monkeypatch.setattr(usage, "LLM_PRICE_INPUT_PER_M", 0.15)
    monkeypatch.setattr(usage, "LLM_PRICE_OUTPUT_PER_M", 0.60)
    monkeypatch.setattr(usage, "OPENAI_SEARCH_PRICE_PER_CALL", 0.01)
    m = usage.Meter()
    m.record(1_000_000, 0)
    m.record(0, 1_000_000)
    m.record(0, 0, search=True)
    assert (m.calls, m.search_calls) == (2, 1)
    assert m.cost_usd() == pytest.approx(0.15 + 0.60 + 0.01)


def test_the_budget_is_reached_only_when_one_is_set():
    m = usage.Meter()
    m.record(10_000_000, 10_000_000)
    assert not m.over_budget()
    capped = usage.Meter(budget_usd=0.01)
    capped.record(1_000_000, 0)
    assert capped.over_budget()


def test_a_call_over_budget_is_refused_without_reaching_the_api(monkeypatch):
    monkeypatch.setattr(client, "ENABLE_LLM_SCORING", True)
    monkeypatch.setattr(client, "OPENAI_API_KEY", "sk-test")

    def no_api():
        raise AssertionError("the API must not be called once the cap is reached")
    monkeypatch.setattr(client, "_get_client", no_api)

    async def scan():
        meter = usage.start(budget_usd=0.01)
        meter.record(1_000_000, 0)
        return await client.judge("prompt"), meter

    res, meter = asyncio.run(scan())
    assert not res.ok and "budget" in res.error
    assert meter.refused == 1 and meter.as_dict()["budget_reached"]


def test_a_meter_belongs_to_the_scan_that_started_it():
    async def scan():
        usage.start()
        return usage.current()

    assert asyncio.run(scan()) is not None
    assert usage.current() is None


def test_the_estimate_is_a_fixed_part_plus_a_per_page_part():
    history = [{"pages": 100, "calls": 160, "cost_usd": 0.12, "scaling_calls": 100, "scaling_cost_usd": 0.10},
               {"pages": 200, "calls": 260, "cost_usd": 0.22, "scaling_calls": 200, "scaling_cost_usd": 0.20},
               {"pages": 50, "calls": 110, "cost_usd": 0.07, "scaling_calls": 50, "scaling_cost_usd": 0.05}]
    est = usage.estimate(1000, history)
    assert est["based_on_scans"] == 3
    assert est["fixed_cost_usd"] == pytest.approx(0.02)
    assert est["cost_usd"] == pytest.approx(0.02 + 1000 * 0.001)
    assert est["calls"] == 60 + 1000


def test_a_one_page_scan_does_not_inflate_the_per_page_rate():
    """Its cost is almost all fixed; spread over one page it once read as dollars per page."""
    tiny = [{"pages": 1, "calls": 54, "cost_usd": 0.02, "scaling_calls": 2, "scaling_cost_usd": 0.0005}]
    assert usage.estimate(100, tiny)["cost_usd"] == pytest.approx(0.0195 + 100 * 0.0005)


def test_batched_classification_is_counted_as_the_scaling_part():
    m = usage.Meter()
    m.record(1000, 100, scales=True)
    m.record(1000, 100)
    assert (m.calls, m.scaling_calls) == (2, 1)
    assert m.scaling_cost_usd() == pytest.approx(m.cost_usd() / 2)


def test_no_history_means_no_estimate():
    assert usage.estimate(500, []) is None

"""TECH-07 measures real Core Web Vitals: CrUX field data, else a lab run, else the old proxy.

The browser and the CrUX API are replaced by fakes here; what is pinned is the scoring of each
source, the fallback order, and that the plain-English working rebuilds the same score.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.crawler import vitals
from app.parameters import technical, working_technical
from app.parameters.common import vital_rating, vital_score

SPEC = {"parameter_id": "TECH-07", "section": "technical", "name": "TECH-07", "weight": 1.0}


def _ctx(snapshot=None):
    page = SimpleNamespace(url="https://www.acme.com/", page_type="home",
                           result=SimpleNamespace(ok=True, elapsed_ms=500, content=b"x" * 100_000))
    return SimpleNamespace(origin="https://www.acme.com", pages=[page], homepage=page, snapshot=snapshot or {})


def _tech_07(monkeypatch, measured):
    async def fake(ctx):
        return measured
    monkeypatch.setattr(vitals, "measure", fake)
    return asyncio.run(technical.tech_07(SPEC, _ctx()))


@pytest.mark.parametrize("metric,value,points", [
    ("lcp_ms", 2500, 100), ("lcp_ms", 3250, 70), ("lcp_ms", 4000, 25), ("lcp_ms", 9000, 25),
    ("inp_ms", 200, 100), ("inp_ms", 350, 70), ("tbt_ms", 400, 70), ("cls", 0.1, 100), ("cls", 0.175, 70), ("cls", 0.3, 25),
])
def test_each_vital_is_scored_against_googles_thresholds(metric, value, points):
    assert vital_score(metric, value) == pytest.approx(points)


def test_ratings_use_googles_three_bands():
    assert vital_rating("lcp_ms", 2400) == "good"
    assert vital_rating("lcp_ms", 3000) == "needs improvement"
    assert vital_rating("lcp_ms", 4100) == "poor"


def test_field_data_scores_the_mean_of_lcp_inp_and_cls(monkeypatch):
    row = _tech_07(monkeypatch, {"source": "field", "metrics": {"lcp_ms": 2000, "inp_ms": 350, "cls": 0.3},
                                 "origin": "https://www.acme.com", "form_factor": "PHONE"})
    assert row["score"] == pytest.approx((100 + 70 + 25) / 3, abs=0.1)
    ev = row["evidence"]
    assert ev["vitals_source"] == "field" and row["confidence"] == 0.9
    assert ev["ratings"] == {"lcp_ms": "good", "inp_ms": "needs improvement", "cls": "poor"}
    assert "Interaction to Next Paint" in row["recommendation"]


def test_lab_data_scores_tbt_in_place_of_inp(monkeypatch):
    row = _tech_07(monkeypatch, {"source": "lab", "metrics": {"lcp_ms": 2000, "tbt_ms": 100, "cls": 0.01},
                                 "lab_pages": [], "throttled": True, "field_unavailable": "no key"})
    assert row["score"] == 100 and row["recommendation"] is None
    assert set(row["evidence"]["metrics"]) == {"lcp_ms", "tbt_ms", "cls"}
    assert row["confidence"] == 0.7


def test_no_vitals_source_falls_back_to_the_proxy_and_says_why(monkeypatch):
    row = _tech_07(monkeypatch, {"source": None, "metrics": {}, "unavailable": ["no key", "no browser"]})
    ev = row["evidence"]
    assert ev["vitals_source"] == "proxy" and ev["vitals_unavailable"] == ["no key", "no browser"]
    assert row["score"] == 100 and row["confidence"] == 0.4


@pytest.mark.parametrize("measured", [
    {"source": "field", "metrics": {"lcp_ms": 3100, "inp_ms": 180, "cls": 0.12}},
    {"source": "lab", "metrics": {"lcp_ms": 5200, "tbt_ms": 450, "cls": 0.02}},
])
def test_the_working_sentence_rebuilds_the_same_score(monkeypatch, measured):
    row = _tech_07(monkeypatch, measured)
    text, score = working_technical.tech_07(row["evidence"])
    assert score == pytest.approx(row["score"], abs=0.1)
    assert "average of the three" in text


def test_measure_prefers_field_data_and_skips_the_lab(monkeypatch):
    async def crux(origin):
        return {"metrics": {"lcp_ms": 1, "inp_ms": 1, "cls": 0.0}, "form_factor": "PHONE"}

    async def lab(urls):
        raise AssertionError("the lab must not run when field data exists")
    monkeypatch.setattr(vitals, "crux", crux)
    monkeypatch.setattr(vitals, "lab", lab)
    assert asyncio.run(vitals.measure(_ctx()))["source"] == "field"


def test_a_saved_site_copy_is_never_lab_measured(monkeypatch):
    async def crux(origin):
        return {"metrics": {}, "error": "no key"}

    async def lab(urls):
        raise AssertionError("a snapshot scan must not measure the live site")
    monkeypatch.setattr(vitals, "crux", crux)
    monkeypatch.setattr(vitals, "lab", lab)
    out = asyncio.run(vitals.measure(_ctx(snapshot={"host": "www.acme.com"})))
    assert out["source"] is None and any("saved copy" in u for u in out["unavailable"])


def test_lab_takes_the_median_page_per_metric(monkeypatch):
    async def crux(origin):
        return {"metrics": {}, "error": "no data"}

    async def lab(urls):
        return [{"url": "a", "lcp_ms": 1000, "cls": 0.3, "tbt_ms": 10},
                {"url": "b", "lcp_ms": 3000, "cls": 0.0, "tbt_ms": 900},
                {"url": "c", "lcp_ms": 9000, "cls": 0.1, "tbt_ms": 100},
                {"url": "d", "error": "timed out"}]
    monkeypatch.setattr(vitals, "crux", crux)
    monkeypatch.setattr(vitals, "lab", lab)
    out = asyncio.run(vitals.measure(_ctx()))
    assert out["source"] == "lab"
    assert out["metrics"] == {"lcp_ms": 3000, "cls": 0.1, "tbt_ms": 100}

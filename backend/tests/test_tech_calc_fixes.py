"""TECH-* score-calculation fixes: each test pins one bug a read-only audit reproduced.

Handlers are run on hand-built pages (the same SimpleNamespace shape test_accuracy.py uses), and
where a working sentence exists it must rebuild the handler's score from the stored evidence.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.crawler.parse import make_soup
from app.llm.client import LLMResult
from app.parameters import technical, working_technical
from app.parameters.technical import has_fixed_width_layout


def _run(coro):
    return asyncio.run(coro)


def _spec(pid):
    return {"parameter_id": pid, "section": "technical", "name": pid, "weight": 1.0}


def _page(url="https://x/p", *, text="", html="", title="", description="", page_type="other", headings=(),
          schema=(), images=(), canonical="", ok=True, words=None, render=None, rendered_html="", dates=None):
    return SimpleNamespace(
        url=url, title=title, meta_description=description, text=text,
        word_count=len(text.split()) if words is None else words, page_type=page_type,
        soup=make_soup(html) if html else None, headings=list(headings), links=[], images=list(images),
        schema_blocks=[{"ok": True, "data": s} for s in schema], dates=dates or {}, hreflang=[],
        canonical=canonical, blocked_reason="", render=render or {}, rendered_html=rendered_html,
        result=SimpleNamespace(final_url=url, text=html, ok=ok, status_code=200 if ok else 404, error=None,
                               hops=0, elapsed_ms=10, content=b""),
    )


def _ctx(pages, **extra):
    base = dict(origin="https://x", domain="x", pages=pages, homepage=pages[0] if pages else None,
                company_name="X", brand_terms=["x"], crawl_errors=[], render={}, sitemap={})
    base.update(extra)
    return SimpleNamespace(**base)


def _working(pid, row):
    out = working_technical.WORKING[pid](row["evidence"])
    assert out is not None, f"{pid}: no working for {row['evidence']}"
    return out


def _h(level, text="Heading"):
    return {"level": level, "text": text}


# --- 1. TECH-20: a canonical pointing at the homepage ------------------------------------------

def test_tech20_a_page_canonicalised_to_the_homepage_fails():
    about = _page("https://x/about", canonical="https://x/", page_type="about")
    out = _run(technical.tech_20(_spec("TECH-20"), _ctx([about])))
    assert out["score"] == 0 and out["evidence"]["pages"][0]["notes"] == ["canonical differs from final URL"]


def test_tech20_the_homepage_itself_is_still_exempt():
    home = _page("https://x/", canonical="https://x/", page_type="home")
    assert _run(technical.tech_20(_spec("TECH-20"), _ctx([home])))["score"] == 100


# --- 2. TECH-10: heading-less pages count as pages with no H1 ---------------------------------

def test_tech10_a_page_without_headings_scores_as_no_h1():
    good = _page("https://x/a", headings=[_h(1), _h(2)])
    bare = _page("https://x/b", text="Readable copy with no headings at all.")
    out = _run(technical.tech_10(_spec("TECH-10"), _ctx([good, bare])))
    assert [r["score"] for r in out["evidence"]["pages"]] == [100, 60]
    assert out["score"] == 80
    text, score = _working("TECH-10", out)
    assert score == 80 and "no headings at all" in text


def test_tech10_still_scores_0_when_no_page_has_any_heading():
    out = _run(technical.tech_10(_spec("TECH-10"), _ctx([_page("https://x/a"), _page("https://x/b")])))
    assert out["score"] == 0
    assert _working("TECH-10", out)[1] == 0


# --- 3. TECH-13: text from the rendered DOM is measured against the rendered DOM --------------

def test_tech13_a_rendered_page_is_measured_against_its_rendered_html():
    shell = "<html><body><div id='root'></div></body></html>"
    rendered = "<html><body>" + "<p>" + "word " * 40 + "</p>" * 1 + "<div>" * 300 + "</body></html>"
    page = _page(text="word " * 40, html=shell, rendered_html=rendered)
    out = _run(technical.tech_13(_spec("TECH-13"), _ctx([page])))
    row = out["evidence"]["pages"][0]
    assert row["html_source"] == "rendered"
    assert row["text_to_code"] == pytest.approx(len(page.text) / len(rendered) * 100, abs=0.01)
    assert row["text_to_code"] < 100


def test_tech13_failed_pages_are_not_averaged_in():
    html = "<html><body>" + "<p>word</p>" * 10 + "</body></html>"
    good = _page("https://x/a", text="word " * 10, html=html)
    failed = _page("https://x/b", ok=False)
    out = _run(technical.tech_13(_spec("TECH-13"), _ctx([good, failed])))
    assert [r["url"] for r in out["evidence"]["pages"]] == ["https://x/a"]


# --- 4. TECH-11: the model's count is clamped to the sections actually sent ----------------------

def _tech11_ctx():
    section = "<h2>What we do</h2><p>" + "word " * 80 + "</p>"
    html = "<html><body>" + section * 3 + "</body></html>"
    page = _page(text="word " * 240, html=html, headings=[_h(2)] * 3)
    return _ctx([page])


def _tech11_with(monkeypatch, parsed, ok=True):
    async def fake_judge(prompt, **kwargs):
        return LLMResult(ok=ok, parsed=parsed, error=None if ok else "down")
    monkeypatch.setattr(technical, "judge", fake_judge)
    return _run(technical.tech_11(_spec("TECH-11"), _tech11_ctx()))


def test_tech11_a_model_count_above_the_sections_sent_cannot_exceed_100(monkeypatch):
    out = _tech11_with(monkeypatch, {"liftable_count": 12, "total": 2})
    assert out["score"] == 100
    assert out["evidence"]["llm"]["total"] == 3 and out["evidence"]["llm"]["liftable_count"] == 3
    assert _working("TECH-11", out)[1] == 100


def test_tech11_the_denominator_is_the_sections_sent_not_the_model_total(monkeypatch):
    out = _tech11_with(monkeypatch, {"liftable_count": 2, "total": 40})
    assert out["score"] == pytest.approx(2 / 3 * 100, abs=0.1)
    assert _working("TECH-11", out)[1] == pytest.approx(out["score"], abs=0.1)


@pytest.mark.parametrize("parsed", [{"liftable_count": "lots", "total": 3}, {"total": 3}, {"liftable_count": 1.5}, None])
def test_tech11_a_malformed_reply_falls_back_to_the_heuristic(monkeypatch, parsed):
    out = _tech11_with(monkeypatch, parsed)
    assert out["evidence"]["method"] == "heuristic" and "llm" not in out["evidence"]


# --- 5-7. TECH-09: the probe is always compared, and nothing is rounded early --------------------

def _rendered(url, raw, rendered):
    info = {"content_source": "rendered", "applied": True, "raw_words": raw, "rendered_words": rendered,
            "gained_words": rendered - raw}
    return _page(url, render=info)


def test_tech09_the_homepage_probe_is_compared_alongside_shell_pages():
    shell = _rendered("https://x/app", 100, 1000)     # 90% hidden
    ctx = _ctx([_page("https://x/"), shell])
    ctx.render = {"decision": "applied", "probe_url": "https://x/",
                  "probe": {"raw_words": 400, "rendered_words": 400, "gained_words": 0}}
    out = _run(technical.tech_09(_spec("TECH-09"), ctx))
    assert out["evidence"]["compared_pages"] == 2 and out["score"] == 55
    assert _working("TECH-09", out)[1] == 55


def test_tech09_the_probe_is_not_counted_twice_when_it_was_rendered_as_a_page():
    home = _rendered("https://x/", 100, 1000)
    ctx = _ctx([home])
    ctx.render = {"decision": "applied", "probe_url": "https://x/",
                  "probe": {"raw_words": 100, "rendered_words": 1000, "gained_words": 900}}
    out = _run(technical.tech_09(_spec("TECH-09"), ctx))
    assert out["evidence"]["compared_pages"] == 1 and out["score"] == 10


def test_tech09_not_needed_scores_the_probe_with_the_same_formula():
    ctx = _ctx([_page("https://x/a")])
    ctx.render = {"decision": "not_needed", "probe_url": "https://x/", "reason": "no material difference",
                  "probe": {"raw_words": 400, "rendered_words": 410, "gained_words": 10, "gain_ratio": 0.025}}
    out = _run(technical.tech_09(_spec("TECH-09"), ctx))
    assert out["score"] == pytest.approx((1 - 10 / 410) * 100, abs=0.05)
    assert out["evidence"]["summary"] == "no material difference"
    assert _working("TECH-09", out)[1] == out["score"]


def test_tech09_the_score_is_not_rounded_to_a_whole_number():
    ctx = _ctx([_rendered("https://x/a", 2, 3)])     # a third hidden
    ctx.render = {"decision": "applied"}
    out = _run(technical.tech_09(_spec("TECH-09"), ctx))
    assert out["score"] == 66.7
    assert _working("TECH-09", out)[1] == 66.7


def test_tech09_working_still_rebuilds_rows_saved_before_the_fix():
    old = {"compared_pages": 1, "pages": [{"js_only_share_pct": 33.3}], "average_js_only_share_pct": 33.3}
    assert working_technical.WORKING["TECH-09"](old)[1] == 67


# --- 8. TECH-14: only video embeds are videos; transcripts are read from visible text -----------

def test_tech14_tag_manager_and_map_iframes_are_not_videos():
    html = ("<html><body><noscript><iframe src='https://www.googletagmanager.com/ns.html?id=GTM-1'></iframe></noscript>"
            "<iframe src='https://www.google.com/maps/embed?pb=1'></iframe>"
            "<iframe src='https://www.youtube-nocookie.com/embed/abc'></iframe>"
            "<iframe data-src='https://player.vimeo.com/video/1'></iframe><video src='a.mp4'></video>"
            "<script>var transcript = null;</script></body></html>")
    page = _page(html=html, text="Watch our product tour.", images=[{"alt": "Rack enclosure front view"}])
    out = _run(technical.tech_14(_spec("TECH-14"), _ctx([page])))
    assert out["evidence"]["videos"] == 3
    assert out["evidence"]["transcript_signals"] == 0


def test_tech14_a_visible_transcript_counts():
    html = "<html><body><iframe src='https://youtu.be/abc'></iframe></body></html>"
    page = _page(html=html, text="Video transcript: welcome to the tour.")
    out = _run(technical.tech_14(_spec("TECH-14"), _ctx([page])))
    assert out["evidence"]["videos"] == 1 and out["evidence"]["transcript_signals"] == 1 and out["score"] == 100


# --- 9. failed pages are crawl findings, not content -------------------------------------------

def test_failed_pages_do_not_count_in_content_checks():
    good = _page("https://x/a", title="A clear page title for this page", headings=[_h(1)],
                 dates={"published": "2025-01-01"}, page_type="about",
                 schema=[{"@type": "Organization"}], canonical="https://x/a")
    failed = _page("https://x/b", ok=False, page_type="about")
    ctx = _ctx([good, failed])
    assert _run(technical.tech_10(_spec("TECH-10"), ctx))["score"] == 100
    assert _run(technical.tech_15(_spec("TECH-15"), ctx))["score"] == 100
    assert _run(technical.tech_17(_spec("TECH-17"), ctx))["score"] == 100
    assert _run(technical.tech_20(_spec("TECH-20"), ctx))["score"] == 100
    assert len(_run(technical.tech_22(_spec("TECH-22"), ctx))["evidence"]["pages"]) == 1


def test_tech05_coverage_counts_only_retrieved_pages(monkeypatch):
    async def fake_check(url):
        return SimpleNamespace(ok=True)
    monkeypatch.setattr(technical, "check", fake_check)
    ctx = _ctx([_page("https://x/a"), _page("https://x/gone", ok=False)], sitemap={"urls": ["https://x/a"]})
    out = _run(technical.tech_05(_spec("TECH-05"), ctx))
    assert out["evidence"]["crawled_coverage_pct"] == 100


# --- 10. TECH-17: type names match exactly --------------------------------------------------------

@pytest.mark.parametrize("schema_type,match", [
    ("OfferCatalog", False), ("ProductCollection", False), ("Offer", True), ("schema:Product", True),
    ("https://schema.org/Service", True),
])
def test_tech17_type_names_match_exactly(schema_type, match):
    page = _page(page_type="service", schema=[{"@type": schema_type}])
    out = _run(technical.tech_17(_spec("TECH-17"), _ctx([page])))
    assert out["evidence"]["pages"][0]["match"] is match


def test_tech17_organization_subtypes_still_satisfy_the_homepage():
    page = _page("https://x/", page_type="home", schema=[{"@type": "MedicalOrganization"}])
    assert _run(technical.tech_17(_spec("TECH-17"), _ctx([page])))["score"] == 100


# --- 11. TECH-21: regional prefixes name languages too ------------------------------------------

def test_tech21_one_country_in_two_languages_is_several_markets():
    pages = [_page(f"https://x/{loc}/{p}") for loc in ("en-us", "es-us") for p in ("a", "b", "c")]
    out = _run(technical.tech_21(_spec("TECH-21"), _ctx(pages)))
    assert out["evidence"]["languages"] == ["en", "es"] and out["evidence"]["several_markets"] is True
    assert out["status"] == "FAIL"


# --- 12. TECH-08: desktop widths inside min-width media blocks ----------------------------------

def test_tech08_a_wide_min_width_inside_a_desktop_media_block_is_not_fixed():
    assert not has_fixed_width_layout("@media (min-width: 1024px){.container{min-width:1200px}}")
    assert not has_fixed_width_layout("@media screen and (min-width:768px){@supports (display:grid){.a{min-width:1100px}}}")


def test_tech08_fixed_widths_outside_desktop_media_blocks_are_still_flagged():
    assert has_fixed_width_layout("@media (min-width:900px){.a{min-width:1200px}} body{min-width:1100px}")
    assert has_fixed_width_layout("@media (max-width:600px){body{min-width:1200px}}")
    assert has_fixed_width_layout("@media (min-width:480px){body{min-width:1200px}}")


# --- 13. TECH-22: uniqueness from the full descriptions ------------------------------------------

def test_tech22_working_uses_the_exact_uniqueness_counts():
    shared = "A" * 180
    pages = [_page(f"https://x/{i}", title=f"Page title number {i} here", description=shared + f" ending {i}")
             for i in range(2)]
    out = _run(technical.tech_22(_spec("TECH-22"), _ctx(pages)))
    assert out["evidence"]["uniqueness_counts"]["descriptions"] == {"unique": 2, "nonempty": 2}
    text, score = _working("TECH-22", out)
    assert score == out["score"] and "2 of 2 descriptions are unique" in text


# --- 14. TECH-07: unrounded points, stored for the working ----------------------------------------

def test_tech07_lab_points_are_averaged_unrounded_and_stored(monkeypatch):
    async def fake(ctx):
        return {"source": "lab", "metrics": {"lcp_ms": 3111.4, "tbt_ms": 333.3, "cls": 0.1333}}
    monkeypatch.setattr(technical.vitals, "measure", fake)
    out = _run(technical.tech_07(_spec("TECH-07"), _ctx([_page()])))
    points = out["evidence"]["metric_points"]
    from app.parameters.common import vital_score
    assert points["lcp_ms"] == vital_score("lcp_ms", 3111.4)
    assert out["score"] == round(sum(points.values()) / 3, 1)
    assert _working("TECH-07", out)[1] == out["score"]


def test_tech07_proxy_working_reads_the_stored_band_points(monkeypatch):
    async def fake(ctx):
        return {"source": None, "metrics": {}, "unavailable": ["no key"]}
    monkeypatch.setattr(technical.vitals, "measure", fake)
    out = _run(technical.tech_07(_spec("TECH-07"), _ctx([_page()])))
    ev = out["evidence"]
    assert "latency_score" in ev and "weight_score" in ev
    assert _working("TECH-07", out)[1] == out["score"]
    # The stored band points win over re-banding the rounded averages.
    forced = {**ev, "latency_score": ev["latency_score"] - 30}
    assert working_technical.WORKING["TECH-07"](forced)[1] < out["score"]


# --- 15. TECH-03: non-web links in llms.txt are not checked -------------------------------------

def test_tech03_mailto_tel_javascript_and_anchors_are_not_counted_as_broken(monkeypatch):
    requested = []

    async def fake_check(url):
        requested.append(url)
        return SimpleNamespace(ok=True)
    monkeypatch.setattr(technical, "check", fake_check)
    body = ("# Site\n[Mail](mailto:hi@x.com) [Call](tel:+15550100) [Top](#top) [JS](javascript:void(0)) "
            "[About](/about) [Docs](https://docs.x.com/start)\nUpdated 2025-01-01")
    llms = SimpleNamespace(ok=True, text=body, url="https://x/llms.txt", final_url="https://x/llms.txt", status_code=200, content_type="text/plain")
    out = _run(technical.tech_03(_spec("TECH-03"), _ctx([_page()], llms=llms)))
    assert requested == ["https://x/about", "https://docs.x.com/start"]
    assert out["evidence"]["links_found"] == 2 and out["evidence"]["non_web_links_skipped"] == 4
    assert out["score"] == 100
    assert _working("TECH-03", out)[1] == 100


# --- 16. TECH-06: nothing checked is unmeasured, not a crash -----------------------------------

def test_tech06_no_checkable_links_is_unknown_not_a_zero_division(monkeypatch):
    monkeypatch.setattr(technical, "internal_links", lambda ctx: ["https://x/never-crawled"])
    monkeypatch.setattr(technical, "LINK_RECHECK_LIMIT", 0)
    out = _run(technical.tech_06(_spec("TECH-06"), _ctx([_page("https://x/a")])))
    assert out["status"] == "UNKNOWN" and out["score"] is None
    assert out["evidence"]["not_checked"] == 1

"""Each technical check reads the links and pages its scope says it reads, and nothing else."""
import asyncio
from types import SimpleNamespace

from app.parameters import technical
from app.parameters.technical import _homepage_organization, _market_urls
from tests.test_tech_calc_fixes import _ctx, _page, _spec


def _run(coro):
    return asyncio.run(coro)


def test_tech03_html_page_at_llms_txt_is_not_an_llms_txt():
    html = "<!doctype html><html><body><a href='/about'>About</a> 2025-01-01 " + "x" * 200 + "</body></html>"
    llms = SimpleNamespace(ok=True, text=html, url="https://x/llms.txt", final_url="https://x/", status_code=200,
                           content_type="text/html; charset=utf-8")
    out = _run(technical.tech_03(_spec("TECH-03"), _ctx([_page()], llms=llms)))
    assert out["score"] == 0
    assert out["evidence"]["served_html_page"] is True


def test_tech21_ignores_other_sites_links():
    page = _page("https://x/a")
    page.links = [{"href": "https://microsoft.com/en-us/a"}, {"href": "https://microsoft.com/de-de/b"},
                  {"href": "https://x/about"}]
    urls = _market_urls(_ctx([page], sitemap={"urls": ["https://x/a", "https://other.com/fr-fr/x"]}))
    assert urls == ["https://x/a", "https://x/about"]


def test_tech18_and_19_skip_pages_that_failed():
    good = _page("https://x/a", schema=[{"@type": "Article", "author": {"@type": "Person", "name": "Ann"}}])
    failed = _page("https://x/b", ok=False, schema=[{"@type": "Article", "author": {"@type": "Person", "name": "Bo"}}])
    t18 = _run(technical.tech_18(_spec("TECH-18"), _ctx([good, failed])))
    assert [a["url"] for a in t18["evidence"]["author_signals"]] == ["https://x/a"]
    t19 = _run(technical.tech_19(_spec("TECH-19"), _ctx([good, failed])))
    assert t19["evidence"]["blocks"] == 1


def test_tech16_reads_organization_subtypes():
    blocks = [{"ok": True, "data": {"@type": "LocalBusiness", "name": "X", "url": "https://x", "logo": "l.png"}}]
    assert _homepage_organization(blocks)["name"] == "X"
    blocks = [{"ok": True, "data": {"@type": ["NGO"], "name": "X"}}]
    assert _homepage_organization(blocks) is not None


def test_tech11_samples_sections_across_pages(monkeypatch):
    seen = {}

    async def fake_judge(prompt, system=None):
        seen["prompt"] = prompt
        return SimpleNamespace(ok=True, parsed={"liftable_count": 1, "total": 40}, error=None)

    monkeypatch.setattr(technical, "judge", fake_judge)
    monkeypatch.setattr(technical, "heading_blocks",
                        lambda page: [{"heading": f"{page.url} h{i}", "words": 80} for i in range(30)])
    pages = [_page(f"https://x/{n}", words=500) for n in range(5)]
    out = _run(technical.tech_11(_spec("TECH-11"), _ctx(pages)))
    assert out["evidence"]["llm"]["total"] == 40
    assert out["evidence"]["llm"]["pages_sampled"] == 5

"""The browser render pass must work on whatever event loop the server runs.

uvicorn runs a SelectorEventLoop on Windows under --reload, which cannot start the Chromium
process, so every render failed with NotImplementedError and TECH-09 went unscored.
"""
import asyncio
import sys

import pytest

from app.crawler import render


def _on(loop_factory, coro):
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        return runner.run(coro)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows event-loop behaviour")
def test_a_selector_loop_hands_the_browser_to_a_proactor_thread(monkeypatch):
    seen = {}

    async def fake_live(urls, results, *args):
        seen["loop"] = type(asyncio.get_running_loop()).__name__
        for u in urls:
            results[u] = render.RenderResult(url=u, html="<html>ok</html>", final_url=u)
    monkeypatch.setattr(render, "_render_live", fake_live)
    monkeypatch.setattr(render, "render_unavailable", lambda: None)
    out = _on(asyncio.SelectorEventLoop, render.render_many(["https://example.com/"]))
    assert out["https://example.com/"].ok
    assert seen["loop"] == "ProactorEventLoop"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows event-loop behaviour")
def test_a_proactor_loop_renders_in_place(monkeypatch):
    seen = {}

    async def fake_live(urls, results, *args):
        seen["same_thread"] = True
    monkeypatch.setattr(render, "_render_live", fake_live)
    monkeypatch.setattr(render, "_on_proactor_loop", lambda make: pytest.fail("should not leave the loop"))
    monkeypatch.setattr(render, "render_unavailable", lambda: None)
    _on(asyncio.ProactorEventLoop, render.render_many(["https://example.com/"]))
    assert seen["same_thread"]

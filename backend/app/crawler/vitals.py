"""Core Web Vitals for TECH-07: real-user field data first, a measured lab run second.

TECH-07 used to approximate page speed from the crawler's own HTTP latency and the size of the
HTML -- a proxy its evidence had to apologise for, because neither number is what Google or a
visitor experiences. This module measures the real metrics instead, from the best source
available, and says which one it used:

  1. Field: Google's Chrome UX Report (CrUX) API -- the 75th-percentile LCP, INP and CLS real
     Chrome users experienced on this origin over the last 28 days. This is the data behind
     Search Console's Core Web Vitals report. Phone data is asked for first, since that is what
     Google ranks on; the all-device record is the fallback.
  2. Lab: when CrUX holds no record for the origin (too little traffic) or no key is set, a few
     key pages are loaded in headless Chromium and LCP, CLS and Total Blocking Time are read
     from the browser's own performance timeline. INP needs a real user interacting, so it
     cannot be measured in a lab; TBT is the standard lab stand-in for it, and the evidence
     names it as such. The run is throttled like Lighthouse's mobile preset (4x CPU slowdown,
     slow-4G network) so its numbers are comparable with PageSpeed Insights.

When neither is possible -- no key, no browser, or a scan of a saved site copy, whose speed on
the day it was saved cannot be re-measured -- the caller falls back to the old proxy and says so.
This is a separate browser pass from crawler/render.py on purpose: that one blocks images,
fonts and media to save time, and those are exactly what decide LCP and CLS.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
from typing import Optional

import httpx

from ..config import (
    BROWSER_UA,
    CRUX_API_KEY,
    CRUX_ENDPOINT,
    CWV_LAB_PAGES,
    CWV_LAB_SETTLE_MS,
    CWV_LAB_THROTTLE,
    CWV_LAB_TIMEOUT,
)
from .render import _loop_can_start_processes, _on_proactor_loop, render_unavailable

log = logging.getLogger("crawler.vitals")

CRUX_METRICS = {
    "largest_contentful_paint": "lcp_ms",
    "interaction_to_next_paint": "inp_ms",
    "cumulative_layout_shift": "cls",
}

# Lighthouse's mobile preset as DevTools applies it (packet-level throttling): slow 4G -- 562.5ms
# round trip, ~1.47 Mbps down, 675 Kbps up -- on a 4x slower CPU, in a Moto G Power-sized viewport.
_NETWORK = {"offline": False, "latency": 562.5, "downloadThroughput": 1474.56 * 1024 / 8,
            "uploadThroughput": 675 * 1024 / 8}
_CPU_SLOWDOWN = 4
_MOBILE = {"viewport": {"width": 412, "height": 823}, "device_scale_factor": 1.75, "is_mobile": True, "has_touch": True,
           "user_agent": ("Mozilla/5.0 (Linux; Android 11; moto g power (2022)) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")}
_DESKTOP = {"viewport": {"width": 1350, "height": 940}, "user_agent": BROWSER_UA}

# Installed before any page script runs, so buffered observers see the whole load. CLS uses the
# current definition: the largest "session window" of shifts (gaps under 1s, windows under 5s),
# ignoring shifts right after user input. Long tasks are kept raw so TBT can be cut at FCP.
_OBSERVERS = """
(() => {
  const c = window.__cwv = {lcp: null, cls: 0, fcp: null, tasks: []};
  let win = 0, first = null, last = null;
  const watch = (type, fn) => { try { new PerformanceObserver(l => l.getEntries().forEach(fn)).observe({type, buffered: true}); } catch (e) {} };
  watch('largest-contentful-paint', e => { c.lcp = e.renderTime || e.loadTime || e.startTime; });
  watch('paint', e => { if (e.name === 'first-contentful-paint') c.fcp = e.startTime; });
  watch('longtask', e => { c.tasks.push([e.startTime, e.duration]); });
  watch('layout-shift', e => {
    if (e.hadRecentInput) return;
    if (win && e.startTime - last < 1000 && e.startTime - first < 5000) { win += e.value; }
    else { win = e.value; first = e.startTime; }
    last = e.startTime;
    if (win > c.cls) c.cls = win;
  });
})();
"""

# Total Blocking Time: the part of every long task beyond 50ms, counted from first contentful paint.
_READ = """
() => {
  const c = window.__cwv || {}, nav = performance.getEntriesByType('navigation')[0];
  const fcp = c.fcp;
  const tbt = (c.tasks || []).filter(([s]) => fcp == null || s >= fcp).reduce((t, [, d]) => t + Math.max(0, d - 50), 0);
  return {lcp_ms: c.lcp, cls: c.cls ?? null, tbt_ms: tbt, fcp_ms: fcp, ttfb_ms: nav ? nav.responseStart : null};
}
"""


# ---------------------------------------------------------------------------------------------
# Field data
# ---------------------------------------------------------------------------------------------
async def crux(origin: str) -> dict:
    """CrUX p75 values for `origin`. Always returns a dict; `metrics` is empty when there is none."""
    if not CRUX_API_KEY:
        return {"metrics": {}, "error": "no Google API key configured for the Chrome UX Report API (CRUX_API_KEY)"}
    origin = origin.rstrip("/")
    last_error = None
    async with httpx.AsyncClient(timeout=20) as client:
        for form_factor in ("PHONE", None):
            body = {"origin": origin, "metrics": list(CRUX_METRICS)}
            if form_factor:
                body["formFactor"] = form_factor
            try:
                res = await client.post(CRUX_ENDPOINT, params={"key": CRUX_API_KEY}, json=body)
            except httpx.HTTPError as exc:
                return {"metrics": {}, "error": f"Chrome UX Report API unreachable: {type(exc).__name__}: {exc}"[:300]}
            if res.status_code == 404:
                last_error = "Chrome UX Report has no real-user data for this site (too little Chrome traffic)"
                continue
            if res.status_code != 200:
                detail = ""
                try:
                    detail = res.json().get("error", {}).get("message", "")
                except Exception:
                    pass
                return {"metrics": {}, "error": f"Chrome UX Report API returned HTTP {res.status_code} {detail}".strip()[:300]}
            record = res.json().get("record") or {}
            metrics = {}
            for name, key in CRUX_METRICS.items():
                p75 = (((record.get("metrics") or {}).get(name) or {}).get("percentiles") or {}).get("p75")
                if p75 is not None:
                    metrics[key] = float(p75)
            period = res.json().get("collectionPeriod") or {}
            return {"metrics": metrics, "form_factor": form_factor or "ALL", "collection_period": _period(period),
                    "origin": origin}
    return {"metrics": {}, "error": last_error}


def _period(p: dict) -> Optional[str]:
    def day(d: dict) -> str:
        return f"{d.get('year')}-{int(d.get('month') or 0):02d}-{int(d.get('day') or 0):02d}" if d else "?"
    return f"{day(p.get('firstDate'))} to {day(p.get('lastDate'))}" if p else None


# ---------------------------------------------------------------------------------------------
# Lab data
# ---------------------------------------------------------------------------------------------
def pick_pages(ctx, limit: int = CWV_LAB_PAGES) -> list[str]:
    """The homepage, then one page of each kind a buyer lands on, in crawl order."""
    ok = [p for p in ctx.pages if p.result and p.result.ok]
    chosen = [ctx.homepage.url] if ctx.homepage and ctx.homepage.result and ctx.homepage.result.ok else []
    for kind in ("service", "article", "case_study", "about"):
        page = next((p for p in ok if p.page_type == kind and p.url not in chosen), None)
        if page:
            chosen.append(page.url)
    for p in ok:
        if len(chosen) >= limit:
            break
        if p.url not in chosen:
            chosen.append(p.url)
    return chosen[:max(1, limit)]


async def lab(urls: list[str]) -> list[dict]:
    """Measure each URL in turn. One page at a time: pages loading together on one throttled CPU
    would slow each other down and the figures would describe this machine, not the site."""
    if not urls:
        return []
    unavailable = render_unavailable()
    if unavailable:
        return [{"url": u, "error": unavailable} for u in urls]
    out: list[dict] = []
    if _loop_can_start_processes():
        await _measure(urls, out)
    else:
        # Same Windows constraint as the render pass: see crawler/render.py.
        await asyncio.to_thread(_on_proactor_loop, lambda: _measure(urls, out))
    return out


async def _measure(urls: list[str], out: list[dict]) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        except Exception as exc:
            reason = f"could not launch a browser: {type(exc).__name__}: {exc}"[:300]
            out.extend({"url": u, "error": reason} for u in urls)
            return
        try:
            for url in urls:
                out.append(await _one(browser, url))
        finally:
            await browser.close()


async def _one(browser, url: str) -> dict:
    context = await browser.new_context(ignore_https_errors=True, **(_MOBILE if CWV_LAB_THROTTLE else _DESKTOP))
    try:
        page = await context.new_page()
        await page.add_init_script(_OBSERVERS)
        if CWV_LAB_THROTTLE:
            cdp = await context.new_cdp_session(page)
            await cdp.send("Network.enable")
            await cdp.send("Network.emulateNetworkConditions", _NETWORK)
            await cdp.send("Emulation.setCPUThrottlingRate", {"rate": _CPU_SLOWDOWN})
        response = await page.goto(url, wait_until="load", timeout=CWV_LAB_TIMEOUT * 1000)
        await page.wait_for_timeout(CWV_LAB_SETTLE_MS)
        values = await page.evaluate(_READ)
        status = response.status if response else None
        if status and status >= 400:
            return {"url": url, "status": status, "error": f"page returned HTTP {status}"}
        return {"url": url, "status": status, **{k: (round(v, 3) if k == "cls" else round(v))
                                                 for k, v in values.items() if v is not None}}
    except Exception as exc:
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        await context.close()


# ---------------------------------------------------------------------------------------------
# Both
# ---------------------------------------------------------------------------------------------
async def measure(ctx) -> dict:
    """The site's Core Web Vitals from the best available source.

    Returns {"source": "field" | "lab" | None, "metrics": {...}, ...}. `metrics` holds lcp_ms and
    cls, plus inp_ms (field) or tbt_ms (lab). `source` None means neither could be measured, and
    `unavailable` lists why, for the evidence.
    """
    unavailable: list[str] = []
    field = await crux(ctx.origin)
    if {"lcp_ms", "cls"} <= set(field["metrics"]) and ("inp_ms" in field["metrics"]):
        return {"source": "field", **field}
    unavailable.append(field.get("error") or "Chrome UX Report has only partial data for this site")

    if getattr(ctx, "snapshot", None):
        unavailable.append("lab run skipped: this scan reads a saved copy of the site, and its speed "
                           "on the day it was saved cannot be re-measured")
        return {"source": None, "metrics": {}, "unavailable": unavailable}

    pages = await lab(pick_pages(ctx))
    measured = [p for p in pages if "lcp_ms" in p and "cls" in p and "tbt_ms" in p]
    if not measured:
        unavailable.append("lab run failed: " + "; ".join(sorted({p.get("error", "no metrics") for p in pages})))
        return {"source": None, "metrics": {}, "unavailable": unavailable, "lab_pages": pages}
    # The median page, per metric: with a handful of pages one outlier should not decide the site.
    metrics = {k: statistics.median(p[k] for p in measured) for k in ("lcp_ms", "cls", "tbt_ms")}
    return {"source": "lab", "metrics": metrics, "lab_pages": pages, "throttled": CWV_LAB_THROTTLE,
            "field_unavailable": unavailable[0]}

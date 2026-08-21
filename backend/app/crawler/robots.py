from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

from protego import Protego

from .http import FetchResult, fetch


AI_BOTS = ["GPTBot", "ChatGPT-User", "ClaudeBot", "Google-Extended", "PerplexityBot"]


@dataclass
class RobotsData:
    url: str
    result: FetchResult
    raw: str
    parser: Protego | None
    sitemap_urls: list[str]


async def fetch_robots(origin: str) -> RobotsData:
    url = urljoin(origin.rstrip("/") + "/", "robots.txt")
    result = await fetch(url)
    raw = result.text if result.ok else ""
    parser = None
    sitemaps: list[str] = []
    if result.ok and raw.strip():
        try:
            parser = Protego.parse(raw)
        except Exception:
            parser = None
        for line in raw.splitlines():
            if line.lower().startswith("sitemap:"):
                loc = line.split(":", 1)[1].strip()
                if loc:
                    sitemaps.append(loc)
    return RobotsData(url=url, result=result, raw=raw, parser=parser, sitemap_urls=sitemaps)


def bot_decision(robots: RobotsData, bot: str, path: str = "/") -> dict:
    if not robots.result.ok:
        return {"name": bot, "decision": "UNKNOWN", "rule": "robots.txt unavailable"}
    if robots.parser is None:
        if not robots.raw.strip():
            return {"name": bot, "decision": "ALLOWED", "rule": "empty robots.txt (allow all)"}
        return {"name": bot, "decision": "UNKNOWN", "rule": "robots.txt parse error"}
    allowed = robots.parser.can_fetch(path, bot)
    wildcard = robots.parser.can_fetch(path, "*")
    decision = "ALLOWED" if allowed else "BLOCKED"
    rule = f"User-agent: {bot} evaluated for {path}; wildcard={'allow' if wildcard else 'disallow'}"
    return {"name": bot, "decision": decision, "rule": rule, "allowed": bool(allowed)}

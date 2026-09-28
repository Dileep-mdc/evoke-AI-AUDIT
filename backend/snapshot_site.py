"""Save a site to disk so the audit app can score it from the folder instead of the live web.

    python snapshot_site.py https://www.evoketechnologies.com/
    python snapshot_site.py https://www.nvent.com/ --pages urls.csv

With --pages, exactly the listed pages are saved (a .csv with a "url" column, or one URL per
line) plus the homepage, which the homepage-only checks need; links are not followed. Without
it, the whole site is saved.

Writes <project root>/<host>/snapshot/ (e.g. AI Audit/www.evoketechnologies.com/snapshot/) (manifest.json + gzipped responses). From then on, every
scan of that host in the app reads the saved copy -- see app/crawler/snapshot.py. Run it again
to refresh the copy; delete the snapshot/ folder to go back to live scans.

Everything is fetched with the app's own fetch(), user-agents and redirect handling, so the saved
responses are exactly what a live scan would have received:
  * the homepage, robots.txt, llms.txt and every sitemap file;
  * every page: the sitemap's, the folder's _index.csv if present, and every same-host page
    linked from those (followed until nothing new turns up) -- or only the --pages list;
  * every other internal link (PDFs, images, broken URLs) as status + redirect hops only,
    which is all TECH-05 and TECH-06 read from them;
  * every page again as a browser renders it (Playwright), so content a site assembles with
    JavaScript -- product grids, tabs -- is scored from the folder, and TECH-09 can compare.
"""
import argparse
import asyncio
import csv
import os
import sys
import time
from urllib.parse import urldefrag, urljoin, urlparse

os.environ["USE_SNAPSHOTS"] = "false"  # read the live site, never an older snapshot

from lxml import html as lh  # noqa: E402

from app.config import BROWSER_UA, MAX_PAGES, SNAPSHOT_ROOT  # noqa: E402
from app.crawler import robots as robots_mod, sitemap as sitemap_mod  # noqa: E402
from app.crawler.http import fetch, is_html, looks_like_document, normalize_url, origin_of, same_host  # noqa: E402
from app.crawler.render import render_many  # noqa: E402
from app.crawler.sitemap import locale_of  # noqa: E402
from app.crawler.snapshot import Recorder  # noqa: E402
from app.parameters.technical import _LLMS_LINK_RE as LLMS_LINK_RE  # noqa: E402

CONCURRENCY = 4          # the CDN in front of many sites throttles bursts; this stays under it
STATUS_CONCURRENCY = 8   # HEAD requests for link/sitemap status carry no body
ATTEMPTS = 4
RETRY_STATUS = {None, 403, 429, 500, 502, 503, 504}


async def patient_fetch(url: str, **kw):
    """fetch(), retried with backoff when the answer looks like throttling rather than fact."""
    res = None
    for attempt in range(ATTEMPTS):
        res = await fetch(url, **kw)
        if res.status_code not in RETRY_STATUS:
            return res
        await asyncio.sleep(1.5 * (attempt + 1))
    return res


def page_links(res) -> list[str]:
    try:
        tree = lh.fromstring(res.content)
    except Exception:
        return []
    return [urldefrag(urljoin(res.final_url, h.strip()))[0] for h in tree.xpath("//a/@href") if h.strip()]


async def settle_pages(rec: Recorder, pages: list[str]) -> list[str]:
    """One entry per real page. A URL that redirects (http:// links, old slugs) stays recorded
    for the broken-link check, but the page is its destination, fetched directly -- the way the
    sitemap lists it -- so its redirect hops are not charged to the page itself."""
    direct: dict[str, str] = {}
    for u in pages:
        if rec.entries[u]["final_url"].rstrip("/") == u.rstrip("/"):
            direct.setdefault(u.rstrip("/"), u)
    targets = {rec.entries[u]["final_url"] for u in pages} - set(direct.values())
    targets = [t for t in targets if t.rstrip("/") not in direct]
    for t in targets:
        res = await patient_fetch(t, user_agent=BROWSER_UA)
        rec.add(t, res)
        if res.ok and is_html(res) and res.final_url.rstrip("/") == t.rstrip("/"):
            direct.setdefault(t.rstrip("/"), t)
    return list(direct.values())


def read_page_list(path: str) -> list[str]:
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    rows = list(csv.DictReader(text.splitlines()))
    if rows and any(k and k.strip().lower() == "url" for k in rows[0]):
        key = next(k for k in rows[0] if k and k.strip().lower() == "url")
        urls = [r[key].strip() for r in rows if (r.get(key) or "").strip()]
    else:
        urls = [line.strip() for line in text.splitlines() if line.strip().lower().startswith("http")]
    return list(dict.fromkeys(urls))


async def status_only(rec: Recorder, urls: list[str]) -> None:
    """Record status + redirect hops (no body) for each URL: all TECH-05, TECH-06 and TECH-03
    read from a linked URL. HEAD, since thousands of full downloads would make a snapshot take
    an hour; a server that does not implement HEAD is asked again with GET."""
    sem = asyncio.Semaphore(STATUS_CONCURRENCY)

    async def one(u: str) -> None:
        async with sem:
            res = await patient_fetch(u, method="HEAD")
            if res.status_code in (405, 501):
                res = await patient_fetch(u)
        rec.add(u, res, keep_body=False)

    await asyncio.gather(*(one(u) for u in urls))


def missing_link_statuses(rec: Recorder, origin: str) -> list[str]:
    """Internal links the scan will check but the snapshot has no response for: links that
    exist only in a page's rendered copy, and the links llms.txt lists."""
    from app.crawler.snapshot import Snapshot
    snap = Snapshot(rec.folder, {"host": rec.host, "pages": rec.pages, "entries": rec.entries})
    wanted: set[str] = set()
    for page in rec.pages:
        for html in (snap.rendered(page),):
            if html:
                tree = lh.fromstring(html)
                for h in tree.xpath("//a/@href"):
                    u = urldefrag(urljoin(page, h.strip()))[0]
                    if u and same_host(u, origin):
                        wanted.add(u)
    llms = snap.result(urljoin(origin, "/llms.txt"))
    if llms is not None and llms.ok:
        for link in LLMS_LINK_RE.findall(llms.text):
            wanted.add(link if link.startswith("http") else origin.rstrip("/") + "/" + link.lstrip("/"))
    return sorted(u for u in wanted if snap.entry(u) is None)


async def add_renders(rec: Recorder) -> int:
    """Save each page as a browser shows it. Returns how many rendered."""
    results = await render_many(rec.pages)
    for url, res in results.items():
        if res.ok:
            rec.add_rendered(url, res.html)
        else:
            print(f"  not rendered: {url} ({res.error})")
    return sum(1 for r in results.values() if r.ok)


async def main(url: str, pages_file: str | None = None) -> int:
    started = time.perf_counter()
    home = normalize_url(url)
    origin = origin_of(home)
    host = urlparse(origin).netloc.lower()
    listed = read_page_list(pages_file) if pages_file else []
    if pages_file and not listed:
        sys.exit(f"No URLs found in {pages_file}")
    rec = Recorder(host)
    print(f"Saving {origin} -> {rec.folder}" + (f" ({len(listed)} listed pages)" if listed else ""))

    async def recorded(u, **kw):
        res = await patient_fetch(u, **kw)
        rec.add(u, res)
        return res

    # robots.txt and the sitemap files, through the app's own parsers so nothing is missed.
    robots_mod.fetch = recorded
    sitemap_mod.fetch = recorded
    robots = await robots_mod.fetch_robots(origin)
    # The homepage first, for the locale it redirects to: a multi-language sitemap is read for
    # that locale first (see sitemap.fetch_sitemaps), exactly as a scan will read it.
    home_res = await recorded(origin.rstrip("/") + "/", user_agent=BROWSER_UA)
    locale = locale_of(home_res.final_url) or (locale_of(listed[0]) if listed else "")
    sitemap = await sitemap_mod.fetch_sitemaps(origin, robots.sitemap_urls, prefer=locale)
    await recorded(urljoin(origin, "/llms.txt"))
    print(f"robots.txt {robots.result.status_code}, sitemap {len(sitemap.get('urls', []))} URLs")

    follow = not listed
    if listed:
        seeds = [origin.rstrip("/") + "/"] + listed
    else:
        seeds = [origin.rstrip("/") + "/", home] + list(sitemap.get("urls", []))
        index = SNAPSHOT_ROOT / host / "_index.csv"
        if index.exists():
            seeds += [r["URL"] for r in csv.DictReader(open(index, encoding="utf-8")) if r.get("File")]

    queued: set[str] = set()
    queue: list[str] = []
    other_links: set[str] = set()

    def enqueue(u: str) -> None:
        u = urldefrag(u)[0]
        if not u or not same_host(u, origin):
            return
        # Query-string URLs (site search "?s=...", tracking parameters) are not pages of their
        # own: search results have path "/" and would be scored as extra homepages.
        if not looks_like_document(u) or urlparse(u).query:
            other_links.add(u)
            return
        key = u.rstrip("/")  # "/about" and "/about/" are one page
        if key not in queued and len(queued) < MAX_PAGES:
            queued.add(key)
            queue.append(u)

    for s in seeds:
        enqueue(s)

    def note_link(u: str) -> None:
        """A link on a listed page, when links are not followed: status only, for TECH-06."""
        u = urldefrag(u)[0]
        if u and same_host(u, origin) and u.rstrip("/") not in queued:
            other_links.add(u)

    sem = asyncio.Semaphore(CONCURRENCY)
    pages: list[str] = []

    async def grab(u: str) -> None:
        async with sem:
            res = await patient_fetch(u, user_agent=BROWSER_UA)
        rec.add(u, res)
        if res.ok and is_html(res):
            pages.append(u)
            for link in page_links(res):
                (enqueue if follow else note_link)(link)
        elif not res.ok:
            print(f"  {res.status_code or res.error}  {u}")

    done = 0
    while done < len(queue):
        batch = queue[done:]
        done = len(queue)
        await asyncio.gather(*(grab(u) for u in batch))
        print(f"pages saved: {len(pages)} (queued {len(queue)})")

    # Everything else an internal link points at, for the broken-link and sitemap checks.
    rest = sorted(u for u in other_links if rec.entries.get(u) is None)
    rest += [u for u in sitemap.get("urls", []) if urldefrag(u)[0] not in rec.entries and u not in rest]

    print(f"checking {len(rest)} linked and sitemap URLs (status only)")
    await status_only(rec, rest)

    order = {u: i for i, u in enumerate(queue)}
    rec.pages = sorted(await settle_pages(rec, pages), key=lambda u: order.get(u, len(order)))
    print(f"rendering {len(rec.pages)} pages in a browser")
    rendered = await add_renders(rec)
    print(f"rendered: {rendered}/{len(rec.pages)}")
    extra = missing_link_statuses(rec, origin)
    print(f"checking {len(extra)} more URLs linked from rendered pages and llms.txt (status only)")
    await status_only(rec, extra)
    path = rec.save()
    print(f"\nDone in {time.perf_counter() - started:.0f}s: {len(rec.pages)} pages, "
          f"{len(rec.entries)} responses saved -> {path.parent}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Save a site so the audit app can score it from disk.")
    parser.add_argument("url", help="the site's homepage, e.g. https://www.nvent.com/")
    parser.add_argument("--pages", help="save only these pages: a .csv with a 'url' column, or one URL per line")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.url, args.pages)))

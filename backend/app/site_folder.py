"""The readable copy of every scanned site, written beside the project like a saved folder:

    <SITE_FOLDERS_ROOT>/www.example.com/
        _index.csv               URL, HTTP Status, Title, File -- one row per page the scan read
        <path_of_page>.txt       one per HTML page: URL / Title / Meta description, then
                                   ===== MAIN CONTENT =====
                                   ===== HEADER, MENUS, POPUPS & FOOTER =====
                                   ===== LINKS ON THIS PAGE (n) =====
        <company>_links.xlsx     Summary, All Links, Pages Crawled, Link Occurrences, External Domains
        all_links.txt            every unique link found on the site, one per line
        _site.json               company name, scan id, timings and counts

scan_output.py keeps the machine copy (JSON + gzipped HTML) in the data directory; this is the
copy a person opens. The format is the one the hand-saved folders already used, so _index.csv
is also what snapshot_site.py seeds from.

Reading the text is the costly part -- a full DOM walk per page -- so it runs in the crawl's
extraction worker processes (crawler/extract.py), and the files are written by a thread pool.
Nothing here may fail a scan: every failure is logged and the scan carries on.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urldefrag, urljoin, urlparse

from .config import OUTPUT_WRITE_WORKERS, SAVE_SITE_FOLDER, SITE_FOLDERS_ROOT
from .crawler.extract import _pool_for
from .crawler.http import is_html

log = logging.getLogger("site_folder")

MAIN_HEADER = "===== MAIN CONTENT ====="
CHROME_HEADER = "===== HEADER, MENUS, POPUPS & FOOTER ====="
INDEX_NAME = "_index.csv"
# Excel's hard row limit, less the header row.
_XLSX_MAX_ROWS = 1_048_575
# Page file names are cut to fit <root>/<host>/<name>.txt inside Windows' 260-character path
# limit, which a deep OneDrive folder reaches quickly; never shorter than _MIN_NAME.
_MAX_PATH = 259
_MAX_NAME = 110
_MIN_NAME = 24

_DROP_TAGS = ("head", "script", "style", "noscript", "template", "svg", "iframe", "object", "canvas")
_INLINE = {"a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "font", "i", "kbd",
           "mark", "q", "s", "samp", "small", "span", "strong", "sub", "sup", "time", "u", "var", "wbr",
           "label", "img", "input", "select", "option"}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_CHROME_TAGS = {"header", "nav", "footer", "aside"}
_CHROME_ROLES = {"banner", "navigation", "contentinfo", "dialog", "alertdialog"}
_POPUP_RE = re.compile(r"cookie|consent|gdpr|modal|popup|pop-up|lightbox|offcanvas|off-canvas", re.I)
_WS = re.compile(r"\s+")


def _holds_page(el) -> bool:
    return bool(el.xpath("boolean(.//main | .//h1 | .//*[@role='main'])"))


# --- Reading one page (runs in a worker process) -----------------------------------------

def _is_chrome(el, tag: str) -> bool:
    if tag in _CHROME_TAGS:
        return True
    attrib = el.attrib
    if not attrib:
        return False
    marked = ((attrib.get("role") or "").lower() in _CHROME_ROLES or attrib.get("aria-modal") == "true"
              or _POPUP_RE.search(f"{attrib.get('class') or ''} {attrib.get('id') or ''}"))
    # A role or class name is only a hint. Drupal wraps the entire page in
    # <div class="dialog-off-canvas-main-canvas">, so anything holding the page's own <main> or
    # <h1> is the page, not a popup, whatever it is called.
    return bool(marked) and not _holds_page(el)


def read_page(html: str, base_url: str) -> dict:
    """{"main": [...lines], "chrome": [...lines], "links": [(href, text, rel, area)]}.

    Lines are the visible text, one per block, headings marked '#'..'######'. "chrome" is the
    text inside the header, menus, popups and footer; "main" is everything else. One walk of
    the tree does all three, which matters on rendered pages that run past a megabyte.

    Top-level so the worker processes can import it. Never raises: a document lxml cannot
    parse comes back empty, and the page is still listed in the index.
    """
    out = {"main": [], "chrome": [], "links": []}
    if not html:
        return out
    try:
        from lxml import etree

        # Bytes with a stated encoding: lxml refuses a str whose markup declares its own.
        root = etree.fromstring(html.encode("utf-8", "replace"),
                                etree.HTMLParser(encoding="utf-8", remove_comments=True, remove_pis=True))
    except Exception:
        return out
    if root is None:
        return out
    try:
        base_el = root.find(".//base[@href]")
        base = urljoin(base_url, base_el.get("href")) if base_el is not None else base_url
        etree.strip_elements(root, *_DROP_TAGS, with_tail=False)
        body = root.find("body")
        body = body if body is not None else root

        main, chrome, links = out["main"], out["chrome"], out["links"]
        buf: list[str] = []
        state = {"prefix": "", "into": main}
        joined: dict[str, Optional[str]] = {}  # menus repeat the same hrefs on every page

        def flush() -> None:
            text = _WS.sub(" ", "".join(buf)).strip()
            buf.clear()
            if text:
                state["into"].append(state["prefix"] + text)

        def link(el, in_chrome: bool) -> None:
            href = (el.get("href") or "").strip()
            if not href or href == "#" or href[:11].lower().startswith(("javascript:", "data:")):
                return
            if href not in joined:
                try:
                    url = urljoin(base, href)
                    joined[href] = urldefrag(url)[0] if url.startswith(("http://", "https://")) else url
                except ValueError:
                    joined[href] = None
            url = joined[href]
            if not url:
                return
            text = _WS.sub(" ", "".join(el.itertext())).strip() or (el.get("aria-label") or el.get("title") or "").strip()
            if not text:
                img = el.find(".//img[@alt]")
                text = (img.get("alt") or "").strip() if img is not None else ""
            links.append((url, text, (el.get("rel") or "").strip(), "chrome" if in_chrome else "main"))

        def walk(el, in_chrome: bool) -> None:
            tag = el.tag
            if not isinstance(tag, str):
                return
            tag = tag.lower()
            # Outermost chrome block only: a nav inside a header is already part of the header.
            enters = not in_chrome and _is_chrome(el, tag)
            if enters:
                flush()
                state["into"] = chrome
            in_chrome = in_chrome or enters
            if tag == "a":
                link(el, in_chrome)
            block = tag not in _INLINE
            if block or tag == "br":
                flush()
            level = _HEADINGS.get(tag)
            if level:
                state["prefix"] = "#" * level + " "
            if el.text:
                buf.append(el.text)
            for child in el:
                walk(child, in_chrome)
                if child.tail:
                    buf.append(child.tail)
            if block:
                flush()
            if level:
                state["prefix"] = ""
            if enters:
                flush()
                state["into"] = main

        walk(body, False)
        flush()
    except RecursionError:
        out["main"] = [_WS.sub(" ", "".join(root.itertext())).strip()]
    except Exception:
        log.debug("site folder: could not read %s", base_url, exc_info=True)
    return out


async def _read_all(jobs: list[tuple[str, str]]) -> list[dict]:
    """read_page for every (html, base_url), in the extraction worker processes when it pays."""
    pool = _pool_for(len(jobs))
    if pool is None:
        return await asyncio.to_thread(lambda: [read_page(h, b) for h, b in jobs])
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, lambda: list(pool.map(read_page, [h for h, _ in jobs], [b for _, b in jobs], chunksize=8)))
    except Exception as exc:
        log.warning("site folder: worker pool failed (%s); reading pages in-process", exc)
        return await asyncio.to_thread(lambda: [read_page(h, b) for h, b in jobs])


# --- Naming --------------------------------------------------------------------------

def _folder_name(host: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", host.lower()).strip(". ") or "site"


def _name_budget(folder: Path) -> int:
    """Longest stem that keeps <folder>/<stem>_NN.txt inside the Windows path limit.

    Measured on the resolved path: a short 8.3 spelling (C:\\Users\\DMUPPA~1\\...) writes fine
    but is longer once expanded, and the file then cannot be opened by its full name.
    """
    try:
        full = str(folder.resolve())
    except OSError:
        full = str(folder)
    return max(_MIN_NAME, min(_MAX_NAME, _MAX_PATH - len(full) - len("\\_999.txt") - 4))


def _file_name(url: str, seen: set[str], budget: int = _MAX_NAME) -> str:
    """'/about-us/company-overview/' -> 'about_us_company_overview.txt', '/' -> 'home.txt'."""
    parsed = urlparse(url)
    stem = re.sub(r"[^a-z0-9]+", "_", (parsed.path or "/").lower()).strip("_") or "home"
    if parsed.query:
        stem += "_" + re.sub(r"[^a-z0-9]+", "_", parsed.query.lower()).strip("_")
    digest = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:8]
    if len(stem) > budget:
        stem = f"{stem[:budget - 9]}_{digest}"
    name = stem
    n = 2
    while name in seen:
        name = f"{stem}_{n}"
        n += 1
    seen.add(name)
    return name + ".txt"


def _link_type(url: str, host: str) -> str:
    scheme = url.split(":", 1)[0].lower()
    if scheme in ("mailto", "tel"):
        return scheme
    if scheme not in ("http", "https"):
        return "other"
    link_host = (urlparse(url).hostname or "").lower()
    return "internal" if link_host.removeprefix("www.") == host.removeprefix("www.") else "external"


def _company_slug(company: str, host: str) -> str:
    name = (company or host.removeprefix("www.").split(".")[0]).lower()
    return re.sub(r"[^a-z0-9]+", "", name) or "site"


# --- Writing --------------------------------------------------------------------------

def _page_text(p, title: str, meta: str, read: dict, links: list[tuple]) -> str:
    out = [f"URL: {p.url}", f"Title: {title}", f"Meta description: {meta}", "", MAIN_HEADER]
    out += read["main"] or ["(no main content found)"]
    out += ["", CHROME_HEADER]
    out += read["chrome"] or ["(none found)"]
    out += ["", f"===== LINKS ON THIS PAGE ({len(links)}) ====="]
    for url, text, _rel, area in links:
        where = " [header/menu/footer]" if area == "chrome" else ""
        out.append(f"{text or '(no text)'} -> {url}{where}")
    return "\n".join(out) + "\n"


def _clear_previous(folder: Path) -> None:
    """Delete the page files the last export listed, and nothing else in the folder."""
    index = folder / INDEX_NAME
    if not index.exists():
        return
    try:
        with index.open(encoding="utf-8", newline="") as fh:
            names = [r.get("File") or "" for r in csv.DictReader(fh)]
    except Exception:
        return
    for name in names:
        if name.endswith(".txt") and "/" not in name and "\\" not in name:
            (folder / name).unlink(missing_ok=True)


def _write_workbook(path: Path, summary: list[tuple], pages: list[tuple], occurrences: list[tuple],
                    unique: dict[str, dict]) -> None:
    """The links workbook. xlsxwriter in constant-memory mode: measured 2-3x faster than
    openpyxl on the 264k-row Link Occurrences sheet of a 1,000-page site. URLs are written as
    plain text -- auto-linking them is slow and Excel caps a sheet at 65,530 hyperlinks."""
    import xlsxwriter
    from xlsxwriter.exceptions import FileCreateError

    wb = xlsxwriter.Workbook(str(path), {"constant_memory": True, "strings_to_urls": False,
                                         "strings_to_numbers": False, "strings_to_formulas": False})
    bold = wb.add_format({"bold": True})

    def sheet(title: str, header: list[str], rows, widths: tuple = ()):
        ws = wb.add_worksheet(title)
        ws.write_row(0, 0, header, bold)
        for col, width in enumerate(widths):
            ws.set_column(col, col, width)
        for r, row in enumerate(rows, 1):
            if r > _XLSX_MAX_ROWS:
                break
            ws.write_row(r, 0, row)

    sheet("Summary", ["Metric", "Value"], summary, (30, 70))
    sheet("All Links", ["#", "URL", "Type", "Domain", "Anchor Text (sample)", "Times Linked",
                        "Found On (sample page)", "Nofollow"],
          ((i, url, u["type"], u["domain"], " | ".join(sorted(u["texts"])[:3]) or None, u["count"],
            u["first_page"], "yes" if u["nofollow"] else None)
           for i, (url, u) in enumerate(sorted(unique.items()), 1)),
          (7, 70, 10, 28, 40, 12, 60, 9))
    sheet("Pages Crawled", ["#", "Page URL", "HTTP Status", "Page Title", "Content Type", "Words", "Links", "File"],
          ((i, *row) for i, row in enumerate(pages, 1)), (7, 70, 11, 50, 26, 9, 8, 50))
    sheet("Link Occurrences", ["#", "Source Page", "Link URL", "Anchor Text", "Type", "Rel", "Nofollow", "Area"],
          ((i, *row) for i, row in enumerate(occurrences, 1)), (8, 60, 60, 36, 10, 18, 9, 18))
    domains = Counter(u["domain"] for u in unique.values() if u["type"] == "external")
    sheet("External Domains", ["#", "External Domain", "Unique URLs Linked"],
          ((i, d, n) for i, (d, n) in enumerate(sorted(domains.items(), key=lambda kv: (-kv[1], kv[0])), 1)),
          (7, 40, 18))
    try:
        wb.close()
    except FileCreateError as exc:
        raise PermissionError(str(exc)) from None


def _write(folder: Path, ctx, reads: list[Optional[dict]], meta: dict) -> Path:
    host = meta["host"]
    folder.mkdir(parents=True, exist_ok=True)
    _clear_previous(folder)

    seen: set[str] = set()
    budget = _name_budget(folder)
    index_rows: list[list] = []
    page_rows: list[tuple] = []
    occurrences: list[tuple] = []
    unique: dict[str, dict] = {}
    files: list[tuple[Path, str]] = []

    for p, read in zip(ctx.pages or [], reads):
        res = p.result
        status = res.status_code if res else None
        title = (p.title or "").strip()
        page_links = read["links"] if read else []
        name = ""
        if read is not None:
            name = _file_name(p.url, seen, budget)
            files.append((folder / name, _page_text(p, title, (p.meta_description or "").strip(), read, page_links)))
        index_rows.append([p.url, status if status is not None else "", title, name])
        page_rows.append((p.url, status, title, res.content_type if res else "", p.word_count,
                          len(page_links), name or None))
        for url, text, rel, area in page_links:
            # Classified once per unique URL: a menu link recurs on every page, 264k times in all
            # on a 1,000-page site, and parsing each occurrence cost seconds.
            u = unique.get(url)
            if u is None:
                kind = _link_type(url, host)
                domain = (urlparse(url).hostname or "").lower() if kind in ("internal", "external") else ""
                u = unique[url] = {"type": kind, "domain": domain.removeprefix("www."), "texts": set(),
                                   "count": 0, "first_page": p.url, "nofollow": False}
            nofollow = "nofollow" in rel.lower()
            occurrences.append((p.url, url, text or None, u["type"], rel or None, "yes" if nofollow else None,
                                "header/menu/footer" if area == "chrome" else "main"))
            u["count"] += 1
            u["nofollow"] = u["nofollow"] or nofollow
            if text and len(u["texts"]) < 10:
                u["texts"].add(text)

    by_type = Counter(u["type"] for u in unique.values())
    summary = [
        ("Site", meta["site"]),
        ("Company", meta["company_name"]),
        ("Scan ID", meta["scan_id"]),
        ("Saved at", meta["saved_at"]),
        ("Scraping time (seconds)", meta["scraping_seconds"]),
        ("Pages crawled", len(index_rows)),
        ("Page text files", len(files)),
        ("Unique links found", len(unique)),
        ("Total link occurrences", len(occurrences)),
        ("Internal links (unique)", by_type.get("internal", 0)),
        ("External links (unique)", by_type.get("external", 0)),
        ("Email / phone links (unique)", by_type.get("mailto", 0) + by_type.get("tel", 0)),
    ]
    xlsx = folder / f"{_company_slug(meta['company_name'], host)}_links.xlsx"
    meta["links_workbook"] = xlsx.name

    def write_one(job: tuple[Path, str]) -> Optional[str]:
        try:
            job[0].write_text(job[1], encoding="utf-8")
            return None
        except OSError as exc:
            return f"{job[0].name}: {exc}"

    # One unwritable page (a file locked by sync, a path the OS refuses) costs that page only.
    with ThreadPoolExecutor(max_workers=max(1, OUTPUT_WRITE_WORKERS)) as writers:
        failed = [e for e in writers.map(write_one, files) if e]
    if failed:
        log.warning("site folder: %d page file(s) not written, e.g. %s", len(failed), failed[0])
        unwritten = {e.split(":", 1)[0] for e in failed}
        for row in index_rows:
            if row[3] in unwritten:
                row[3] = ""

    with (folder / INDEX_NAME).open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["URL", "HTTP Status", "Title", "File"])
        w.writerows(index_rows)

    (folder / "all_links.txt").write_text("\n".join(sorted(unique)) + "\n", encoding="utf-8")

    meta.update({
        "pages_crawled": len(index_rows),
        "page_files_written": len(files) - len(failed),
        "unique_links": len(unique),
        "total_link_occurrences": len(occurrences),
        "unique_links_by_type": dict(by_type),
    })
    (folder / "_site.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # The workbook is the slowest single file: 264k link rows on a 1,000-page site take ~7s in
    # xlsxwriter (18s in openpyxl), plus shipping the rows to a worker. Nothing in the scan reads
    # it, so on a big site it finishes in a worker process while scoring starts; a small site's
    # is written here, where it costs well under a second.
    args = (xlsx, summary, page_rows, occurrences, unique)
    pool = _pool_for(len(occurrences) // 1000)
    if pool is not None:
        try:
            book = pool.submit(_write_workbook, *args)
            _pending.add(book)
            book.add_done_callback(lambda f: _workbook_done(f, xlsx))
            return folder
        except Exception as exc:
            log.warning("site folder: workbook worker unavailable (%s); writing it here", exc)
    try:
        _write_workbook(*args)
    except PermissionError:
        log.warning("site folder: %s is open in another program; links workbook not refreshed", xlsx)
    return folder


# Workbooks still being written in a worker process. Kept so they are not collected mid-write
# and so a caller that must have the file (a test, a script) can wait: see wait_for_workbooks().
_pending: set = set()


def _workbook_done(future, xlsx: Path) -> None:
    _pending.discard(future)
    if future.cancelled():
        return
    exc = future.exception()
    if isinstance(exc, PermissionError):
        # Open in Excel: the rest of the folder is already written and stays.
        log.warning("site folder: %s is open in another program; links workbook not refreshed", xlsx)
    elif exc is not None:
        log.warning("site folder: links workbook %s not written: %s", xlsx, exc)


def wait_for_workbooks(timeout: Optional[float] = None) -> None:
    from concurrent.futures import wait

    if _pending:
        wait(list(_pending), timeout=timeout)


async def save(scan_id: str, ctx, crawl_ms: Optional[int] = None) -> Optional[Path]:
    """Write the site folder for this scan. Returns its path, or None if it was skipped or failed."""
    if not SAVE_SITE_FOLDER or not ctx.pages:
        return None
    if not any(p.result and p.result.ok for p in ctx.pages):
        # Nothing was read -- tcs.com answered every request with 403 -- so a folder would hold
        # only the refusal page. Better no folder than one that looks like a saved site.
        log.info("Scan %s: no page of %s could be read; no site folder written", scan_id, ctx.domain)
        return None
    try:
        host = (urlparse(ctx.origin or ctx.normalized_url or "").hostname or ctx.domain or "").lower()
        jobs, positions = [], []
        for i, p in enumerate(ctx.pages):
            html = getattr(p, "rendered_html", "") or ((p.result.text or "") if p.result and is_html(p.result) else "")
            if html:
                positions.append(i)
                jobs.append((html, p.result.final_url or p.url))
        reads: list[Optional[dict]] = [None] * len(ctx.pages)
        for i, read in zip(positions, await _read_all(jobs)):
            reads[i] = read
        meta = {
            "site": ctx.origin,
            "host": host,
            "company_name": ctx.company_name or "",
            "scan_id": scan_id,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "scraping_seconds": round(crawl_ms / 1000, 1) if crawl_ms is not None else None,
        }
        folder = SITE_FOLDERS_ROOT / _folder_name(host)
        return await asyncio.to_thread(_write, folder, ctx, reads, meta)
    except Exception:
        log.exception("Scan %s: could not write the site folder", scan_id)
        return None

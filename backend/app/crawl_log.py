"""The crawl log: one PDF per audit, written so anyone can follow it. Page 1 gives the answer
(did the audit read the site, and what needs attention); then each problem with what happened,
why it matters, where it is and how to fix it; the 13 steps (0-12) in plain words; the checks;
every page with a problem, grouped by reason; every page read; and, last, the technical detail
and a glossary.

Built from {site}-scraped-content.json (the crawl's own record: the steps, and each page's
status, timing and how it was found) plus the stored report (what ran after the crawl, and
the checks). The scan writes it to data/scan_output/{site}-crawl-log.pdf and into the site
folder when it finishes; GET /scans/{id}/crawl-log.pdf rebuilds it on request.
"""

from __future__ import annotations

import json
import logging
import shutil
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import KeepTogether, LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .config import CRAWL_LOGS_DIR
from .crawl_diagnosis import FAIL, OK, SKIP, WARN, _url_errors, diagnose, http_words, site_issues
from .scan_output import OUTPUT_DIR, _friendly_duration, _friendly_timestamp, _slug

log = logging.getLogger("crawl_log")

NAVY = colors.HexColor("#14367A")
GREEN = colors.HexColor("#1B8A5A")
AMBER = colors.HexColor("#B45309")
RED = colors.HexColor("#B91C1C")
GRAY = colors.HexColor("#64748B")
LINE = colors.HexColor("#CBD5E1")
HEAD_BG = colors.HexColor("#E8EEF8")
ZEBRA = colors.HexColor("#F8FAFC")
RED_BG = colors.HexColor("#FEF2F2")
AMBER_BG = colors.HexColor("#FFFBEB")

# The steps of crawl_site(), in the words the report uses, with the code that does each one.
STEP_CODE = {
    "0": "http.py normalize_url()",
    "1": "snapshot.py load()",
    "2": "resolver.py prefer_reachable_addresses()",
    "3": "http.py fetch()",
    "4": "robots.py fetch_robots()",
    "5": "sitemap.py fetch_sitemaps()",
    "6": "http.py fetch()",
    "7": "discover.py _priority_urls() + fetch_many.py crawl_raw()",
    "8": "extract.py extract_many() + discover.py parse_page()",
    "9": "discover.py _discover()",
    "10": "discover.py _run_render_pass() + render.py",
    "10b": "discover.py _discover() + _render_and_apply()",
    "11": "discover.py crawl_site()",
}


def _clean(text) -> str:
    """Text for a Paragraph: XML-escaped, and limited to what the built-in PDF fonts can draw."""
    s = "" if text is None else str(text)
    s = s.replace("→", "->").replace("…", "...").replace("≥", ">=").replace("≤", "<=")
    s = s.encode("cp1252", "replace").decode("cp1252")
    return escape(s)


_VERDICT = {OK: ("OK", "ok"), WARN: ("Warning", "warn"), FAIL: ("Problem", "bad"), SKIP: ("Skipped", None)}
_TONE_COLOR = {OK: GREEN, WARN: AMBER, FAIL: RED, SKIP: GRAY}
_PILLAR = {"technical": "Technical", "on_page": "Content", "off_page": "Reputation"}
_CHECK_RESULT = {"PASS": ("Passed", "ok"), "PARTIAL": ("Partly passed", "warn"), "FAIL": ("Failed", "bad"),
                 "UNKNOWN": ("Not measured", "warn")}

GLOSSARY = [
    ("Crawl, crawler", "The audit's program visiting the site's pages one by one, the way a search engine or AI bot does."),
    ("HTTP status", "The code a server sends back with every page. 200 = OK; 301/302 = moved; 403 = access denied; "
                    "404 = page not found; 429 = too many requests; 500 and above = the server failed."),
    ("robots.txt", "A file at the root of the site telling search engines and AI bots which pages they may read."),
    ("Sitemap", "A file listing the site's pages, so search engines and AI crawlers can find them all."),
    ("llms.txt", "A short text file pointing AI assistants (ChatGPT, Claude, Perplexity...) to the site's key pages."),
    ("Opened in a browser", "Some pages only show their text after JavaScript runs; the audit opens those in a real "
                            "browser to read them. Many AI crawlers cannot do this."),
    ("Thin page", "A page with under 50 words of text: too little for an AI assistant to understand or quote."),
    ("Bot-check page", "A 'prove you are human' page (a CAPTCHA) shown instead of the real content."),
    ("Saved copy", "A copy of the site saved earlier, read instead of the live website."),
    ("Check", "One of the audit's measurements, scored 0-100. Technical = can AI read the site; Content = is the "
              "content useful; Reputation = what the rest of the web says about the company."),
]


def _dur(ms) -> str:
    return f"{int(ms):,} ms" if ms < 1000 else _friendly_duration(ms)


def _link(text) -> str:
    """Paragraph markup for a 'where' entry, with a leading web address made clickable."""
    raw = "" if text is None else str(text)
    if not raw.startswith(("http://", "https://")):
        return _clean(raw)
    address, sep, rest = raw.partition(" ")
    if address[-1] in ":,;.":
        address, sep, rest = address[:-1], "", address[-1] + sep + rest
    href = _clean(address).replace('"', "&quot;")
    return f'<a href="{href}" color="#1D4ED8">{_clean(address)}</a>' + (_clean(sep + rest) if sep else "")


def _wrap(text, width: float, size: float = 8) -> str:
    """Text broken into lines that fit a column, for plain table cells: much faster than a
    Paragraph on tables with thousands of rows. Breaks after '/', '-', '?', '&' or a space
    where it can, mid-word where it must."""
    text = " ".join(str(text or "").encode("cp1252", "replace").decode("cp1252").split())
    per_line = max(8, int((width - 10) / (size * 0.5)))
    lines = []
    while len(text) > per_line:
        cut = max(text.rfind(c, per_line // 2, per_line) for c in "/-?& ")
        cut = cut + 1 if cut > 0 else per_line
        lines.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return "\n".join(lines + [text])


def _outcome(page: dict) -> tuple[str, str]:
    """(what happened to this URL in plain words, tone) -- tone is ok / warn / bad."""
    status = page.get("status_code")
    if page.get("fetch_error") or status is None:
        return http_words(None, page.get("fetch_error")), "bad"
    if status in (401, 403, 429):
        return f"Refused by the site: {http_words(status)}", "bad"
    if not 200 <= status < 300:
        return http_words(status), "bad"
    if page.get("blocked_reason"):
        return f"Bot-check page instead of content ({page['blocked_reason']})", "bad"
    notes = []
    asked, landed = page.get("url") or "", page.get("final_url") or ""
    secure_only = landed and _same_page(asked) == _same_page(landed)
    if landed and landed != asked and not secure_only:
        moved = urlparse(landed)
        notes.append(f"moved to {moved.path or '/'}" if moved.netloc == urlparse(asked).netloc else f"moved to {landed}")
    if page.get("bot_ua_refused"):
        notes.append("the site refused the audit bot, so it was read as a normal browser")
    if (page.get("word_count") or 0) < 50:
        notes.append("very little text")
    rendered = page.get("content_source") == "rendered"
    if notes:
        return "Read, but " + "; ".join(notes), "warn"
    return "OK (opened in a browser)" if rendered else "OK", "ok"


def _same_page(address: str) -> str:
    """An address without the parts a routine redirect changes: http/https, www, a trailing slash."""
    parts = urlparse(address)
    host = parts.netloc.lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}?{parts.query}"


def _fallback_steps(crawl: dict) -> list[dict]:
    """What can still be said about an audit saved before steps were recorded."""
    render = crawl.get("render") or {}
    access = crawl.get("access") or {}
    pages = crawl.get("pages") or []
    rows = [
        {"step": "0", "name": "Normalise the address", "result": f"{crawl.get('input_url')} -> {crawl.get('normalized_url')}"},
        {"step": "1", "name": "Look for a saved copy of the site",
         "result": f"Saved copy: {crawl['snapshot'].get('folder')}" if (crawl.get("snapshot") or {}).get("folder") else "Live website read"},
        {"step": "3", "name": "Open the homepage",
         "result": f"HTTP {access.get('homepage_status')}" if access.get("homepage_status") else "Not recorded"},
        {"step": "5", "name": "Read the sitemaps", "result": f"{crawl.get('sitemap_url_count', 0):,} page address(es) listed"},
        {"step": "7-9", "name": "Download, read and follow links", "result": f"{len(pages):,} page(s) held"},
        {"step": "10", "name": "Open pages in a browser where needed",
         "result": f"{render.get('decision', 'not recorded')}: {render.get('rendered_count', 0)} rendered" + (f" ({render['reason']})" if render.get("reason") else "")},
        {"step": "11", "name": "Put the crawl together", "result": f"{len(pages):,} unique page(s); company \"{crawl.get('company_name')}\""},
    ]
    return [{**r, "ms": None} for r in rows]


def _table(rows: list[list], widths: list[float], *, header: bool = True, tones: Optional[list[str]] = None,
           tone_col: Optional[int] = None, long: bool = False) -> Table:
    cls = LongTable if long else Table
    t = cls(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, LINE),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                  ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]
    first = 1 if header else 0
    for i in range(first, len(rows)):
        tone = tones[i - first] if tones else None
        if tone == "bad":
            style.append(("BACKGROUND", (0, i), (-1, i), RED_BG))
        elif tone == "warn":
            style.append(("BACKGROUND", (0, i), (-1, i), AMBER_BG))
        elif (i - first) % 2:
            style.append(("BACKGROUND", (0, i), (-1, i), ZEBRA))
        if tone_col is not None and tone:
            style.append(("TEXTCOLOR", (tone_col, i), (tone_col, i), {"ok": GREEN, "warn": AMBER, "bad": RED}[tone]))
    t.setStyle(TableStyle(style))
    return t


def _headline(verdicts: list[dict], pages: list, access: dict) -> tuple[str, str, colors.Color]:
    """(headline, one-sentence explanation, colour) for the banner on page 1."""
    problems = [v for v in verdicts if v["status"] == FAIL]
    warnings = [v for v in verdicts if v["status"] == WARN]
    if access.get("blocked") or not any(200 <= (p.get("status_code") or 0) < 300 for p in pages):
        return ("The audit could not read this website",
                "The site refused or did not answer the audit's crawler, so the checks that read the site's own pages "
                "have nothing to work with. Checks of other websites (search results, AI answers, reviews) still ran. "
                "The first problem below explains why and what to do.", RED)
    if problems:
        return (f"The audit finished, but {len(problems)} step{'s' if len(problems) != 1 else ''} had a problem",
                "Part of the site could not be read or checked. Each problem is explained below with where it is and how to fix it.", RED)
    if warnings:
        return (f"The audit finished, with {len(warnings)} thing{'s' if len(warnings) != 1 else ''} to look at",
                "The site was read and checked. The warnings below are worth fixing; none of them stopped the audit.", AMBER)
    return ("The audit finished with no problems",
            "Every step worked: the site was read in full and every check was measured.", GREEN)


def build_crawl_log_pdf(crawl: dict, report: Optional[dict] = None) -> bytes:
    report = report or {}
    buf = BytesIO()
    page_size = landscape(A4)
    margin = 0.5 * inch
    width = page_size[0] - 2 * margin
    domain = crawl.get("domain") or report.get("domain") or ""
    doc = SimpleDocTemplate(buf, pagesize=page_size, leftMargin=margin, rightMargin=margin,
                            topMargin=0.5 * inch, bottomMargin=0.55 * inch,
                            title=f"Crawl log - {domain}", author="AI Visibility Audit")
    styles = getSampleStyleSheet()
    title = ParagraphStyle("T", parent=styles["Title"], textColor=NAVY, fontSize=20, leading=24, alignment=0, spaceAfter=2)
    h = ParagraphStyle("H", parent=styles["Heading2"], textColor=NAVY, fontSize=13, leading=16, spaceBefore=14, spaceAfter=3)
    body = ParagraphStyle("B", parent=styles["BodyText"], fontSize=9, leading=12)
    intro = ParagraphStyle("I", parent=body, textColor=GRAY, spaceAfter=6)
    small = ParagraphStyle("S", parent=body, fontSize=7.5, leading=9.5, textColor=GRAY)
    cell = ParagraphStyle("C", parent=body, fontSize=8, leading=10)
    url = ParagraphStyle("U", parent=cell, splitLongWords=1)  # long addresses break at the cell edge
    bold = ParagraphStyle("CB", parent=cell, fontName="Helvetica-Bold")
    head_cell = ParagraphStyle("HC", parent=bold, textColor=colors.white)

    pages = crawl.get("pages") or []
    access = crawl.get("access") or {}
    steps = (crawl.get("crawl_log") or {}).get("steps") or []
    recorded = bool(steps)
    if not recorded:
        steps = _fallback_steps(crawl)
    after = report.get("pipeline_log") or []
    outcomes = [_outcome(p) for p in pages]
    read_ok = sum(1 for _, tone in outcomes if tone != "bad")
    unanswered = _url_errors(crawl)
    rendered = sum(1 for p in pages if p.get("content_source") == "rendered")
    crawl_ms = report.get("crawl_duration_ms") or sum(s.get("ms") or 0 for s in steps) or None
    verdicts = diagnose(crawl, report)
    params = report.get("parameters") or []
    attention = [v for v in verdicts if v["status"] in (FAIL, WARN)]
    attention.sort(key=lambda v: v["status"] != FAIL)
    number = iter(range(1, 20))

    # Page 1: the answer first.
    story = [
        Paragraph(f"Crawl Log &mdash; {_clean(domain)}", title),
        Paragraph(
            f"What happened when the audit read this website, step by step, and what to fix. "
            f"Read {_clean(_friendly_timestamp(report.get('audited_at') or report.get('generated_at')) or '-')} "
            f"&middot; started from {_link(crawl.get('input_url') or crawl.get('normalized_url') or '')} "
            f"&middot; audit {_clean(crawl.get('scan_id') or report.get('scan_id') or '')}", small),
        Spacer(1, 10),
    ]
    headline, explain, tone_color = _headline(verdicts, pages, access)
    banner = Table([[[Paragraph(_clean(headline), ParagraphStyle("BH", parent=body, fontName="Helvetica-Bold", fontSize=15,
                                                               leading=19, textColor=colors.white)),
                      Spacer(1, 3),
                      Paragraph(_clean(explain), ParagraphStyle("BE", parent=body, fontSize=9.5, leading=13, textColor=colors.white))]]],
                   colWidths=[width])
    banner.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), tone_color), ("LEFTPADDING", (0, 0), (-1, -1), 12),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 12), ("TOPPADDING", (0, 0), (-1, -1), 10),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 11)]))
    story += [banner, Spacer(1, 10)]

    measured = sum(1 for p in params if p.get("status") != "UNKNOWN")
    tiles = [
        ("Pages found", f"{len(pages) + len(unanswered):,}", None),
        ("Read OK", f"{read_ok:,}", GREEN if read_ok else RED),
        ("Could not be read", f"{len(pages) - read_ok + len(unanswered):,}", RED if len(pages) - read_ok + len(unanswered) else None),
        ("Needed a browser", f"{rendered:,}", None),
        ("Listed in the sitemap", f"{crawl.get('sitemap_url_count', 0):,}", None),
        ("Checks measured", f"{measured} of {len(params)}" if params else "-", AMBER if params and measured < len(params) else None),
        ("Time to read the site", _friendly_duration(crawl_ms) if crawl_ms else "Not recorded", None),
    ]
    tile_label = ParagraphStyle("TL", parent=small, fontSize=7, leading=9)
    tile_value = ParagraphStyle("TV", parent=body, fontName="Helvetica-Bold", fontSize=15, leading=18, textColor=NAVY)
    tile_table = Table([[[Paragraph(k.upper(), tile_label),
                          Paragraph(f"<font color='{c.hexval().replace('0x', '#')}'>{v}</font>" if c else v, tile_value)]
                         for k, v, c in tiles]], colWidths=[width / len(tiles)] * len(tiles))
    tile_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
        ("BACKGROUND", (0, 0), (-1, -1), ZEBRA), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(tile_table)

    # What needs attention, one line each; the cards in section 1 give the detail.
    story.append(Paragraph("What needs attention", h))
    if attention:
        rows = [[Paragraph("", head_cell), Paragraph("Step", head_cell), Paragraph("In short", head_cell)]]
        for v in attention:
            label, _ = _VERDICT[v["status"]]
            rows.append([Paragraph(f"<b>{label}</b>", cell), Paragraph(f"<b>{v['step']}. {_clean(v['name'])}</b>", cell),
                         Paragraph(_clean(v["what"]), cell)])
        t = _table(rows, [0.75 * inch, 2.4 * inch, width - 3.15 * inch], tones=[_VERDICT[v["status"]][1] for v in attention])
        t.setStyle(TableStyle([("TEXTCOLOR", (0, i + 1), (0, i + 1), _TONE_COLOR[v["status"]]) for i, v in enumerate(attention)]))
        story.append(t)
    else:
        story.append(Paragraph("Nothing: every step of the audit worked.", body))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "<b>How to read this log:</b> <font color='#B91C1C'><b>red</b></font> = a problem that stopped part of the audit or "
        "needs fixing; <font color='#B45309'><b>amber</b></font> = a warning worth fixing; "
        "<font color='#1B8A5A'><b>green</b></font> = worked. Blue addresses are links. Unfamiliar words are explained at the end.",
        small))

    # 1. Each problem: what happened, why it matters, where, how to fix.
    story.append(PageBreak())
    story.append(Paragraph(f"{next(number)}. What went wrong and how to fix it", h))
    if not attention:
        story.append(Paragraph("Every step worked; there is nothing to fix in how the site was read.", body))
    label_style = ParagraphStyle("CL", parent=cell, fontSize=8.5, leading=11.5)
    for v in attention:
        label, tone = _VERDICT[v["status"]]
        color = _TONE_COLOR[v["status"]]
        content = [Paragraph(f"Step {v['step']} &middot; {_clean(v['name'])}",
                             ParagraphStyle("CT", parent=body, fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=color)),
                   Paragraph(_clean(v["purpose"]), small), Spacer(1, 4),
                   Paragraph(f"<b>What happened:</b> {_clean(v['what'])}", label_style)]
        if v.get("why"):
            content.append(Paragraph(f"<b>Why it matters:</b> {_clean(v['why'])}", label_style))
        if v["where"]:
            content.append(Paragraph("<b>Where:</b>", label_style))
            content.append(Paragraph("<br/>".join(f"&bull; {_link(w)}" for w in v["where"]),
                                     ParagraphStyle("CW", parent=url, leftIndent=8, fontSize=8, leading=10.5)))
        if v["fix"]:
            content.append(Paragraph(f"<b>How to fix:</b> {_clean(v['fix'])}", label_style))
        card = Table([[Paragraph(f"<b>{label.upper()}</b>", ParagraphStyle("BL", parent=cell, textColor=colors.white,
                                                                          fontSize=8.5, alignment=1)), content]],
                     colWidths=[0.95 * inch, width - 0.95 * inch])
        card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), color), ("BACKGROUND", (1, 0), (1, 0), RED_BG if tone == "bad" else AMBER_BG),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (-1, -1), 0.6, color),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (1, 0), (1, 0), 10), ("RIGHTPADDING", (1, 0), (1, 0), 10),
        ]))
        story += [Spacer(1, 7), card]

    # 2. Every step at a glance, with what it is for.
    story.append(Paragraph(f"{next(number)}. All {len(verdicts)} steps of the audit", h))
    story.append(Paragraph("The audit reads a website in the same order every time. Each step, what it is for, and how it went.", intro))
    # Summed, not overwritten: link discovery (9) and the rendered-page rounds (10b) record one
    # entry per round, and the step's time is all of its rounds.
    step_ms: dict[str, int] = {}
    for s in steps:
        if s.get("ms") is not None:
            key = str(s.get("step"))
            step_ms[key] = step_ms.get(key, 0) + s["ms"]
    if "10b" in step_ms and "10" in step_ms:
        step_ms["10"] += step_ms["10b"]
    if after and all(s.get("ms") is not None for s in after):
        step_ms["12"] = sum(s["ms"] for s in after)
    rows = [[Paragraph(t, head_cell) for t in ("Step", "What this step does", "Result", "What happened", "Time")]]
    for v in verdicts:
        label, _ = _VERDICT[v["status"]]
        rows.append([Paragraph(f"<b>{v['step']}</b>", cell),
                     [Paragraph(_clean(v["name"]), bold), Paragraph(_clean(v["purpose"]), small)],
                     Paragraph(f"<b>{label}</b>", cell), Paragraph(_clean(v["what"]), cell),
                     _dur(step_ms[v["step"]]) if step_ms.get(v["step"]) is not None else "-"])
    t = _table(rows, [0.45 * inch, 3.3 * inch, 0.75 * inch, width - 5.25 * inch, 0.75 * inch],
               tones=[_VERDICT[v["status"]][1] if v["status"] != OK else None for v in verdicts])
    t.setStyle(TableStyle([("TEXTCOLOR", (2, i + 1), (2, i + 1), _TONE_COLOR[v["status"]]) for i, v in enumerate(verdicts)]))
    story.append(t)
    for s in steps:
        bots = (s.get("details") or {}).get("ai_bots")
        if bots:
            story.append(Spacer(1, 5))
            story.append(Paragraph("<b>What robots.txt says to each AI crawler:</b> " + ", ".join(
                f"{_clean(b)} <font color='{'#1B8A5A' if r == 'allowed' else '#B91C1C'}'>{r}</font>" for b, r in bots.items()), small))

    # 3. The site's own problems, from the checks.
    issues = site_issues(report)
    if issues:
        story.append(Paragraph(f"{next(number)}. Problems the checks found on the website ({len(issues)})", h))
        story.append(Paragraph("These are about the website itself, not about reading it. Lowest score first. 'Where' names "
                               "the pages or items to change; the full list for each check is in the report and the Excel file.", intro))
        rows = [[Paragraph(t, head_cell) for t in ("Check", "Score", "What is wrong", "Where", "How to fix")]]
        tones = []
        for item in issues:
            score = item["score"]
            what = item["what"] if len(item["what"]) <= 320 else item["what"][:317].rsplit(" ", 1)[0] + "..."
            rows.append([[Paragraph(f"<b>{_clean(item['name'])}</b>", cell), Paragraph(_PILLAR.get(item["section"], ""), small)],
                         Paragraph(f"<b>{'Failed' if item['status'] == 'FAIL' else 'Partly passed'}</b><br/>"
                                   + (f"{score:g} / 100" if isinstance(score, (int, float)) else ""), cell),
                         Paragraph(_clean(what) or "-", cell), Paragraph("<br/>".join(_link(w) for w in item["where"]) or "-", url),
                         Paragraph(_clean(item["fix"]) or "-", cell)])
            tones.append("bad" if item["status"] == "FAIL" else "warn")
        story.append(_table(rows, [1.7 * inch, 0.8 * inch, (width - 2.5 * inch) * 0.34,
                                   (width - 2.5 * inch) * 0.36, (width - 2.5 * inch) * 0.30], tones=tones, tone_col=1, long=True))

    # 4. Every check
    if params:
        story.append(Paragraph(f"{next(number)}. Every check and its result ({len(params)})", h))
        rows = [[Paragraph(t, head_cell) for t in ("#", "Check", "Area", "Result", "Score", "Time")]]
        tones = []
        for i, p in enumerate(params, 1):
            result, tone = _CHECK_RESULT.get(p.get("status") or "UNKNOWN", (p.get("status") or "-", None))
            score = p.get("score")
            rows.append([str(i), Paragraph(_clean(p.get("name")), cell), _PILLAR.get(p.get("section"), p.get("section") or ""),
                         result, f"{score:g}" if isinstance(score, (int, float)) else "-",
                         _dur(p["duration_ms"]) if p.get("duration_ms") is not None else "-"])
            tones.append(tone)
        story.append(_table(rows, [0.4 * inch, width - 4.1 * inch, 1 * inch, 1.1 * inch, 0.7 * inch, 0.9 * inch],
                            tones=tones, tone_col=3, long=True))

    # 5. Pages that could not be read, grouped by why.
    groups: dict[str, list[str]] = {}
    for p, (text, tone) in zip(pages, outcomes):
        if tone == "bad":
            groups.setdefault(text, []).append(p.get("url") or "")
    for u, reason in unanswered:
        groups.setdefault(http_words(None, reason), []).append(u)
    thin = [p.get("url") or "" for p, (_, tone) in zip(pages, outcomes)
            if tone != "bad" and (p.get("word_count") or 0) < 50]
    if groups or thin:
        story.append(Paragraph(f"{next(number)}. Pages with a problem, grouped by reason", h))
        story.append(Paragraph("Every page the audit could not read, or could read but found almost no text on. "
                               "Fix the page, or remove the links that point to it.", intro))
        widths = [0.45 * inch, width * 0.62, width * 0.38 - 0.45 * inch]
        rows = [[Paragraph(t, head_cell) for t in ("#", "Page address", "Reason")]]
        tones, firsts = [], []
        ordered = sorted(groups.items(), key=lambda kv: -len(kv[1])) + ([("Very little text (under 50 words)", thin)] if thin else [])
        for reason, urls in ordered:
            firsts.append(len(rows))
            for i, u in enumerate(urls, 1):
                rows.append([str(i), _wrap(u, widths[1]),
                             _wrap(reason + (f" - {len(urls):,} pages" if i == 1 and len(urls) > 1 else ""), widths[2])])
                tones.append("warn" if reason.startswith("Very little text") else "bad")
        t = _table(rows, widths, tones=tones, long=True)
        t.setStyle(TableStyle([("FONTNAME", (2, r), (2, r), "Helvetica-Bold") for r in firsts]
                              + [("LINEABOVE", (0, r), (-1, r), 1, GRAY) for r in firsts[1:]]))
        story.append(t)

    # 6. Every page
    story.append(Paragraph(f"{next(number)}. Every page the audit read ({len(pages):,})", h))
    story.append(Paragraph("In the order they were read, homepage first. 'Found through' says how the audit learned of the page. "
                           "Red rows could not be read; amber rows were read with a note.", intro))
    widths = [0.45 * inch, width - 5.15 * inch, 2.2 * inch, 1.3 * inch, 0.55 * inch, 0.65 * inch]
    rows = [[Paragraph(t, head_cell) for t in ("#", "Page address", "Result", "Found through", "Words", "Time")]]
    tones = []
    for i, (p, (text, tone)) in enumerate(zip(pages, outcomes), 1):
        rows.append([str(i), _wrap(p.get("url"), widths[1]), _wrap(text, widths[2]), _wrap(p.get("found_via") or "-", widths[3]),
                     f"{p.get('word_count') or 0:,}", f"{p['fetch_ms']:,} ms" if p.get("fetch_ms") else "-"])
        tones.append(tone if tone != "ok" else None)
    story.append(_table(rows, widths, tones=tones, tone_col=2, long=True))

    # 7. Technical detail, for whoever maintains the audit.
    story.append(Paragraph(f"{next(number)}. Technical detail (for developers)", h))
    if not recorded:
        story.append(Paragraph("This audit ran before step details were recorded, so the steps below are rebuilt from "
                               "what it saved. Run the audit again for the full record.", intro))
    rows = [[Paragraph(t, head_cell) for t in ("Step", "Name", "Recorded result", "Code", "Time")]]
    for s in steps + after:
        rows.append([Paragraph(f"<b>{_clean(s.get('step'))}</b>", cell), Paragraph(_clean(s.get("name")), cell),
                     Paragraph(_clean(s.get("result")), url), Paragraph(_clean(STEP_CODE.get(str(s.get("step")), "scans.py execute_scan()" if str(s.get("step")).startswith("12") else "")), small),
                     _dur(s["ms"]) if s.get("ms") is not None else "-"])
    story.append(_table(rows, [0.45 * inch, 2.2 * inch, width - 5.2 * inch, 1.85 * inch, 0.7 * inch], long=True))

    files = [
        f"{_slug(domain)}-scraped-content.json and {_slug(domain)}-raw/ (every page's text and its HTML)",
        f"{_slug(domain)}-failed-links.json (every link that could not be read)",
        f"{_slug(domain)}-parameter-new-logic.xlsx (the checks, as a spreadsheet)",
        f"{_slug(domain)}-crawl-log.pdf (this log), with a copy in the project's 'Crawl Logs' folder",
        f"Site folder {domain}/ (a .txt file per page, _index.csv, all_links.txt, the links workbook)",
    ]
    story.append(KeepTogether([Paragraph(f"{next(number)}. Files saved", h),
                               Paragraph("<br/>".join(f"&bull; {_clean(f)}" for f in files), body)]))

    rows = [[Paragraph(f"<b>{_clean(term)}</b>", cell), Paragraph(_clean(meaning), cell)] for term, meaning in GLOSSARY]
    story.append(KeepTogether([Paragraph(f"{next(number)}. Words used in this log", h),
                               _table(rows, [1.8 * inch, width - 1.8 * inch], header=False)]))

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GRAY)
        canvas.drawString(margin, 0.3 * inch, f"AI Visibility Audit · Crawl log · {domain}")
        canvas.drawRightString(page_size[0] - margin, 0.3 * inch, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def load_crawl(domain: str, scan_id: str) -> Optional[dict]:
    """This audit's saved crawl record, or None when it is gone or a newer audit replaced it."""
    path = OUTPUT_DIR / f"{_slug(domain)}-scraped-content.json"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        crawl = json.load(fh)
    return crawl if crawl.get("scan_id") == scan_id else None


def save(scan_id: str, report: dict, site_dir: Optional[Path] = None) -> Optional[Path]:
    """Write the crawl log PDF beside the audit's other files. Logs, never raises."""
    try:
        crawl = load_crawl(report.get("domain") or "", scan_id)
        if crawl is None:
            return None
        path = OUTPUT_DIR / f"{_slug(crawl.get('domain') or report.get('domain'))}-crawl-log.pdf"
        path.write_bytes(build_crawl_log_pdf(crawl, report))
        if site_dir:
            shutil.copyfile(path, Path(site_dir) / "_crawl_log.pdf")
        try:
            CRAWL_LOGS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, CRAWL_LOGS_DIR / f"{crawl.get('domain') or report.get('domain')}-crawl-log.pdf")
        except OSError:
            log.warning("Scan %s: could not copy the crawl log to %s", scan_id, CRAWL_LOGS_DIR)
        return path
    except Exception:
        log.exception("Scan %s: could not write the crawl log PDF", scan_id)
        return None

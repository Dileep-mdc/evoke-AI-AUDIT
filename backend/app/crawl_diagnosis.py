"""What went wrong in an audit, step by step, and where on the site each problem is.

Two readings for the crawl log PDF (crawl_log.py):

  diagnose()     one verdict per audit step, 0-12: OK, warning, failed or skipped, with what
                 happened, where (the URL, file or check), and what to do about it.
  site_issues()  every check the site failed or only partly passed, with the pages or items
                 that need the change.

Built from the saved crawl record and the stored report. An audit saved before the steps were
recorded still gets a verdict per step, from what it did save.
"""

from __future__ import annotations

from typing import Optional

from .config import CRAWL_TIME_BUDGET, MAX_PAGES, PARAMETER_TIMEOUT
from .parameters import page_fixes

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

# Each step in plain words: its name, what it does, and why a problem there matters.
STEP_NAMES = {
    "0": "Check the web address",
    "1": "Look for a saved copy of the site",
    "2": "Reach the site's server",
    "3": "Open the homepage",
    "4": "Read robots.txt",
    "5": "Read the sitemap",
    "6": "Look for llms.txt",
    "7": "Download the pages",
    "8": "Read the text on each page",
    "9": "Follow links to find more pages",
    "10": "Open JavaScript pages in a browser",
    "11": "Put the results together",
    "12": "Run the checks and save the files",
}

STEP_PURPOSE = {
    "0": "Tidies the address typed in (https://, www) so every page is matched to the same site.",
    "1": "Uses a copy of the site saved earlier, if one was given, instead of the live website.",
    "2": "Finds which of the site's servers answer, so the crawl does not wait on dead ones.",
    "3": "Opens the first page. If this fails, nothing else on the site can be read.",
    "4": "Reads robots.txt, the file that tells search engines and AI bots which pages they may read.",
    "5": "Reads the sitemap, the site's own list of its pages.",
    "6": "Looks for llms.txt, a short file that points AI assistants to the site's key pages.",
    "7": "Downloads every page found so far: the homepage, the sitemap's pages and key sections.",
    "8": "Takes the title, headings and text out of each downloaded page.",
    "9": "Follows the links on the pages read to find pages the sitemap missed.",
    "10": "Opens pages in a real browser when their content only appears after JavaScript runs.",
    "11": "Removes duplicate pages and works out the company name.",
    "12": "Runs the audit's checks on what was read and saves the report files.",
}

STEP_WHY = {
    "0": "",
    "1": "The results describe the site as it was when the copy was made, not as it is today.",
    "2": "Servers that do not answer slow the audit down and can make pages fail.",
    "3": "If the homepage cannot be opened, the audit cannot read the site -- and AI crawlers probably cannot either.",
    "4": "Without clear rules, or with AI bots blocked, AI assistants may not read the site at all.",
    "5": "Without a sitemap, search engines and AI crawlers can miss pages.",
    "6": "Without it, AI assistants have no shortcut to the site's most important pages.",
    "7": "Broken pages cannot be read by visitors, search engines or AI tools, and waste their time.",
    "8": "Pages with almost no text give AI assistants nothing to understand or quote.",
    "9": "Pages that were not read were not checked, so the scores cover only part of the site.",
    "10": "Content that needs JavaScript is invisible to many AI crawlers.",
    "11": "If the company name is guessed, checks that search the web for the brand may look for the wrong name.",
    "12": "Checks that could not be measured count against the score until they are re-checked.",
}

_HTTP_WORDS = {
    301: "Moved permanently", 302: "Moved temporarily", 307: "Moved temporarily", 308: "Moved permanently",
    400: "Bad request", 401: "Login required", 403: "Access denied", 404: "Page not found", 405: "Not allowed",
    406: "Not acceptable", 408: "Timed out", 410: "Page removed", 429: "Too many requests (rate-limited)",
    500: "Server error", 502: "Server error (bad gateway)", 503: "Server unavailable", 504: "Server timed out",
}


def http_words(code, error=None) -> str:
    """An HTTP result in plain words: 'Page not found (HTTP 404)', 'No answer (timed out)'."""
    if code is None:
        text = " ".join(str(error or "").split()).rstrip(".")
        if "timeout" in text.lower() or "timed out" in text.lower():
            return "No answer (timed out)"
        if "redirect" in text.lower():
            return "Too many redirects (a redirect loop)"
        return f"No answer ({text[:80]})" if text else "No answer"
    if code in _HTTP_WORDS:
        return f"{_HTTP_WORDS[code]} (HTTP {code})"
    if 200 <= code < 300:
        return f"OK (HTTP {code})"
    if 500 <= code:
        return f"Server error (HTTP {code})"
    return f"Error (HTTP {code})"


_REFUSED = {401, 403, 429}
_THIN_WORDS = 50


def _verdict(step: str, status: str, what: str, where: list | str = "", fix: str = "") -> dict:
    where_list = [where] if isinstance(where, str) and where else list(where or [])
    return {"step": step, "name": STEP_NAMES[step], "purpose": STEP_PURPOSE[step], "why": STEP_WHY[step],
            "status": status, "what": what, "where": [w for w in where_list if w], "fix": fix}


def _details(crawl: dict, step: str) -> Optional[dict]:
    for s in (crawl.get("crawl_log") or {}).get("steps") or []:
        if str(s.get("step")) == step:
            return s.get("details") or {}
    return None


def _row(report: dict, pid: str) -> dict:
    return next((p for p in report.get("parameters") or [] if p.get("parameter_id") == pid), {})


def _url_errors(crawl: dict) -> list[tuple[str, str]]:
    """(url, reason) for each URL the crawl requested and got nothing back for."""
    held = {p.get("url") for p in crawl.get("pages") or []}
    out = []
    for entry in crawl.get("crawl_errors") or []:
        url, _, reason = entry.partition(": ")
        if url.startswith(("http://", "https://")) and url not in held:
            out.append((url, reason or entry))
    return out


def _refused(access: dict, what: str) -> Optional[dict]:
    """The verdict for a site file the site refused to serve, or None when it was not refused."""
    if what not in (access.get("refused") or []):
        return None
    code = access.get("homepage_status")
    return {"status": FAIL, "what": f"The site refused to serve {what}" + (f" (HTTP {code})" if code else "") + ", like the homepage.",
            "fix": "See step 3: the site blocks the audit's crawler."}


def _examples(urls: list[str], total: int, limit: int = 5) -> list[str]:
    out = urls[:limit]
    if total > len(out):
        out.append(f"... and {total - len(out):,} more (all listed under 'Pages with a problem')")
    return out


def diagnose(crawl: dict, report: Optional[dict] = None) -> list[dict]:
    report = report or {}
    pages = crawl.get("pages") or []
    origin = (crawl.get("origin") or "").rstrip("/")
    access = crawl.get("access") or {}
    snapshot = crawl.get("snapshot") or {}
    out = []

    # 0. The address
    out.append(_verdict("0", OK, f"Audited {crawl.get('normalized_url') or crawl.get('input_url')}",
                        crawl.get("normalized_url") or ""))

    # 1. Saved copy
    if snapshot.get("folder"):
        out.append(_verdict("1", WARN, "The site was read from a saved copy, not the live website, so the results "
                                       "describe the site as it was when the copy was made.", snapshot.get("folder"),
                            "Run the audit from the live web address to audit the site as it is today."))
    else:
        out.append(_verdict("1", OK, "No saved copy; the live website was read."))

    # 2. Server addresses
    d2 = _details(crawl, "2")
    reach = (d2 or {}).get("reach") or {}
    note = next((e for e in crawl.get("crawl_errors") or [] if "addresses for this host" in e), "")
    if snapshot.get("folder"):
        out.append(_verdict("2", SKIP, "Not needed: the pages came from the saved copy."))
    elif reach.get("checked") and not reach.get("reachable"):
        out.append(_verdict("2", FAIL, f"None of the {reach.get('total', 0)} server addresses for this site answered.",
                            reach.get("host", ""), "Check the site is online and that this network can reach it "
                                                   "(a firewall or VPN can block it)."))
    elif reach.get("checked") and reach.get("reachable", 0) < reach.get("total", 0) or note:
        missing = reach.get("total", 0) - reach.get("reachable", 0)
        out.append(_verdict("2", WARN, (f"{missing} of {reach.get('total')} server addresses did not answer; the crawl used "
                                        f"the {reach.get('reachable')} that did.") if reach else note,
                            reach.get("host", ""), "Nothing to do for this audit. If it keeps happening, the site's "
                                                   "DNS or CDN lists addresses that do not respond."))
    else:
        out.append(_verdict("2", OK, "The site's server answered normally."))

    # 3. Homepage
    d3 = _details(crawl, "3") or {}
    status = d3.get("status") if d3 else access.get("homepage_status")
    home = d3.get("url") or crawl.get("normalized_url") or origin + "/"
    if d3 and status is None:
        out.append(_verdict("3", FAIL, f"The homepage did not answer: {d3.get('error') or 'no response'}.", home,
                            "Check the address is right and the site is online. If it is, the site may be blocking this network."))
    elif status in _REFUSED or access.get("blocked"):
        out.append(_verdict("3", FAIL, access.get("reason") or f"The site refused automated access (HTTP {status}).", home,
                            "Ask the site owner to allow the audit's user-agent (AIVisibilityAuditBot/1.0) or this "
                            "network's IP address, then run the audit again."))
    elif isinstance(status, int) and status >= 400:
        out.append(_verdict("3", FAIL, f"The homepage returned an error (HTTP {status}).", home,
                            "Fix the homepage so it returns HTTP 200, then run the audit again."))
    elif d3.get("bot_ua_refused") or d3.get("switched_host"):
        notes, fixes = [], []
        if d3.get("bot_ua_refused"):
            notes.append("the audit bot was refused, so the page was read with a browser identity")
            fixes.append("The site blocks unknown bots; AI crawlers may be blocked too (see check 2, 'AI crawlers not blocked by any CDN').")
        if d3.get("switched_host"):
            notes.append("the address typed did not answer, so the www / non-www version was used")
            fixes.append("Make both www and non-www addresses work and redirect one to the other.")
        out.append(_verdict("3", WARN, f"The homepage was read (HTTP {status}), but " + " and ".join(notes) + ".",
                            d3.get("final_url") or home, " ".join(fixes)))
    else:
        out.append(_verdict("3", OK, f"The homepage answered (HTTP {status})." if status else "The homepage was read.",
                            d3.get("final_url") or home))

    # 4. robots.txt
    d4 = _details(crawl, "4")
    robots_url = (d4 or {}).get("url") or origin + "/robots.txt"
    if d4 is None:
        t1 = _row(report, "TECH-01")
        decisions = (t1.get("evidence") or {}).get("decisions") or []
        found = t1.get("status") not in (None, "UNKNOWN")
        blocked = [d.get("name") for d in decisions if d.get("decision") == "BLOCKED"]
    else:
        found, blocked = d4.get("found"), d4.get("blocked") or []
    refused = _refused(access, "robots.txt")
    if refused:
        out.append(_verdict("4", refused["status"], refused["what"], robots_url, refused["fix"]))
    elif not found:
        out.append(_verdict("4", WARN, "The site has no readable robots.txt.", robots_url,
                            "Publish /robots.txt that allows AI crawlers and includes a 'Sitemap:' line."))
    elif blocked:
        out.append(_verdict("4", WARN, f"robots.txt blocks these AI crawlers: {', '.join(blocked)}.", robots_url,
                            "Allow these crawlers in robots.txt if the site should appear in AI answers."))
    else:
        out.append(_verdict("4", OK, "robots.txt was read and allows the AI crawlers.", robots_url))

    # 5. Sitemaps
    d5 = _details(crawl, "5") or {}
    count = d5.get("count", crawl.get("sitemap_url_count", 0)) or 0
    errors = d5.get("errors") or []
    refused = _refused(access, "the sitemap")
    if refused and not count:
        out.append(_verdict("5", refused["status"], refused["what"], errors[:5] or origin + "/sitemap.xml", refused["fix"]))
    elif not count:
        out.append(_verdict("5", FAIL, "No sitemap with page addresses was found, so the crawl relied on following links.",
                            (d5.get("tried") or [origin + "/sitemap.xml"])[:5] + errors[:3],
                            "Publish an XML sitemap listing every page, and add 'Sitemap: <its URL>' to robots.txt."))
    elif errors:
        out.append(_verdict("5", WARN, f"{count:,} page addresses were found, but {len(errors)} sitemap address(es) "
                                       "could not be read.", errors[:5],
                            "Fix or remove the sitemap files listed; they return an error or invalid XML."))
    else:
        out.append(_verdict("5", OK, f"{count:,} page addresses were listed in the sitemaps."))

    # 6. llms.txt
    d6 = _details(crawl, "6")
    llms_url = (d6 or {}).get("url") or origin + "/llms.txt"
    if d6 is None:
        ev = _row(report, "TECH-03").get("evidence") or {}
        found6 = bool(ev.get("non_empty") or ev.get("links_found"))
        served_html = bool(ev.get("served_html_page"))
    else:
        found6, served_html = d6.get("found"), d6.get("served_html_page")
    refused = _refused(access, "llms.txt")
    if refused:
        out.append(_verdict("6", refused["status"], refused["what"], llms_url, refused["fix"]))
    elif served_html:
        out.append(_verdict("6", WARN, "/llms.txt returns an HTML web page, not a text file.", llms_url,
                            "Publish a plain-text /llms.txt listing the site's key pages for AI assistants."))
    elif not found6:
        out.append(_verdict("6", WARN, "The site has no /llms.txt.", llms_url,
                            "Publish /llms.txt listing the site's key pages for AI assistants."))
    else:
        out.append(_verdict("6", OK, "/llms.txt was found.", llms_url))

    # 7. Downloads
    no_answer = _url_errors(crawl)
    failed = [p for p in pages if p.get("fetch_error") or p.get("status_code") is None
              or not 200 <= (p.get("status_code") or 0) < 300]
    bad_total = len(failed) + len(no_answer)
    requested = len(pages) + len(no_answer)
    if not pages or bad_total >= requested:
        out.append(_verdict("7", FAIL, f"None of the {requested:,} page(s) requested could be downloaded.",
                            _examples([u for u, _ in no_answer] + [p["url"] for p in failed], bad_total),
                            "See step 3: the site is refusing or not answering the crawler."))
    elif bad_total:
        kinds: dict[str, int] = {}
        for p in failed:
            kind = http_words(p.get("status_code"), p.get("fetch_error"))
            kinds[kind] = kinds.get(kind, 0) + 1
        for _, reason in no_answer:
            kind = http_words(None, reason)
            kinds[kind] = kinds.get(kind, 0) + 1
        breakdown = ", ".join(f"{n:,} {k}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
        out.append(_verdict("7", WARN, f"{bad_total:,} of {requested:,} pages could not be downloaded: {breakdown}.",
                            _examples([p["url"] for p in failed] + [u for u, _ in no_answer], bad_total),
                            "Fix or remove the broken pages and the links that point to them; 'Pages with a problem' lists each one."))
    else:
        out.append(_verdict("7", OK, f"All {requested:,} pages requested were downloaded."))

    # 8. Reading pages
    ok_pages = [p for p in pages if 200 <= (p.get("status_code") or 0) < 300 and not p.get("fetch_error")]
    challenged = [p for p in ok_pages if p.get("blocked_reason")]
    readable = [p for p in ok_pages if not p.get("blocked_reason")]
    thin = [p for p in readable if (p.get("word_count") or 0) < _THIN_WORDS]
    if pages and not readable:
        out.append(_verdict("8", FAIL, "No page had readable content (only errors or bot-check pages).",
                            _examples([p["url"] for p in challenged], len(challenged)),
                            "The site shows a bot check instead of its content; ask the owner to allow the audit's crawler."))
    elif challenged or thin:
        parts = []
        if challenged:
            parts.append(f"{len(challenged):,} page(s) showed a bot-check page instead of content")
        if thin:
            parts.append(f"{len(thin):,} page(s) had very little text (under {_THIN_WORDS} words)")
        out.append(_verdict("8", WARN, "; ".join(parts) + ".", _examples([p["url"] for p in challenged + thin], len(challenged) + len(thin)),
                            " ".join(([f"Bot-check pages: ask the site owner to allow the audit's crawler."] if challenged else [])
                                     + (["Thin pages: add real text, or check whether the content only appears after "
                                         "JavaScript (step 10)."] if thin else []))))
    else:
        out.append(_verdict("8", OK, f"{len(readable):,} pages were read with real content."))

    # 9. Link discovery
    d11 = _details(crawl, "11") or {}
    stopped = any("link discovery stopped" in e for e in crawl.get("crawl_errors") or [])
    if snapshot.get("folder"):
        out.append(_verdict("9", SKIP, "Not needed: the saved copy is the whole site for this audit."))
    elif d11.get("refused") or access.get("blocked"):
        out.append(_verdict("9", SKIP, "Skipped: the site refused the crawl (see step 3)."))
    elif d11.get("page_cap_reached") or len(pages) >= MAX_PAGES:
        out.append(_verdict("9", WARN, f"The crawl reached its limit of {d11.get('page_cap', MAX_PAGES):,} pages; "
                                       "the rest of the site was not read.", origin,
                            "Normal for a large site. Raise MAX_PAGES in backend/.env to read more pages."))
    elif d11.get("time_budget_reached") or stopped:
        minutes = (d11.get("time_budget_s") or CRAWL_TIME_BUDGET) / 60
        out.append(_verdict("9", WARN, f"Link following stopped at the {minutes:.0f}-minute crawl time limit; pages "
                                       "found after that were not read.", origin,
                            "Raise CRAWL_TIME_BUDGET in backend/.env to read more of a slow site."))
    else:
        out.append(_verdict("9", OK, "Links were followed until no new pages turned up; no limit was reached."))

    # 10. Browser rendering
    render = crawl.get("render") or {}
    decision = render.get("decision")
    if decision == "unavailable":
        out.append(_verdict("10", WARN, f"Pages could not be opened in a browser: {render.get('reason') or 'browser unavailable'}.",
                            "", "Install the browser: pip install playwright && playwright install chromium."))
    elif decision == "disabled":
        out.append(_verdict("10", SKIP, "Browser rendering is switched off (ENABLE_RENDER)."))
    elif decision == "skipped":
        reason = render.get("reason") or ""
        out.append(_verdict("10", SKIP, ("Skipped: " + reason) if reason else "Skipped."))
    elif render.get("failed_count"):
        out.append(_verdict("10", WARN, f"{render['failed_count']} page(s) failed to open in the browser; "
                                        f"{render.get('rendered_count', 0)} opened.", "",
                            "Those pages were scored from their plain HTML instead. Usually a slow or blocked page; re-run to retry."))
    elif decision == "not_needed":
        out.append(_verdict("10", OK, "Not needed: the server already sends each page's content without JavaScript.",
                            render.get("probe_url") or ""))
    else:
        out.append(_verdict("10", OK, f"{render.get('rendered_count', 0)} page(s) were opened in a browser to read "
                                      "content that only appears after JavaScript."))

    # 11. Assemble
    if not pages:
        out.append(_verdict("11", FAIL, "No page was collected, so the on-site checks had nothing to read."))
    elif d11.get("name_source") == "the web address":
        out.append(_verdict("11", WARN, f"The company name \"{d11.get('company')}\" was guessed from the web address, "
                                        "because the homepage does not state it.", crawl.get("normalized_url") or "",
                            "Add og:site_name or Organization structured data with the company name on the homepage."))
    else:
        dup = d11.get("duplicates") or 0
        out.append(_verdict("11", OK, f"{len(pages):,} unique pages collected"
                                      + (f"; {dup:,} duplicate address(es) that redirect to a page already held were merged" if dup else "")
                                      + f"; company \"{d11.get('company') or crawl.get('company_name')}\"."))

    out.append(_checks_verdict(report))
    return out


def _time_note(row: dict) -> str:
    ev = row.get("evidence") or {}
    if ev.get("time_limited"):
        return ev["time_limited"]
    for value in ev.values():
        if isinstance(value, dict) and value.get("time_limited"):
            return value["time_limited"]
    return ""


def _checks_verdict(report: dict) -> dict:
    params = report.get("parameters") or []
    if not params:
        return _verdict("12", SKIP, "No checks have been run on this crawl yet.")
    unmeasured = [p for p in params if p.get("status") == "UNKNOWN"]
    sampled = [p for p in params if _time_note(p)]
    folder = next((s for s in report.get("pipeline_log") or [] if s.get("step") == "12c"), {})
    no_folder = "not written" in (folder.get("result") or "")
    listed = ([f"{p.get('name')}: {_short(p.get('error') or (p.get('evidence') or {}).get('summary') or 'not measured')}"
               for p in unmeasured]
              + [f"{p.get('name')}: scored from a sample (time limit)" for p in sampled])
    where = listed[:8] + ([f"... and {len(listed) - 8} more (see 'All checks')"] if len(listed) > 8 else [])
    parts = []
    if unmeasured:
        parts.append(f"{len(unmeasured)} of {len(params)} checks could not be measured")
    if sampled:
        parts.append(f"{len(sampled)} check(s) were scored from a sample because they reached the "
                     f"{PARAMETER_TIMEOUT:.0f}-second limit")
    if no_folder:
        parts.append("the readable site folder was not written (no page could be read)")
    if not parts:
        return _verdict("12", OK, f"All {len(params)} checks were measured and the files were saved.")
    status = FAIL if len(unmeasured) >= len(params) / 2 else WARN
    fix = []
    if unmeasured:
        fix.append("Use 'Re-check unmeasured' on the report, or run the audit again; the reason is listed for each.")
    if sampled:
        fix.append("Sampled checks are still valid; their evidence says how much was judged.")
    return _verdict("12", status, "; ".join(parts) + ".", where, " ".join(fix))


def _short(text: str, limit: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3].rsplit(" ", 1)[0] + "..."


def site_issues(report: Optional[dict]) -> list[dict]:
    """Every check the site failed or only partly passed, worst first, with where the problem is."""
    report = report or {}
    params = [p for p in report.get("parameters") or [] if p.get("status") in ("FAIL", "PARTIAL")]
    if not params:
        return []
    site = page_fixes.site_from_report(report)
    out = []
    for row in sorted(params, key=lambda p: (p.get("score") if isinstance(p.get("score"), (int, float)) else 0)):
        try:
            fixes = page_fixes.fixes_for(row, site)
        except Exception:
            fixes = {"total": 0, "fixes": []}
        where = [f"{f['target']} - {_short(f['issue'])}" for f in (fixes.get("fixes") or [])[:3]]
        total = fixes.get("total") or 0
        if total > len(where):
            where.append(f"... {total:,} pages or items in all")
        if not where and row.get("checked_url_or_source"):
            where = [row["checked_url_or_source"]]
        out.append({"parameter_id": row.get("parameter_id"), "name": row.get("name"), "section": row.get("section"),
                    "status": row.get("status"), "score": row.get("score"),
                    "what": (row.get("evidence") or {}).get("summary") or row.get("recommendation") or "",
                    "fix": row.get("recommendation") or "", "where": where, "total": total})
    return out

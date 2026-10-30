from app.crawl_log import _outcome, build_crawl_log_pdf


def _page(**kw):
    base = {"url": "https://a.com/x", "final_url": "https://a.com/x", "status_code": 200, "word_count": 400,
            "scrape_confidence_label": "High", "found_via": "sitemap", "fetch_ms": 120, "raw_html_bytes": 2048}
    return {**base, **kw}


def test_outcome_tones():
    assert _outcome(_page())[1] == "ok"
    assert _outcome(_page(final_url="https://a.com/y"))[1] == "warn"
    assert _outcome(_page(status_code=403))[0].startswith("Refused")
    assert _outcome(_page(status_code=None, fetch_error="timeout"))[1] == "bad"
    assert _outcome(_page(blocked_reason="captcha"))[1] == "bad"


def test_pdf_with_recorded_steps():
    crawl = {"domain": "a.com", "scan_id": "s1", "input_url": "a.com", "sitemap_url_count": 1,
             "crawl_log": {"steps": [{"step": "0", "name": "Normalise the address", "result": "a.com → https://a.com/", "ms": 0},
                                     {"step": "4", "name": "Read robots.txt", "result": "HTTP 200", "ms": 50,
                                      "details": {"ai_bots": {"GPTBot": "blocked"}}}]},
             "pages": [_page(), _page(url="https://a.com/z", status_code=404)],
             "crawl_errors": ["https://a.com/q: timed out"]}
    report = {"pipeline_log": [{"step": "12a", "name": "Identify", "result": "A", "ms": 5}],
              "parameters": [{"parameter_id": "TECH-01", "name": "Robots", "section": "technical", "status": "PASS", "score": 100}]}
    pdf = build_crawl_log_pdf(crawl, report)
    assert pdf.startswith(b"%PDF")


def test_pdf_for_audit_saved_before_the_log_existed():
    pdf = build_crawl_log_pdf({"domain": "a.com", "pages": [_page()], "render": {"decision": "not_needed"}})
    assert pdf.startswith(b"%PDF")


def test_routine_redirects_read_as_ok_and_real_moves_show_the_new_path():
    assert _outcome(_page(url="http://a.com/x", final_url="https://www.a.com/x/")) == ("OK", "ok")
    assert _outcome(_page(url="https://a.com/x", final_url="https://a.com/new"))[0] == "Read, but moved to /new"


def test_long_addresses_wrap_inside_their_column():
    from app.crawl_log import _wrap
    text = _wrap("https://a.com/" + "/".join(["section"] * 30), 200)
    assert all(len(line) <= 48 for line in text.split("\n")) and text.replace("\n", "").startswith("https://a.com/")


def test_pdf_for_a_refused_site_leads_with_the_refusal():
    from app.crawl_log import _headline
    from app.crawl_diagnosis import diagnose
    crawl = {"domain": "a.com", "pages": [_page(status_code=403, word_count=0)],
             "access": {"blocked": True, "reason": "refused", "homepage_status": 403}}
    assert _headline(diagnose(crawl, {}), crawl["pages"], crawl["access"])[0] == "The audit could not read this website"
    assert build_crawl_log_pdf(crawl).startswith(b"%PDF")

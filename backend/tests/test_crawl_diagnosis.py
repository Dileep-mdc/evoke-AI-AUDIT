from app.crawl_diagnosis import FAIL, OK, SKIP, WARN, diagnose, site_issues


def _page(url, status=200, words=300, **kw):
    return {"url": url, "final_url": url, "status_code": status, "word_count": words, **kw}


def _by_step(verdicts):
    return {v["step"]: v for v in verdicts}


def test_every_step_gets_a_verdict():
    crawl = {"normalized_url": "https://a.com/", "origin": "https://a.com", "pages": [_page("https://a.com/")],
             "sitemap_url_count": 5, "render": {"decision": "not_needed"}}
    v = _by_step(diagnose(crawl, {"parameters": [{"parameter_id": "TECH-01", "status": "PASS", "evidence": {"decisions": []}}]}))
    assert list(v) == [str(n) for n in range(13)]
    assert v["3"]["status"] == OK and v["7"]["status"] == OK


def test_refused_site_points_every_file_at_the_refusal():
    crawl = {"normalized_url": "https://a.com/", "origin": "https://a.com", "pages": [_page("https://a.com/", 403, 0)],
             "access": {"blocked": True, "reason": "refused", "homepage_status": 403,
                        "refused": ["the homepage", "robots.txt", "the sitemap", "llms.txt"]},
             "render": {"decision": "skipped", "reason": "the site refused"}}
    v = _by_step(diagnose(crawl, {}))
    assert [v[s]["status"] for s in ("3", "4", "5", "6", "7")] == [FAIL] * 5
    assert v["9"]["status"] == SKIP and v["10"]["status"] == SKIP


def test_broken_and_thin_pages_name_where_they_are():
    pages = [_page("https://a.com/"), _page("https://a.com/gone", 404), _page("https://a.com/thin", words=10)]
    crawl = {"origin": "https://a.com", "pages": pages, "sitemap_url_count": 3, "render": {},
             "crawl_errors": ["https://a.com/timeout: timed out"],
             "crawl_log": {"steps": [{"step": "11", "details": {"page_cap_reached": False, "time_budget_reached": True,
                                                                 "time_budget_s": 900, "name_source": "og:site_name"}}]}}
    v = _by_step(diagnose(crawl, {}))
    assert v["7"]["status"] == WARN and "https://a.com/gone" in v["7"]["where"] and "https://a.com/timeout" in v["7"]["where"]
    assert v["8"]["status"] == WARN and "https://a.com/thin" in v["8"]["where"] and "Bot-check" not in v["8"]["fix"]
    assert v["9"]["status"] == WARN and "15-minute" in v["9"]["what"]


def test_unmeasured_checks_are_listed_and_site_issues_worst_first():
    report = {"domain": "a.com", "parameters": [
        {"parameter_id": "TECH-07", "name": "Speed", "status": "UNKNOWN", "error": "timed out"},
        {"parameter_id": "TECH-03", "name": "llms", "section": "technical", "status": "FAIL", "score": 0,
         "evidence": {"url": "https://a.com/llms.txt", "status": 404}, "recommendation": "Publish llms.txt"},
        {"parameter_id": "TECH-08", "name": "Mobile", "section": "technical", "status": "PARTIAL", "score": 70,
         "evidence": {}, "recommendation": "Add viewport"},
    ]}
    v = _by_step(diagnose({"pages": [_page("https://a.com/")]}, report))
    assert v["12"]["status"] == WARN and any("Speed" in w for w in v["12"]["where"])
    issues = site_issues(report)
    assert [i["parameter_id"] for i in issues] == ["TECH-03", "TECH-08"]
    assert any("llms.txt" in w for w in issues[0]["where"])


def test_http_results_in_plain_words():
    from app.crawl_diagnosis import http_words
    assert http_words(404) == "Page not found (HTTP 404)"
    assert http_words(403) == "Access denied (HTTP 403)"
    assert http_words(599) == "Server error (HTTP 599)"
    assert http_words(None, "ReadTimeout: timed out") == "No answer (timed out)"
    assert http_words(None, "Exceeded maximum allowed redirects.") == "Too many redirects (a redirect loop)"


def test_every_verdict_explains_the_step():
    v = diagnose({"pages": [_page("https://a.com/")]}, {})
    assert all(x["purpose"] for x in v) and all(x["why"] for x in v if x["step"] != "0")

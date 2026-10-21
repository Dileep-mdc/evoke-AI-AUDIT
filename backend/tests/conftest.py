"""Test-session defaults.

backend/.env carries a real OPENAI_API_KEY and ENABLE_LLM_SCORING=true so that local scans
score properly. config.py loads it through load_dotenv() at import, which means the test
suite inherits both -- so any test that reaches an LLM-assisted handler makes a real, billed
API call and asserts against whatever the model happened to say that run.

Every LLM-assisted handler keeps a deterministic fallback (test_frozen_spec.py enforces
that), so switching the model off site-wide here is exactly what the tests should be
measuring: the reproducible half of each parameter.

client.py binds ENABLE_LLM_SCORING by value at import, so patching app.config alone would
have no effect -- the flag has to be overridden where judge() actually reads it.
"""
from __future__ import annotations

import pytest

import app.llm.client as llm_client


@pytest.fixture(autouse=True, scope="session")
def no_live_llm_calls_during_tests():
    original = llm_client.ENABLE_LLM_SCORING
    llm_client.ENABLE_LLM_SCORING = False
    yield
    llm_client.ENABLE_LLM_SCORING = original


@pytest.fixture(autouse=True, scope="session")
def no_live_web_search_during_tests():
    """The same key would also run billed OpenAI web searches. Tests stub google_search(), so
    the off-page tools are pinned to that provider; test_openai_search.py covers the other."""
    import app.parameters.offpage_tools as tools

    original = tools.SEARCH_PROVIDER
    tools.SEARCH_PROVIDER = "google"
    yield
    tools.SEARCH_PROVIDER = original


@pytest.fixture(autouse=True)
def no_output_outside_the_test_directory(tmp_path_factory, monkeypatch):
    """A test that runs a scan or a re-check end to end reaches the writers of the site folder and
    the <company>.xlsx workbook, which default to the project root and the user's data directory.
    One did, and left site/site.xlsx in the project. Every test writes under its own temp dir."""
    import app.parameter_links as parameter_links
    import app.site_folder as site_folder

    root = tmp_path_factory.mktemp("output")
    monkeypatch.setattr(site_folder, "SITE_FOLDERS_ROOT", root / "sites")
    monkeypatch.setattr(parameter_links, "SITE_FOLDERS_ROOT", root / "sites")
    monkeypatch.setattr(parameter_links, "OUTPUT_DIR", root / "data")


@pytest.fixture(autouse=True)
def no_request_pacing_during_tests(monkeypatch):
    """backend/.env records the OpenAI project's requests-per-minute limit. Each test starts
    unpaced and with no learned limit; a test that is about pacing or sampling sets its own."""
    monkeypatch.setattr(llm_client, "_rpm_limit", None)
    monkeypatch.setattr(llm_client, "_tat", 0.0)

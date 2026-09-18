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

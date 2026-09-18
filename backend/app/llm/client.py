from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from typing import Optional

from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from ..config import ENABLE_LLM_SCORING, LLM_CONCURRENCY, LLM_MODEL, LLM_RETRIES, LLM_TIMEOUT, OPENAI_API_KEY

RETRY_BACKOFF_SECONDS = 0.6
# Rate limits get their own, longer schedule. Every parameter of a scan is model-scored now,
# so a run fires sixty-odd requests in a burst and 429s are an expected part of a healthy
# scan rather than a sign something is wrong. On the old linear 0.6s/1.2s retries a
# measured run lost 11 of 59 parameters to rate limiting -- each one silently demoted to
# its rules-based score -- because the retries landed inside the same rate-limit window
# that rejected the first attempt.
RATE_LIMIT_RETRIES = 5
RATE_LIMIT_BASE_SECONDS = 2.0
RATE_LIMIT_MAX_SLEEP = 30.0


def _rate_limit_delay(attempt: int, exc: Exception) -> float:
    """How long to wait before retrying a 429, honouring Retry-After when the API sends one.

    The server knows when its window reopens and we do not, so its header wins. Failing that,
    exponential backoff with jitter -- without the jitter, sixty parameters rejected together
    would retry together and collide again in lockstep.
    """
    retry_after = getattr(getattr(exc, "response", None), "headers", None)
    if retry_after:
        for header in ("retry-after-ms", "retry-after"):
            raw = retry_after.get(header)
            if raw:
                try:
                    seconds = float(raw) / (1000.0 if header.endswith("-ms") else 1.0)
                    return min(RATE_LIMIT_MAX_SLEEP, max(0.0, seconds))
                except (TypeError, ValueError):
                    pass
    return min(RATE_LIMIT_MAX_SLEEP, RATE_LIMIT_BASE_SECONDS * (2 ** attempt) * (0.5 + random.random()))

_semaphore = asyncio.Semaphore(LLM_CONCURRENCY)
_client: Optional[AsyncOpenAI] = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=LLM_TIMEOUT)
    return _client


@dataclass
class LLMResult:
    ok: bool
    text: str = ""
    parsed: Optional[dict] = None
    error: Optional[str] = None
    elapsed_ms: int = 0


async def judge(prompt: str, *, system: str = "", json_mode: bool = True) -> LLMResult:
    """Ask the configured LLM to judge something and return its response.

    Mirrors crawler/http.py's fetch() shape: bounded retries with backoff on
    transient errors, a single result object the caller checks .ok on. Never
    raises -- a disabled/misconfigured/failing LLM should never crash a scan,
    only cause the caller to fall back to its existing heuristic.
    """
    if not ENABLE_LLM_SCORING:
        return LLMResult(ok=False, error="LLM scoring is disabled (ENABLE_LLM_SCORING=false)")
    if not OPENAI_API_KEY:
        return LLMResult(ok=False, error="No OPENAI_API_KEY configured")

    # A separate system-role message was empirically found to trigger a 400 "could not
    # parse the JSON body" error from the API for some real (clean, ASCII) scraped-page
    # content combined with certain other parameters, reproducible 100% of the time for
    # the affected content and not caused by the content itself (verified byte-by-byte).
    # Folding the system instruction into a single user message avoids it entirely and
    # is a standard, safe pattern regardless of root cause.
    combined = f"{system}\n\n{prompt}" if system else prompt
    messages = [{"role": "user", "content": combined}]

    async with _semaphore:
        started = time.perf_counter()
        last_error = None
        # Rate-limit retries are counted separately so a burst of 429s cannot exhaust the
        # budget meant for genuine failures, and vice versa.
        attempt = 0
        rate_limited = 0
        while True:
            try:
                client = _get_client()
                kwargs = {"model": LLM_MODEL, "messages": messages, "temperature": 0}
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = await client.chat.completions.create(**kwargs)
                text = (resp.choices[0].message.content or "").strip()
                parsed = None
                if json_mode:
                    try:
                        parsed = json.loads(text)
                    except Exception as exc:
                        last_error = f"model did not return valid JSON: {exc}"
                        if attempt < LLM_RETRIES:
                            attempt += 1
                            await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
                            continue
                        return LLMResult(ok=False, text=text, error=last_error, elapsed_ms=int((time.perf_counter() - started) * 1000))
                return LLMResult(ok=True, text=text, parsed=parsed, elapsed_ms=int((time.perf_counter() - started) * 1000))
            except RateLimitError as exc:
                last_error = f"rate limited: {exc}"
                if rate_limited < RATE_LIMIT_RETRIES:
                    await asyncio.sleep(_rate_limit_delay(rate_limited, exc))
                    rate_limited += 1
                    continue
                return LLMResult(ok=False, error=last_error, elapsed_ms=int((time.perf_counter() - started) * 1000))
            except APITimeoutError as exc:
                last_error = f"timed out: {exc}"
            except APIConnectionError as exc:
                last_error = f"connection error: {exc}"
            except APIError as exc:
                last_error = f"API error: {exc}"
            except Exception as exc:
                last_error = str(exc)
            if attempt >= LLM_RETRIES:
                return LLMResult(ok=False, error=last_error, elapsed_ms=int((time.perf_counter() - started) * 1000))
            attempt += 1
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)

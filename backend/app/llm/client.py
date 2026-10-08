from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Optional

from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from ..config import (
    ENABLE_LLM_SCORING,
    LLM_CONCURRENCY,
    LLM_MODEL,
    LLM_RATE_LIMIT_BASE_SECONDS,
    LLM_RATE_LIMIT_MAX_SLEEP,
    LLM_RATE_LIMIT_RETRIES,
    LLM_RETRIES,
    LLM_RETRY_BACKOFF_SECONDS,
    LLM_RPM_LIMIT,
    LLM_SCORING_CONCURRENCY,
    LLM_TIMEOUT,
    OPENAI_API_KEY,
)
from . import usage

RETRY_BACKOFF_SECONDS = LLM_RETRY_BACKOFF_SECONDS
# Rate limits get their own, longer schedule. Every parameter of a scan is model-scored now,
# so a run fires sixty-odd requests in a burst and 429s are an expected part of a healthy
# scan rather than a sign something is wrong. On the old linear 0.6s/1.2s retries a
# measured run lost 11 of 59 parameters to rate limiting -- each one silently demoted to
# its rules-based score -- because the retries landed inside the same rate-limit window
# that rejected the first attempt.
RATE_LIMIT_RETRIES = LLM_RATE_LIMIT_RETRIES
RATE_LIMIT_BASE_SECONDS = LLM_RATE_LIMIT_BASE_SECONDS
RATE_LIMIT_MAX_SLEEP = LLM_RATE_LIMIT_MAX_SLEEP


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
# Reserved for the per-parameter scoring call so a classification fan-out cannot starve it.
_scoring_semaphore = asyncio.Semaphore(LLM_SCORING_CONCURRENCY)


def _lane_semaphore(lane: str) -> asyncio.Semaphore:
    return _scoring_semaphore if lane == "score" else _semaphore


# Requests-per-minute pacing. OpenAI caps each project's requests per minute, and a project can
# be capped far below its organisation (10 RPM was measured against an org allowing 30,000).
# Firing LLM_CONCURRENCY calls at once into a 10-RPM cap got most of them refused; each then
# backed off and collided again, until whole checks hit PARAMETER_TIMEOUT -- the bulk of a
# 70-80 minute audit. Once the limit is known, calls are spaced evenly to it instead, so every
# call waits its turn once rather than being refused and retried.
_RPM_IN_ERROR = re.compile(r"requests per min \(RPM\): Limit (\d+)", re.I)
_rpm_limit: Optional[int] = LLM_RPM_LIMIT or None
# Generic cell rate algorithm: the theoretical time the next request is due. OpenAI's limiter is a
# bucket that holds `limit` requests and refills one every 60/limit seconds, so this admits the same
# burst and then the same steady rate -- no faster (which would be refused), and no slower.
_tat = 0.0


def rpm_limit() -> Optional[int]:
    return _rpm_limit


def _learn_rpm(exc: Exception) -> bool:
    """Adopt the RPM limit a refusal names. True when it set (or lowered) the pace."""
    global _rpm_limit, _tat
    m = _RPM_IN_ERROR.search(str(exc))
    if not m:
        return False
    limit = max(1, int(m.group(1)))
    if _rpm_limit is None or limit < _rpm_limit:
        _rpm_limit = limit
        # Refused, so the server's bucket is empty: start from empty rather than from a full burst.
        _tat = time.monotonic() + (limit - 1) * 60.0 / limit
        return True
    return False


async def pace(requests: int = 1) -> None:
    """Wait until `requests` more requests fit under the RPM limit, and claim them.

    Lock-free: the schedule is read and advanced with no await in between, which is atomic on
    the event loop, so concurrent callers are admitted in order instead of all at once.
    """
    global _tat
    if not _rpm_limit:
        return
    interval = 60.0 / _rpm_limit
    burst = (_rpm_limit - 1) * interval
    now = time.monotonic()
    due = max(_tat, now)
    start = max(now, due - burst)
    _tat = due + requests * interval
    if start > now:
        await asyncio.sleep(start - now)


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


async def judge(prompt: str, *, system: str = "", json_mode: bool = True, lane: str = "classify") -> LLMResult:
    """Ask the configured LLM to judge something and return its response.

    Mirrors crawler/http.py's fetch() shape: bounded retries with backoff on
    transient errors, a single result object the caller checks .ok on. Never
    raises -- a disabled/misconfigured/failing LLM should never crash a scan,
    only cause the caller to fall back to its existing heuristic.

    `lane` picks which concurrency budget the call waits in, and the two must stay separate.
    Classification fans out: one handler can issue ninety batched calls at once. Scoring does
    not: it is one call per parameter, and it is the one that produces the number in the
    report. On a shared semaphore the scoring call queued behind every outstanding
    classification call and blew JUDGEMENT_TIMEOUT -- a measured run graded only 25 of 60
    parameters, with 24 timing out, so the report fell back to rules-based scores and looked
    exactly like a run with no model at all.
    """
    if not ENABLE_LLM_SCORING:
        return LLMResult(ok=False, error="LLM scoring is disabled (ENABLE_LLM_SCORING=false)")
    if not OPENAI_API_KEY:
        return LLMResult(ok=False, error="No OPENAI_API_KEY configured")

    # The scan's spending cap (llm/usage.py). Refused before any request is made, so the
    # caller falls back exactly as it does for any other unavailable model.
    meter = usage.current()
    if meter is not None and meter.over_budget():
        return LLMResult(ok=False, error=meter.refuse())

    # A separate system-role message was empirically found to trigger a 400 "could not
    # parse the JSON body" error from the API for some real (clean, ASCII) scraped-page
    # content combined with certain other parameters, reproducible 100% of the time for
    # the affected content and not caused by the content itself (verified byte-by-byte).
    # Folding the system instruction into a single user message avoids it entirely and
    # is a standard, safe pattern regardless of root cause.
    combined = f"{system}\n\n{prompt}" if system else prompt
    messages = [{"role": "user", "content": combined}]

    async with _lane_semaphore(lane):
        started = time.perf_counter()
        last_error = None
        # Rate-limit retries are counted separately so a burst of 429s cannot exhaust the
        # budget meant for genuine failures, and vice versa.
        attempt = 0
        rate_limited = 0
        while True:
            try:
                client = _get_client()
                await pace()
                kwargs = {"model": LLM_MODEL, "messages": messages, "temperature": 0}
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = await client.chat.completions.create(**kwargs)
                if meter is not None:
                    used = getattr(resp, "usage", None)
                    meter.record(getattr(used, "prompt_tokens", 0), getattr(used, "completion_tokens", 0),
                                 scales=lane == "batch")
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
                    # A refusal that names the RPM limit sets the pace; pace() then supplies the
                    # wait, so a backoff on top would only add idle time.
                    if not _learn_rpm(exc):
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

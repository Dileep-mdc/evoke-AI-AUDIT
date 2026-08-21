from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Optional

from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from ..config import ENABLE_LLM_SCORING, LLM_CONCURRENCY, LLM_MODEL, LLM_RETRIES, LLM_TIMEOUT, OPENAI_API_KEY

RETRY_BACKOFF_SECONDS = 0.6

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

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    async with _semaphore:
        started = time.perf_counter()
        last_error = None
        for attempt in range(LLM_RETRIES + 1):
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
                            await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                            continue
                        return LLMResult(ok=False, text=text, error=last_error, elapsed_ms=int((time.perf_counter() - started) * 1000))
                return LLMResult(ok=True, text=text, parsed=parsed, elapsed_ms=int((time.perf_counter() - started) * 1000))
            except RateLimitError as exc:
                last_error = f"rate limited: {exc}"
            except APITimeoutError as exc:
                last_error = f"timed out: {exc}"
            except APIConnectionError as exc:
                last_error = f"connection error: {exc}"
            except APIError as exc:
                last_error = f"API error: {exc}"
            except Exception as exc:
                last_error = str(exc)
            if attempt < LLM_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        return LLMResult(ok=False, error=last_error, elapsed_ms=int((time.perf_counter() - started) * 1000))

"""Each check's time limit, readable from anywhere the check's work runs.

The engine starts a deadline before it runs a check (parameters/engine.py). The slow parts of a
check -- batches of AI calls, link and sitemap requests, web searches, the browser speed test --
read it and stop starting new work once it has passed, so the check scores what finished in time
instead of being cut off with nothing. A context variable, so every task the check starts sees
its own check's deadline and no other's; outside a check there is none and nothing is limited.
"""
from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar, Token
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence

_deadline: ContextVar[Optional[float]] = ContextVar("check_deadline", default=None)

# Stands in for a result that was not produced because the time limit came first.
NOT_DONE = object()


def start(seconds: float) -> Token:
    return _deadline.set(time.monotonic() + max(0.0, seconds))


def reset(token: Token) -> None:
    _deadline.reset(token)


def remaining() -> Optional[float]:
    """Seconds left, or None when no check deadline is running."""
    end = _deadline.get()
    return None if end is None else end - time.monotonic()


def expired(margin: float = 0.0) -> bool:
    left = remaining()
    return left is not None and left <= margin


def spread(items: Sequence[Any], groups: int) -> list[Any]:
    """The items reordered so any first part of the list covers the whole of it evenly:
    0, k, 2k, ..., then 1, k+1, ... Work cut short by the deadline is then a sample of the site,
    not its first pages."""
    groups = max(1, min(groups, len(items) or 1))
    return [item for start_at in range(groups) for item in items[start_at::groups]]


async def gather_until(coros: Iterable[Awaitable[Any]]) -> list[Any]:
    """asyncio.gather(return_exceptions=True) that stops at the deadline: work still running then
    is cancelled and comes back as NOT_DONE."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    if not tasks:
        return []
    left = remaining()
    if left is None:
        return list(await asyncio.gather(*tasks, return_exceptions=True))
    await asyncio.wait(tasks, timeout=max(0.0, left))
    out = []
    for task in tasks:
        if task.done() and not task.cancelled():
            out.append(task.exception() if task.exception() is not None else task.result())
        else:
            task.cancel()
            out.append(NOT_DONE)
    pending = [t for t in tasks if t.cancelled() or not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return out


async def map_until(func: Callable[[Any], Awaitable[Any]], items: Sequence[Any], *, workers: int) -> list[Any]:
    """func over items with at most `workers` running at once, in order, until the deadline.

    A few workers instead of one task per item: thousands of queued requests from one check used to
    hold the shared request queue, so another check's handful of requests waited minutes behind them.
    Items not started or not finished in time come back as NOT_DONE.
    """
    results: list[Any] = [NOT_DONE] * len(items)
    position = 0

    async def worker() -> None:
        nonlocal position
        while position < len(items) and not expired():
            index = position
            position += 1
            try:
                results[index] = await func(items[index])
            except Exception as exc:  # recorded like gather(return_exceptions=True)
                results[index] = exc

    runners = [asyncio.ensure_future(worker()) for _ in range(max(1, min(workers, len(items))))]
    left = remaining()
    if left is None:
        await asyncio.gather(*runners)
        return results
    await asyncio.wait(runners, timeout=max(0.0, left))
    for runner in runners:
        runner.cancel()
    await asyncio.gather(*runners, return_exceptions=True)
    return results

"""A check's slow work stops at its time limit and scores what finished."""
import asyncio

from app import deadline
from app.llm import batch


def _run(coro):
    return asyncio.run(coro)


def test_spread_covers_the_whole_list_first():
    assert deadline.spread(list(range(10)), 3) == [0, 3, 6, 9, 1, 4, 7, 2, 5, 8]
    assert deadline.spread([], 4) == []


def test_no_deadline_means_nothing_is_cut():
    async def go():
        async def slow(x):
            await asyncio.sleep(0.01)
            return x * 2
        return await deadline.map_until(slow, [1, 2, 3], workers=2)
    assert _run(go()) == [2, 4, 6]


def test_map_until_stops_at_the_deadline():
    async def go():
        token = deadline.start(0.3)
        try:
            async def work(x):
                await asyncio.sleep(0.1)
                return x
            return await deadline.map_until(work, list(range(50)), workers=2)
        finally:
            deadline.reset(token)
    out = _run(go())
    done = [x for x in out if x is not deadline.NOT_DONE]
    assert 2 <= len(done) < 50
    assert out[:len(done)] == done


def test_gather_until_marks_unfinished_work():
    async def go():
        token = deadline.start(0.2)
        try:
            return await deadline.gather_until([asyncio.sleep(0, result="fast"), asyncio.sleep(5, result="slow")])
        finally:
            deadline.reset(token)
    assert _run(go()) == ["fast", deadline.NOT_DONE]


def test_judge_all_reports_a_time_limited_sample(monkeypatch):
    class Reply:
        ok = True
        error = None

        def __init__(self, n):
            self.parsed = {"results": [{"ok": True}] * n}

    calls = []

    async def fake_judge(prompt, system="", lane="classify"):
        calls.append(prompt)
        await asyncio.sleep(0 if len(calls) <= 2 else 5)
        return Reply(prompt.count("item"))

    monkeypatch.setattr(batch, "judge", fake_judge)

    async def go():
        token = deadline.start(0.3)
        try:
            return await batch.judge_all([{"item": i} for i in range(100)], lambda b: " ".join("item" for _ in b), batch_size=10)
        finally:
            deadline.reset(token)

    verdicts, coverage = _run(go())
    assert coverage.time_limited == 8
    assert coverage.assessed == len(verdicts) == 20
    assert not coverage.complete
    assert "time_limited" in coverage.as_evidence()

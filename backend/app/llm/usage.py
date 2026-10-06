"""What one scan spends on AI: calls, tokens, an estimated cost, and an optional spending cap.

Full-coverage classification (LLM_FULL_COVERAGE) puts every heading, list and page in front of
the model, so a large site can mean hundreds of calls, and nothing reported how many. Every
model call and every OpenAI web search now goes through a Meter belonging to the scan that made
it, so the progress screen can show spend as it happens and the report keeps the final figure.

The meter travels in a ContextVar. execute_scan() starts one inside the scan's own task, and
asyncio copies the context into every task and thread that scan starts -- so concurrent scans
never share a meter, and code outside a scan (a test, a one-off script) meters nothing.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from ..config import LLM_PRICE_INPUT_PER_M, LLM_PRICE_OUTPUT_PER_M, OPENAI_SEARCH_PRICE_PER_CALL


def _price(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * LLM_PRICE_INPUT_PER_M + output_tokens * LLM_PRICE_OUTPUT_PER_M) / 1_000_000


@dataclass
class Meter:
    budget_usd: float = 0.0
    calls: int = 0
    search_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    refused: int = 0
    # The share that grows with the site: batched classification over every heading, list and
    # page (llm/batch.py). The rest -- one scoring call per check, the web searches -- is roughly
    # the same for every scan, which is why the estimate keeps the two apart.
    scaling_calls: int = 0
    scaling_input_tokens: int = 0
    scaling_output_tokens: int = 0

    def cost_usd(self) -> float:
        return _price(self.input_tokens, self.output_tokens) + self.search_calls * OPENAI_SEARCH_PRICE_PER_CALL

    def scaling_cost_usd(self) -> float:
        return _price(self.scaling_input_tokens, self.scaling_output_tokens)

    def over_budget(self) -> bool:
        return self.budget_usd > 0 and self.cost_usd() >= self.budget_usd

    def refuse(self) -> str:
        """Count a call the cap stopped, and say why in the words the evidence will carry."""
        self.refused += 1
        return f"AI budget for this scan reached (${self.budget_usd:.2f}); call not made"

    def record(self, input_tokens: Optional[int], output_tokens: Optional[int], *, search: bool = False,
               scales: bool = False) -> None:
        if search:
            self.search_calls += 1
        else:
            self.calls += 1
        self.input_tokens += int(input_tokens or 0)
        self.output_tokens += int(output_tokens or 0)
        if scales:
            self.scaling_calls += 1
            self.scaling_input_tokens += int(input_tokens or 0)
            self.scaling_output_tokens += int(output_tokens or 0)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "search_calls": self.search_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd(), 4),
            "scaling_calls": self.scaling_calls,
            "scaling_cost_usd": round(self.scaling_cost_usd(), 4),
            "budget_usd": self.budget_usd or None,
            "budget_reached": self.over_budget(),
            "calls_refused": self.refused,
            "search_fee_counted": OPENAI_SEARCH_PRICE_PER_CALL > 0,
        }


_current: ContextVar[Optional[Meter]] = ContextVar("ai_usage_meter", default=None)


def start(budget_usd: float = 0.0) -> Meter:
    """Begin metering the calling task (and everything it starts) with a fresh meter."""
    meter = Meter(budget_usd=max(0.0, float(budget_usd or 0)))
    _current.set(meter)
    return meter


def current() -> Optional[Meter]:
    return _current.get()


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def estimate(pages: int, history: list[dict]) -> Optional[dict]:
    """Expected calls and cost for a crawl of `pages`, from what earlier scans spent.

    Split in two, because the halves behave differently: a fixed part every scan pays (scoring
    each check, the web searches) and a part that grows with the site (classifying every
    heading, list and page). Each is the median over recent scans, so one unusual site does not
    skew it -- and a one-page test scan cannot inflate the per-page rate with the fixed cost it
    happened to carry. None when there is no usable history yet.
    """
    usable = [h for h in history if h.get("pages") and h.get("calls") and "scaling_calls" in h]
    if not usable or pages <= 0:
        return None
    fixed_calls = _median([h["calls"] - h["scaling_calls"] for h in usable])
    fixed_cost = _median([h["cost_usd"] - h["scaling_cost_usd"] for h in usable])
    page_calls = _median([h["scaling_calls"] / h["pages"] for h in usable])
    page_cost = _median([h["scaling_cost_usd"] / h["pages"] for h in usable])
    return {"calls": round(fixed_calls + page_calls * pages), "cost_usd": round(fixed_cost + page_cost * pages, 4),
            "fixed_cost_usd": round(fixed_cost, 4), "cost_per_page_usd": round(page_cost, 6),
            "based_on_scans": len(usable), "pages": pages}

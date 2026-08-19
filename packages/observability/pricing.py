"""Token → cost, for the ``cost`` field on a generation span.

Most OpenAI-compatible endpoints — DeepSeek included — return token counts and
no cost, so cost has to be computed here or not shown at all. Not showing it is
the wrong answer: "which agent is expensive" is one of the two questions this
instrumentation exists to answer, and a dashboard of token counts across three
models with a 10x price spread does not answer it.

**Rates are USD per million tokens and are a snapshot, not an oracle.** They
drift, and a stale table quietly under-reports. Two things make that
survivable: an unknown model returns ``None`` rather than guessing (so a new
model shows "unknown", not "free"), and the table names the date it was checked
so a reader can judge how much to trust it.

Cached input is priced separately because DeepSeek's cache discount is roughly
10x and prompt caching is the main lever on this platform's LLM bill — a turn
that re-sends a 6,000-token system prompt costs an order of magnitude less on a
cache hit, and a cost figure that ignored that would misattribute the saving.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from packages.core.ports.tracing import GenerationUsage

__all__ = ["estimate_cost_usd"]


@dataclass(frozen=True, slots=True)
class _Rate:
    """USD per 1M tokens."""

    input_: float
    output: float
    cached_input: float | None = None


#: Checked 2026-08-18. Keys are matched case-insensitively by prefix, so
#: ``deepseek-chat`` also matches a dated variant like
#: ``deepseek-chat-2026-01-01`` without a table edit.
_RATES: Final[dict[str, _Rate]] = {
    "deepseek-reasoner": _Rate(input_=0.55, output=2.19, cached_input=0.07),
    "deepseek-chat": _Rate(input_=0.27, output=1.10, cached_input=0.07),
    "gpt-4o-mini": _Rate(input_=0.15, output=0.60, cached_input=0.075),
    "gpt-4o": _Rate(input_=2.50, output=10.00, cached_input=1.25),
    "gpt-4.1-mini": _Rate(input_=0.40, output=1.60, cached_input=0.10),
    "gpt-4.1": _Rate(input_=2.00, output=8.00, cached_input=0.50),
    "claude-sonnet-4": _Rate(input_=3.00, output=15.00, cached_input=0.30),
    "claude-haiku-4": _Rate(input_=1.00, output=5.00, cached_input=0.10),
    # The stub adapter reports usage so the graph's accounting paths are
    # exercised offline. It costs nothing, and saying so explicitly keeps a
    # local trace from displaying "unknown" on every span.
    "stub": _Rate(input_=0.0, output=0.0, cached_input=0.0),
}


def _rate_for(model: str) -> _Rate | None:
    key = (model or "").strip().lower()
    if not key:
        return None
    if (exact := _RATES.get(key)) is not None:
        return exact
    # Longest prefix wins, so `deepseek-reasoner` is not shadowed by a shorter
    # `deepseek-` entry someone adds later.
    best: _Rate | None = None
    best_len = 0
    for prefix, rate in _RATES.items():
        if key.startswith(prefix) and len(prefix) > best_len:
            best, best_len = rate, len(prefix)
    return best


def estimate_cost_usd(model: str, usage: GenerationUsage) -> float | None:
    """Cost for one call, or ``None`` when the model is not in the table.

    ``None`` rather than ``0.0`` is load-bearing: a zero renders as "this call
    was free", which for an unpriced model is a false statement that survives
    into a cost dashboard. Unknown must look unknown.
    """
    rate = _rate_for(model)
    if rate is None:
        return None

    # Cached prompt tokens are reported *inside* prompt_tokens by both OpenAI
    # and DeepSeek, so they are subtracted before pricing the remainder at the
    # full input rate. Counting them twice would inflate every cached turn.
    cached = max(0, usage.cached_prompt_tokens)
    fresh = max(0, usage.prompt_tokens - cached)
    cached_rate = rate.cached_input if rate.cached_input is not None else rate.input_

    total = (
        fresh * rate.input_
        + cached * cached_rate
        + max(0, usage.completion_tokens) * rate.output
    ) / 1_000_000.0
    return round(total, 8)

"""The guard node and the router — 03 §4.3.

Every edge in a compiled graph passes through one ``__guard`` node. That is the
design decision that makes budgets and loop detection **unbypassable**: there
is no compiled edge that skips it, so a manifest cannot author its way around
the limits and a node kind added later gets the guard for free.

The alternative — a check inside each node — means every future node kind must
remember to call it, and the one that forgets is a manifest that runs forever.
One choke point costs an extra graph hop per turn (sub-millisecond; the guard
does no I/O) and buys a structural guarantee.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from packages.ai.limits import CEILINGS
from packages.ai.state import ConversationState
from packages.verticals.manifest import GraphSpec, NodeKind

__all__ = ["build_router", "guard_node"]

log = structlog.get_logger(__name__)


def guard_node(
    spec: GraphSpec,
) -> Callable[[ConversationState], Awaitable[dict[str, Any]]]:
    """Enforce every budget, once per hop.

    Sets ``overflow_reason`` rather than raising. An exception here would
    abandon the turn with nothing said to the caller; setting the flag lets the
    router send the run to the overflow terminal, which produces a graceful
    "let me put you through to someone" instead of a dropped call.
    """

    async def run(state: ConversationState) -> dict[str, Any]:
        if state.get("overflow_reason"):
            return {}

        steps = int(state.get("step_count", 0))
        max_steps = int(state.get("max_steps", spec.max_steps))
        if steps >= max_steps:
            log.info("graph.guard.step_budget", steps=steps, limit=max_steps)
            return {"overflow_reason": "step_budget_exhausted"}

        tool_calls = int(state.get("tool_call_count", 0))
        max_tool_calls = int(state.get("max_tool_calls", spec.max_tool_calls))
        if tool_calls >= max_tool_calls:
            log.info("graph.guard.tool_budget", calls=tool_calls, limit=max_tool_calls)
            return {"overflow_reason": "tool_budget_exhausted"}

        deadline = int(state.get("deadline_ms", 0))
        if deadline and int(time.monotonic() * 1000) >= deadline:
            log.info("graph.guard.wall_clock", deadline_ms=deadline)
            return {"overflow_reason": "wall_clock_exceeded"}

        # ── Anti-ping-pong (03 §4.3) ──────────────────────────────────────
        # Two agents handing back and forth burn the step budget while saying
        # nothing new, and to the caller it sounds like the line went quiet.
        # Counting *recent* visits rather than total: an agent legitimately
        # runs many times across a long conversation, so the whole history is
        # the wrong window. The last few hops are where a loop shows up.
        history = list(state.get("agent_history", []))
        recent = history[-(CEILINGS.max_consecutive_same_agent * 2) :]
        if recent:
            most_common, count = Counter(recent).most_common(1)[0]
            if count > CEILINGS.max_consecutive_same_agent:
                log.info(
                    "graph.guard.ping_pong", agent=most_common, count=count
                )
                return {"overflow_reason": "routing_loop_detected"}

        return {}

    return run


def build_router(
    spec: GraphSpec, *, overflow: str
) -> Callable[[ConversationState], str]:
    """The conditional edge out of the guard.

    Pure and synchronous: given a state, it names the next node. That makes
    every routing rule assertable in a unit test without a graph, a model, or
    an event loop — which matters because routing is where a manifest change
    most often has a non-obvious effect.

    Precedence, and each step is load-bearing:

    1. **Overflow wins.** A budget breach must not be overridden by a pending
       ``next_agent`` a node set before the guard ran.
    2. **A declared outcome ends the run.** A terminal node has spoken.
    3. **An explicit ``next_agent``** — the supervisor's decision.
    4. **First hop goes to the entrypoint.**
    5. **Otherwise end.** A specialist that returned control with no successor
       has finished its turn; the conversation waits for the next user message.
       Returning to the supervisor here instead would make every turn two model
       calls and reopen the ping-pong the guard just closed.
    """
    from langgraph.graph import END

    agent_keys = {a.key for a in spec.agents}
    terminals = {a.key for a in spec.agents if a.kind is NodeKind.TERMINAL}

    def route(state: ConversationState) -> str:
        if state.get("overflow_reason"):
            return overflow

        if state.get("outcome") is not None:
            return END

        current = state.get("current_agent")
        if current in terminals:
            return END

        requested = state.get("next_agent")
        if requested and requested in agent_keys:
            return str(requested)

        if not state.get("agent_history"):
            return spec.entrypoint

        return END

    return route

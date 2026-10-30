"""Hold side-effecting tools until a speculative turn is confirmed.

A speculative voice turn starts the agent *before* the caller is known to have
finished — on an early end-of-turn signal — so that a confirmed turn already
has its answer half-written. Most of what the agent does in that window is
safe to throw away: reading the diary, drafting a reply. A booking is not. If
the caller was only pausing ("book me Thursday at ten … actually, eleven") the
ten o'clock slot would already be taken by the time they finish.

So the run is started inside :func:`side_effects_held`, and the executor calls
:func:`await_side_effect_clearance` before any tool whose spec says
``side_effecting``. That call returns at once outside a speculative run, and
inside one it waits until the turn is confirmed. A turn the caller resumed is
cancelled while it waits, and the write never happens.

This is a mechanism rather than an instruction to the model on purpose: the
model cannot know the turn is speculative, and must not need to.

A ``ContextVar`` because the graph run is several tasks deep below the code
that starts it; ``asyncio`` copies context into every task it creates, so the
gate set at the top is visible at the tool call without threading a parameter
through the runner, the graph and every node.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

import structlog

__all__ = ["await_side_effect_clearance", "side_effects_held"]

log = structlog.get_logger(__name__)

_GATE: ContextVar[asyncio.Event | None] = ContextVar("side_effect_gate", default=None)


@contextlib.contextmanager
def side_effects_held(gate: asyncio.Event) -> Iterator[None]:
    """Run the enclosed code with side-effecting tools held behind ``gate``."""
    token = _GATE.set(gate)
    try:
        yield
    finally:
        _GATE.reset(token)


async def await_side_effect_clearance(tool: str) -> None:
    """Block until the current turn may write. Immediate when not speculative."""
    gate = _GATE.get()
    if gate is None or gate.is_set():
        return
    log.info("ai.tool.held_for_confirmation", tool=tool)
    await gate.wait()
    log.info("ai.tool.released", tool=tool)

"""The per-turn stream sink — how a graph node reaches the wire.

A graph node needs to publish tokens as they arrive, but a compiled graph is
**cached and shared across every turn for a tenant** (03 §4.4). A node closing
over one turn's sink would therefore keep streaming into that turn's socket for
as long as the cache entry lived — turn N's tokens arriving in turn N-1's
browser, for a different customer, on the same tenant.

So the sink is bound per turn in a ``ContextVar`` and nodes reach it through
:func:`emit`.

**Why a ``ContextVar`` and not a module global.** Exactly the reason
:mod:`packages.core.tenancy` gives: asyncio tasks inherit context at creation,
so a value set at the top of a turn is visible to the graph task that turn
spawns and to nothing else. A module global is shared by every coroutine on the
event loop — which in a process serving every tenant is a cross-tenant leak by
construction.

This module is separate from :mod:`packages.ai.runner` only to keep the import
graph acyclic: ``runner`` imports ``cache`` imports ``compiler`` imports
``nodes``, so ``nodes`` cannot import ``runner``. Both import this.
"""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable
from typing import Any

__all__ = ["StreamSink", "bind_sink", "emit", "unbind_sink"]

#: A coroutine taking an event type and a JSON-serializable payload.
StreamSink = Callable[[str, dict[str, Any]], Awaitable[None]]

_ACTIVE_SINK: contextvars.ContextVar[StreamSink | None] = contextvars.ContextVar(
    "receptionai_graph_sink", default=None
)


def bind_sink(sink: StreamSink) -> contextvars.Token[StreamSink | None]:
    """Bind ``sink`` for the current task. Returns the token to unbind with."""
    return _ACTIVE_SINK.set(sink)


def unbind_sink(token: contextvars.Token[StreamSink | None]) -> None:
    """Restore the previous sink. Always called from a ``finally``.

    **Tolerates a cross-context reset**, which is not defensive padding — it is
    the ordinary case for an async generator. A generator's ``finally`` runs
    whenever the generator is closed or garbage-collected, and that can be a
    different ``Context`` than the one its body was suspended in: Starlette
    closes a ``StreamingResponse`` body iterator from its own task group, so a
    client disconnect unwinds the generator from a context that never saw the
    ``set()``. ``ContextVar.reset`` then raises ``ValueError: was created in a
    different Context``.

    Swallowing it is correct rather than lazy. The token is per-turn and the
    context it belonged to is being discarded either way, so there is nothing
    left to restore — and clearing the var explicitly covers the case where the
    *current* context does hold a binding. Letting the ``ValueError`` escape
    would replace whatever actually ended the turn (a disconnect, a provider
    failure) with a confusing error from the cleanup path.
    """
    try:
        _ACTIVE_SINK.reset(token)
    except ValueError:
        # A different context. Clear whatever this one holds instead, so a
        # stale sink cannot outlive its turn.
        _ACTIVE_SINK.set(None)


async def emit(event_type: str, data: dict[str, Any]) -> None:
    """Publish an event from inside a graph node, if a sink is bound.

    A no-op when nothing is bound rather than an error: an eval harness or a
    Temporal activity runs the same graph with no client attached, and those
    callers should not have to install a null sink to use it.
    """
    sink = _ACTIVE_SINK.get()
    if sink is not None:
        await sink(event_type, data)

"""Server-Sent Events — PHASE1-API-CONTRACT, 06 §3.2/§3.3.

The wire format is fixed by the contract and both frontends are built against
it::

    id: 7
    event: token
    data: {"turn_id":"019f...","delta":"Sorry you're"}

    : heartbeat

Four mechanics, each with a failure mode behind it.

**Event ids are monotonic per turn.** ``Last-Event-ID`` resumes from one, so it
must order. They are integers rather than the Redis Stream entry ids 06 §3.2
describes because Phase 1 streams straight from the graph runner to the socket
with no Redis Stream in between (that arrives with voice, which needs fan-out
to a second consumer). An integer is monotonic, comparable, and survives the
substitution later — a client written against it keeps working when the ids
become Redis entry ids, because it only ever compares them for ordering.

**The heartbeat is a comment line, every 15 s.** Proxies and load balancers reap
idle connections at 30-60 s, and a chat where the model is thinking for 20
seconds is idle by that measure. A comment costs three bytes and is discarded
by every conforming ``EventSource``, so it cannot be mistaken for data.

**``retry:`` is sent first, always.** ``EventSource`` reconnects on a
browser-chosen interval, which is fine for a blip and wrong for an outage —
ten thousand widgets reconnecting in lockstep is a thundering herd. The server
proposes 5 s and the client layers jitter on top (06 §3.3).

**Disconnect must cancel the producer.** This is the one that costs money. When
the browser goes away the response generator is closed, and the ``finally``
below closes the upstream generator, which cancels the graph task, which closes
the provider socket. Without that chain a closed tab keeps generating tokens
and keeps billing until the model stops on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Final

import structlog

__all__ = [
    "SSE_HEADERS",
    "SSEEvent",
    "format_sse",
    "heartbeat_frame",
    "retry_frame",
    "sse_stream",
]

log = structlog.get_logger(__name__)

#: Headers every SSE response carries, and why each is required.
SSE_HEADERS: Final[dict[str, str]] = {
    # The media type. Without it a browser treats the body as a download.
    "Content-Type": "text/event-stream; charset=utf-8",
    # `no-cache` alone is not enough — some proxies still buffer — but it is
    # the part clients honour.
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # nginx-specific and load-bearing in practice: without it nginx buffers the
    # whole response and the stream arrives as one blob at the end, which looks
    # exactly like "streaming is broken" and is the single most common SSE
    # deployment failure.
    "X-Accel-Buffering": "no",
}

#: The reconnect interval proposed to the client, milliseconds.
_RETRY_MS: Final = 5_000


@dataclass(frozen=True, slots=True)
class SSEEvent:
    """One event, before framing."""

    event: str
    data: dict[str, Any]
    id: int | None = None


def format_sse(event: SSEEvent) -> str:
    """Frame one event.

    ``data`` is emitted as a single compact JSON line. Multi-line ``data:`` is
    legal SSE, but a JSON payload containing a newline would silently split
    into two ``data:`` lines that the client rejoins with ``\\n`` — producing
    valid-looking JSON that is subtly not what was sent. ``json.dumps`` escapes
    newlines, so one event is always one line.
    """
    lines: list[str] = []
    if event.id is not None:
        lines.append(f"id: {event.id}")
    lines.append(f"event: {event.event}")
    lines.append(f"data: {json.dumps(event.data, separators=(',', ':'), default=str)}")
    return "\n".join(lines) + "\n\n"


def retry_frame(retry_ms: int = _RETRY_MS) -> str:
    """The reconnect hint, sent as the first frame of every stream."""
    return f"retry: {retry_ms}\n\n"


def heartbeat_frame() -> str:
    """A comment line. Discarded by the client; keeps the proxy from reaping."""
    return ": heartbeat\n\n"


async def sse_stream(
    events: AsyncIterator[SSEEvent],
    *,
    heartbeat_seconds: float = 15.0,
    idle_timeout_seconds: float = 900.0,
    start_id: int = 0,
    on_close: Callable[[], Any] | None = None,
) -> AsyncIterator[str]:
    """Frame an event stream, with heartbeats and clean cancellation.

    Args:
        events: The upstream events. Consumed exactly once.
        start_id: The id of the *last event already delivered*. Ids continue
            from ``start_id + 1``, which is what makes ``Last-Event-ID`` resume
            produce a contiguous sequence rather than restarting at 1 and
            confusing a client that deduplicates on id.
        on_close: Ran when the stream ends for any reason, including a client
            disconnect. Where the caller persists the turn — a disconnect must
            still write the row, or a customer's message vanishes because they
            closed the tab.

    The heartbeat is implemented by racing the next event against a timer
    rather than by a background task, so there is exactly one consumer of
    ``events`` and no chance of two coroutines pulling from it.
    """
    event_id = start_id
    idle_elapsed = 0.0

    yield retry_frame()

    iterator = events.__aiter__()
    pending: asyncio.Task[SSEEvent] | None = None

    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(_anext(iterator))

            done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)

            if not done:
                idle_elapsed += heartbeat_seconds
                if idle_elapsed >= idle_timeout_seconds:
                    # 06 §3.3(4): a stream is not a subscription to the future.
                    # After genuine idleness the generator returns and the
                    # connection closes normally; the client treats a clean
                    # close as "the conversation went quiet", not an error.
                    log.info("sse.idle_timeout", idle_seconds=idle_elapsed)
                    break
                yield heartbeat_frame()
                continue

            idle_elapsed = 0.0
            try:
                event = pending.result()
            except StopAsyncIteration:
                break
            finally:
                pending = None

            event_id += 1
            yield format_sse(
                SSEEvent(event=event.event, data=event.data, id=event_id)
            )

    except asyncio.CancelledError:
        # The client disconnected. Propagate: swallowing it would leave
        # Starlette believing the response is still being produced.
        log.info("sse.client_disconnected", last_event_id=event_id)
        raise
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending
        # Closing the upstream generator is the link in the chain that stops
        # the model. `aclose()` throws GeneratorExit into it, which unwinds
        # `GraphRunner.run_turn`'s finally, which cancels the graph task, which
        # closes the provider socket.
        with contextlib.suppress(Exception):
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                await aclose()
        if on_close is not None:
            with contextlib.suppress(Exception):
                result = on_close()
                if asyncio.iscoroutine(result):
                    await result


async def _anext(iterator: Any) -> SSEEvent:
    return await iterator.__anext__()


def parse_last_event_id(raw: str | None) -> int:
    """Read a ``Last-Event-ID`` header into a resume cursor.

    Returns 0 — "start from the beginning" — for anything unparseable. The
    header is client-supplied and is echoed back into our id sequence, so it
    must not be trusted; a negative or absurd value would produce ids that go
    backwards, and a client deduplicating on id would then silently discard
    every subsequent event.
    """
    if not raw:
        return 0
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return 0
    return value if 0 <= value < 1_000_000 else 0

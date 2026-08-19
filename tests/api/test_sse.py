"""SSE framing, resume, heartbeat and cancellation.

Split in two halves deliberately:

* the **pure** tests, against :mod:`apps.api.sse` alone, which need no stack
  and assert the wire format byte for byte;
* the **integration** tests, which drive the real endpoint and assert that
  tokens genuinely arrive incrementally rather than as one buffered blob —
  the failure that makes a chat UI look broken and that a unit test of the
  formatter cannot detect.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from apps.api.sse import (
    SSEEvent,
    format_sse,
    heartbeat_frame,
    parse_last_event_id,
    retry_frame,
    sse_stream,
)
from httpx import AsyncClient

from tests.api.conftest import requires_stack

# ─────────────────────────────────────────────────────────────────────────────
# Pure framing — no stack required
# ─────────────────────────────────────────────────────────────────────────────


def test_event_framing_matches_the_frozen_contract() -> None:
    """The exact bytes PHASE1-API-CONTRACT shows, in the order it shows them."""
    frame = format_sse(
        SSEEvent(event="token", data={"turn_id": "019fd1ef", "delta": "Sorry you're"}, id=7)
    )

    assert frame == (
        "id: 7\n"
        'event: token\n'
        'data: {"turn_id":"019fd1ef","delta":"Sorry you\'re"}\n'
        "\n"
    )


def test_data_is_always_exactly_one_line() -> None:
    """A newline inside the payload must not split the ``data:`` field.

    Multi-line ``data:`` is legal SSE, and the client rejoins the pieces with
    ``\\n`` — so a payload containing a newline would arrive as valid-looking
    JSON that is subtly not what was sent. ``json.dumps`` escapes it.
    """
    frame = format_sse(SSEEvent(event="token", data={"delta": "line one\nline two"}, id=1))

    data_lines = [line for line in frame.splitlines() if line.startswith("data:")]
    assert len(data_lines) == 1
    assert json.loads(data_lines[0][5:].strip())["delta"] == "line one\nline two"


def test_heartbeat_is_a_comment() -> None:
    """A comment line, so a conforming client discards it as non-data."""
    assert heartbeat_frame() == ": heartbeat\n\n"
    assert heartbeat_frame().startswith(":")


def test_retry_frame_proposes_a_reconnect_interval() -> None:
    assert retry_frame(5000) == "retry: 5000\n\n"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12", 12),
        ("0", 0),
        (None, 0),
        ("", 0),
        ("not-a-number", 0),
        # Client-supplied and echoed into our own id sequence, so a hostile
        # value must not make ids go backwards — a client deduplicating on id
        # would then silently discard everything after it.
        ("-5", 0),
        ("99999999", 0),
    ],
)
def test_last_event_id_parsing_is_defensive(raw: str | None, expected: int) -> None:
    assert parse_last_event_id(raw) == expected


async def test_ids_are_monotonic_and_resume_from_the_cursor() -> None:
    """``Last-Event-ID`` continues the sequence rather than restarting it.

    Restarting at 1 after a resume would hand the client ids it has already
    seen for *different* events, and a client that deduplicates on id would
    drop the whole rest of the turn.
    """

    async def events() -> AsyncIterator[SSEEvent]:
        for index in range(3):
            yield SSEEvent(event="token", data={"delta": str(index)})

    frames = [
        chunk
        async for chunk in sse_stream(events(), heartbeat_seconds=5.0, start_id=41)
    ]
    ids = [
        int(line[4:])
        for chunk in frames
        for line in chunk.splitlines()
        if line.startswith("id: ")
    ]

    assert ids == [42, 43, 44]


async def test_first_frame_is_always_retry() -> None:
    async def events() -> AsyncIterator[SSEEvent]:
        yield SSEEvent(event="token", data={"delta": "hi"})

    frames = [chunk async for chunk in sse_stream(events(), heartbeat_seconds=5.0)]
    assert frames[0] == retry_frame()


async def test_heartbeat_is_emitted_while_the_producer_is_silent() -> None:
    """Proxies reap idle connections; a thinking model looks idle."""

    async def slow() -> AsyncIterator[SSEEvent]:
        await asyncio.sleep(0.25)
        yield SSEEvent(event="token", data={"delta": "finally"})

    frames = [
        chunk
        async for chunk in sse_stream(
            slow(), heartbeat_seconds=0.05, idle_timeout_seconds=5.0
        )
    ]

    assert heartbeat_frame() in frames
    assert any("finally" in chunk for chunk in frames)


async def test_idle_timeout_closes_the_stream_cleanly() -> None:
    """06 §3.3(4): a stream is not a subscription to the future.

    After genuine idleness the generator returns and the connection closes
    *normally* — the client reads a clean close as "the conversation went
    quiet", not as an error, and does not reconnect unless the user acts.
    """

    async def never() -> AsyncIterator[SSEEvent]:
        await asyncio.sleep(30)
        yield SSEEvent(event="token", data={})

    frames = [
        chunk
        async for chunk in sse_stream(
            never(), heartbeat_seconds=0.05, idle_timeout_seconds=0.2
        )
    ]

    # Returned rather than raised, and it emitted heartbeats while waiting.
    assert heartbeat_frame() in frames


async def test_client_disconnect_closes_the_producer() -> None:
    """The link in the chain that stops the model — and the billing.

    When the browser goes away, the response generator is closed, and that
    close must propagate into the upstream generator. Without it a closed tab
    keeps generating tokens until the provider stops on its own.
    """
    closed = asyncio.Event()

    async def producer() -> AsyncIterator[SSEEvent]:
        try:
            while True:
                yield SSEEvent(event="token", data={"delta": "x"})
                await asyncio.sleep(0.01)
        finally:
            closed.set()

    stream = sse_stream(producer(), heartbeat_seconds=5.0)
    # Consume the retry frame and two events, then abandon the stream.
    await stream.__anext__()
    await stream.__anext__()
    await stream.__anext__()
    await stream.aclose()

    await asyncio.wait_for(closed.wait(), timeout=1.0)


# ─────────────────────────────────────────────────────────────────────────────
# Against the real endpoint
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@requires_stack
async def test_demo_stream_delivers_tokens_incrementally(
    client: AsyncClient, demo_session: dict
) -> None:
    """Tokens arrive as separate frames, in order, ending with turn_complete.

    The assertion that matters is ``len(tokens) > 1``. A stream that buffers
    the whole reply and flushes once is indistinguishable from a working one in
    a response-body assertion, and it is the single most common SSE deployment
    failure (usually an intermediary ignoring ``X-Accel-Buffering``).
    """
    conversation_id = demo_session["conversation_id"]

    accepted = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        params={"ticket": demo_session["ticket"]},
        json={"content": "I need to see someone about my tooth"},
    )
    assert accepted.status_code == 202, accepted.text
    turn_id = accepted.json()["turn_id"]

    events: list[tuple[str, dict]] = []
    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/stream",
        params={"ticket": demo_session["ticket"]},
        timeout=30.0,
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # The header that stops nginx buffering the whole response.
        assert response.headers.get("x-accel-buffering") == "no"

        event_name: str | None = None
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: ") and event_name:
                events.append((event_name, json.loads(line[6:])))
                if event_name in ("turn_complete", "error"):
                    break

    kinds = [name for name, _ in events]
    assert "error" not in kinds, events

    tokens = [payload["delta"] for name, payload in events if name == "token"]
    assert len(tokens) > 1, f"reply arrived as one blob, not a stream: {tokens}"
    assert "".join(tokens).strip(), "the assistant said nothing"

    assert kinds[-1] == "turn_complete"
    completion = events[-1][1]
    assert completion["finish_reason"] == "stop"
    assert completion["turn_id"] == turn_id

    # 06 §3.2: usage is never sent to a widget audience — it leaks unit
    # economics to an anonymous member of the public.
    assert "usage" not in completion

    # Every TURN event carries the turn id so a client can correlate.
    #
    # `session` and `status` are stream-lifecycle frames, not turn frames: the
    # session credential is minted when the connection opens, before any turn
    # exists, so a `turn_id` on it would have to be invented. They are excluded
    # by name rather than by "has a turn_id", so a turn event that lost its id
    # still fails this.
    lifecycle = {"session", "status"}
    turn_events = [(name, p) for name, p in events if name not in lifecycle]
    assert turn_events, "the stream carried no turn events at all"
    assert all(payload.get("turn_id") == turn_id for _, payload in turn_events)

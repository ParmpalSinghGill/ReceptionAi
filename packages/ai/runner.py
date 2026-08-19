"""``GraphRunner`` — one user turn in, an event stream out. 03 §4.5.

The shape of this module is dictated by one problem: a LangGraph run is a
coroutine that returns a final state, but the API must emit tokens *while it is
running*. Reconciling those needs a producer and a consumer.

The design is a bounded queue plus a background task:

* The node factory is built with a ``sink`` that puts events on the queue.
* :meth:`GraphRunner.run_turn` starts the graph in a task and drains the queue
  as an async generator.
* A sentinel closes the generator when the graph completes.

**Why a bounded queue.** An unbounded one lets a fast provider outrun a slow
client until the process runs out of memory — one stalled browser becoming an
OOM. Bounded means the sink's ``put`` awaits, which back-pressures into the
provider stream and eventually into the socket, where TCP already knows what to
do.

**Cancellation is the load-bearing path**, not an edge case. When the client
disconnects the SSE endpoint cancels this generator; the ``finally`` cancels
the graph task and awaits it. Without that await, the graph keeps running —
still calling the model, still being billed — for a browser tab that closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog
from packages.ai.cache import CompiledGraphCache
from packages.ai.state import ConversationState, new_state
from packages.ai.streaming import bind_sink, unbind_sink
from packages.core.errors import DomainError, PortUnavailableError
from packages.core.ports.llm import Message, Usage
from packages.core.ports.tracing import TracingPolicy, TurnContext
from packages.observability.tracing import turn_trace

__all__ = ["GraphRunner", "TurnEvent", "TurnResult"]

log = structlog.get_logger(__name__)

#: Queue depth. Large enough that ordinary token bursts never block, small
#: enough that a wedged consumer is bounded at a few hundred short strings.
_QUEUE_MAXSIZE = 256

_SENTINEL: Any = object()


@dataclass(frozen=True, slots=True)
class TurnEvent:
    """One event on the way to the wire.

    Deliberately *not* SSE-shaped: framing (``id:``, ``event:``, ``data:``) is
    the API layer's job. This carries the vocabulary from 06 §3.2 and lets
    ``apps/api`` decide what a given audience may see — a widget must not
    receive usage, and that decision belongs where the principal is known.
    """

    type: str
    data: dict[str, Any]


@dataclass(slots=True)
class TurnResult:
    """What the caller persists after the stream ends."""

    text: str = ""
    agent_key: str | None = None
    outcome: str | None = None
    finish_reason: str = "stop"
    usage: Usage | None = None
    latency_ms: int = 0
    overflow_reason: str | None = None
    error: DomainError | None = None
    events: list[TurnEvent] = field(default_factory=list)
    #: Facts the agents gathered this turn — a booking reference, the slots that
    #: were offered, the customer's name. Carried out so the caller can persist
    #: them: without that, every turn starts blank and the model re-derives (or
    #: re-asks for) what it already knew, which is how an agent offers three
    #: times and then cannot book any of them.
    collected_data: dict[str, Any] = field(default_factory=dict)


class GraphRunner:
    """Runs one turn against a tenant's compiled graph."""

    def __init__(self, cache: CompiledGraphCache) -> None:
        self._cache = cache

    async def run_turn(
        self,
        *,
        tenant_id: UUID,
        conversation_id: UUID,
        turn_id: UUID,
        user_text: str,
        history: list[Message],
        channel: str = "web_chat",
        locale: str = "en-GB",
        timezone: str = "UTC",
        manifest_version: str | None = None,
        collected_data: dict[str, Any] | None = None,
        result: TurnResult | None = None,
        tracing_policy: TracingPolicy | None = None,
    ) -> AsyncIterator[TurnEvent]:
        """Stream one turn.

        Args:
            tracing_policy: How much of this tenant's prompt/completion text
                may be recorded. ``None`` means masked — the safe default —
                so a caller that does not know about tracing cannot
                accidentally opt a tenant in.
            result: An out-parameter the caller passes in so it can persist the
                turn after the stream ends. An out-parameter rather than a
                return value because an async generator's return value is not
                reachable by an ``async for``, and the alternative — yielding a
                final event carrying the whole turn — would put the assistant's
                full text on the wire twice.
        """
        out = result if result is not None else TurnResult()
        started = time.perf_counter()

        queue: asyncio.Queue[TurnEvent | Any] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

        async def sink(event_type: str, data: dict[str, Any]) -> None:
            await queue.put(TurnEvent(type=event_type, data=data))

        try:
            compiled = await self._cache.get(tenant_id, manifest_version)
        except DomainError as exc:
            out.error = exc
            yield TurnEvent(
                type="error",
                data={
                    "code": exc.code,
                    "title": exc.title,
                    "detail": exc.message,
                    "retryable": exc.http_status >= 500,
                },
            )
            return

        # Bind the sink for this turn. Nodes reach it through
        # `packages.ai.streaming.emit`, so the cached graph never closes over a
        # per-turn object — see that module for why that matters.
        token = bind_sink(sink)

        # The root span for this turn. Opened after the graph resolves so a
        # compile failure is not recorded as a turn that ran, and entered with
        # an ExitStack so the single `finally` below closes it on every path —
        # including the cancellation path, which is the one that matters: a
        # caller hanging up mid-turn is normal, and an unclosed span would
        # leave the trace open until the SDK timed it out.
        stack = contextlib.ExitStack()
        turn_span = stack.enter_context(
            turn_trace(
                TurnContext(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    channel=channel,
                    manifest_version=compiled.manifest_version,
                    # Defaults to masked when the caller passes nothing, which
                    # is what every test and every un-migrated call site does.
                    # Capture is opt-in and this is where that is enforced for
                    # the whole turn.
                    policy=tracing_policy or TracingPolicy(),
                )
            )
        )
        turn_span.set_content(input=user_text)

        now_ms = int(time.monotonic() * 1000)
        seed = new_state(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            manifest_version=compiled.manifest_version,
            channel=channel,
            locale=locale,
            timezone=timezone,
            user_message=Message(role="user", content=user_text),
            history=history,
            max_steps=compiled.max_steps,
            max_tool_calls=compiled.max_tool_calls,
            deadline_ms=now_ms + compiled.wall_clock_budget_ms,
            started_at_ms=now_ms,
            collected_data=collected_data,
        )

        config = {
            "configurable": {
                "thread_id": str(conversation_id),
                # Tenant-scoped namespace, so a checkpointer added later cannot
                # collide two tenants' threads even if a conversation id were
                # somehow reused.
                "checkpoint_ns": str(tenant_id),
            },
            # Above our own max_steps: LangGraph's limit is the backstop, ours
            # fires first and lands the caller in __overflow with a graceful
            # message rather than raising GraphRecursionError mid-call.
            "recursion_limit": compiled.max_steps + 4,
        }

        async def drive() -> ConversationState:
            try:
                return await compiled.graph.ainvoke(seed, config)  # type: ignore[attr-defined]
            finally:
                await queue.put(_SENTINEL)

        task = asyncio.create_task(drive(), name=f"graph-turn-{turn_id}")

        yield TurnEvent(
            type="turn.started",
            data={"agent_key": compiled.spec.entrypoint, "role": "assistant"},
        )

        chunks: list[str] = []
        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                assert isinstance(item, TurnEvent)
                if item.type == "token":
                    chunks.append(str(item.data.get("text", "")))
                yield item

            final = await task
            out.text = "".join(chunks) or _last_assistant_text(final)
            out.agent_key = final.get("current_agent")
            out.outcome = final.get("outcome")
            out.collected_data = dict(final.get("collected_data") or {})
            out.overflow_reason = final.get("overflow_reason")
            out.finish_reason = (
                "overflow" if out.overflow_reason else str(final.get("finish_reason") or "stop")
            )
            out.latency_ms = int((time.perf_counter() - started) * 1000)
            # Summed across every model call in the turn — the supervisor's
            # routing hop as well as the specialist's reply. Left as None when
            # the provider reported nothing, so a null column means "unknown"
            # rather than "free".
            if final.get("prompt_tokens") or final.get("completion_tokens"):
                out.usage = Usage(
                    prompt_tokens=int(final.get("prompt_tokens", 0)),
                    completion_tokens=int(final.get("completion_tokens", 0)),
                    cached_prompt_tokens=int(final.get("cached_tokens", 0)),
                )

            # The turn's own summary, on the root span. `set_content` is
            # policy-gated inside the adapter, so the reply text is masked
            # unless this tenant opted in.
            turn_span.set_content(output=out.text)
            turn_span.set_attribute("turn.finish_reason", out.finish_reason)
            turn_span.set_attribute("turn.latency_ms", out.latency_ms)
            if out.agent_key:
                turn_span.set_attribute("ai.agent.key", out.agent_key)
            if out.outcome:
                turn_span.set_attribute("conversation.outcome", out.outcome)
            if out.usage is not None:
                turn_span.set_attribute(
                    "turn.prompt_tokens", out.usage.prompt_tokens
                )
                turn_span.set_attribute(
                    "turn.completion_tokens", out.usage.completion_tokens
                )
                turn_span.set_attribute(
                    "turn.cached_tokens", out.usage.cached_prompt_tokens
                )

            yield TurnEvent(
                type="turn.finished",
                data={
                    "finish_reason": out.finish_reason,
                    "latency_ms": out.latency_ms,
                    "agent_key": out.agent_key,
                    "outcome": out.outcome,
                },
            )

        except asyncio.CancelledError:
            # The client went away. Cancel the graph and *wait* for it — a
            # bare cancel returns before the task observes it, and the model
            # call would keep running and keep billing.
            log.info(
                "graph.turn.cancelled",
                conversation_id=str(conversation_id),
                turn_id=str(turn_id),
            )
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            out.finish_reason = "cancelled"
            out.text = "".join(chunks)
            turn_span.set_attribute("turn.finish_reason", "cancelled")
            raise

        except PortUnavailableError as exc:
            out.error = exc
            out.text = "".join(chunks)
            out.finish_reason = "error"
            turn_span.set_error(exc)
            yield TurnEvent(
                type="error",
                data={
                    "code": exc.code,
                    "title": exc.title,
                    "detail": "The assistant is temporarily unavailable.",
                    "retryable": True,
                },
            )

        except Exception as exc:
            log.exception(
                "graph.turn.failed",
                conversation_id=str(conversation_id),
                turn_id=str(turn_id),
                error=type(exc).__name__,
            )
            out.text = "".join(chunks)
            out.finish_reason = "error"
            turn_span.set_error(exc)
            yield TurnEvent(
                type="error",
                data={
                    "code": "internal_error",
                    "title": "Internal server error",
                    # The exception message is NOT forwarded: it can carry
                    # prompt content, which is customer PII (contract §8).
                    "detail": "The assistant could not complete this turn.",
                    "retryable": True,
                },
            )

        finally:
            unbind_sink(token)
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            # Closes the turn span on every exit path. `close()` rather than a
            # `with` block around the whole body because this is an async
            # generator: a `with` here would be exited by GC on an abandoned
            # generator rather than at the point the turn actually ends.
            stack.close()


def _last_assistant_text(state: ConversationState) -> str:
    for message in reversed(list(state.get("messages", []))):
        if message.role == "assistant" and message.content:
            return message.content
    return ""

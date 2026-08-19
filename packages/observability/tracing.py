"""Shared instrumentation helpers — the AI layer's entry point to tracing.

``packages/ai`` imports from here and nothing else. It never imports an adapter,
never imports the Langfuse SDK, and never branches on whether tracing is
configured. That last point is the design:

    with llm_generation("llm.chat", model=m, provider=p) as span:
        ...

runs identically whether a backend exists or not, because
:func:`get_tracer` returns a :class:`~packages.core.ports.tracing.NoOpTracer`
when nothing is configured and every helper below is written against the port.
The alternative — ``if tracer is not None:`` at each of a dozen call sites — is
a dozen opportunities for the traced and untraced paths to diverge, and the
untraced path is the one that runs in every test.

**Process-global, set once at startup.** A tracer is a client with a background
exporter thread; constructing one per request would be a connection leak. It is
installed by ``apps/api`` during lifespan and read from here by code far below
that has no access to application settings — which is the same shape the OTel
tracer in ``apps/api/observability.py`` already has.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from typing import Any, Final

import structlog
from packages.core.ports.tracing import (
    Capture,
    GenerationUsage,
    NoOpTracer,
    SpanHandle,
    ToolStatus,
    TracingPolicy,
    TracingPort,
    TurnContext,
)

__all__ = [
    "CAPTURE_KEY",
    "CAPTURE_NAMESPACE",
    "get_tracer",
    "graph_node",
    "llm_generation",
    "record_tool_outcome",
    "resolve_policy",
    "set_tracer",
    "tool_call",
    "turn_trace",
    "usage_from_port",
]

log = structlog.get_logger(__name__)

_TRACER: TracingPort = NoOpTracer()


def set_tracer(tracer: TracingPort | None) -> None:
    """Install the process-wide tracer. ``None`` restores the no-op."""
    global _TRACER
    _TRACER = tracer or NoOpTracer()
    log.info("tracing.configured", enabled=_TRACER.enabled)


def get_tracer() -> TracingPort:
    """The installed tracer, or a no-op. Never ``None``, never raises."""
    return _TRACER


#: The `tenant_settings` coordinates of the per-tenant capture opt-in.
#: Stored as data rather than as a settings-file flag because it is a *tenant*
#: decision (contract §8: "unless the tenant has explicitly opted in"), and a
#: process-wide environment variable cannot express one clinic consenting and
#: another not.
CAPTURE_NAMESPACE: Final = "observability"
CAPTURE_KEY: Final = "tracing.capture"


def resolve_policy(
    tenant_value: str | None, *, ceiling: str = "masked"
) -> TracingPolicy:
    """Combine a tenant's opt-in with the deployment's ceiling.

    Two inputs, and the *more restrictive* wins. The tenant setting cannot
    exceed the process ceiling, so a staging deployment configured
    ``LANGFUSE_CAPTURE=masked`` cannot be talked into full capture by a tenant
    row — which matters because the tenant row is data, and data is restorable
    from a backup taken before a policy change.

    An unrecognized value masks rather than raising. A typo in a settings row
    must not fail a turn, and must not be read as consent.
    """
    order = {Capture.NONE: 0, Capture.MASKED: 1, Capture.FULL: 2}

    def parse(raw: str | None) -> Capture:
        try:
            return Capture(str(raw).strip().lower())
        except ValueError:
            return Capture.MASKED

    tenant = parse(tenant_value) if tenant_value is not None else Capture.MASKED
    limit = parse(ceiling)
    return TracingPolicy(capture=min(tenant, limit, key=lambda c: order[c]))


@contextlib.contextmanager
def turn_trace(context: TurnContext) -> Iterator[SpanHandle]:
    """The root span for one conversation turn.

    Everything opened inside this block nests under it, so a trace is exactly
    one turn: user message in, assistant message out, with every node, model
    call and tool in between. 05 §D14.2's rule — one trace per *synchronous*
    unit of work — makes a turn the right boundary, since a conversation can
    span days and a trace covering it would be unreadable.
    """
    span = _TRACER.start_turn(context)
    with span:
        span.set_attribute("tenant.id", str(context.tenant_id))
        span.set_attribute("conversation.id", str(context.conversation_id))
        span.set_attribute("turn.id", str(context.turn_id))
        span.set_attribute("conversation.channel", context.channel)
        if context.manifest_version:
            span.set_attribute("manifest.version", context.manifest_version)
        yield span


@contextlib.contextmanager
def graph_node(node_name: str, *, agent_key: str) -> Iterator[SpanHandle]:
    """One graph node — ``ai.node.<node_name>`` per 05 §D14.3.

    The span name carries the *node*, not the tenant or conversation: those are
    attributes, because a high-cardinality span name fragments aggregation and
    makes "how slow is the scheduler node, generally" unanswerable.
    """
    span = _TRACER.start_node(f"ai.node.{node_name}", agent_key=agent_key)
    started = time.perf_counter()
    with span:
        span.set_attribute("ai.agent.key", agent_key)
        try:
            yield span
        except Exception as exc:
            span.set_error(exc)
            raise
        finally:
            span.set_attribute(
                "duration_ms", int((time.perf_counter() - started) * 1000)
            )


@contextlib.contextmanager
def llm_generation(
    operation: str, *, model: str, provider: str, agent_key: str | None = None
) -> Iterator[SpanHandle]:
    """One model call — ``llm.chat`` or ``llm.stream``.

    The caller is responsible for calling :meth:`SpanHandle.set_usage` with the
    token counts once the provider reports them, and
    :meth:`SpanHandle.set_content` with the prompt and completion. The latter is
    policy-gated inside the adapter, so passing content here is safe by
    construction rather than by the caller's judgement.
    """
    span = _TRACER.start_generation(operation, model=model, provider=provider)
    started = time.perf_counter()
    with span:
        span.set_attribute("gen_ai.system", provider)
        span.set_attribute("gen_ai.request.model", model)
        if agent_key:
            span.set_attribute("ai.agent.key", agent_key)
        try:
            yield span
        except Exception as exc:
            span.set_error(exc)
            raise
        finally:
            span.set_attribute(
                "duration_ms", int((time.perf_counter() - started) * 1000)
            )


@contextlib.contextmanager
def tool_call(tool_name: str, *, agent_key: str) -> Iterator[SpanHandle]:
    """One tool invocation — ``ai.tool.<tool_name>`` per 05 §D14.3.

    The outcome is recorded by the caller via :func:`record_tool_outcome`,
    because the executor already computes a status and a duration for its audit
    record and re-deriving them here would let the two disagree.
    """
    span = _TRACER.start_tool(f"ai.tool.{tool_name}", agent_key=agent_key)
    with span:
        span.set_attribute("ai.tool.name", tool_name)
        span.set_attribute("ai.agent.key", agent_key)
        try:
            yield span
        except Exception as exc:
            span.set_error(exc)
            raise


def record_tool_outcome(span: SpanHandle, record: dict[str, Any]) -> None:
    """Copy an executor audit record onto a tool span.

    Only the fields 05 §D14.5 permits: a status, a duration, an error *class*.
    ``redacted_args`` is deliberately not copied even though the executor has
    already masked it — ``ai.tool.arguments`` is on the forbidden list outright,
    and a partially-masked argument is still a natural-language string a person
    said.
    """
    status = str(record.get("status", "ok"))
    span.set_attribute("ai.tool.status", status)
    if (duration := record.get("duration_ms")) is not None:
        span.set_attribute("duration_ms", int(duration))
    if (error_type := record.get("error")) is not None:
        span.set_attribute("error.type", str(error_type))
    if (code := record.get("code")) is not None:
        span.set_attribute("error.code", str(code))

    if status not in (ToolStatus.OK, ToolStatus.OK.value):
        # A business error is a *failed span* but not an incident: the model
        # recovers from it, and it is the executor's own vocabulary that draws
        # the line. Marking it lets a waterfall show which tool round failed
        # without inventing a second status field.
        span.set_error(status)


def usage_from_port(usage: Any, *, model: str) -> GenerationUsage:
    """Convert a ``packages.core.ports.llm.Usage`` into tracing's shape.

    Takes ``Any`` rather than importing ``Usage`` so this module stays usable
    from the voice pipeline, which reports token counts from a realtime session
    that never produced an ``LLMPort`` result.
    """
    from packages.observability.pricing import estimate_cost_usd

    if usage is None:
        return GenerationUsage()

    out = GenerationUsage(
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        cached_prompt_tokens=int(getattr(usage, "cached_prompt_tokens", 0) or 0),
        reasoning_tokens=int(getattr(usage, "reasoning_tokens", 0) or 0),
    )
    return GenerationUsage(
        prompt_tokens=out.prompt_tokens,
        completion_tokens=out.completion_tokens,
        cached_prompt_tokens=out.cached_prompt_tokens,
        reasoning_tokens=out.reasoning_tokens,
        cost_usd=estimate_cost_usd(model, out),
    )

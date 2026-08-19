"""``TracingPort`` — LLM observability, contract §5.

The AI layer needs to say "a turn started", "this agent ran", "this model call
cost 1,240 prompt tokens", "this tool took 3.1 seconds and returned a business
error". None of those statements should name a vendor. This module is the seam:
``packages/ai`` speaks these dataclasses, and only an adapter under
``packages/providers/observability`` knows what a Langfuse trace looks like.

**Why a port at all, when OpenTelemetry is already a vendor-neutral API.** Two
reasons. First, the layering check (contract §9) bans the Langfuse SDK outside
``providers/``, and the SDK is what turns a span into a *generation* with token
accounting and cost — an OTel span alone has no such concept, only attributes a
backend may or may not interpret. Second, and more importantly, LLM
observability has a semantic model that plain tracing does not: an observation
carries an ``input`` and an ``output`` that are *the actual prompt and
completion*. That is the highest-density PII in the system (contract §8), so it
needs a typed, reviewable choke point rather than a free-form attribute bag.
Hence :class:`Capture`.

**The PII design, stated once because it is the whole point.** A span may
always carry identifiers, counts, durations and categories. It may carry the
prompt and the completion **only** when the tenant has opted in, and the
decision is made by :class:`TracingPolicy` at the boundary rather than by each
call site remembering to check. A call site hands over the content
unconditionally; the policy decides whether it is recorded, masked, or dropped.
Making it the caller's job is how one forgotten branch leaks a phone number
into a dashboard, and 05 §D14.5 is explicit that content never belongs on a
span by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

__all__ = [
    "Capture",
    "GenerationUsage",
    "NoOpTracer",
    "SpanHandle",
    "ToolStatus",
    "TracingPolicy",
    "TracingPort",
    "TurnContext",
]


class Capture(StrEnum):
    """How much of a prompt/completion pair may be recorded.

    Three values rather than a boolean because "off" has two useful meanings.
    ``MASKED`` records that content existed and how big it was, which is what
    makes a trace readable — an empty generation span looks like a bug, and an
    engineer debugging latency still needs to see the *shape* of the turn.
    ``NONE`` records nothing at all, for a tenant whose contract forbids even
    that.
    """

    #: Full prompt and completion text. Opt-in, per tenant, never the default.
    FULL = "full"
    #: Structure and lengths only: role, character count, a redaction marker.
    MASKED = "masked"
    #: No input/output fields whatsoever.
    NONE = "none"


class ToolStatus(StrEnum):
    """The outcome vocabulary shared with ``packages/ai/tools/executor.py``.

    Mirrors the executor's own ``record["status"]`` values so a dashboard can
    group on one enum. ``BUSINESS_ERROR`` is deliberately distinct from
    ``ERROR``: a slot already being taken is information the model recovers
    from, and paging someone for it — or letting it inflate an error-rate
    panel — is how a real incident gets lost in noise.
    """

    OK = "ok"
    BUSINESS_ERROR = "business_error"
    ERROR = "error"
    DENIED = "denied"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class TracingPolicy:
    """Per-tenant capture rules, resolved once per turn.

    Default-deny by construction: the dataclass default is ``Capture.MASKED``,
    so a policy built with no arguments — which is what every code path that
    forgets to look up a tenant's setting produces — masks. Turning capture on
    requires someone to type ``Capture.FULL``, which is a reviewable diff.
    """

    capture: Capture = Capture.MASKED
    #: Character ceiling on any recorded content, applied even under
    #: ``Capture.FULL``. A 200KB knowledge-base dump in a trace is a storage
    #: problem, not an observability win.
    max_chars: int = 4_000


@dataclass(frozen=True, slots=True)
class TurnContext:
    """Identity for one conversation turn — the trace's root.

    These are the identifiers 05 §D14.4 permits in baggage, plus the ones a
    dashboard groups by. All opaque; none is a natural-language string.
    """

    tenant_id: UUID
    conversation_id: UUID
    turn_id: UUID
    channel: str = "web_chat"
    #: Manifest version, so a regression can be attributed to a prompt change.
    manifest_version: str | None = None
    environment: str = "local"
    #: This tenant's capture rules. Carried on the turn context rather than
    #: read from configuration inside the adapter, so the decision is made once
    #: per turn, by the layer that knows which tenant it is, and is visible in
    #: the same object as the tenant id it applies to. Defaults to masked, so a
    #: caller that constructs a context without thinking about capture gets the
    #: safe answer.
    policy: TracingPolicy = field(default_factory=TracingPolicy)


@dataclass(frozen=True, slots=True)
class GenerationUsage:
    """Token accounting for one model call.

    ``cost_usd`` is computed by the adapter from a pricing table rather than
    reported by the provider, because most OpenAI-compatible endpoints do not
    return a cost field at all. ``None`` means "unknown", never "free" — the
    distinction matters when reconciling a provider invoice.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@runtime_checkable
class SpanHandle(Protocol):
    """One open observation. A context manager, so it cannot be left unclosed.

    Every method is a no-op on the null implementation, which is what makes
    "Langfuse is not configured" cost a method call and nothing else.
    """

    def set_attribute(self, key: str, value: Any) -> None:
        """Record one identifier, count, duration or category.

        Never content: see :meth:`set_content`, which is policy-gated.
        """
        ...

    def set_content(self, *, input: Any = None, output: Any = None) -> None:
        """Offer the prompt and completion, subject to the tenant's policy.

        The call site passes raw content unconditionally and the implementation
        decides what survives. That inversion is the control — a call site that
        has to remember to check is a call site that eventually does not.
        """
        ...

    def set_usage(self, usage: GenerationUsage) -> None: ...

    def set_error(self, exc: BaseException | str) -> None:
        """Mark the span failed.

        Takes an exception or a bare string. Implementations record the
        exception *type*, never ``str(exc)``, because a provider error body
        routinely embeds the request — and therefore the prompt (05 §D14.5).
        """
        ...

    def __enter__(self) -> SpanHandle: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


@runtime_checkable
class TracingPort(Protocol):
    """LLM observability: traces, generations, tool and node spans.

    Universal adapter requirements (contract §5) apply, with one amendment that
    matters more here than anywhere else: **this port must never raise.** An
    observability backend being down, slow, or misconfigured is not permitted
    to become a product outage, so every method swallows its own failures. The
    existing OTel setup in ``apps/api/observability.py`` makes the same promise
    and this follows it deliberately.
    """

    enabled: bool

    def start_turn(
        self, context: TurnContext, *, name: str = "conversation.turn"
    ) -> SpanHandle:
        """Open the root span for one user turn."""
        ...

    def start_node(self, name: str, *, agent_key: str) -> SpanHandle:
        """Open a child span for one graph node — 05 §D14.3 ``ai.node.<name>``."""
        ...

    def start_generation(
        self, name: str, *, model: str, provider: str
    ) -> SpanHandle:
        """Open a child span for one LLM call, carrying token accounting."""
        ...

    def start_tool(self, name: str, *, agent_key: str) -> SpanHandle:
        """Open a child span for one tool call — ``ai.tool.<tool_name>``."""
        ...

    def flush(self) -> None:
        """Force pending observations out.

        Needed because a CLI or a test process can exit before the SDK's
        background exporter runs, and a trace that never left the process is
        indistinguishable from instrumentation that was never written.
        """
        ...

    def shutdown(self) -> None: ...


# ── The null object ───────────────────────────────────────────────────────
#
# Lives in core rather than in the adapter package on purpose. "Langfuse is
# optional" is a property of the *platform*, not of the Langfuse adapter, and
# the fallback must therefore be reachable without importing anything that
# knows Langfuse exists — including on a clean clone where the SDK is not
# installed at all.


@dataclass(slots=True)
class _NullSpan:
    """Absorbs every call and records nothing."""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_content(self, *, input: Any = None, output: Any = None) -> None:
        return None

    def set_usage(self, usage: GenerationUsage) -> None:
        return None

    def set_error(self, exc: BaseException | str) -> None:
        return None

    def __enter__(self) -> _NullSpan:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


@dataclass(slots=True)
class NoOpTracer:
    """The default ``TracingPort``: correct, silent, and free.

    Returned whenever no key is configured. Every instrumentation site in
    ``packages/ai`` calls this unconditionally, so there is exactly one code
    path whether or not tracing is on — the alternative, ``if tracer:`` at
    forty call sites, is forty chances to write the untraced path differently
    from the traced one.
    """

    enabled: bool = False

    def start_turn(
        self, context: TurnContext, *, name: str = "conversation.turn"
    ) -> SpanHandle:
        return _NullSpan()

    def start_node(self, name: str, *, agent_key: str) -> SpanHandle:
        return _NullSpan()

    def start_generation(
        self, name: str, *, model: str, provider: str
    ) -> SpanHandle:
        return _NullSpan()

    def start_tool(self, name: str, *, agent_key: str) -> SpanHandle:
        return _NullSpan()

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

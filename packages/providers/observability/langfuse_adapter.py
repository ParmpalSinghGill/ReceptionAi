"""``LangfuseTracingAdapter`` — the only module that may import Langfuse.

Contract §5/§9: no subsystem imports a vendor SDK outside ``providers`` |
``connectors`` | ``channels``. ``langfuse`` is on ``tools/check_layering.py``'s
``VENDOR_SDKS`` list, so an import of it anywhere else fails CI.

**Three properties this adapter must have, in priority order.**

1. *It must never raise.* An observability backend is not permitted to take
   down a phone call. Every public method is wrapped so a Langfuse failure
   becomes a log line and nothing else. This is the same promise
   ``apps/api/observability.py`` makes for OTel, made again because the failure
   mode is identical and the consequence is worse: this code runs inside the
   turn loop, not at startup.

2. *It must be absent-safe.* ``langfuse`` is an optional dependency. The import
   is inside :func:`build_langfuse_tracer`, so a clean clone without the
   package returns a ``NoOpTracer`` instead of an ``ImportError`` at module
   load — and ``packages/ai`` never imports this module at all.

3. *It must not leak PII.* Content passes through
   :func:`packages.observability.redaction.mask_content` before it reaches the
   SDK, governed by the tenant's :class:`TracingPolicy`. A second, independent
   guard is installed as the SDK's own ``mask`` callback, so content that
   reaches Langfuse by a path this adapter does not control — an auto-
   instrumentation library, a future SDK feature — is still scrubbed. Defence
   in depth is warranted because the failure is unrecoverable: a phone number
   written to a trace store cannot be un-written by a later fix.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from types import TracebackType
from typing import Any

import structlog
from packages.core.ports.tracing import (
    Capture,
    GenerationUsage,
    NoOpTracer,
    SpanHandle,
    TracingPolicy,
    TracingPort,
    TurnContext,
)
from packages.observability.redaction import mask_content, scrub

__all__ = ["LangfuseTracingAdapter", "build_langfuse_tracer"]

log = structlog.get_logger(__name__)

#: The policy in force for the current turn. A ContextVar rather than an
#: attribute because a single adapter instance serves every concurrent turn in
#: the process, and two tenants with different capture settings are routinely
#: in flight at the same moment. An attribute would let one tenant's opt-in
#: apply to another tenant's conversation — which is precisely the leak the
#: opt-in exists to prevent.
#: Defaults to ``None`` rather than a policy instance so the fallback is
#: resolved in one place (:func:`current_policy`) — and so that place can be
#: read as the answer to "what happens when nobody set a policy?".
_POLICY: contextvars.ContextVar[TracingPolicy | None] = contextvars.ContextVar(
    "langfuse_tracing_policy", default=None
)


def set_turn_policy(
    policy: TracingPolicy,
) -> contextvars.Token[TracingPolicy | None]:
    """Bind the capture policy for the current turn. Returns a reset token."""
    return _POLICY.set(policy)


def reset_turn_policy(token: contextvars.Token[TracingPolicy | None]) -> None:
    _POLICY.reset(token)


def current_policy() -> TracingPolicy:
    """The turn's policy, or the default-deny fallback.

    An unset policy masks. That is the whole reason this returns a value rather
    than ``None``: a caller that had to handle ``None`` would eventually handle
    it by capturing.
    """
    return _POLICY.get() or TracingPolicy()


class _LangfuseSpan:
    """Wraps one Langfuse observation as a :class:`SpanHandle`.

    Holds the SDK's context manager rather than subclassing anything from it:
    the port's surface is four methods, and mapping them explicitly means an
    SDK upgrade that renames ``update()`` breaks here, loudly, in one file.
    """

    __slots__ = ("_cm", "_is_generation", "_obs", "_policy")

    def __init__(self, cm: Any, *, is_generation: bool = False) -> None:
        self._cm = cm
        self._obs: Any = None
        self._is_generation = is_generation
        # Snapshot the policy at construction, so a span's capture decision
        # cannot change under it when the turn-scoped ContextVar is reset.
        self._policy: TracingPolicy | None = current_policy()

    def __enter__(self) -> _LangfuseSpan:
        try:
            self._obs = self._cm.__enter__()
        except Exception as exc:  # pragma: no cover - SDK internal failure
            log.warning("tracing.span.start_failed", error=type(exc).__name__)
            self._obs = None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self._cm.__exit__(exc_type, exc, tb)
        except Exception as err:  # pragma: no cover - SDK internal failure
            log.warning("tracing.span.end_failed", error=type(err).__name__)
        # Deliberately returns None, never True: swallowing the caller's
        # exception here would make an observability wrapper silently change
        # program behaviour, which is the worst thing this code could do.

    @property
    def started(self) -> bool:
        """Whether the underlying observation exists.

        The SDK can fail to open one (network, misconfiguration) and every
        method below tolerates that by doing nothing.
        """
        return self._obs is not None

    def identify_trace(self, context: TurnContext, *, name: str) -> None:
        """Stamp the root trace with session, tenant and manifest identity."""
        if self._obs is None:
            return
        try:
            self._obs.update_trace(
                name=name,
                session_id=str(context.conversation_id),
                user_id=str(context.tenant_id),
                tags=[
                    f"channel:{context.channel}",
                    f"env:{context.environment}",
                ],
                metadata={
                    "tenant.id": str(context.tenant_id),
                    "conversation.id": str(context.conversation_id),
                    "turn.id": str(context.turn_id),
                    "manifest.version": context.manifest_version or "unknown",
                },
            )
        except Exception as exc:
            log.warning("tracing.identify_trace_failed", error=type(exc).__name__)

    def set_attribute(self, key: str, value: Any) -> None:
        """Attach one identifier, count, duration or category as metadata.

        Langfuse has no first-class attribute bag, so these land in
        ``metadata``. Written one key at a time rather than accumulated and
        flushed at exit, because a span that errors mid-turn should still carry
        what was known before the error.
        """
        if self._obs is None:
            return
        try:
            self._obs.update(metadata={key: value})
        except Exception as exc:
            log.warning(
                "tracing.set_attribute_failed", key=key, error=type(exc).__name__
            )

    def set_content(self, *, input: Any = None, output: Any = None) -> None:
        """Record prompt/completion subject to the current tenant's policy.

        The masking decision happens here — at the boundary, once — rather than
        at each call site. See ``packages/core/ports/tracing.py``'s module
        docstring for why that inversion is the control rather than a
        convenience.
        """
        if self._obs is None:
            return
        # The span's OWN policy, captured when it was opened, in preference to
        # the ambient ContextVar.
        #
        # The turn's final `set_content(output=...)` runs after the streamed
        # reply has finished, by which point the turn-scoped policy token has
        # already been reset — so the ambient lookup returned the default-deny
        # fallback and masked the reply while every earlier call in the SAME
        # turn captured fully. The trace showed a readable question and a
        # redacted answer, which reads as a bug in the masking rather than in
        # the lifetime of a ContextVar.
        policy = getattr(self, "_policy", None) or current_policy()
        if policy.capture is Capture.NONE:
            return
        try:
            payload: dict[str, Any] = {}
            if input is not None:
                masked = mask_content(input, policy)
                if masked is not None:
                    payload["input"] = masked
            if output is not None:
                masked_out = mask_content(output, policy)
                if masked_out is not None:
                    payload["output"] = masked_out
            if payload:
                self._obs.update(**payload)
        except Exception as exc:
            log.warning("tracing.set_content_failed", error=type(exc).__name__)

    def set_usage(self, usage: GenerationUsage) -> None:
        """Token counts and cost, in Langfuse's ``usage_details`` shape.

        ``input`` here means *fresh* prompt tokens: Langfuse sums the detail
        keys for its total, so passing the full prompt count alongside a
        separate cached count would double-count every cached turn and inflate
        the token graph exactly on the turns prompt caching was meant to make
        cheaper.
        """
        if self._obs is None:
            return
        try:
            cached = max(0, usage.cached_prompt_tokens)
            fresh = max(0, usage.prompt_tokens - cached)
            details: dict[str, int] = {
                "input": fresh,
                "output": max(0, usage.completion_tokens),
            }
            if cached:
                details["cache_read_input_tokens"] = cached
            if usage.reasoning_tokens:
                details["reasoning"] = usage.reasoning_tokens

            update: dict[str, Any] = {"usage_details": details}
            if usage.cost_usd is not None:
                update["cost_details"] = {"total": usage.cost_usd}
            self._obs.update(**update)
        except Exception as exc:
            log.warning("tracing.set_usage_failed", error=type(exc).__name__)

    def set_error(self, exc: BaseException | str) -> None:
        """Mark the observation failed, recording the type and never the message.

        ``str(exc)`` on a provider error routinely contains the request body,
        and therefore the prompt (05 §D14.5). The exception *class* is the
        diagnosable part and carries no content.
        """
        if self._obs is None:
            return
        try:
            label = exc if isinstance(exc, str) else type(exc).__qualname__
            self._obs.update(level="ERROR", status_message=str(label)[:200])
        except Exception as err:
            log.warning("tracing.set_error_failed", error=type(err).__name__)


class LangfuseTracingAdapter:
    """``TracingPort`` backed by a self-hosted Langfuse instance."""

    enabled = True

    def __init__(self, client: Any, *, environment: str = "local") -> None:
        self._client = client
        self._environment = environment

    # ── Span factories ────────────────────────────────────────────────────

    def start_turn(
        self, context: TurnContext, *, name: str = "conversation.turn"
    ) -> SpanHandle:
        """Open the root span and stamp the trace's identity.

        ``session_id`` is the conversation, so Langfuse groups every turn of a
        call onto one session view — which is the screen someone actually wants
        when a caller says "the bot got confused near the end". ``user_id`` is
        the *tenant*, deliberately: there is no end-user identifier that is not
        PII, and the tenant is the axis every dashboard filters by anyway.
        """
        try:
            cm = self._client.start_as_current_span(name=name)
            span = _LangfuseSpan(cm)
            span.__enter__()
            span.identify_trace(context, name=name)
            # Bind this tenant's capture policy for the life of the turn. Every
            # `set_content` below — on this span and on every child span opened
            # inside it — reads it from the ContextVar, so one decision made
            # here governs the whole trace. The token is released when the turn
            # span closes.
            # The span carries the policy itself, not only the ContextVar.
            # The turn's closing `set_content(output=...)` runs after the
            # streamed reply completes and the token has been released, so an
            # ambient-only lookup masked the reply while the question — written
            # earlier in the same turn — came through in full.
            span._policy = context.policy
            return _AlreadyEntered(span, policy_token=set_turn_policy(context.policy))
        except Exception as exc:
            log.warning("tracing.start_turn_failed", error=type(exc).__name__)
            return NoOpTracer().start_turn(context)

    def start_node(self, name: str, *, agent_key: str) -> SpanHandle:
        return self._observation(name, as_type="agent")

    def start_generation(
        self, name: str, *, model: str, provider: str
    ) -> SpanHandle:
        try:
            cm = self._client.start_as_current_observation(
                name=name,
                as_type="generation",
                model=model,
                metadata={"gen_ai.system": provider},
            )
            return _LangfuseSpan(cm, is_generation=True)
        except Exception as exc:
            log.warning("tracing.start_generation_failed", error=type(exc).__name__)
            return NoOpTracer().start_generation(name, model=model, provider=provider)

    def start_tool(self, name: str, *, agent_key: str) -> SpanHandle:
        return self._observation(name, as_type="tool")

    def _observation(self, name: str, *, as_type: str) -> SpanHandle:
        try:
            cm = self._client.start_as_current_observation(name=name, as_type=as_type)
            return _LangfuseSpan(cm)
        except Exception as exc:
            log.warning(
                "tracing.start_observation_failed",
                as_type=as_type,
                error=type(exc).__name__,
            )
            return NoOpTracer().start_node(name, agent_key="")

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:
            log.warning("tracing.flush_failed", error=type(exc).__name__)

    def shutdown(self) -> None:
        try:
            self._client.shutdown()
        except Exception as exc:
            log.warning("tracing.shutdown_failed", error=type(exc).__name__)


class _AlreadyEntered:
    """A span whose ``__enter__`` has run, so ``with`` does not re-enter it.

    ``start_turn`` must enter its context manager early in order to call
    ``update_trace`` on the resulting observation. Returning the raw span would
    make the caller's ``with`` block enter it a second time, which the SDK
    treats as a nested observation and which would silently produce a
    double-counted turn.
    """

    __slots__ = ("_policy_token", "_span")

    def __init__(
        self,
        span: _LangfuseSpan,
        *,
        policy_token: contextvars.Token[TracingPolicy | None] | None = None,
    ) -> None:
        self._span = span
        self._policy_token = policy_token

    def __enter__(self) -> _LangfuseSpan:
        return self._span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self._span.__exit__(exc_type, exc, tb)
        finally:
            # Reset even if the span failed to close: leaving one tenant's
            # policy bound to a pooled task is how the next turn on that task
            # inherits somebody else's opt-in.
            if self._policy_token is not None:
                with contextlib.suppress(ValueError):
                    reset_turn_policy(self._policy_token)

    def set_attribute(self, key: str, value: Any) -> None:
        self._span.set_attribute(key, value)

    def set_content(self, *, input: Any = None, output: Any = None) -> None:
        self._span.set_content(input=input, output=output)

    def set_usage(self, usage: GenerationUsage) -> None:
        self._span.set_usage(usage)

    def set_error(self, exc: BaseException | str) -> None:
        self._span.set_error(exc)


def _sdk_mask(*, data: Any) -> Any:
    """The SDK-level backstop, applied to every field Langfuse serializes.

    This runs *after* this adapter's own masking and catches anything that
    reached the client by another route. It only scrubs strings — structural
    masking is the policy's job and is already done by the time data gets here.

    It must never raise: the SDK calls it inside serialization, and an
    exception there drops the whole observation.
    """
    try:
        if isinstance(data, str):
            return scrub(data)
        if isinstance(data, list):
            return [_sdk_mask(data=item) for item in data]
        if isinstance(data, dict):
            return {k: _sdk_mask(data=v) for k, v in data.items()}
        return data
    except Exception:  # pragma: no cover - defensive
        return "[masking-failed]"


def build_langfuse_tracer(
    *,
    public_key: str | None,
    secret_key: str | None,
    host: str | None,
    environment: str = "local",
    release: str | None = None,
    enabled: bool = True,
) -> TracingPort:
    """Build a tracer, or a no-op when Langfuse is not configured or absent.

    Returns :class:`NoOpTracer` — never raises and never returns ``None`` — for
    every reason it might not work: disabled by settings, missing keys, package
    not installed, or a client that cannot be constructed. "Langfuse is
    optional" has to be true on a clean clone with no SDK and no server, and
    the only way to be sure of that is for this function to have no failure
    path that reaches the caller.

    Note that connectivity is *not* checked here. ``auth_check()`` is a network
    round-trip, and putting one in the startup path means a slow or down
    Langfuse delays every boot — turning an observability outage into a product
    outage, which is exactly the inversion this whole module refuses.
    """
    if not enabled:
        log.info("tracing.disabled", reason="langfuse_enabled=false")
        return NoOpTracer()

    if not public_key or not secret_key:
        log.info(
            "tracing.disabled",
            reason="no_keys",
            remediation=(
                "Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY to record LLM "
                "traces. The platform runs fully without them."
            ),
        )
        return NoOpTracer()

    try:
        from langfuse import Langfuse
    except ImportError:
        log.warning(
            "tracing.unavailable",
            reason="langfuse package is not installed",
            remediation="pip install -e '.[observability]'",
        )
        return NoOpTracer()

    try:
        # The SDK reads a pile of OTEL_* environment variables and will happily
        # attach itself to the application's global tracer provider. That is
        # not wanted: the existing OTel pipeline exports to the collector, and
        # letting Langfuse take over the global provider would reroute every
        # HTTP and database span into Langfuse as well. Its own isolated
        # provider keeps the two pipelines independent.
        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host or "http://localhost:3110",
            environment=environment,
            release=release,
            mask=_sdk_mask,
            tracing_enabled=True,
        )
    except Exception as exc:
        log.warning(
            "tracing.client_failed",
            error=type(exc).__name__,
            remediation="Langfuse is disabled; the platform continues normally.",
        )
        return NoOpTracer()

    log.info("tracing.enabled", backend="langfuse", host=host, environment=environment)
    return LangfuseTracingAdapter(client, environment=environment)


def build_from_env() -> TracingPort:
    """Convenience for processes without a settings object (workers, scripts)."""
    return build_langfuse_tracer(
        public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
        secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
        host=os.getenv("LANGFUSE_HOST"),
        environment=os.getenv("ENV", "local"),
        enabled=os.getenv("LANGFUSE_ENABLED", "false").lower() in ("1", "true", "yes"),
    )

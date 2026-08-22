"""The tool execution chokepoint — 03 §6.2, §6.3.

**Nothing in this package calls a tool except through** :meth:`ToolExecutor.
execute_many`. That is the property the whole security model rests on: contract
§8 says a tool is reachable only via an explicit manifest grant, and a second
call path would be a second place for that check to be missing.

The six steps, in order, and none of them is skippable:

    1. NAME CHECK    is `name` in THIS agent's grant set?     -> denied
                     A closure over AgentSpec.tools, computed
                     per call from the manifest. Not a lookup
                     the model can influence.
    2. AVAILABILITY  is the tool executable in this build?    -> error
    3. BOUND ARGS    overwrite manifest-pinned arguments.
                     The model's choice is discarded.
    4. SCHEMA        validate against tool schema ∩ grant     -> invalid_args
                     constraints                                 (fed back to model)
    5. CONFIRMATION  requires_confirmation and not yet        -> deferred
                     confirmed by the user
    6. EXECUTE       tenant-scoped ctx, timeout, structured
                     result

Two rules about what comes *back*, both load-bearing:

**Every path returns a message, none raises.** The OpenAI and Anthropic tool
protocols require every ``tool_call`` to be answered by a ``tool_result`` with a
matching id. Raising leaves an unanswered call in the message list and the next
request 400s — so a denial, a validation failure and a timeout all produce a
tool message, they just produce different ones.

**Bland text for denials, specific text for validation errors.** A validation
error is a cooperative correction: telling the model exactly which field is wrong
lets it fix the call, which is what we want. A grant denial is
adversarial-adjacent: if injected content is probing for a tool it was not
granted, detailed feedback is an oracle. Same reasoning as not distinguishing
"wrong password" from "no such user".
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import jsonschema
import structlog
from packages.ai.tools.registry import (
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from packages.core.errors import DomainError
from packages.core.ports.llm import Message, ToolCall, ToolSchema
from packages.observability.tracing import record_tool_outcome, tool_call
from packages.verticals.manifest import AgentSpec, ToolGrant

__all__ = ["ToolBusinessError", "ToolExecutor", "ToolOutcome"]

log = structlog.get_logger(__name__)

#: Floor on a per-call timeout. A turn whose deadline has nearly elapsed still
#: gives a tool a quarter-second rather than a negative budget, so the failure is
#: an honest timeout rather than an immediate one that looks like a bug.
_MIN_TIMEOUT_MS = 250

#: Parallelism ceiling for read tools. Enough to make three availability checks
#: concurrent; low enough that a model emitting twenty does not open twenty
#: database sessions.
_MAX_PARALLEL = 4

#: What the model is told when a tool it named is not granted to it. Deliberately
#: the same string for "not granted" and "does not exist": the difference is
#: exactly the information a probe is looking for.
_DENIED_TEXT = "That action is not available in this conversation."


class ToolBusinessError(DomainError):
    """An expected domain failure the model should recover from.

    A slot being taken, a booking not being found, a cancellation policy refusing
    — these are *information*, not incidents. ``model_message`` is written to be
    read aloud, so the recovery is a sentence to the caller rather than an
    apology and a transfer.
    """

    code = "conflict"
    http_status = 409

    def __init__(
        self,
        message: str,
        *,
        model_message: str | None = None,
        structured: dict[str, Any] | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.model_message = model_message or message
        self.structured = structured or {}


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """One executed (or refused) tool call, ready for the message list."""

    message: Message
    record: dict[str, Any]
    collected_data: dict[str, Any] = field(default_factory=dict)
    structured: dict[str, Any] = field(default_factory=dict)
    #: Set when the manifest requires a human yes before this side effect runs.
    confirmation: dict[str, Any] | None = None


class ToolExecutor:
    """Runs a turn's tool calls under the manifest's grants."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    # ── Compile-time checks, called by the node factory ───────────────────

    def unknown_tools(self, agent: AgentSpec) -> set[str]:
        """Tools this agent grants that the platform does not ship."""
        return {grant.tool for grant in agent.tools} - self._registry.names()

    def validate_grants(self, agent: AgentSpec) -> None:
        """Assert every grant **narrows** the tool's schema, never widens it.

        Without this, a manifest could declare ``arg_constraints`` that relax the
        tool's own validation — a privilege escalation through data. A grant may
        add a required field, restrict an enum, tighten a bound; it may not
        introduce a property the tool never declared, because the implementation
        has no code to handle it and the strict schema would then reject the call
        the manifest just authorised.
        """
        for grant in agent.tools:
            spec = self._registry.get(grant.tool)
            if spec is None:
                continue
            properties = set(spec.parameters.get("properties") or {})
            if unknown := set(grant.bound_args) - properties:
                raise ValueError(
                    f"agent {agent.key!r} binds arguments {sorted(unknown)} that "
                    f"tool {grant.tool!r} does not declare"
                )
            if grant.arg_constraints:
                constrained = set(grant.arg_constraints.get("properties") or {})
                if widened := constrained - properties:
                    raise ValueError(
                        f"agent {agent.key!r} constrains {sorted(widened)} on tool "
                        f"{grant.tool!r}, which the tool does not declare; a grant "
                        f"may narrow, never widen"
                    )

    def schemas_for(self, agent: AgentSpec) -> list[ToolSchema]:
        """The tools this agent may see, with bound args hidden.

        A bound argument is **removed from the exposed schema entirely**, and its
        name is stripped from ``required``. The model never sees a field the
        manifest pins, so it cannot be tempted to set it — and a schema that
        listed a hidden field as required would be unsatisfiable, producing a
        model that cannot call a tool it was granted.
        """
        out: list[ToolSchema] = []
        for grant in agent.tools:
            spec = self._registry.get(grant.tool)
            if spec is None:
                continue
            params = _project(spec.parameters, hide=set(grant.bound_args))
            if grant.arg_constraints:
                params = _intersect(params, grant.arg_constraints)
            out.append(
                ToolSchema(
                    name=spec.name, description=spec.description, parameters=params
                )
            )
        return out

    # ── Call time ─────────────────────────────────────────────────────────

    async def execute_many(
        self,
        *,
        calls: list[ToolCall],
        agent: AgentSpec,
        context: ToolExecutionContext,
        confirmed: frozenset[str] = frozenset(),
    ) -> list[ToolOutcome]:
        """Run every call the model emitted, in the model's own order.

        **Reads run in parallel; writes run serially.** Two parallel
        ``book_appointment`` calls against the same slot is a double-booking, and
        a model asked to "book Tuesday and Thursday" *will* emit them together.
        The partition is on the spec's own ``idempotent and not side_effecting``,
        so it is a property of the tool rather than a list someone maintains.

        Results are re-ordered to match ``calls`` before returning, because the
        protocol pairs results to calls by id and a client that assumes positional
        correspondence would mismatch them.
        """
        grants = {g.tool: g for g in agent.tools}
        parallel: list[ToolCall] = []
        serial: list[ToolCall] = []
        for call in calls:
            spec = self._registry.get(call.name)
            target = (
                parallel
                if spec is not None and spec.idempotent and not spec.side_effecting
                else serial
            )
            target.append(call)

        results: dict[str, ToolOutcome] = {}

        if parallel:
            semaphore = asyncio.Semaphore(_MAX_PARALLEL)

            async def run(call: ToolCall) -> None:
                async with semaphore:
                    results[call.id] = await self._execute_one(
                        call, grants, agent, context, confirmed
                    )

            await asyncio.gather(*(run(call) for call in parallel))

        for call in serial:
            results[call.id] = await self._execute_one(
                call, grants, agent, context, confirmed
            )

        return [results[call.id] for call in calls]

    async def _execute_one(
        self,
        call: ToolCall,
        grants: dict[str, ToolGrant],
        agent: AgentSpec,
        context: ToolExecutionContext,
        confirmed: frozenset[str],
    ) -> ToolOutcome:
        """Open a tool span, then run the call.

        A wrapper around :meth:`_execute_one_traced` rather than a context
        manager inside it, because every one of that method's eight return
        paths — denied, unconfirmed, validation-failed, timed out, business
        error, error, ok — must land on the span. Wrapping the call site is
        the only way to get that without eight edits that a ninth return path
        would silently escape.

        The outcome is read from the executor's own audit record, so the span
        and the audit log cannot disagree about what happened.
        """
        with tool_call(call.name, agent_key=agent.key) as span:
            outcome = await self._execute_one_traced(
                call, grants, agent, context, confirmed
            )
            record_tool_outcome(span, outcome.record)
            # What went in and what came back — as span CONTENT, never as
            # attributes (05 §D14.5 forbids `ai.tool.arguments` as an
            # attribute, for the same reason it forbids `gen_ai.prompt`).
            # Content goes through the adapter's `set_content`, which applies
            # the tenant's capture policy and the SDK mask — identical to how
            # a prompt reaches the trace. The arguments are the executor's
            # already-PII-redacted copy; the output is the model-facing text.
            span.set_content(
                input=outcome.record.get("redacted_args"),
                output=outcome.message.content,
            )
            return outcome

    async def _execute_one_traced(
        self,
        call: ToolCall,
        grants: dict[str, ToolGrant],
        agent: AgentSpec,
        context: ToolExecutionContext,
        confirmed: frozenset[str],
    ) -> ToolOutcome:
        started = time.monotonic()

        # ── 1. NAME CHECK — the whole authorization surface ───────────────
        #
        # A set-membership test on data loaded from the manifest. Not a prompt
        # instruction, not a model decision, not a lookup the model can steer.
        # This is the line that makes contract §8's claim true.
        grant = grants.get(call.name)
        if grant is None:
            log.warning(
                "ai.tool.grant_violation",
                tenant_id=str(context.tenant_id),
                conversation_id=str(context.conversation_id),
                agent=agent.key,
                tool=call.name,
            )
            return self._deny(call, agent, started, "denied", _DENIED_TEXT)

        spec = self._registry.get(call.name)
        if spec is None or not spec.available:
            # The registry changed under a cached graph, or the tool is declared
            # but not wired. Distinct from a denial: it is *our* fault, so the
            # text says "temporarily", which is what drives a sensible retry.
            return self._deny(
                call,
                agent,
                started,
                "error",
                "That action is temporarily unavailable.",
            )

        # ── 3. BOUND ARGS — the manifest wins, always ─────────────────────
        args = {**call.arguments, **grant.bound_args}

        # ── 4. SCHEMA ─────────────────────────────────────────────────────
        schema = spec.parameters
        if grant.arg_constraints:
            schema = _intersect(schema, grant.arg_constraints)
        try:
            jsonschema.validate(args, schema, cls=jsonschema.Draft202012Validator)
        except jsonschema.ValidationError as exc:
            return self._deny(
                call,
                agent,
                started,
                "invalid_args",
                f"Invalid arguments: {_short(exc)}. Correct them and call the "
                f"tool again.",
            )

        # ── 5. CONFIRMATION ───────────────────────────────────────────────
        if grant.requires_confirmation and call.name not in confirmed:
            return ToolOutcome(
                message=Message(
                    role="tool",
                    tool_call_id=call.id,
                    name=call.name,
                    content=(
                        "Read the details back to the customer and get a clear "
                        "yes before performing this action."
                    ),
                ),
                record=self._record(call, agent, started, "deferred", spec),
                confirmation={
                    "tool": call.name,
                    "call_id": call.id,
                    "args": _redact(args, spec.pii_arg_paths),
                },
            )

        # ── 6. EXECUTE ────────────────────────────────────────────────────
        #
        # The budget is the tool's own timeout capped by what remains of the
        # turn's deadline, floored so it is never zero or negative. A tool that
        # outlives the turn is billing for an answer nobody will hear.
        remaining = context.deadline_ms - int(time.monotonic() * 1000)
        budget_ms = min(spec.timeout_ms, max(remaining, _MIN_TIMEOUT_MS))

        try:
            assert spec.impl is not None  # `available` checked above
            result: ToolResult = await asyncio.wait_for(
                spec.impl(context, args), timeout=budget_ms / 1000
            )
        except TimeoutError:
            return self._deny(
                call,
                agent,
                started,
                "timeout",
                f"The {call.name} system did not respond in time. Tell the "
                f"customer you could not complete that right now and offer to "
                f"try again or take a message.",
            )
        except ToolBusinessError as exc:
            # An expected domain failure. The model recovers from it in prose.
            return ToolOutcome(
                message=Message(
                    role="tool",
                    tool_call_id=call.id,
                    name=call.name,
                    content=exc.model_message,
                ),
                record=self._record(
                    call, agent, started, "business_error", spec,
                    error_class=type(exc).__name__,
                    error=exc.message,
                    error_code=exc.code,
                ),
                structured=exc.structured,
            )
        except DomainError as exc:
            return ToolOutcome(
                message=Message(
                    role="tool",
                    tool_call_id=call.id,
                    name=call.name,
                    # `model_message`, never `message`: the latter is diagnostic
                    # and has been read to a caller verbatim before now.
                    content=exc.model_message,
                ),
                record=self._record(
                    call, agent, started, "error", spec,
                    error_class=type(exc).__name__,
                    # The domain message is written FOR the model and carries no
                    # raw PII (a tool's own arguments are redacted separately),
                    # so it is safe in a log and is the single most useful field
                    # when working out why an agent gave up.
                    error=exc.message,
                    error_code=exc.code,
                ),
            )
        except Exception:
            # Never leak an internal exception into the prompt: stack traces
            # carry paths, table names and sometimes secrets, and the model may
            # read them aloud to the caller.
            log.exception(
                "ai.tool.unhandled_error",
                tool=call.name,
                agent=agent.key,
                tenant_id=str(context.tenant_id),
            )
            return self._deny(
                call,
                agent,
                started,
                "error",
                "That action failed unexpectedly. Apologise briefly and offer to "
                "have a team member follow up.",
            )

        return ToolOutcome(
            message=Message(
                role="tool",
                tool_call_id=call.id,
                name=call.name,
                content=result.content,
            ),
            record=self._record(call, agent, started, "ok", spec),
            collected_data=result.collected_data,
            structured=result.structured,
        )

    def _deny(
        self,
        call: ToolCall,
        agent: AgentSpec,
        started: float,
        status: str,
        content: str,
    ) -> ToolOutcome:
        """Answer a call that will not run.

        Still a ``tool`` message — see the module docstring on why every path
        returns one.
        """
        spec = self._registry.get(call.name)
        return ToolOutcome(
            message=Message(
                role="tool", tool_call_id=call.id, name=call.name, content=content
            ),
            record=self._record(call, agent, started, status, spec),
        )

    @staticmethod
    def _record(
        call: ToolCall,
        agent: AgentSpec,
        started: float,
        status: str,
        spec: ToolSpec | None = None,
        **failure: Any,
    ) -> dict[str, Any]:
        """The audit row for one call. Arguments are redacted before it exists.

        ``failure`` carries the error class, code and model-facing message on the
        paths that have one. A bare ``status=error`` is not diagnosable: it says
        a tool failed and nothing about why, so working out the cause means
        reading the source and guessing which branch was taken. That is not a
        thing an on-call engineer should have to do at 2am, and it is exactly
        what happened when a past-date availability failure had to be
        reconstructed by hand.
        """
        return {
            "call_id": call.id,
            "tool": call.name,
            "agent": agent.key,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "status": status,
            "redacted_args": _redact(
                call.arguments, spec.pii_arg_paths if spec else ()
            ),
            **{k: v for k, v in failure.items() if v is not None},
        }


def _redact(args: dict[str, Any], pii_paths: tuple[str, ...]) -> dict[str, Any]:
    """Mask PII arguments for logs, traces and audit records — contract §8.

    Partial rather than total: ``"Ro***im"`` still lets an engineer correlate two
    records as the same person while carrying almost no identity. Replacing the
    value entirely makes a log useless for debugging, which is how "we mask
    everything" becomes "we log nothing" becomes "we log raw".
    """
    out: dict[str, Any] = {}
    for key, value in args.items():
        if key not in pii_paths:
            out[key] = value
        elif isinstance(value, str) and len(value) > 4:
            out[key] = f"{value[:2]}***{value[-2:]}"
        else:
            out[key] = "***"
    return out


def _short(exc: jsonschema.ValidationError, limit: int = 200) -> str:
    """A one-line validation message, capped.

    Capped because a schema error on a nested object can serialize the entire
    schema, and putting that in the message list burns context the conversation
    needs. The path plus the reason is what the model can act on.
    """
    path = ".".join(str(p) for p in exc.absolute_path) or "(root)"
    return f"{path}: {exc.message}"[:limit]


def _project(schema: dict[str, Any], *, hide: set[str]) -> dict[str, Any]:
    """Remove bound properties from a schema, and from its ``required`` list.

    Both halves matter. Removing the property but leaving it required produces a
    schema no argument object can satisfy, and the model then fails every call to
    a tool it holds a perfectly good grant for.
    """
    if not hide:
        return schema
    projected = dict(schema)
    projected["properties"] = {
        k: v for k, v in (schema.get("properties") or {}).items() if k not in hide
    }
    projected["required"] = [r for r in (schema.get("required") or []) if r not in hide]
    return projected


def _intersect(schema: dict[str, Any], constraints: dict[str, Any]) -> dict[str, Any]:
    """Apply a grant's narrowing overlay to a tool's own schema.

    Per-property merge, and ``required`` is unioned rather than replaced — a
    grant may make a field mandatory that the tool left optional (a practice that
    always wants a phone number), but it can never make a tool's required field
    optional, because that would let the manifest hand the implementation a
    ``None`` it has no branch for.
    """
    merged = dict(schema)
    properties = dict(schema.get("properties") or {})
    for name, overlay in (constraints.get("properties") or {}).items():
        if name in properties and isinstance(overlay, dict):
            properties[name] = {**properties[name], **overlay}
    merged["properties"] = properties
    merged["required"] = sorted(
        set(schema.get("required") or []) | set(constraints.get("required") or [])
    )
    return merged

"""Node factories — 03 §4.1.

Three node kinds, one factory method each, matching
:class:`~packages.verticals.manifest.NodeKind`. Every node this module builds is
a closure over an :class:`~packages.core.ports.llm.LLMPort` and an
:class:`~packages.verticals.manifest.AgentSpec` — **nothing here knows what a
scheduler or a triage agent is**. Grepping this file for a domain word returns
nothing, which is contract §4 holding at the code level rather than in prose.

**Why the supervisor routes with a constrained text completion and not a tool
call.** A supervisor's only job is to name the next specialist. Modelling that
as a tool call costs a schema round-trip and admits a malformed-arguments
failure mode for a decision whose entire output space is a short, closed list
of agent keys. Asking for the key and matching it against the members list
gives the same answer, degrades gracefully (an unrecognized answer falls back
to the first member rather than failing the turn), and works on providers whose
tool support is weak or absent — which matters because the router hop is
exactly where a cheap, fast, tool-less model belongs.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import structlog
from packages.ai.prompts import PromptAssembler
from packages.ai.state import ConversationState
from packages.ai.streaming import emit
from packages.ai.tools import ToolExecutionContext, ToolExecutor
from packages.core.ports.llm import (
    LLMPort,
    Message,
    ToolCall,
    ToolSchema,
    Usage,
)
from packages.observability.tracing import (
    graph_node,
    llm_generation,
    usage_from_port,
)
from packages.verticals.manifest import AgentSpec, GraphSpec, VerticalManifest

__all__ = ["NodeFactory"]

log = structlog.get_logger(__name__)

#: How many model→tool→model round trips one agent turn may make before it must
#: speak. Three is enough for the real chains (look up a booking, check the new
#: time, move it) and low enough that a model looping on a failing tool stops
#: rather than burning the turn's budget on the same call.
_MAX_TOOL_ROUNDS = 3



class NodeFactory:
    """Builds runnable graph nodes from manifest specs.

    Args:
        llm: The port every node reaches a model through. One adapter for the
            whole graph in Phase 1; 03 §9.2's per-``model_class`` routing slots
            in here by making this a resolver rather than an instance, with no
            change to any node.
        manifest: Read for the prompt layers a node needs (vocabulary,
            policies, template variables).
        variables: Tenant values for the manifest's declared template variables.
        tenant_customization: Layer 4 free text.
    """

    def __init__(
        self,
        *,
        llm: LLMPort,
        manifest: VerticalManifest,
        variables: dict[str, str] | None = None,
        tenant_customization: str = "",
        tool_schemas: dict[str, ToolSchema] | None = None,
        executor: ToolExecutor | None = None,
    ) -> None:
        self._llm = llm
        self._manifest = manifest
        self._tool_schemas = tool_schemas or {}
        #: The single chokepoint every tool call goes through. ``None`` means
        #: this build has no executable tools, and an agent's granted schemas are
        #: then never sent to the model — describing a tool that cannot run is
        #: how an agent tells a caller it has booked something that does not
        #: exist.
        self._executor = executor
        self._assembler = PromptAssembler(
            manifest=manifest,
            tenant_customization=tenant_customization,
            variables=variables or {},
        )

    # ── Preflight, called by the compiler ─────────────────────────────────

    def unknown_tools(self, agent: AgentSpec) -> set[str]:
        """Tools this agent grants that the platform does not ship.

        A manifest referencing a tool we do not have is a hard compile failure
        (03 §4.2). Silently dropping it produces an agent that promises a
        capability it does not have, which is worse than refusing to compile —
        the caller is told "I've booked that for you" by an agent with no
        booking tool.
        """
        if self._executor is not None:
            return self._executor.unknown_tools(agent)
        return {grant.tool for grant in agent.tools} - set(self._tool_schemas)

    def validate_grants(self, agent: AgentSpec) -> None:
        """A grant's ``arg_constraints`` may only narrow, never widen.

        A widening constraint would let a manifest grant access to arguments the
        tool's own schema excludes, which is precisely the escalation the grant
        model exists to prevent. Delegated to the executor, which owns the
        schema-projection rules the check has to agree with — two copies of
        "what narrowing means" is how one of them ends up wrong.
        """
        if self._executor is not None:
            self._executor.validate_grants(agent)
            return
        for grant in agent.tools:
            schema = self._tool_schemas.get(grant.tool)
            if schema is None:
                continue
            properties = set(schema.parameters.get("properties") or {})
            if unknown := set(grant.bound_args) - properties:
                raise ValueError(
                    f"agent {agent.key!r} binds arguments {sorted(unknown)} that "
                    f"tool {grant.tool!r} does not declare"
                )

    # ── The three node kinds ──────────────────────────────────────────────

    @staticmethod
    def _handoff_notice(spec: GraphSpec, chosen: str) -> str:
        """What the CALLER is told while the specialist starts work.

        Routing costs a model round trip, and the specialist then costs another
        before its first token — measured at 3.7s of complete silence on a turn
        that calls no tool, so not even the "Checking the diary" indicator
        appears. A visitor watching a blank panel for four seconds assumes it
        is broken.

        The text comes from the MANIFEST (`agents[].handoff_notice`), never from
        the agent key: "connecting you to triage" tells a caller the practice
        has a triage specialist, which is internal structure and none of their
        business. The fallback is deliberately vague for the same reason.
        """
        target = spec.agent(chosen)
        notice = getattr(target, "handoff_notice", None) if target else None
        return notice or "One moment"

    def supervisor(
        self, agent: AgentSpec, spec: GraphSpec
    ) -> Callable[[ConversationState], Awaitable[dict[str, Any]]]:
        """A routing node: pick the next member, emit nothing to the user.

        Deliberately silent on the wire. A supervisor that greets and *then*
        hands off produces two assistant messages for one user turn, and the
        widget renders a stutter. The specialist speaks; the supervisor decides.
        """
        members = list(agent.members)

        async def _traced_run(state: ConversationState) -> dict[str, Any]:
            # ── Contentless message: answer here, skip routing entirely ──
            #
            # ROUTING IS STILL THE PRIORITY. This is the one case where there
            # is nothing to route: "hi", "thanks", "bye" — no request, no
            # specialist that could serve it better. Everything else, however
            # short, falls straight through to `_route` below and is dispatched
            # exactly as before.
            #
            # Placed BEFORE the routing call rather than after it because the
            # saving IS the call. Deciding who should say hello costs a full
            # model round trip, and the router was choosing the knowledge-base
            # specialist — the wrong agent for a greeting, and the one with the
            # largest prompt in the manifest. Two calls become one.
            #
            # Gated on `may_greet` in the MANIFEST: a vertical that wants its
            # supervisor strictly silent simply does not set it.
            if agent.may_greet and _is_greeting_turn(state):
                log.info("graph.supervisor.answered_greeting", agent=agent.key)
                prompt = self._assembler.assemble(
                    agent=agent,
                    history=list(state.get("messages", [])),
                    channel=str(state.get("channel", "web_chat")),
                    locale=str(state.get("locale", "en-GB")),
                    timezone=str(state.get("timezone", "UTC")),
                    now=datetime.now(tz=UTC),
                )
                text, usage, _ = await self._stream_reply(
                    prompt.messages, agent, succeeded=set(), previously=set()
                )
                return {
                    "messages": [
                        Message(role="assistant", content=text, name=agent.key)
                    ],
                    "next_agent": None,
                    "current_agent": agent.key,
                    "agent_history": [agent.key],
                    "step_count": 1,
                    "finish_reason": "stop",
                    **_usage_delta(usage),
                }

            chosen = await self._route(agent, members, state)

            # ── Structural anti-stick ─────────────────────────────────────
            #
            # A specialist that just announced a handoff must not receive the
            # next turn. Left to the routing prompt alone this was unreliable —
            # it held on some runs and not others, and when it failed triage
            # repeated "I'll pass you to my colleague" for four consecutive
            # turns while the scheduler's booking tools stayed unreachable and
            # the caller, in pain, got nothing.
            #
            # "Unreliable in prose" is the signal to move a rule into structure.
            # The manifest already declares each agent's `handoffs`, which is
            # the authoritative statement of where it may send someone — so when
            # the current specialist has said it is handing over and the router
            # picks it *again*, the decision is overridden with its own declared
            # target. Data-driven and manifest-agnostic: nothing here knows what
            # triage or a scheduler is.
            # The last SPEAKING agent, not `current_agent`: the supervisor sets
            # `current_agent` to itself on every hop, so by the time it runs
            # again that field says "receptionist" and never names the
            # specialist whose turn just ended. Reading `agent_history` backwards
            # for the most recent member is what actually answers "who just
            # spoke" — getting this wrong is why the first version of this
            # override silently never fired.
            previous = _last_speaker(state, members)

            # ── A named destination is authoritative ──────────────────────
            #
            # When a specialist says where it is sending the caller — "I'll pass
            # you to the scheduler" — that is a decision already taken, by the
            # agent holding the context. Re-deciding it from a one-line
            # catalogue is how an emergency became an escalation: the router
            # sent roughly one in four chipped-tooth callers to
            # `human_transfer`, which told them "someone will be in touch" and
            # ended the conversation with no booking and nobody alerted.
            #
            # The routing prompt already asks the model to honour it, and asking
            # was not enough — a probe of the exact failing transcript picked
            # `scheduler` six times out of six in isolation, so the failure is
            # not reproducible by argument, only by volume. That is precisely
            # when a rule belongs in structure rather than in prose.
            if previous is not None and (
                named := _named_destination(state, members)
            ):
                if named != chosen:
                    log.info(
                        "graph.supervisor.destination_honoured",
                        agent=agent.key,
                        router_chose=chosen,
                        specialist_named=named,
                        reason="the specialist named where it was sending the "
                        "caller; that decision was taken with the context",
                    )
                chosen = named

            elif (
                chosen == previous
                and previous is not None
                and _announced_handoff(state)
            ):
                if forced := self._declared_handoff(previous, members):
                    log.info(
                        "graph.supervisor.handoff_forced",
                        agent=agent.key,
                        stuck_on=previous,
                        forced_to=forced,
                        reason="specialist announced a handoff and was re-selected",
                    )
                    chosen = forced

            log.debug(
                "graph.supervisor.routed",
                agent=agent.key,
                chosen=chosen,
                conversation_id=str(state.get("conversation_id")),
            )
            if chosen != state.get("current_agent"):
                await emit(
                    "handoff",
                    {
                        "from_agent": agent.key,
                        "to_agent": chosen,
                        "reason": "supervisor_routing",
                        # A caller-safe phrase for the widget, which never sees
                        # `to_agent` — a visitor should not be told the practice
                        # runs a triage specialist. Manifest-authored, so a
                        # vertical writes its own; falls back to a neutral line
                        # rather than inventing one here.
                        "notice": self._handoff_notice(spec, chosen),
                    },
                )
            return {
                "next_agent": chosen,
                "current_agent": agent.key,
                "agent_history": [agent.key],
                "step_count": 1,
                "handoff_reason": "supervisor_routing",
            }

        async def run(state: ConversationState) -> dict[str, Any]:
            """Open the supervisor's node span. See `llm_agent` for the shape."""
            with graph_node(agent.key, agent_key=agent.key) as span:
                span.set_attribute("ai.node.kind", "supervisor")
                result = await _traced_run(state)
                if (nxt := result.get("next_agent")) is not None:
                    span.set_attribute("ai.route.next_agent", str(nxt))
                return result

        return run

    def llm_agent(
        self, agent: AgentSpec, spec: GraphSpec
    ) -> Callable[[ConversationState], Awaitable[dict[str, Any]]]:
        """A speaking node: call tools if it needs to, then stream a reply.

        **The tool loop is what makes an agent able to do anything.** Without it
        a "scheduler" is a model that talks about scheduling — it says "I've
        booked that for you" and no row exists. The loop is:

            model → tool calls? → execute (grant-checked) → model → …

        bounded by :data:`_MAX_TOOL_ROUNDS` and by the turn's own budget. The
        final pass is *streamed*, so the caller sees the answer token by token
        while the intermediate tool round-trips are silent — a caller should hear
        "Tuesday at nine is free", not a narration of the lookup.

        **Tools are only offered when they can actually run.** If this build has
        no executor, ``grant_schemas`` is never sent, so the model is never told
        about a capability that would fail. Describing an unavailable tool is how
        an agent promises something it cannot deliver.
        """
        grant_schemas = (
            self._executor.schemas_for(agent) if self._executor is not None else []
        )

        async def _traced_run(state: ConversationState) -> dict[str, Any]:
            history = list(state.get("messages", []))
            collected = dict(state.get("collected_data") or {})
            usage_total: Usage | None = None
            tool_calls_made = 0
            #: One nudge per turn. A model that declines twice is not going to
            #: act, and looping on it burns the turn's budget while the caller
            #: waits — the guard below handles the reply instead.
            nudged = False
            #: What the last failing tool said, in caller-ready words. Carried
            #: so a suppressed claim can explain the actual obstacle instead of
            #: degrading to a generic apology that invites the caller to repeat
            #: the input that just failed.
            failure_message: str | None = None
            #: Which side-effecting tools actually returned ``ok`` this turn.
            #: The reply guard is checked against this and nothing else: a claim
            #: that something was moved or cancelled is only true if the write
            #: that would have moved or cancelled it succeeded.
            succeeded: set[str] = set()
            #: Set by the fast path below: prose the model has already produced
            #: and that no tool ran after, so it can be streamed as-is instead
            #: of paying a second round trip to regenerate it.
            reply_text: str | None = None
            confirmed: frozenset[str] = frozenset(
                collected.get("_confirmed_tools") or ()
            )

            # ── The tool rounds. Non-streaming: a round that ends in a tool
            #    call is mostly not text, and the text it does carry is vetted
            #    and released as a PREAMBLE (see `_emit_preamble`) rather than
            #    streamed raw — a half-vetted sentence cannot be recalled once
            #    a token of it has left.
            for _round in range(_MAX_TOOL_ROUNDS):
                if not grant_schemas or self._executor is None:
                    break
                if state.get("tool_call_count", 0) + tool_calls_made >= state.get(
                    "max_tool_calls", 16
                ):
                    log.info("graph.tools.budget_exhausted", agent=agent.key)
                    break

                prompt = self._assembler.assemble(
                    agent=agent,
                    history=history,
                    tool_schemas=grant_schemas,
                    collected_data=collected,
                    running_summary=str(state.get("running_summary") or ""),
                    channel=str(state.get("channel", "web_chat")),
                    locale=str(state.get("locale", "en-GB")),
                    timezone=str(state.get("timezone", "UTC")),
                    now=datetime.now(tz=UTC),
                )
                # The tool-deciding model call. On a tool-calling turn this is
                # the first of two round-trips to the provider, and separating
                # it from the streamed reply below is what makes a waterfall
                # able to answer "was it the model or the tool that was slow".
                with llm_generation(
                    "llm.chat",
                    model=getattr(self._llm, "model", "unknown"),
                    provider=self._llm.provider,
                    agent_key=agent.key,
                ) as gen:
                    gen.set_content(input=list(prompt.messages))
                    gen.set_attribute("gen_ai.request.round", _round)
                    gen.set_attribute("gen_ai.request.tool_count", len(grant_schemas))
                    result = await self._llm.chat(
                        messages=prompt.messages,
                        tools=grant_schemas,
                        temperature=agent.temperature,
                        timeout_ms=20_000,
                    )
                    gen.set_usage(
                        usage_from_port(result.usage, model=result.model)
                    )
                    gen.set_attribute(
                        "gen_ai.response.finish_reason", result.finish_reason
                    )
                    gen.set_attribute(
                        "gen_ai.response.tool_calls",
                        [call.name for call in result.tool_calls],
                    )
                    gen.set_content(output=result.message)
                usage_total = _sum_usage(usage_total, result.usage)

                if not result.tool_calls:
                    # The model answered in prose rather than calling a tool.
                    #
                    # ── The trap this guards ─────────────────────────────
                    #
                    # Prose is NOT proof that the model is finished. It routinely
                    # announces an intention first — "I'll move your check-up to
                    # Friday. One moment while I confirm that." — and calls the
                    # tool on the next exchange. That is a normal, human way to
                    # answer, and the model has no way to know this loop is about
                    # to end.
                    #
                    # Breaking here sends it to a final pass built with
                    # `tool_schemas=None`, so it becomes STRUCTURALLY INCAPABLE
                    # of acting: it promised to move an appointment, the tool was
                    # taken away mid-turn, and the turn ends. The caller is told
                    # their appointment moved and it did not move. Nothing
                    # anywhere reports a failure — not the logs, not the model,
                    # not the transcript — which makes it materially worse than
                    # an error.
                    #
                    # So an unfulfilled promise gets one more round WITH tools,
                    # and a nudge naming what it said it would do. If it declines
                    # again, the loop exits normally and the guard below strips
                    # the false claim before anyone hears it.
                    if (
                        _promises_action(
                            result.message.content,
                            backed=succeeded
                            | set(collected.get("_completed_tools") or ()),
                        )
                        and not nudged
                    ):
                        nudged = True
                        history.append(result.message)
                        history.append(
                            Message(role="user", content=_ACTION_NUDGE)
                        )
                        continue

                    # Genuinely finished.
                    #
                    # ── The fast path ────────────────────────────────────
                    #
                    # A round that answers in prose and calls no tool IS the
                    # answer: it was written with every tool result so far in
                    # its history, and nothing runs after it that could change
                    # it. Regenerating it costs a second full round trip to the
                    # provider for a reply we are holding.
                    #
                    # Measured on "hi": 1.56s to decide no tool was needed, then
                    # 0.95s to say "Hello! How can I help you today?" — 2.7s of
                    # silence for six words, and the second call produced
                    # essentially what the first already had. Measured again
                    # after a tool: the post-tool round said "I can get you in
                    # with Dr Deshpande at 5:30 this afternoon… may I have your
                    # name?" and the regenerating stream then said the same
                    # sentence, 1.07s and a full prompt's tokens later.
                    #
                    # This used to be gated on `tool_calls_made == 0`, from a
                    # worry that post-tool prose "predates the tool result". It
                    # does not: prose that arrives WITH a tool call never reaches
                    # this branch (the call runs instead), and prose from the
                    # round AFTER a tool was generated from that tool's output.
                    # The replay still goes through the claim guard with the
                    # turn's `succeeded` set, so a claim is checked identically
                    # on both paths.
                    if not nudged and result.message.content:
                        # `not nudged` matters: once the model has been nudged
                        # for narrating without acting, the prose in hand is the
                        # narration itself — "I'll move it. One moment." —
                        # and replaying it would put a promise on the caller's
                        # screen that the guard never gets to check. The final
                        # pass regenerates, and the guard sees the claim.
                        reply_text = result.message.content
                    break

                history.append(result.message)
                # ── The preamble ──────────────────────────────────────
                # "I'm sorry to hear you're in pain — let me check what
                # emergency slots we have." written in the SAME round as the
                # tool call. Before this it was discarded, and the caller sat
                # in silence for the tool round-trip plus the whole final
                # generation. Showing it now costs nothing (it is already
                # generated) and moves the first visible response from ~4s to
                # ~1.5s on a tool-calling turn.
                if result.message.content:
                    await self._emit_preamble(
                        result.message.content,
                        agent=agent,
                        previously=set(collected.get("_completed_tools") or ()),
                    )
                outcomes = await self._run_tools(
                    calls=result.tool_calls,
                    agent=agent,
                    state=state,
                    confirmed=confirmed,
                )
                for call, outcome in zip(result.tool_calls, outcomes, strict=True):
                    history.append(outcome.message)
                    collected.update(outcome.collected_data)
                    status = outcome.record.get("status")
                    if status == "ok":
                        succeeded.add(call.name)
                    elif status == "business_error":
                        # An EXPECTED failure the caller can act on — a short
                        # phone number, a taken slot, a cancellation policy. Its
                        # message was written to be spoken; keep it so the guard
                        # can use it if the model then over-claims.
                        failure_message = outcome.message.content
                tool_calls_made += len(result.tool_calls)

            prompt = self._assembler.assemble(
                agent=agent,
                # The nudge is scaffolding for the tool loop, not conversation.
                # Left in, the model answers the *instruction* rather than the
                # customer — "the tool returned success, no further action is
                # needed" — which is an engineer's voice in a receptionist's
                # mouth and names internals the platform frame forbids.
                history=[m for m in history if m.content != _ACTION_NUDGE],
                # Tools are withheld from the final pass so it must speak. A
                # model still holding a tool schema after three rounds will
                # often call it again rather than answer, and the caller gets
                # silence.
                tool_schemas=None,
                collected_data=collected,
                running_summary=str(state.get("running_summary") or ""),
                channel=str(state.get("channel", "web_chat")),
                locale=str(state.get("locale", "en-GB")),
                timezone=str(state.get("timezone", "UTC")),
                now=datetime.now(tz=UTC),
            )

            # Per-node timing is deliberately not recorded here: the runner
            # times the whole turn, which is what `turns.latency_ms` means and
            # what a caller experiences. A per-node figure would need its own
            # column to be anything other than misleading.
            text, usage, finish = await self._stream_reply(
                prompt.messages,
                agent,
                succeeded=succeeded,
                previously=set(collected.get("_completed_tools") or ()) - succeeded,
                failure=failure_message,
                # Present only when no tool ran: stream the prose already in
                # hand instead of paying a second round trip for it.
                replay=reply_text,
            )
            usage_total = _sum_usage(usage_total, usage)

            # Who spoke, durably. See `_last_speaker` for why `agent_history`
            # cannot answer this.
            collected["_last_speaker"] = agent.key
            # Everything that has ever succeeded in this conversation. A caller
            # who says "yes, book it" once more after it is booked is seeking
            # reassurance; answering that with "I wasn't able to complete that"
            # is false and alarming. The guard needs to tell "never happened"
            # from "happened a turn ago", and only a persisted set can.
            if succeeded:
                collected["_completed_tools"] = sorted(
                    set(collected.get("_completed_tools") or ()) | succeeded
                )
            reply = Message(role="assistant", content=text, name=agent.key)
            return {
                # Only the assistant's spoken reply joins the durable history.
                # The intermediate tool messages did their work in this node's
                # local `history` and would otherwise be replayed into every
                # subsequent turn as context the model never asked for.
                "messages": [reply],
                "collected_data": collected,
                "current_agent": agent.key,
                "agent_history": [agent.key],
                "step_count": 1,
                "tool_call_count": tool_calls_made,
                # An llm_agent hands control back rather than choosing its own
                # successor. Letting a specialist pick the next specialist is
                # how a two-agent ping-pong starts, and the supervisor already
                # holds the routing role.
                "next_agent": None,
                "finish_reason": finish,
                # Real state keys, not a `_private` one: LangGraph drops any
                # key the state schema does not declare, so an underscore-
                # prefixed field would vanish silently and every turn would
                # persist null token counts.
                **_usage_delta(usage_total),
            }

        async def run(state: ConversationState) -> dict[str, Any]:
            """Open the node span, then run the agent.

            A wrapper rather than a `with` inside the body: the body is long,
            and re-indenting it to add a context manager would bury a
            one-line instrumentation change in a 180-line diff that nobody
            can review for behaviour changes.
            """
            with graph_node(agent.key, agent_key=agent.key) as span:
                span.set_attribute("ai.node.kind", "llm_agent")
                result = await _traced_run(state)
                if (reason := result.get("finish_reason")) is not None:
                    span.set_attribute("ai.node.finish_reason", str(reason))
                span.set_attribute(
                    "ai.node.tool_calls", int(result.get("tool_call_count", 0) or 0)
                )
                return result

        return run

    async def _run_tools(
        self,
        *,
        calls: list[ToolCall],
        agent: AgentSpec,
        state: ConversationState,
        confirmed: frozenset[str],
    ) -> list[Any]:
        """Execute one round of tool calls through the grant chokepoint.

        Emits ``tool.started`` / ``tool.finished`` around each so the widget can
        show "Checking the diary…" — with the manifest-authored label and the
        tool key only. 06 §3.2 forbids putting arguments on the wire: they hold
        whatever the model extracted from the conversation, which for a medical
        intake is health data.
        """
        assert self._executor is not None  # only called when one exists

        for call in calls:
            await emit(
                "tool.started",
                {
                    "tool_call_id": call.id,
                    "tool_key": call.name,
                    "label": self._label_for(agent, call.name),
                },
            )

        context = ToolExecutionContext(
            tenant_id=_uuid(state.get("tenant_id")),
            conversation_id=_uuid(state.get("conversation_id")),
            agent_key=agent.key,
            correlation_id=str(state.get("conversation_id")),
            deadline_ms=int(state.get("deadline_ms", 0)),
            channel=str(state.get("channel", "web_chat")),
        )
        outcomes = await self._executor.execute_many(
            calls=calls, agent=agent, context=context, confirmed=confirmed
        )

        for call, outcome in zip(calls, outcomes, strict=True):
            # The failure fields are emitted alongside the status, not instead of
            # it. `status=error` with no reason is not diagnosable: it says a
            # tool failed and nothing about why, so the cause has to be
            # reconstructed by reading source and guessing which branch ran.
            # Only the keys that are actually present: `_record` omits the
            # failure fields on a successful call, and emitting them as explicit
            # `None`s makes every happy-path line three columns wider for no
            # information — which is how a log people skim becomes a log people
            # ignore.
            log.info(
                "graph.tool.executed",
                agent=agent.key,
                tool=call.name,
                **{
                    key: value
                    for key, value in outcome.record.items()
                    if key not in ("call_id", "agent", "tool")
                },
            )
            await emit(
                "tool.finished",
                {
                    "tool_call_id": call.id,
                    "status": outcome.record.get("status", "ok"),
                    # The structured body is the tool's own answer, already
                    # shaped for display by the tool. Safe to send: it is what
                    # the practice would put on a confirmation screen.
                    "result": outcome.structured or None,
                },
            )
        return outcomes

    def terminal(
        self, agent: AgentSpec, spec: GraphSpec
    ) -> Callable[[ConversationState], Awaitable[dict[str, Any]]]:
        """A closing node: speak once, write the Outcome, end.

        The outcome is manifest-declared (contract §7's ``Outcome``), which is
        why a restaurant's ``wrap_up`` can write ``booked`` and a support
        vertical's can write ``resolved`` with no code difference.
        """

        async def run(state: ConversationState) -> dict[str, Any]:
            prompt = self._assembler.assemble(
                agent=agent,
                history=list(state.get("messages", [])),
                collected_data=dict(state.get("collected_data") or {}),
                channel=str(state.get("channel", "web_chat")),
                locale=str(state.get("locale", "en-GB")),
                timezone=str(state.get("timezone", "UTC")),
                now=datetime.now(tz=UTC),
            )
            # A terminal node runs no tools, so everything it can truthfully say
            # was done on an EARLIER turn — which is exactly the state the guard
            # treats as unverified unless told otherwise. Without this, the
            # closing agent summarising a booking that genuinely happened was
            # answered with "I wasn't able to complete that, let me put you
            # through to a colleague", immediately followed by the reference and
            # arrival instructions. Contradictory, alarming, and it invented a
            # transfer.
            completed = set(
                (state.get("collected_data") or {}).get("_completed_tools") or ()
            )
            text, usage, _ = await self._stream_reply(
                prompt.messages, agent, succeeded=set(), previously=completed
            )
            return {
                "messages": [Message(role="assistant", content=text, name=agent.key)],
                "current_agent": agent.key,
                "agent_history": [agent.key],
                "step_count": 1,
                "next_agent": None,
                "outcome": agent.outcome,
                "finish_reason": "stop",
                **_usage_delta(usage),
            }

        return run

    # ── Shared internals ──────────────────────────────────────────────────

    async def _emit_preamble(
        self, content: str, *, agent: AgentSpec, previously: set[str]
    ) -> None:
        """Release the prose a tool-calling round wrote, if it is safe to say now.

        The text is pushed through a claim guard whose ``succeeded`` set is
        EMPTY — because at this moment nothing has succeeded: the tool has not
        run yet. So "I've booked that, one moment" is not shown (it is a claim
        about a write that has not happened), while "Let me check the diary" is.
        A preamble that would be rewritten is dropped outright rather than
        replaced: the replacement sentence says nothing changed, which is the
        wrong thing to say a second before the tool changes it.

        Questions are dropped too: a round that asks "what time suits?" AND
        calls a tool is narrating, and the final pass will ask properly — one
        question per reply is a guard rule, and a preamble must not spend it.

        Emitted as ordinary ``token`` deltas, flagged ``interim``, so a web
        client renders it in place with no changes and the voice worker can
        speak it the moment it arrives. The trailing space is the seam between
        this and the final reply, which streams into the same message.
        """
        guard = _UnverifiedClaimGuard(set(), previously, None)
        text = (guard.feed(content) + guard.flush()).strip()
        if guard.tainted or guard.rewrote or guard.leaked_handle or not text:
            log.info("graph.reply.preamble_withheld", agent=agent.key)
            return
        if "?" in text:
            log.info("graph.reply.preamble_withheld", agent=agent.key, reason="question")
            return
        payload = text + " "
        for i in range(0, len(payload), _REPLAY_CHUNK_CHARS):
            await emit(
                "token",
                {"text": payload[i : i + _REPLAY_CHUNK_CHARS], "interim": True},
            )
        log.info("graph.reply.preamble", agent=agent.key, chars=len(text))

    async def _stream_reply(
        self,
        messages: list[Message],
        agent: AgentSpec,
        *,
        succeeded: set[str] | None = None,
        previously: set[str] | None = None,
        failure: str | None = None,
        replay: str | None = None,
    ) -> tuple[str, Any, str]:
        """Stream one completion, emitting deltas as they land.

        ``replay`` short-circuits the provider entirely: the text is already in
        hand from the tool-deciding round and no tool ran after it, so a second
        round trip would regenerate what we are holding. It is still emitted
        through the SAME guard and the same sink, so every protection —
        unverified claims, slot handles, one ask per reply — applies identically.
        The saving is a full provider round trip, which on a simple greeting is
        half the total wait.

        Returns the assembled text so the node can put it in state. The sink
        gets tokens *as they arrive*; the return value is for the durable
        record. Both are needed and they are not the same thing — the sink is
        best-effort delivery to a browser that may have closed, the return
        value is what gets written to ``turns``.

        Args:
            succeeded: Side-effecting tools that returned ``ok`` this turn. Any
                sentence claiming a booking was made, moved or cancelled is
                checked against this and **replaced** if the corresponding write
                did not happen. See :class:`_UnverifiedClaimGuard` — telling a
                caller their appointment moved when it did not is the single most
                damaging thing this agent can do, and it is precisely the failure
                the booking engine exists to prevent.
        """
        chunks: list[str] = []
        usage = None
        finish = "stop"
        guard = _UnverifiedClaimGuard(
            succeeded or set(), previously or set(), failure
        )

        if replay is not None:
            # Chunked rather than emitted whole: the sink's contract is a
            # stream of deltas, and a browser rendering one 200-character token
            # jumps the text into place instead of typing it. The guard buffers
            # by sentence either way, so this changes nothing it can see.
            for i in range(0, len(replay), _REPLAY_CHUNK_CHARS):
                piece = guard.feed(replay[i : i + _REPLAY_CHUNK_CHARS])
                if piece:
                    chunks.append(piece)
                    await emit("token", {"text": piece})
            if tail := guard.flush():
                chunks.append(tail)
                await emit("token", {"text": tail})
            log.info("graph.reply.replayed", agent=agent.key, chars=len(replay))
            return "".join(chunks), None, "stop"

        # The streamed reply. Its span is separate from the tool-round `chat`
        # spans above so a waterfall shows the two provider round-trips as two
        # bars: on a tool-calling turn they are the two largest, and which of
        # them dominates is the question this instrumentation exists to answer.
        stack = contextlib.ExitStack()
        gen = stack.enter_context(
            llm_generation(
                "llm.stream",
                model=getattr(self._llm, "model", "unknown"),
                provider=self._llm.provider,
                agent_key=agent.key,
            )
        )
        gen.set_content(input=list(messages))
        started = time.perf_counter()
        #: Time to first token, measured at the first text delta the guard
        #: releases. This is the number a caller actually experiences — a
        #: provider that streams its first token in 300ms feels responsive even
        #: if the full reply takes two seconds — and it is invisible in a total
        #: duration.
        ttft_ms: int | None = None

        try:
            stream = self._llm.stream(
                messages=messages,
                temperature=agent.temperature,
                timeout_ms=30_000,
            )
            async for delta in stream:
                match delta.kind:
                    case "text" if delta.text:
                        # Buffered by sentence: a claim cannot be un-said once a
                        # token carrying it has left, so the guard holds a sentence
                        # back until it can see the whole of it.
                        if safe := guard.feed(delta.text):
                            if ttft_ms is None:
                                ttft_ms = int((time.perf_counter() - started) * 1000)
                            chunks.append(safe)
                            await emit("token", {"text": safe})
                    case "tool_call_start" if delta.tool_call is not None:
                        # 06 §3.2: the wire carries the tool key and a
                        # manifest-authored label, never `partial_args` — arguments
                        # hold whatever the model extracted from the conversation,
                        # which for a medical intake is PHI.
                        await emit(
                            "tool.started",
                            {
                                "tool_call_id": delta.tool_call.id,
                                "tool_key": delta.tool_call.name,
                                "label": self._label_for(agent, delta.tool_call.name),
                            },
                        )
                    case "tool_call_end" if delta.tool_call is not None:
                        await emit(
                            "tool.finished",
                            {"tool_call_id": delta.tool_call.id, "status": "ok"},
                        )
                    case "usage":
                        usage = delta.usage
                    case "done":
                        pass
        except Exception as exc:
            gen.set_error(exc)
            stack.close()
            raise

        if tail := guard.flush():
            chunks.append(tail)
            await emit("token", {"text": tail})

        if guard.trimmed_asks:
            log.info(
                "graph.reply.compound_ask_trimmed",
                agent=agent.key,
                trimmed=guard.trimmed_asks,
            )

        if guard.dropped_questions:
            # Not a warning: a model that bundles two asks is being helpful in
            # the wrong shape, not doing harm. Logged so the rate is visible —
            # if it climbs, the role prompt is asking for too much at once and
            # the fix belongs there rather than in the guard.
            log.info(
                "graph.reply.extra_question_dropped",
                agent=agent.key,
                dropped=guard.dropped_questions,
            )

        if guard.leaked_handle:
            log.warning(
                "graph.reply.slot_handle_scrubbed",
                agent=agent.key,
                reason="the model tried to speak an internal slot handle; it "
                "was removed before any token was emitted",
            )

        if guard.rewrote:
            # A model claiming work it did not do is a correctness incident, not
            # a style nit. Logged loudly because the guard makes it invisible to
            # the caller — and an invisible failure that nobody counts is one
            # that never gets fixed.
            log.warning(
                "graph.reply.unverified_claim_suppressed",
                agent=agent.key,
                claims=sorted(guard.rewrote),
                succeeded=sorted(succeeded or ()),
            )

        text = "".join(chunks)
        gen.set_usage(usage_from_port(usage, model=getattr(self._llm, "model", "")))
        gen.set_content(output=text)
        gen.set_attribute("gen_ai.response.finish_reason", finish)
        if ttft_ms is not None:
            gen.set_attribute("gen_ai.response.ttft_ms", ttft_ms)
        if guard.rewrote:
            # Surfaced as a span attribute as well as a log line: a suppressed
            # claim is invisible to the caller by design, so the trace is the
            # only place someone reviewing a conversation would ever see it.
            gen.set_attribute("ai.reply.claims_suppressed", sorted(guard.rewrote))
        stack.close()

        return text, usage, finish

    @staticmethod
    def _label_for(agent: AgentSpec, tool_key: str) -> str:
        for grant in agent.tools:
            if grant.tool == tool_key:
                return grant.label
        return "Working…"

    def _declared_handoff(self, agent_key: str, members: list[str]) -> str | None:
        """Where the manifest says this agent hands off to.

        The first declared handoff that is also a member of the supervisor, and
        that is not the agent itself. Manifest data, so a restaurant's agents
        route by their own declarations with no code change — and an agent that
        declares no handoff is left alone rather than being sent somewhere
        invented.
        """
        try:
            spec = self._manifest.graph.agent(agent_key)
        except KeyError:  # pragma: no cover - agent_key comes from members
            return None
        for target in spec.handoffs:
            if target != agent_key and target in members:
                return target
        return None

    async def _route(
        self, agent: AgentSpec, members: list[str], state: ConversationState
    ) -> str:
        """Ask the model for the next agent key; fall back to the first member.

        The fallback is not laziness. A supervisor that fails to produce a
        recognizable key has one of two causes — a flaky completion, or a
        manifest whose member descriptions do not distinguish them — and in
        both cases routing to the first declared member and continuing beats
        failing the caller's turn. The manifest author puts the general-purpose
        specialist first, and the log line below is what surfaces the second
        cause.
        """
        member_specs = [self._manifest.graph.agent(key) for key in members]
        catalogue = "\n".join(
            f"- {m.key}: {m.display_name}. {_routing_blurb(m.role_prompt)}"
            for m in member_specs
        )

        # ── Route on the CONVERSATION, not on the last utterance ──────────
        #
        # This originally passed only the most recent user message, and that is
        # a routing bug with a very specific signature: it works perfectly on
        # turn one and degrades to nonsense by turn three, because the answers
        # people give to follow-up questions are contextless on their own.
        #
        # Observed: "chipped tooth" -> triage (right). "from yesterday night" ->
        # triage (right, by luck). "yes" -> wrap_up (wrong) — the supervisor saw
        # the single word "yes" and reasonably concluded the caller was done.
        # The triage agent, mid-assessment, never ran again, and the caller was
        # dropped into a closing message while describing a worsening symptom.
        #
        # A supervisor's whole job is to know what the conversation is *about*,
        # which is a property of the exchange, not of its last line. The recent
        # window is enough — routing needs the topic, not the transcript — and
        # bounding it keeps the cheap router model cheap.
        recent = _recent_exchange(list(state.get("messages", [])))
        # The last SPEAKING agent — see the note in `supervisor`. `current_agent`
        # is the supervisor itself by the time this runs, so naming it here told
        # the router "the specialist handling this is receptionist", which is
        # never true and is no help at all.
        current = _last_speaker(state, members)

        prompt = self._assembler.assemble(
            agent=agent,
            history=[],
            channel=str(state.get("channel", "web_chat")),
            locale=str(state.get("locale", "en-GB")),
            timezone=str(state.get("timezone", "UTC")),
            now=datetime.now(tz=UTC),
        )
        # Naming who is currently handling it, and saying that an unfinished
        # assessment continues, is what stops a mid-flow "yes" being read as
        # "the caller is finished". Without it the supervisor re-decides from
        # scratch every turn and a specialist can never ask a second question.
        # Continuity, with an explicit release condition.
        #
        # Both halves are needed and the first without the second is its own
        # bug. "Keep routing to them" alone makes a specialist sticky forever:
        # triage assessed the symptom, said "I'll pass you to the scheduler",
        # and the supervisor sent the next four turns straight back to triage —
        # which then repeated the handoff sentence it had already said, while no
        # booking tool was ever reachable.
        #
        # So: stay while they are mid-task, and leave the moment they say they
        # are handing over. The specialist's own words are the signal, which is
        # what makes this work for any manifest without naming an agent here.
        continuity = (
            f"\nThe specialist currently handling this is {current}. Keep "
            f"routing to them while they are mid-task — asking follow-up "
            f"questions, collecting details, working through a booking.\n"
            f"BUT if their last message said they are handing over, passing the "
            f"customer on, or that someone else will take it from here, route "
            f"to THAT specialist now. A specialist that has announced a handoff "
            f"is finished; sending the turn back to them makes them repeat "
            f"themselves.\n"
            if current and current in members
            else ""
        )
        instruction = Message(
            role="user",
            content=(
                f"Specialists you may route to:\n{catalogue}\n"
                f"{continuity}\n"
                f"The recent conversation:\n{recent}\n\n"
                f"Reply with exactly one specialist key from the list above and "
                f"nothing else."
            ),
        )

        # The supervisor's routing hop. Given its own generation span because
        # it is a full provider round-trip that happens *before* the specialist
        # has said anything — so on a slow turn it is a plausible suspect, and
        # without a span of its own it would be invisible inside the node's
        # total duration.
        try:
            with llm_generation(
                "llm.chat",
                model=getattr(self._llm, "model", "unknown"),
                provider=self._llm.provider,
                agent_key=agent.key,
            ) as gen:
                gen.set_attribute("ai.node.kind", "supervisor_route")
                gen.set_content(input=[*prompt.messages, instruction])
                result = await self._llm.chat(
                    messages=[*prompt.messages, instruction],
                    temperature=0.0,
                    max_tokens=16,
                    timeout_ms=8_000,
                )
                gen.set_usage(usage_from_port(result.usage, model=result.model))
                gen.set_content(output=result.message)
        except Exception:
            log.warning(
                "graph.supervisor.route_failed", agent=agent.key, fallback=members[0]
            )
            return members[0]

        answer = (result.message.content or "").strip().lower()
        for key in members:
            if key in answer:
                return key
        log.warning(
            "graph.supervisor.unrecognized_route",
            agent=agent.key,
            answer=answer[:80],
            fallback=members[0],
        )
        return members[0]


#: Longest specialist blurb in the routing catalogue. Generous enough to carry
#: a whole first sentence for every first-party agent — the previous 160 cut
#: ALL SIX of the medical manifest's agents mid-word ("never guess what is free
#: and never inv"), which is the worst possible place to stop: the router
#: chooses between descriptions whose distinguishing clause may be the part
#: that was removed.
_ROUTING_BLURB_CHARS: Final = 320


def _routing_blurb(role_prompt: str) -> str:
    """One self-contained sentence describing what a specialist does.

    Breaks on a sentence boundary rather than a character count. A blurb that
    stops mid-word reads as corrupted text to the model as much as to a person,
    and the router's entire input is these six lines.
    """
    first = role_prompt.splitlines()[0].strip()
    if len(first) <= _ROUTING_BLURB_CHARS:
        return first
    window = first[:_ROUTING_BLURB_CHARS]
    # Prefer the last sentence end; fall back to the last word boundary so the
    # result is never clipped mid-word.
    cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
    if cut > _ROUTING_BLURB_CHARS // 2:
        return window[: cut + 1]
    cut = window.rfind(" ")
    return (window[:cut] if cut > 0 else window).rstrip() + "…"


#: How many recent turns the supervisor sees when routing. Six is three
#: exchanges — enough to carry "chipped tooth ... getting worse ... yes" as one
#: topic, small enough that the cheap router model stays cheap and fast.
_ROUTING_CONTEXT_TURNS: Final = 6

#: Per-line cap in the routing context. A caller reciting their medical history
#: must not push the earlier turns out of a bounded prompt.
_ROUTING_LINE_CHARS: Final = 240


#: An agent saying it is handing the customer on. Future tense included here
#: deliberately — unlike the claim guard, which must NOT block an honest "I'll
#: pass you to my colleague", this is asking a different question: has the
#: specialist finished? An announced intention to hand over means yes.
_HANDOFF_ANNOUNCEMENT: Final = re.compile(
    r"\b(?:"
    r"(?:pass|passing|passed|hand|handing|handed|transfer|transferring)\s+"
    r"(?:you|your\s+\w+|this|that|it)\s+"
    r"(?:over\s+)?(?:on\s+)?(?:to|across)"
    r"|(?:my|a)\s+colleague\s+(?:will|can)"
    r"|they'?ll\s+(?:confirm|sort|take|pick)"
    r"|someone\s+else\s+will"
    r")\b",
    re.IGNORECASE,
)


def _last_user_text(state: ConversationState) -> str:
    """The most recent thing the caller said, or "" if they have said nothing."""
    for message in reversed(list(state.get("messages", []))):
        if message.role == "user" and message.content:
            return message.content
    return ""


def _last_speaker(state: ConversationState, members: list[str]) -> str | None:
    """Which specialist last spoke to the caller.

    Two fields are wrong for this and both looked right:

    * ``current_agent`` is the SUPERVISOR by the time it runs again — it sets
      that field to itself on every hop, so it never names a specialist.
    * ``agent_history`` is per-turn: ``new_state`` resets it, so on the first hop
      of a new turn it is empty and cannot say who was handling the conversation
      when the caller last replied.

    So the speaking agent is recorded into ``collected_data``, which
    ``ConversationService`` persists across turns. Checked in that order, with
    the in-turn history as a fallback for a multi-hop turn.
    """
    collected = dict(state.get("collected_data") or {})
    if (speaker := collected.get("_last_speaker")) and speaker in members:
        return str(speaker)
    return next(
        (
            key
            for key in reversed(list(state.get("agent_history", [])))
            if key in members
        ),
        None,
    )


def _named_destination(
    state: ConversationState, members: list[str]
) -> str | None:
    """Which specialist did the last reply say it was handing over to?

    Matched against the manifest's own agent keys and display names, so nothing
    here knows what a scheduler or a triage nurse is — a restaurant's
    ``reservations`` agent is recognised by exactly the same code.

    Returns ``None`` unless the reply BOTH announces a handoff and names a
    member. "I'll pass you to the scheduler" resolves; "let me hand you over"
    does not, and falls through to the router. Requiring both halves is what
    stops a passing mention of an agent's name ("the scheduler has the diary")
    being read as a routing instruction.
    """
    last = next(
        (
            m.content
            for m in reversed(list(state.get("messages", [])))
            if m.role == "assistant" and m.content
        ),
        None,
    )
    if not last:
        return None

    probe = _normalise(last).lower()
    if not _HANDOFF_ANNOUNCEMENT.search(probe):
        return None

    # Search from the END: "I'll pass you to triage, who will pass you to the
    # scheduler" names two, and the last one is the destination.
    # Search from the END, and match both the raw key and its spaced form:
    # a model writes "the scheduler", not "scheduler_agent".
    best: tuple[int, str] | None = None
    for key in members:
        for name in {key, key.replace("_", " ")}:
            position = probe.rfind(name.lower())
            if position >= 0 and (best is None or position > best[0]):
                best = (position, key)
    return best[1] if best else None


def _announced_handoff(state: ConversationState) -> bool:
    """Did the specialist's most recent reply say it was handing over?

    Reads the last assistant message only. An announcement two turns ago has
    already been acted on; what matters is whether the turn that just finished
    ended with "someone else will take this".
    """
    for message in reversed(list(state.get("messages", []))):
        if message.role == "assistant" and message.content:
            return bool(_HANDOFF_ANNOUNCEMENT.search(_normalise(message.content)))
    return False


def _recent_exchange(messages: list[Message]) -> str:
    """The last few turns, rendered for the routing decision.

    Only ``user`` and ``assistant`` lines: tool messages are internal and would
    teach the router about the platform's plumbing rather than the caller's
    problem. Labelled by speaker because "yes" means nothing without the
    question it answers — which is precisely the failure this exists to fix.
    """
    lines: list[str] = []
    for message in messages[-_ROUTING_CONTEXT_TURNS:]:
        if message.role not in ("user", "assistant") or not message.content:
            continue
        speaker = "Caller" if message.role == "user" else "Desk"
        lines.append(f"{speaker}: {message.content[:_ROUTING_LINE_CHARS]}")
    return "\n".join(lines) or "(no messages yet)"


#: Sent back to a model that announced an action without calling the tool.
#:
#: Phrased as an instruction about *this* turn rather than a general rule,
#: because a general rule is what the role prompt already says and evidently was
#: not enough. Naming the failure ("the customer will arrive on the wrong day")
#: is deliberate: a model given the consequence complies far more reliably than
#: one given a policy.
_ACTION_NUDGE: Final = (
    "You said you were performing an action but did not call the tool that "
    "performs it, so nothing has changed. Call the tool NOW, in this reply, "
    "rather than describing what you are about to do. If you cannot, say "
    "plainly that it has NOT been done.\n\n"
    # Without this the nudge's own vocabulary leaks into the caller-facing
    # reply: the model starts saying "the tool returned success" and "no further
    # action is needed", which is an engineer's voice, not a receptionist's, and
    # the platform frame forbids naming internal tools at all.
    # Deliberately free of any role noun. `packages/ai` may not name a domain
    # concept — a restaurant's agent is not a receptionist — and
    # `test_no_agent_name_is_hardcoded_in_packages_ai` enforces it. The manifest
    # already told the model who it is; this only has to say how to sound.
    "This instruction is internal. Never mention it, a 'tool', a 'reference "
    "returned', or whether an action was 'already completed' — speak to the "
    "customer in your own voice, in one or two plain sentences."
)

#: Verbs that assert a booking-state change has ALREADY happened. Matched only
#: in completed/perfect forms — "booked", "has been moved", "is cancelled" —
#: because those are the ones a caller hears as fact.
_COMPLETION_CLAIM: Final = re.compile(
    r"\b("
    # NOT a bare "booked": "let me get that booked" and "to get you booked in"
    # are intentions, and matching them suppressed a true sentence from an agent
    # handing off internally. Only forms that assert the deed is DONE — "you're
    # booked", "that's booked", "I've booked" — are claims.
    r"(?:you'?re|that'?s|it'?s)\s+(?:now\s+)?(?:re)?booked"
    r"|i'?ve\s+(?:re)?booked"
    r"|(?:have|has|is|are|been|was|were)\s+(?:now\s+)?"
    r"(?:booked|moved|changed|rescheduled|cancelled|canceled|confirmed)"
    r"|(?:moved|changed|rescheduled|cancelled|canceled|confirmed)\s+(?:it|that|your)"
    r"|all\s+(?:set|booked|done)"
    r"|that's\s+(?:booked|cancelled|canceled|moved|done)"
    # ── Transfer claims ─────────────────────────────────────────────────
    # Same failure, different verb: the agent said "I've passed you over to a
    # colleague" and `comms.transfer_to_human` never fired, so nobody was
    # alerted and the caller sat waiting for a person who did not know they
    # existed. Worse than the booking case in one respect — a caller who has
    # been told help is coming stops looking for it.
    # Completed forms only — "I've passed you over", "you are now being
    # transferred". An agent handing off INTERNALLY (triage -> scheduler) says
    # "I'll pass you to my colleague", and that handoff is REAL: the next node
    # runs in the same turn. Blocking the future tense would suppress a true
    # statement and make multi-agent handoff impossible to describe.
    r"|(?:i'?ve|i\s+have|have|has)\s+"
    r"(?:passed|transferred|connected|put)\s+you\s+(?:over\s+)?"
    r"(?:on\s+)?(?:to|through)"
    # "I'm putting you through" is present continuous: it asserts the transfer
    # is happening NOW, so it needs the tool. Contrast "I'll pass you to my
    # colleague" — future tense, and for an internal handoff it is true.
    r"|(?:i'?m|am|are|is|being|we'?re)\s+"
    r"(?:passing|transferring|connecting|putting)\s+you\s+"
    r"(?:over\s+)?(?:on\s+)?(?:to|through)"
    # NOTE the deliberate absence of "I'll pass you to my colleague" in the
    # FUTURE tense. An agent handing off INTERNALLY — triage to scheduler — says
    # exactly that, and the handoff is real: the next node runs in the same
    # turn. Only the completed forms ("I've passed you over", "you're being
    # transferred") assert that a person is now involved, which is the claim
    # that needs `comms.transfer_to_human` behind it.
    # Passive future: "this will be booked", "it will be moved". A claim about
    # what is going to happen, made by an agent that is not doing it.
    r"|(?:this|it|that)\s+will\s+be\s+"
    r"(?:booked|moved|changed|rescheduled|cancelled|canceled)"
    r"|(?:a\s+)?colleague\s+will\s+be\s+with\s+you"
    r"|someone\s+will\s+(?:be\s+with|call|contact|ring)\s+you"
    r"|they'?ll\s+be\s+with\s+you"
    r")\b",
    re.IGNORECASE,
)

#: Phrases in which a model announces intent instead of acting. The presence of
#: one in a toolless reply is what triggers the single nudge.
_INTENT_PROMISE: Final = re.compile(
    r"\b("
    r"one\s+moment|just\s+a\s+moment|bear\s+with\s+me|hold\s+on"
    r"|(?:i'?ll|i\s+will|let\s+me)\s+"
    r"(?:go\s+ahead\s+and\s+)?"
    r"(?:move|change|reschedule|cancel|book|confirm|update|sort|do)"
    r"|while\s+i\s+confirm|confirming\s+that\s+now|processing\s+that"
    r")\b",
    re.IGNORECASE,
)

#: **Any** assertion that a person will do something. Inverted from an
#: allow-list of phrasings, because an allow-list is always one phrasing behind:
#: "passed you over" was covered, then "a colleague will be with you", then "take
#: a message and have a colleague follow that up" slipped through. Each addition
#: was a real caller being told help was coming when nobody had been told.
#:
#: The rule is now the general one: naming a human role ("colleague", "the
#: team", "a member of staff", "someone", "they") next to a promise of action
#: requires `comms.transfer_to_human` to have run. Broad on purpose — a false
#: positive costs one bland sentence, a false negative costs a caller who stops
#: looking for help.
_HUMAN_ACTOR: Final = (
    r"(?:a\s+)?colleague|the\s+team|a\s+(?:team\s+)?member|"
    r"(?:a\s+)?member\s+of\s+(?:the\s+)?(?:team|staff)|someone|somebody|"
    r"one\s+of\s+(?:the\s+|our\s+)?(?:team|staff|colleagues)|they|he|she"
)

#: An INTERNAL handoff between agents, which is real and needs no tool: the next
#: node runs in the same turn. Checked first, and it wins.
#:
#: This is the one genuine tension in the inverted rule. "I'll pass you to my
#: colleague to get that booked" is triage handing to the scheduler — true, and
#: the caller is about to be helped by the same system. "I've passed you over to
#: a colleague, they'll be with you shortly" is a claim that a PERSON now has it,
#: which needs `comms.transfer_to_human`.
#:
#: The distinguishing feature is tense plus commitment: an intention to hand over
#: ("I'll pass you", "let me pass you") describes what is about to happen inside
#: the conversation; a completed or scheduled human action ("I've passed you",
#: "they'll be with you shortly") asserts a person is now involved.
_INTERNAL_HANDOFF: Final = re.compile(
    r"\b(?:i'?ll|i\s+will|let\s+me|i'?m\s+going\s+to)\s+"
    r"(?:just\s+)?(?:pass|hand|transfer|put)\s+(?:you|your|this|that|it)\b",
    re.IGNORECASE,
)

_HUMAN_WILL_ACT: Final = re.compile(
    # `they'll` / `she'll` contract the actor and the auxiliary into one token,
    # so the apostrophe form has to be part of the actor alternation rather than
    # only of the auxiliary — otherwise the commonest phrasing of all slips
    # through, which it did.
    rf"\b(?:{_HUMAN_ACTOR})(?:'?ll|'?re)?\s+"
    rf"(?:will|can|is\s+going\s+to|are\s+going\s+to)?\s*"
    rf"\w*\s*"
    rf"(?:call|ring|contact|follow|be\s+with|get\s+back|pick|take|sort|"
    rf"confirm|help|assist|look)"
    rf"|\b(?:pass|passed|passing|transfer|transferred|transferring|connect|"
    rf"connected|connecting|put|putting)\s+"
    # The object is not always the caller. "I've passed A MESSAGE to the team
    # to cancel your appointment" is the same promise — a human will act — and
    # it slipped through a pattern that only looked for "passed you". Observed
    # in place of an actual `cancel_booking` call, so the caller believed their
    # appointment was being cancelled by a person and the row stayed
    # `confirmed`.
    rf"(?:you|your|this|that|it|a\s+message|the\s+details|your\s+details|"
    rf"a\s+note|the\s+request)\b"
    rf"|\bhave\s+(?:{_HUMAN_ACTOR})\s+\w+"
    rf"|\btake\s+a\s+message\b",
    re.IGNORECASE,
)

#: Which tool has to have succeeded for a given claim to be true. Keyed by the
#: verb family the claim uses, because that is what a caller actually keys on.
_CLAIM_REQUIRES: Final[tuple[tuple[re.Pattern[str], frozenset[str]], ...]] = (
    (
        re.compile(r"cancel", re.IGNORECASE),
        frozenset({"booking.cancel_booking"}),
    ),
    (
        re.compile(r"mov(?:e|ed)|reschedul|chang", re.IGNORECASE),
        frozenset({"booking.reschedule_booking"}),
    ),
    (
        re.compile(r"book|all set|confirm", re.IGNORECASE),
        frozenset({"booking.book_appointment", "booking.reschedule_booking"}),
    ),
    (
        _HUMAN_WILL_ACT,
        frozenset({"comms.transfer_to_human"}),
    ),
)

#: What replaces a sentence whose claim could not be verified. Honest, and
#: actionable: it tells the caller the state of the world and what happens next,
#: rather than going silent — which would read as the line dropping.
#: The replacement must not itself promise anything a tool has not done. An
#: earlier version said "let me put you through to a colleague", which invented
#: the very handoff the guard exists to prevent — the guard's own fallback
#: tripping the guard's own rule. It now states the fact and asks a question,
#: which commits to nothing.
_UNVERIFIED_REPLACEMENT: Final = (
    "I'm sorry — I wasn't able to complete that just now, so nothing has "
    "changed. Shall I try again?"
)


#: An internal slot handle, as minted by the booking tools. Matched here rather
#: than imported so `packages/ai/nodes.py` does not depend on a tool module —
#: the pattern is a shape, not a vertical concept.
#:
#: **Why a filter and not an instruction.** The first version of the handle was a
#: 200-character signed blob carrying a UTC instant and two resource UUIDs, and
#: the prompt asked the model not to say it. It was said — read to a caller as
#: "your reference". The handle is now short and opaque, which removes the
#: temptation, and this removes the possibility. An instruction is a request that
#: fails eventually; a filter does not.
_SLOT_HANDLE: Final = re.compile(r"\[?\bslot_[0-9a-f]{4,32}\b\]?", re.IGNORECASE)


def _strip_slot_handles(text: str) -> tuple[str, bool]:
    """Remove internal slot handles from anything a caller will hear."""
    scrubbed, count = _SLOT_HANDLE.subn("", text)
    if not count:
        return text, False
    # Collapse the double space a removal leaves mid-sentence, so the reply does
    # not visibly show where something was taken out.
    return re.sub(r"\s{2,}", " ", scrubbed).strip(), True


#: Unicode punctuation a model emits interchangeably with its ASCII twin, and
#: which every pattern in this module would otherwise silently fail to match.
#:
#: Found the hard way: a legitimate handoff written with U+2019 ("I<U+2019>ll
#: pass you to the scheduler") did not match `i'?ll`, so a TRUE sentence was
#: suppressed mid-triage and the caller got an apology instead of being routed.
#: The rest of this table is the same bug waiting to happen — a model that emits
#: one curly quote emits its partner too.
#:
#: Normalised once, in `_normalise`, and every pattern in this module matches
#: against the normalised copy. The text the CALLER hears is never changed.
_PUNCTUATION_TWINS: Final = str.maketrans(
    {
        "\u2018": "'",  # left single quote
        "\u2019": "'",  # right single quote / apostrophe
        "\u02bc": "'",  # modifier letter apostrophe
        "\u201c": '"',  # left double quote
        "\u201d": '"',  # right double quote
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2011": "-",  # non-breaking hyphen
    }
)


def _normalise(text: str) -> str:
    """A copy of ``text`` with unicode punctuation folded to ASCII.

    For MATCHING only. Every regex in this module is written with ASCII
    apostrophes and dashes; a model emits both forms freely, and a pattern that
    only knows one is a pattern with a hole. Folding at a single seam means a new
    pattern gets the coverage for free rather than having to remember.
    """
    return text.translate(_PUNCTUATION_TWINS)


#: A sentence DENYING that something happened. Checked before the claim
#: patterns, because "nothing has changed" contains "has changed" and an honest
#: denial must never be mistaken for the assertion it denies.
_NEGATED_CLAIM: Final = re.compile(
    r"\bnothing\s+(?:has|have)\s+(?:been\s+)?\w+"
    r"|\b(?:has|have|is|are|was|were)\s+not\s+been\b"
    r"|\b(?:hasn'?t|haven'?t|isn'?t|aren'?t|wasn'?t|weren'?t|didn'?t|"
    r"couldn'?t|wan'?t|won'?t)\b"
    r"|\bnot\s+(?:been\s+)?(?:booked|moved|changed|cancelled|canceled|"
    r"rescheduled|confirmed)\b"
    r"|\bunable\s+to\b",
    re.IGNORECASE,
)


#: A message carrying no request: a greeting, thanks, or a goodbye.
#:
#: Deliberately a WHITELIST of whole utterances, not a keyword search. "hi" is
#: contentless; "hi, I chipped a tooth" is not, and a pattern that matched the
#: first word of the second would hand a dental emergency to a node that cannot
#: book. The bound on length is the second guard: anything with real content is
#: longer than this.
_CONTENTLESS: Final = re.compile(
    r"^(?:"
    r"(?:hi|hey|hello|yo|hiya|namaste|good\s+(?:morning|afternoon|evening|day))"
    r"|(?:thanks?|thank\s+you|cheers|ta|much\s+appreciated)"
    r"|(?:bye|goodbye|see\s+you|that'?s\s+all|nothing\s+else|all\s+good)"
    r")[\s!.,]*$",
    re.IGNORECASE,
)

#: Longest utterance the greeting path will consider, in characters. A belt to
#: the whitelist's braces: even if a pattern above ever grows too permissive, a
#: real request does not fit in this.
_CONTENTLESS_MAX_CHARS: Final = 24


def _is_contentless(text: str) -> bool:
    """Does this message ask for nothing at all?

    Used ONLY to let a supervisor answer without dispatching. Routing is tried
    first and wins whenever the message carries a request; this is the fallback
    for the case where there is genuinely nothing to route.
    """
    stripped = (text or "").strip()
    if not stripped or len(stripped) > _CONTENTLESS_MAX_CHARS:
        return False
    return bool(_CONTENTLESS.match(stripped))


def _is_greeting_turn(state: ConversationState) -> bool:
    """Contentless AND with nothing pending — safe for the supervisor to answer.

    The second half is what the whitelist alone cannot see. "okay" opening a
    conversation is contentless; "okay" after "let me hand you over now" is the
    caller ACCEPTING something, and answering it here would strand them — the
    handoff they just agreed to never happens.

    So: only when the caller has spoken first, or when the last thing the desk
    said was itself a greeting. Anything following a real assistant turn goes
    to the router, which has the context to know what "okay" means.
    """
    messages = list(state.get("messages", []))
    if not _is_contentless(_last_user_text(state)):
        return False
    for message in reversed(messages[:-1] if messages else []):
        if message.role == "assistant" and message.content:
            # A prior desk turn exists: this is a reply to something, not an
            # opening. Let the router decide.
            return False
        if message.role == "user":
            break
    return True


#: Characters per emitted delta when replaying held text. ~24 keeps the
#: perceived typing rhythm close to a real stream; the guard buffers by
#: sentence regardless, so this affects only how it looks.
_REPLAY_CHUNK_CHARS: Final = 24


#: A sentence that asks the caller for something.
#:
#: Deliberately narrow: a question mark, or an imperative request of the kind a
#: receptionist uses instead of one ("could you let me know...", "tell me...").
#: Rhetorical softeners a reply legitimately contains — "no problem?", "right?"
#: — carry no request and would be false positives, so the pattern requires
#: either an interrogative opener or a request verb.
_QUESTION_SHAPE: Final = re.compile(
    r"(?:^|[.!?]\s+)\s*(?:"
    r"who|what|when|where|which|why|how|can|could|would|will|do|does|did|are|is|"
    r"have|has|shall|may|please\s+(?:let|tell|give|confirm)|"
    r"let\s+me\s+know|tell\s+me|give\s+me|confirm"
    r")\b",
    re.IGNORECASE,
)


#: Two requests joined into one sentence: "your name and a contact number",
#: "your date of birth and your address". One question mark, two things to
#: answer — which is the shape the question-count guard cannot see, because
#: nothing about it is a second question.
_COMPOUND_ASK: Final = re.compile(
    r"\b(?:name|number|address|email|date\s+of\s+birth|dob|postcode|pin\s?code)\b"
    r"[^.?!]{0,40}?\b(?:and|plus|along\s+with|as\s+well\s+as|&)\b[^.?!]{0,40}?"
    r"\b(?:name|number|address|email|date\s+of\s+birth|dob|postcode|pin\s?code)\b",
    re.IGNORECASE,
)


def _is_compound_ask(sentence: str) -> bool:
    """Does one sentence request two separate details?

    The question-count guard counts question MARKS, so it passes "Could I take
    your name and a contact number?" — one question, two answers. A caller gives
    one, the model treats the turn as incomplete, and the exchange stalls in the
    same way two separate questions do. The manifest now asks for the name
    alone; this is the mechanism that holds when the model reverts to asking for
    both, which a prompt rule alone has repeatedly failed to prevent here.
    """
    return bool(_COMPOUND_ASK.search(sentence))


def _trim_compound_ask(sentence: str) -> str:
    """Reduce "your name and a contact number?" to "your name?".

    Cuts at the conjunction and restores the terminator. Returns the sentence
    unchanged when the result would be too short to be a sensible request —
    better a compound ask than a truncated fragment.
    """
    match = _COMPOUND_ASK.search(sentence)
    if not match:
        return sentence
    conj = re.search(
        r"\b(?:and|plus|along\s+with|as\s+well\s+as|&)\b",
        sentence[match.start() : match.end()],
        re.IGNORECASE,
    )
    if not conj:
        return sentence
    cut = match.start() + conj.start()
    head = sentence[:cut].rstrip().rstrip(",")
    if len(head) < 12:
        return sentence
    tail = "?" if "?" in sentence else "."
    return f"{head}{tail} "


def _is_question(sentence: str) -> bool:
    """Does this sentence ask the caller for something?

    **Why a guard rather than a prompt rule.** "Ask one question at a time" is
    already the kind of instruction this codebase has watched fail repeatedly —
    a model trying to be efficient bundles two requests into one breath, and no
    amount of emphasis in the system prompt reliably stops it. Observed:

        "Are you a new patient, or have you been seen here before?
         And roughly what's the appointment for?"

    A caller answers one of those, the model treats the turn as incomplete, and
    the conversation stalls re-asking the pair. The failure is not that two
    questions are impolite; it is that a two-question turn has no valid answer.

    So the second request is DROPPED rather than requested-against. The model is
    free to write whatever it likes; only one ask reaches the caller, and the
    other is asked next turn once this one is answered.
    """
    return "?" in sentence or bool(_QUESTION_SHAPE.search(sentence))


class _UnverifiedClaimGuard:
    """Stops the agent telling a caller about work that did not happen.

    **The failure.** The model says "your check-up is now Friday at 8:15" having
    never called ``reschedule_booking``. The appointment is still on Wednesday.
    Nothing reports a failure — not the logs, not the model, not the transcript —
    so the caller simply arrives on the wrong day. That is materially worse than
    an error, because an error is at least visible to someone.

    **Why a guard rather than only a prompt rule.** A prompt rule is a request; a
    model under pressure to sound helpful will break it, and this one already
    did. The nudge in the tool loop is the first line of defence and handles the
    ordinary case. This is the second, and it is the one that holds when the
    model simply refuses — a claim is only allowed through if the write that
    would have made it true actually succeeded.

    **Sentence granularity, and why buffering is unavoidable.** Tokens stream. A
    claim cannot be un-said once a token carrying it has been emitted, so the
    guard holds each sentence until it can see all of it. That costs a sentence
    of latency on the reply, which is the price of never lying to a caller.
    Sentences that make no claim pass straight through.

    **Why a suppressed claim discards the REST of the reply.** Swapping one
    sentence leaves what follows standing, and what follows was written on the
    assumption that the claim was true. A caller saw:

        "I'll book that now. I'm sorry — I wasn't able to complete that just
         now, so nothing has changed. ... Could I grab your full name?"

    Three sentences contradicting each other, because only the middle one was
    replaced. A reply caught lying cannot be repaired a clause at a time.

    But it also cannot be held in its entirety, because then no reply streams —
    and the whole conversation would arrive as a blob after a pause. So the guard
    **releases clean sentences as they complete** and stops dead at the first
    unbacked claim, replacing it and everything after it with one honest
    sentence. A caller sees a normal streaming reply that ends in "I'm sorry, I
    wasn't able to complete that" rather than a paragraph arguing with itself.

    The residue is that a sentence already spoken cannot be recalled — "I'll book
    that now" may have been heard before the claim two sentences later is caught.
    That is unavoidable in a streaming medium, and it is why the nudge in the
    tool loop exists upstream: this is the last line, not the first.
    """

    __slots__ = (
        "_asked_question",
        "_buffer",
        "_failure",
        "_held",
        "_previously",
        "_succeeded",
        "_tainted",
        "dropped_questions",
        "leaked_handle",
        "rewrote",
        "trimmed_asks",
    )

    #: A sentence ends at one of these followed by whitespace or end-of-text.
    _BOUNDARY = re.compile(r"(?<=[.!?])(\s+|$)")

    def __init__(
        self,
        succeeded: set[str],
        previously: set[str] | None = None,
        failure: str | None = None,
    ) -> None:
        #: What the failing tool said, in words a caller can act on.
        #:
        #: **A guard that turns a specific error into a generic one destroys the
        #: information the caller needs to recover.** A caller who typed a
        #: ten-digit phone number was told "I wasn't able to complete that just
        #: now — shall I try again?", which invites them to retype the identical
        #: number and fail identically. The one fact that would have helped —
        #: your number looks short — was the one fact discarded.
        #:
        #: The tool already produced that sentence (`ToolBusinessError.
        #: model_message`); it simply never reached the replacement.
        self._failure = failure
        self._succeeded = succeeded
        #: Tools that succeeded in an EARLIER turn of this conversation. A claim
        #: they back is true — just not new — so it is restated rather than
        #: denied. Without this, a caller confirming an already-made booking was
        #: told "I wasn't able to complete that", immediately followed by the
        #: reference and arrival instructions: contradictory, and it invented a
        #: transfer that never happened.
        self._previously = previously or set()
        self._buffer = ""
        #: Vetted sentences not yet released. Held rather than emitted, because
        #: a later sentence may prove the reply unsalvageable and an emitted
        #: token cannot be recalled.
        self._held: list[str] = []
        #: Set once a claim is suppressed. From that point the whole reply is
        #: discarded — see the class docstring on why a clause-level repair
        #: produces a self-contradictory paragraph.
        self._tainted = False
        # One question per reply. See `_is_question` for why this is a guard
        # and not a line in the prompt.
        self._asked_question = False
        #: Counted so a reply that keeps piling on questions is visible in the
        #: logs rather than silently trimmed every turn.
        self.dropped_questions = 0
        #: Compound asks reduced to their first request.
        self.trimmed_asks = 0
        #: The claims that had to be rewritten, for the log.
        self.rewrote: set[str] = set()
        #: True if an internal slot handle had to be scrubbed from the reply. A
        #: model reaching for one is worth knowing about even though the caller
        #: never sees it.
        self.leaked_handle = False

    @property
    def tainted(self) -> bool:
        """True once a claim has been suppressed and the reply discarded."""
        return self._tainted

    def feed(self, chunk: str) -> str:
        """Absorb a delta; return the sentences that are safe to emit now.

        A sentence is released as soon as it is complete and clean, so ordinary
        replies stream normally. The first unbacked claim stops the reply dead —
        see the class docstring.
        """
        if self._tainted:
            return ""
        self._buffer += chunk
        out: list[str] = []
        while match := self._BOUNDARY.search(self._buffer):
            end = match.end()
            sentence, self._buffer = self._buffer[:end], self._buffer[end:]
            if not self._vet(sentence):
                # Tainted. Everything after this point is discarded; the
                # replacement is emitted by `flush`.
                self._buffer = ""
                return "".join(out)
            vetted = self._held.pop()
            # A compound ask is TRIMMED, not dropped: "your name and a contact
            # number?" still needs to ask for the name, so removing the whole
            # sentence would leave the reply asking for nothing at all.
            if _is_compound_ask(vetted):
                trimmed = _trim_compound_ask(vetted)
                if trimmed != vetted:
                    self.trimmed_asks += 1
                    vetted = trimmed
            if _is_question(vetted):
                if self._asked_question:
                    # A second ask in one reply. Dropped, not rewritten: the
                    # caller gets the first question, answers it, and the model
                    # asks the next one on the following turn — which is the
                    # shape a receptionist uses and the only shape a caller can
                    # actually answer.
                    self.dropped_questions += 1
                    continue
                self._asked_question = True
            out.append(vetted)
        return "".join(out)

    def flush(self) -> str:
        """Release the trailing fragment, or the sentence that replaces it."""
        if self._tainted:
            # Leading space: an already-emitted sentence ends without one, and
            # "now.I'm sorry" is a visible seam in the caller's chat window.
            #
            # NOT subject to the one-question rule. The replacement ends "Shall
            # I try again?", so a reply that had already asked something would
            # have this suppressed as a second question — and the caller would
            # get silence where an honest correction belongs. The guard's own
            # output is never the thing to trim.
            return " " + self._replacement()
        remaining, self._buffer = self._buffer, ""
        if not remaining:
            return ""
        if not self._vet(remaining):
            return " " + self._replacement()
        trailing = self._held.pop() if self._held else ""
        # The final fragment is often exactly where a second question lands —
        # "...and what's it for?" tacked onto an otherwise complete reply.
        if trailing and _is_question(trailing) and self._asked_question:
            self.dropped_questions += 1
            return ""
        return trailing

    def _replacement(self) -> str:
        """What to say instead of the suppressed claim.

        The failing tool's own explanation when there is one, because it names
        the obstacle and the generic apology does not. Falls back to the generic
        sentence when a claim was fabricated with no tool failure behind it —
        there is genuinely nothing to explain in that case.
        """
        if self._failure:
            return self._failure
        return _UNVERIFIED_REPLACEMENT

    def _vet(self, sentence: str) -> bool:
        """Hold a sentence, or taint the whole reply. False once tainted.

        Only *completion* claims are checked. "I can move that for you" and
        "shall I book it?" are offers and questions, not assertions about the
        world, and blocking them would make the agent unable to converse.
        """
        # Matched against a normalised copy — see `_normalise`. The sentence
        # the caller hears is unchanged.
        probe = _normalise(sentence)
        # A NEGATED claim is not a claim. "nothing has changed", "that has not
        # been booked", "I wasn't able to move it" are the honest sentences this
        # guard exists to produce — and its own replacement text contains one,
        # so without this the guard flags its own output. Same false-positive
        # class as "let me get that booked", which is an intention rather than
        # an assertion.
        if _NEGATED_CLAIM.search(probe):
            self._held.append(sentence)
            return True

        # Scrub internal handles from EVERY sentence, claim or not. This runs
        # before any other check because a handle is never speakable regardless
        # of what else the sentence says.
        sentence, scrubbed = _strip_slot_handles(sentence)
        if scrubbed:
            self.leaked_handle = True

        # An internal agent handoff is genuine and needs no tool — the next node
        # runs in the same turn. Checked first so the broad human-action rule
        # below does not suppress a true sentence.
        if _INTERNAL_HANDOFF.search(probe) and not _COMPLETION_CLAIM.search(probe):
            self._held.append(sentence)
            return True

        # Either a completed booking claim, or any promise that a HUMAN will
        # act. The second is checked here rather than folded into
        # `_COMPLETION_CLAIM` because it is a different kind of assertion — not
        # "this is done" but "a person will do this" — and conflating them made
        # the pattern unreadable.
        if not (
            _COMPLETION_CLAIM.search(probe) or _HUMAN_WILL_ACT.search(probe)
        ):
            self._held.append(sentence)
            return True

        for pattern, required in _CLAIM_REQUIRES:
            if not pattern.search(probe):
                continue

            if required & self._succeeded:
                return self._hold(sentence)

            if required & self._previously:
                # TRUE, just not new — let it through unchanged.
                #
                # Replacing it with a generic "that is already done" strips the
                # detail that makes a closing message useful: the time, the
                # clinician, the reference. A caller confirming their
                # appointment wants exactly that read back, and the claim is
                # backed by a real write in this conversation. That it happened
                # a turn ago does not make it false.
                #
                # This is why the two sets are separate rather than unioned:
                # the question is "did this happen", and here the answer is yes.
                return self._hold(sentence)

            # Unbacked. The reply is now unsalvageable — see the class
            # docstring on why a clause-level repair contradicts itself.
            self.rewrote.add(sentence.strip()[:120])
            self._tainted = True
            return False

        return self._hold(sentence)

    def _hold(self, sentence: str) -> bool:
        self._held.append(sentence)
        return True


def _promises_action(
    content: str | None, *, backed: set[str] | frozenset[str] = frozenset()
) -> bool:
    """Did the model announce an action instead of performing one?

    Both halves are required: a bare "I'll check that for you" is a fine thing to
    say *alongside* a tool call, and this is only consulted when no call was
    made. A claim of completion counts too — a model asserting "that's moved"
    with no tool call is the same failure at a later stage.

    ``backed`` is the set of tools that have already succeeded (this turn, or
    earlier in the conversation). A completion claim those tools make true is a
    *report*, not a promise: "that's booked for you" in the round after
    ``book_appointment`` returned ok is the model doing its job. Without this,
    every booking confirmation was nudged as if it were an empty promise — an
    extra provider round trip, and then a regenerated reply on top, to say the
    sentence it already had. The mapping from claim to required tool is the
    guard's own (``_CLAIM_REQUIRES``), so the two cannot disagree about what
    backs what; a claim the table does not know stays a promise.
    """
    if not content:
        return False
    probe = _normalise(content)
    if _INTENT_PROMISE.search(probe):
        return True
    if not _COMPLETION_CLAIM.search(probe):
        return False
    required_sets = [req for pattern, req in _CLAIM_REQUIRES if pattern.search(probe)]
    if required_sets and all(req & backed for req in required_sets):
        return False
    return True


def _sum_usage(left: Usage | None, right: Usage | None) -> Usage | None:
    """Add two usage reports, tolerating either being absent.

    A turn now makes several model calls — a tool round, then the streamed
    answer — and billing must see all of them. ``None`` means "the provider told
    us nothing", which is different from zero and must not become zero by being
    added.
    """
    if left is None:
        return right
    if right is None:
        return left
    return Usage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        cached_prompt_tokens=left.cached_prompt_tokens + right.cached_prompt_tokens,
        reasoning_tokens=left.reasoning_tokens + right.reasoning_tokens,
    )


def _uuid(value: Any) -> UUID:
    """Coerce a state value to a UUID.

    State is a ``TypedDict``, so a checkpoint round-trip can bring an id back as
    a string. Coercing here rather than trusting the annotation means a resumed
    conversation reaches a tool with a real UUID rather than a string that fails
    at the first ``str(uuid)`` comparison in a repository.
    """
    return value if isinstance(value, UUID) else UUID(str(value))


def _usage_delta(usage: Usage | None) -> dict[str, int]:
    """A state delta carrying one call's token usage.

    Returns an empty dict when the provider reported nothing, so the summing
    reducers are not fed zeros that would mask a missing report as a genuine
    zero — "we do not know" and "it cost nothing" are different facts, and only
    the first should leave the column null.
    """
    if usage is None:
        return {}
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "cached_tokens": usage.cached_prompt_tokens,
    }

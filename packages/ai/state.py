"""``ConversationState`` — the graph's state schema, 03 §3.

A ``TypedDict`` rather than a dataclass because that is what LangGraph's
``StateGraph`` reduces over: each node returns a partial dict and the framework
merges it, applying a per-key reducer where one is annotated.

**Why ``messages`` is annotated with a reducer and nothing else is.** Messages
accumulate — every node appends its output and none of them should clobber the
history. Everything else is last-writer-wins, which is correct because only one
node runs at a time in this topology and a later value is a deliberate update
(``current_agent`` changing *is* the handoff). Annotating every key with an
append reducer would make ``step_count`` a list of integers, which is the kind
of bug that looks like a working system until a budget check silently never
fires.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict
from uuid import UUID

from packages.core.ports.llm import Message

__all__ = ["STATE_SCHEMA_VERSION", "ConversationState", "new_state"]

#: Bumped when the shape below changes incompatibly. A checkpoint carrying an
#: older version is refused rather than coerced (03 §5): replaying a v1 state
#: into a v2 graph whose agent set changed routes to a node that no longer
#: exists, and the failure would surface mid-call.
STATE_SCHEMA_VERSION = 1


def _merge_messages(
    left: list[Message], right: list[Message]
) -> list[Message]:
    """Append-only message accumulation.

    ``operator.add`` would do, and is used for the counters. This is spelled
    out because the *semantics* differ: a node returning ``{"messages": [x]}``
    means "add x to the conversation", never "the conversation is now [x]".
    Making that explicit is what stops someone "simplifying" it to a plain
    assignment, which would silently truncate history to the last turn.
    """
    return [*left, *right]


class ConversationState(TypedDict, total=False):
    """What flows between graph nodes — 03 §3."""

    # ── Identity, pinned for the life of the run ──────────────────────────
    schema_version: int
    tenant_id: UUID
    conversation_id: UUID
    manifest_version: str
    channel: str
    locale: str
    #: The business's IANA timezone. Carried in state rather than closed over by
    #: the node factory because the factory is held by a CACHED compiled graph:
    #: anything captured there is frozen at the moment that tenant's graph was
    #: first compiled. A timezone would rarely change, but the *date* rendered
    #: from it changes every day — so the value and the instant both travel
    #: per-turn, through the same channel.
    timezone: str

    # ── The conversation itself ───────────────────────────────────────────
    messages: Annotated[list[Message], _merge_messages]
    #: Facts the agents have gathered (name, date of birth, preferred window).
    #: Merged rather than replaced so a specialist adding one field does not
    #: drop what the supervisor collected.
    collected_data: dict[str, Any]
    running_summary: str

    # ── Routing ───────────────────────────────────────────────────────────
    #: The agent key whose node runs next. The router reads this and nothing
    #: else, which is what keeps routing testable without a graph.
    next_agent: str | None
    current_agent: str | None
    #: Every agent that has run, in order. The anti-ping-pong guard reads it.
    agent_history: Annotated[list[str], operator.add]
    handoff_reason: str | None

    # ── Budgets, clamped at compile time and injected at invocation ───────
    step_count: Annotated[int, operator.add]
    tool_call_count: Annotated[int, operator.add]
    max_steps: int
    max_tool_calls: int
    deadline_ms: int
    started_at_ms: int

    # ── Accounting ────────────────────────────────────────────────────────
    #: Tokens consumed across every model call in this turn, summed.
    #:
    #: Summed rather than last-writer-wins because a turn is routinely more
    #: than one call — the supervisor's routing hop plus the specialist's
    #: reply — and billing must see both. A plain assignment here would record
    #: only the last node's usage and silently under-bill every multi-agent
    #: turn, which is the kind of error that is invisible until someone
    #: reconciles a provider invoice.
    prompt_tokens: Annotated[int, operator.add]
    completion_tokens: Annotated[int, operator.add]
    cached_tokens: Annotated[int, operator.add]

    # ── Termination ───────────────────────────────────────────────────────
    outcome: Literal["resolved", "booked", "transferred", "abandoned"] | None
    finish_reason: str | None
    #: Set by the guard when a budget is exhausted. Distinguishes "the
    #: conversation ended" from "we stopped it", which the dashboard needs and
    #: a bare `outcome` cannot express.
    overflow_reason: str | None


def new_state(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    manifest_version: str,
    channel: str,
    locale: str,
    timezone: str = "UTC",
    user_message: Message,
    history: list[Message],
    max_steps: int,
    max_tool_calls: int,
    deadline_ms: int,
    started_at_ms: int,
    collected_data: dict[str, Any] | None = None,
) -> ConversationState:
    """Seed state for one turn.

    ``history`` is passed in rather than loaded here: this package performs no
    I/O (it would have to import ``persistence`` to do so, which contract §9
    permits but which would make the graph untestable without a database). The
    caller — ``apps/api`` — reads the turns and hands them over.
    """
    return ConversationState(
        schema_version=STATE_SCHEMA_VERSION,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        manifest_version=manifest_version,
        channel=channel,
        locale=locale,
        timezone=timezone,
        messages=[*history, user_message],
        collected_data=collected_data or {},
        running_summary="",
        next_agent=None,
        current_agent=None,
        agent_history=[],
        handoff_reason=None,
        step_count=0,
        tool_call_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        cached_tokens=0,
        max_steps=max_steps,
        max_tool_calls=max_tool_calls,
        deadline_ms=deadline_ms,
        started_at_ms=started_at_ms,
        outcome=None,
        finish_reason=None,
        overflow_reason=None,
    )

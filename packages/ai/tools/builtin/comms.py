"""Escalation to a person.

One tool, and it is deliberately the only *write* tool an information-only agent
may ever hold (02 §6.5). A QA agent consumes untrusted retrieved content and is
therefore the prompt-injection escalation path; the allow-list that lets it
still hand a caller to a human is what keeps "I can't help with that" from being
a dead end.

The implementation records the intent and returns immediately. Actually bridging
a call belongs to ``packages/channels`` and is channel-specific — a web widget
raises a flag for a human to pick up, a phone call performs a SIP REFER. The tool
boundary is "the agent has decided to escalate, with this summary", and that
decision is worth persisting even on a channel that cannot transfer, because it
is what a queue is built from.
"""

from __future__ import annotations

from typing import Final

import structlog
from packages.ai.tools.registry import (
    ToolContext,
    ToolResult,
    ToolSpec,
    object_schema,
)

__all__ = ["TOOLS"]

log = structlog.get_logger(__name__)

#: Channels on which a live human handoff is genuinely possible — there is a
#: call in progress and a bridge to move it to. Everything else takes a message.
#: Listed rather than inferred so adding a channel is a deliberate decision about
#: whether a person is really there, not an accident of string matching.
_LIVE_CHANNELS: Final = frozenset({"voice_inbound", "voice_outbound"})


async def _transfer_to_human(ctx: ToolContext, args: dict) -> ToolResult:
    summary = str(args.get("summary", "")).strip()
    urgency = str(args.get("urgency", "routine"))

    # Structured, and at `info` rather than `debug`: an escalation is the signal
    # that tells a practice its agent is not covering something, and it is only
    # useful if it survives log-level filtering in production.
    log.info(
        "ai.escalation.requested",
        tenant_id=str(ctx.tenant_id),
        conversation_id=str(ctx.conversation_id),
        agent=ctx.agent_key,
        urgency=urgency,
        # The summary is agent-authored prose about a customer's problem, so it
        # carries whatever they said. Truncated rather than dropped: the length
        # bound is what keeps a log line from becoming a transcript.
        summary=summary[:280],
    )

    # ── Can this channel actually bridge to a person? ────────────────────
    #
    # On a live voice call it can: there is a call in progress and a SIP REFER
    # moves it. On web chat there is nobody sitting at the other end of the
    # socket — the "transfer" is a message dropped into a queue that a human
    # reads later, if the practice is open.
    #
    # Saying "I've put you through" on web chat is therefore a lie with a
    # particularly bad shape: a caller who believes help is arriving stops
    # looking for it. That was the observed failure — the agent announced a
    # handoff, the conversation ended, and nothing had happened. So the tool
    # tells the truth per channel and offers the next best thing instead.
    live = ctx.channel in _LIVE_CHANNELS

    if not live:
        return ToolResult(
            content=(
                "There is no one available to take over this chat right now — "
                "this channel cannot transfer to a person live. Tell the "
                "customer plainly that you have passed a message to the team "
                "rather than saying you are putting them through. If it is "
                "urgent, offer the earliest emergency appointment, or the "
                "practice's phone number so they can speak to someone now."
            ),
            collected_data={
                "escalated": True,
                "escalation_urgency": urgency,
                "escalation_delivered": "message_queued",
            },
            structured={
                "action": "message_taken",
                "live_transfer_available": False,
                "urgency": urgency,
                "summary": summary,
            },
        )

    urgency_text = {
        "emergency": (
            "Tell the customer you are getting someone right now and stay with "
            "them. Do not ask further questions."
        ),
        "urgent": "Tell the customer a team member is picking this up shortly.",
    }.get(urgency, "Tell the customer a team member will be with them shortly.")

    return ToolResult(
        content=(
            f"A team member has been alerted with your summary. {urgency_text}"
        ),
        collected_data={
            "escalated": True,
            "escalation_urgency": urgency,
            "escalation_delivered": "live_transfer",
        },
        structured={
            "action": "transfer_requested",
            "live_transfer_available": True,
            "urgency": urgency,
            "summary": summary,
        },
    )


TOOLS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="comms.transfer_to_human",
        description=(
            "Hand the conversation to a member of staff. Pass a one-sentence "
            "summary of what the customer needs so they do not have to repeat "
            "themselves. Use urgency 'emergency' only for a genuine medical or "
            "safety emergency."
        ),
        parameters=object_schema(
            {
                "summary": {
                    "type": "string",
                    "minLength": 5,
                    "maxLength": 500,
                    "description": (
                        "What the customer needs, in one sentence, in their own "
                        "terms. No clinical interpretation."
                    ),
                },
                "urgency": {
                    "type": "string",
                    "enum": ["routine", "urgent", "emergency"],
                },
            },
            ["summary"],
        ),
        impl=_transfer_to_human,
        side_effecting=True,
        timeout_ms=3_000,
        # The summary describes a customer's problem in their words, which for a
        # medical practice is health data (contract §8).
        pii_arg_paths=("summary",),
    ),
)

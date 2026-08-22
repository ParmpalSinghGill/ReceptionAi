"""The agent never tells a caller about work it did not do.

**The failure this file exists to prevent, in full.** The model says "your
check-up is now Friday at 8:15" having never called ``reschedule_booking``. The
appointment is still on Wednesday. Nothing reports a failure — not the logs, not
the model, not the transcript — so the caller simply arrives on the wrong day.
That is materially worse than an error, because an error is visible to someone.

It is also **invisible to eyeballing a transcript**, which is exactly why it
survived two rounds of manual verification: the reply reads perfectly. The only
way to catch it is to assert the invariant against the tool record, which is what
every test below does.

The invariant: *a reply asserting that a booking was made, moved or cancelled may
only be emitted in a turn where the corresponding tool returned ``ok``.*

Two mechanisms enforce it and both are tested here:

1. **The nudge** (``NodeFactory.llm_agent``) — a model that narrates an intention
   without calling the tool gets one more round *with tools*, rather than being
   dropped into a toolless final pass where it is structurally incapable of
   acting. This was an engine bug, not prompt behaviour.
2. **The guard** (``_UnverifiedClaimGuard``) — a claim that survives anyway is
   rewritten before a token of it reaches the caller.
"""

from __future__ import annotations

import re
import time
from typing import Any

import pytest
from packages.ai.nodes import (
    _UNVERIFIED_REPLACEMENT,
    NodeFactory,
    _UnverifiedClaimGuard,
)
from packages.ai.tools import (
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    object_schema,
)
from packages.core.ids import uuid7
from packages.core.ports.llm import (
    ChatResult,
    Message,
    ProviderCapabilities,
    StreamDelta,
    ToolCall,
    Usage,
)
from packages.verticals import load_manifest_file
from packages.verticals.manifests import manifest_path

#: Phrases a caller hears as "it is done". Matched as *affirmative* claims, not
#: as bare words: the honest fallback says "nothing has **changed**", and a
#: keyword match on "changed" would flag the very sentence that exists to
#: prevent the lie. The distinction between asserting a change and denying one
#: is the whole point, so the checker has to be able to tell them apart.
COMPLETION_CLAIMS = re.compile(
    r"\b(?:"
    r"(?:has|have|is|are|been|was|were)\s+(?:now\s+)?"
    r"(?:booked|moved|changed|rescheduled|cancelled)"
    r"|i'?ve\s+(?:booked|moved|changed|rescheduled|cancelled)"
    r"|that'?s\s+booked"
    r"|all\s+set"
    r"|you'?re\s+booked"
    r"|(?:passed|transferred|put)\s+you\s+(?:over\s+)?(?:to|through)"
    r"|colleague\s+will\s+be\s+with\s+you"
    r"|they'?ll\s+be\s+with\s+you"
    r")\b",
    re.IGNORECASE,
)

#: The denial the guard substitutes. Asserted about explicitly wherever it is
#: expected, so a change to its wording fails loudly rather than silently
#: weakening every test that looks for it.
NOTHING_CHANGED = "nothing has changed"


class ScriptedLLM:
    """A provider whose every response is dictated by the test.

    Scripted rather than mocked at the node boundary: the point is to drive the
    *real* tool loop through the exact sequences a live model produces, including
    the one that caused the bug — narration with no tool call.
    """

    provider = "scripted"
    model = "scripted-1"

    def __init__(self, rounds: list[tuple[str, list[ToolCall]]], final: str) -> None:
        self._rounds = list(rounds)
        self._final = final
        self.chat_calls = 0
        self.stream_calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_tools=True,
            supports_parallel_tools=True,
            supports_streaming_tools=True,
            supports_json_schema=True,
            supports_prompt_caching=False,
            context_window=32_768,
            max_output_tokens=4_096,
        )

    def count_tokens(self, messages: Any) -> int:
        return 1

    async def chat(self, *, messages: Any, tools: Any = None, **kwargs: Any):
        self.chat_calls += 1
        # Rounds exhausted: answer in prose, as a real model does once the
        # tool results are in its history. The loop replays that prose as the
        # reply (the fast path), so `final` must be what the prose says.
        content, calls = self._rounds.pop(0) if self._rounds else (self._final, [])
        return ChatResult(
            message=Message(
                role="assistant", content=content, tool_calls=tuple(calls)
            ),
            tool_calls=list(calls),
            usage=Usage(prompt_tokens=1, completion_tokens=1),
            model=self.model,
            provider=self.provider,
            finish_reason="tool_calls" if calls else "stop",
        )

    async def stream(self, *, messages: Any, tools: Any = None, **kwargs: Any):
        self.stream_calls += 1
        # Word by word, so the guard's sentence buffering is genuinely
        # exercised. A single-chunk stream would let a broken guard pass.
        for word in self._final.split(" "):
            yield StreamDelta(kind="text", text=word + " ")
        yield StreamDelta(kind="done")

    async def aclose(self) -> None:
        return None


class Harness:
    """Runs one scheduler turn and records which tools actually succeeded."""

    def __init__(self, rounds: list[tuple[str, list[ToolCall]]], final: str) -> None:
        self.fired: list[str] = []
        self.llm = ScriptedLLM(rounds, final)

        async def reschedule(ctx: Any, args: dict[str, Any]) -> ToolResult:
            self.fired.append("booking.reschedule_booking")
            return ToolResult(content="Moved to Friday 14 August at 8:15am.")

        async def cancel(ctx: Any, args: dict[str, Any]) -> ToolResult:
            self.fired.append("booking.cancel_booking")
            return ToolResult(content="Cancelled BK-TEST01.")

        async def book(ctx: Any, args: dict[str, Any]) -> ToolResult:
            self.fired.append("booking.book_appointment")
            return ToolResult(content="Booked. Reference BK-TEST01.")

        async def transfer(ctx: Any, args: dict[str, Any]) -> ToolResult:
            self.fired.append("comms.transfer_to_human")
            return ToolResult(content="A team member has been alerted.")

        schema = object_schema({"new_start_time": {"type": "string"}}, [])
        self.registry = ToolRegistry(
            (
                ToolSpec(
                    name="booking.reschedule_booking",
                    description="Move a booking.",
                    parameters=schema,
                    impl=reschedule,
                    side_effecting=True,
                ),
                ToolSpec(
                    name="booking.cancel_booking",
                    description="Cancel a booking.",
                    parameters=schema,
                    impl=cancel,
                    side_effecting=True,
                ),
                ToolSpec(
                    name="booking.book_appointment",
                    description="Create a booking.",
                    parameters=schema,
                    impl=book,
                    side_effecting=True,
                ),
                ToolSpec(
                    name="comms.transfer_to_human",
                    description="Escalate to a person.",
                    parameters=schema,
                    impl=transfer,
                    side_effecting=True,
                ),
            )
        )

    async def run(self, agent_key: str = "scheduler") -> str:
        manifest = load_manifest_file(manifest_path("medical"))
        factory = NodeFactory(
            llm=self.llm,
            manifest=manifest,
            executor=ToolExecutor(self.registry),
            tool_schemas=self.registry.schemas(),
        )
        node = factory.llm_agent(
            manifest.graph.agent(agent_key), manifest.graph
        )
        state = {
            "messages": [Message(role="user", content="move it to Friday")],
            "tenant_id": uuid7(),
            "conversation_id": uuid7(),
            "collected_data": {},
            "max_tool_calls": 16,
            "tool_call_count": 0,
            "deadline_ms": int(time.monotonic() * 1000) + 60_000,
            "timezone": "Europe/London",
        }
        result = await node(state)  # type: ignore[arg-type]
        return result["messages"][0].content or ""


def assert_invariant(reply: str, fired: list[str]) -> None:
    """**The invariant.** A completion claim implies a successful write.

    This is the assertion the coordinator asked for, and the one no amount of
    reading a transcript can make: the reply is checked against the *tool
    record*, not against how convincing it sounds.
    """
    # The honest fallback ("nothing has changed") is a denial, not a claim, and
    # must never be read as one — it is the sentence that exists to prevent the
    # lie.
    if NOTHING_CHANGED in reply.lower():
        return
    if claim := COMPLETION_CLAIMS.search(reply):
        assert fired, (
            f"The reply asserts {claim.group(0)!r} but NO tool fired. The caller "
            f"has been told their appointment changed when it did not — they "
            f"will arrive on the wrong day and nothing anywhere reports a "
            f"failure.\nReply: {reply!r}"
        )


async def test_a_narrated_intention_is_recovered_into_a_real_tool_call() -> None:
    """The engine bug, fixed.

    The model announces "I'll move your check-up... one moment" and emits no
    tool call. Before the fix the loop broke here and the final pass ran with
    ``tool_schemas=None`` — so the model was **structurally incapable** of
    acting, whatever it intended. It got one chance, spent it on narration, and
    the turn ended with the caller misinformed.

    Now an unfulfilled promise buys one more round *with tools*.
    """
    harness = Harness(
        rounds=[
            ("I'll move your check-up to Friday. One moment while I confirm.", []),
            (
                "",
                [
                    ToolCall(
                        id="c1",
                        name="booking.reschedule_booking",
                        arguments={"new_start_time": "2026-08-14T08:15"},
                    )
                ],
            ),
        ],
        final="Your check-up has been moved to Friday 14 August at 8:15am.",
    )
    reply = await harness.run()

    assert harness.fired == ["booking.reschedule_booking"], (
        "The narrated intention was not recovered into a call. A model that says "
        "'one moment' must be given another round WITH tools, not dropped into a "
        "toolless final pass."
    )
    assert "moved" in reply.lower()
    assert_invariant(reply, harness.fired)


async def test_a_claim_is_rewritten_when_the_model_refuses_to_act() -> None:
    """The guard, for when the nudge is not enough.

    The model narrates, is nudged, declines again, and then asserts success
    anyway. A prompt rule is a request and a model under pressure to sound
    helpful will break it — this one already did, in production. The claim must
    not reach the caller.
    """
    harness = Harness(
        rounds=[
            ("I'll move it. One moment.", []),
            ("Still working on it.", []),
        ],
        final="Your check-up has been moved to Friday 14 August at 8:15am.",
    )
    reply = await harness.run()

    assert harness.fired == []
    assert "moved to friday" not in reply.lower(), (
        "An unverified claim reached the caller. The guard must rewrite a "
        "sentence asserting a change when the write did not happen."
    )
    assert NOTHING_CHANGED in reply.lower(), (
        "The replacement must say plainly that nothing changed. Going silent "
        "would read to a caller as the line dropping."
    )
    assert_invariant(reply, harness.fired)


@pytest.mark.parametrize(
    ("claim", "required_tool"),
    [
        ("Your appointment has been cancelled.", "booking.cancel_booking"),
        ("That's booked for you.", "booking.book_appointment"),
        ("I've moved it to Friday.", "booking.reschedule_booking"),
        ("Your appointment is now changed to Friday.", "booking.reschedule_booking"),
        ("All set — you're booked in.", "booking.book_appointment"),
    ],
)
async def test_each_kind_of_claim_requires_its_own_tool(
    claim: str, required_tool: str
) -> None:
    """A cancel claim needs a cancel, not merely *some* tool having run.

    Checking only "did any tool fire" would pass a turn that checked
    availability and then announced a cancellation — which is the same lie with
    a busier log.
    """
    # The tool that fired is deliberately the WRONG one for the claim.
    wrong = (
        "booking.book_appointment"
        if required_tool != "booking.book_appointment"
        else "booking.cancel_booking"
    )
    harness = Harness(
        rounds=[("", [ToolCall(id="c1", name=wrong, arguments={})])],
        final=claim,
    )
    reply = await harness.run()

    assert harness.fired == [wrong]
    assert NOTHING_CHANGED in reply.lower(), (
        f"{claim!r} was allowed through on the strength of {wrong} having run. "
        f"It requires {required_tool}."
    )


async def test_a_verified_claim_passes_through_untouched() -> None:
    """The guard must not mangle a true statement.

    A guard that rewrites everything is a different bug with the same symptom —
    an agent that can never confirm anything is as useless as one that confirms
    falsely.
    """
    harness = Harness(
        rounds=[
            (
                "",
                [
                    ToolCall(
                        id="c1",
                        name="booking.cancel_booking",
                        arguments={},
                    )
                ],
            )
        ],
        final="Your check-up on Thursday has been cancelled. Anything else?",
    )
    reply = await harness.run()

    assert harness.fired == ["booking.cancel_booking"]
    assert "has been cancelled" in reply
    assert NOTHING_CHANGED not in reply.lower()
    assert_invariant(reply, harness.fired)


@pytest.mark.parametrize(
    "reply",
    [
        "I have Friday at 8:00, 8:15, or 8:30. Which would you like?",
        "I can move that for you. What time suits?",
        "Shall I book you in for Tuesday morning?",
        "We're open Monday to Friday, eight until six.",
        "Would you like me to cancel it?",
    ],
)
def test_offers_and_questions_are_never_treated_as_claims(reply: str) -> None:
    """Only *completion* claims are checked.

    "I can move that for you" and "shall I cancel it?" are an offer and a
    question — they assert nothing about the world. Blocking them would make the
    agent unable to hold a conversation, which is a worse product than one that
    occasionally over-claims.
    """
    guard = _UnverifiedClaimGuard(succeeded=set())
    out = "".join(guard.feed(word + " ") for word in reply.split(" ")) + guard.flush()
    assert out.strip() == reply
    assert not guard.rewrote


def test_the_guard_holds_a_claim_that_straddles_token_boundaries() -> None:
    """Tokens stream, and a claim cannot be un-said once emitted.

    The guard buffers by sentence for exactly this reason. Fed one character at
    a time — the worst possible chunking — not one word of the claim may escape.
    """
    guard = _UnverifiedClaimGuard(succeeded=set())
    claim = "Your appointment has been moved to Friday. See you then."
    out = "".join(guard.feed(char) for char in claim) + guard.flush()

    assert "moved to Friday" not in out
    assert NOTHING_CHANGED in out.lower()
    # The WHOLE reply is discarded, innocent sentences included. A sentence-level
    # swap left "I'll book that now. I'm sorry — I wasn't able to complete
    # that..." in front of a caller; the surviving sentences were written on the
    # assumption the claim was true, so they go too.
    assert "See you then." not in out


def test_a_verified_claim_streams_without_being_held_hostage() -> None:
    """When the write succeeded, the sentence is released intact."""
    guard = _UnverifiedClaimGuard(succeeded={"booking.reschedule_booking"})
    claim = "Your appointment has been moved to Friday. See you then."
    out = "".join(guard.feed(char) for char in claim) + guard.flush()
    assert out == claim
    assert not guard.rewrote


# ── Transfer claims ──────────────────────────────────────────────────────────
#
# The same failure, found later and with a worse shape. The agent said "I've
# passed you over to a colleague now — they'll be with you shortly" and
# `comms.transfer_to_human` never fired: `grep -c transfer_to_human` over the
# whole run returned 0. Nobody was alerted, and a caller who believes help is
# coming stops looking for it.


@pytest.mark.parametrize(
    "claim",
    [
        "I've passed you over to a colleague now.",
        "I'm putting you through to a colleague.",
        "I'm transferring you to one of the team.",
        "A colleague will be with you shortly.",
        "Someone will call you back this afternoon.",
        "They'll be with you shortly.",
    ],
)
def test_a_transfer_claim_requires_the_transfer_tool(claim: str) -> None:
    """No handoff claim without a handoff.

    Parametrised across the phrasings a model actually reaches for: a single
    regex on "transfer" would miss "passed you over", which is the exact wording
    that shipped.
    """
    guard = _UnverifiedClaimGuard(succeeded=set())
    out = "".join(guard.feed(char) for char in claim) + guard.flush()

    assert NOTHING_CHANGED in out.lower(), (
        f"{claim!r} reached the caller with no transfer tool having run. "
        f"A caller told help is coming stops looking for it."
    )
    assert guard.rewrote


def test_a_transfer_claim_passes_once_the_tool_has_run() -> None:
    """And the true version is left alone."""
    guard = _UnverifiedClaimGuard(succeeded={"comms.transfer_to_human"})
    claim = "I've passed you over to a colleague now."
    out = "".join(guard.feed(char) for char in claim) + guard.flush()

    assert out == claim
    assert not guard.rewrote


def test_a_transfer_claim_is_not_satisfied_by_a_booking_tool() -> None:
    """Per-claim, not "some tool ran".

    A turn that booked an appointment and then announced a transfer is the same
    lie with a busier log.
    """
    guard = _UnverifiedClaimGuard(succeeded={"booking.book_appointment"})
    claim = "I'm putting you through to a colleague."
    out = "".join(guard.feed(char) for char in claim) + guard.flush()

    assert NOTHING_CHANGED in out.lower()


async def test_the_full_loop_blocks_an_unbacked_transfer_claim() -> None:
    """End to end through the real tool loop, asserted against the tool record.

    The engine-level statement of the invariant for transfers: the reply may
    only claim a handoff if `comms.transfer_to_human` returned ok this turn.
    """
    harness = Harness(
        rounds=[("I'll get someone for you. One moment.", []), ("Still here.", [])],
        final="I've passed you over to a colleague now — they'll be with you shortly.",
    )
    reply = await harness.run()

    assert harness.fired == []
    assert "passed you over" not in reply.lower()
    assert NOTHING_CHANGED in reply.lower()
    assert_invariant(reply, harness.fired)


async def test_a_backed_transfer_claim_survives_the_full_loop() -> None:
    harness = Harness(
        rounds=[
            (
                "",
                [
                    ToolCall(
                        id="c1", name="comms.transfer_to_human", arguments={}
                    )
                ],
            )
        ],
        final="I've passed you over to a colleague now.",
    )
    # The escalation agent, not the scheduler: `comms.transfer_to_human` is
    # granted to `human_transfer` alone. Running it as the scheduler would test
    # the grant model rather than the claim guard — and would pass for the wrong
    # reason, since a denied tool never returns ok.
    reply = await harness.run(agent_key="human_transfer")

    assert harness.fired == ["comms.transfer_to_human"]
    assert "passed you over" in reply.lower()
    assert NOTHING_CHANGED not in reply.lower()
    assert_invariant(reply, harness.fired)


@pytest.mark.parametrize(
    "intention",
    [
        # An agent handing off INTERNALLY says exactly this, and the handoff is
        # real — the next node runs in the same turn. Suppressing it would
        # make multi-agent handoff impossible to describe, and it briefly did:
        # triage's honest "I'll pass you to my colleague to book that" was
        # replaced with an apology.
        "I'll pass you over to my colleague now to get that booked.",
        "Let me pass you to my colleague to book that.",
        "Let me get you booked in for this afternoon.",
        "Shall I book that for you?",
        "I can move that to Friday if you like.",
        "Would you like me to cancel it?",
    ],
)
def test_an_intention_is_not_a_claim(intention: str) -> None:
    """Only the *deed done* needs backing, not the intention to do it.

    The distinction is the whole design: a guard that blocks "let me get that
    booked" makes the agent unable to say what it is about to do, which is worse
    than useless — it is an agent that can only ever speak in the past tense.
    """
    guard = _UnverifiedClaimGuard(succeeded=set())
    out = "".join(guard.feed(char) for char in intention) + guard.flush()
    assert out == intention
    assert not guard.rewrote, (
        f"{intention!r} was treated as a completion claim. It states an "
        f"intention; only assertions that the work is DONE require a tool."
    )


# ── Already done, versus never done ──────────────────────────────────────────
#
# A caller who says "yes, book it" once more after the booking succeeded was
# told: "I'm sorry — I wasn't able to complete that just now, so nothing has
# changed. Let me put you through to a colleague" — immediately followed by the
# reference and arrival instructions. Contradictory, alarming, and it invented a
# transfer that never happened. The claim was TRUE; it was just made a turn late.


def test_a_claim_backed_by_an_earlier_turn_is_restated_not_denied() -> None:
    """True, but performed earlier — restate it.

    The guard only ever knew which tools ran in the CURRENT turn, so a
    restatement looked identical to a fabrication. It is not: one is a caller
    seeking reassurance, the other is a lie.
    """
    guard = _UnverifiedClaimGuard(
        succeeded=set(), previously={"booking.book_appointment"}
    )
    claim = "Your appointment has been booked for Tuesday at 9."
    out = "".join(guard.feed(char) for char in claim) + guard.flush()

    assert out == claim, (
        "A true claim was altered. The detail — the day, the time, the "
        "reference — is exactly what a caller confirming their appointment "
        "wants read back, and the claim is backed by a real write in this "
        "conversation."
    )
    assert NOTHING_CHANGED not in out.lower(), (
        "A true claim was denied. Telling a caller their booking did not happen, "
        "when it did, is its own harm."
    )
    assert "colleague" not in out.lower(), (
        "The denial invented a transfer. A restatement must not escalate."
    )


def test_a_claim_backed_by_neither_turn_is_still_denied() -> None:
    """The original protection is untouched."""
    guard = _UnverifiedClaimGuard(succeeded=set(), previously=set())
    claim = "Your appointment has been booked for Tuesday at 9."
    out = "".join(guard.feed(char) for char in claim) + guard.flush()
    assert NOTHING_CHANGED in out.lower()


def test_a_claim_backed_by_this_turn_passes_untouched() -> None:
    """And a fresh success is spoken as written, not replaced by a summary."""
    guard = _UnverifiedClaimGuard(
        succeeded={"booking.book_appointment"}, previously=set()
    )
    claim = "Your appointment has been booked for Tuesday at 9."
    out = "".join(guard.feed(char) for char in claim) + guard.flush()
    assert out == claim
    assert not guard.rewrote


def test_an_earlier_cancel_does_not_license_a_booking_claim() -> None:
    """Per-claim, across turns as well as within them.

    "You're booked" is not made true by a cancellation two turns ago.
    """
    guard = _UnverifiedClaimGuard(
        succeeded=set(), previously={"booking.cancel_booking"}
    )
    claim = "You're booked for Tuesday at 9."
    out = "".join(guard.feed(char) for char in claim) + guard.flush()
    assert NOTHING_CHANGED in out.lower()


async def test_a_terminal_agent_may_restate_a_completed_booking() -> None:
    """The closing agent runs no tools, so every truth it tells is from earlier.

    Without passing the conversation's completed set into the terminal node, the
    guard treated its summary as unverified and replaced "your appointment is
    booked for 1:45 with Dr Okafor" with an apology and an invented transfer —
    while the row sat happily in the database. The contradiction was visible in
    the same reply, since the model went on to give arrival instructions.
    """
    import time as _time

    from packages.ai.tools import ToolExecutor, default_registry

    manifest = load_manifest_file(manifest_path("medical"))
    llm = ScriptedLLM(
        rounds=[],
        final="Your appointment is booked for today at 1:45pm with Dr Okafor.",
    )
    registry = default_registry()
    factory = NodeFactory(
        llm=llm,
        manifest=manifest,
        executor=ToolExecutor(registry),
        tool_schemas=registry.schemas(),
    )
    node = factory.terminal(manifest.graph.agent("wrap_up"), manifest.graph)

    result = await node(  # type: ignore[arg-type]
        {
            "messages": [Message(role="user", content="thanks")],
            # The booking happened on an earlier turn, as it always will by the
            # time a terminal node runs.
            "collected_data": {
                "_completed_tools": ["booking.book_appointment"],
            },
            "tenant_id": uuid7(),
            "conversation_id": uuid7(),
            "timezone": "Europe/London",
            "deadline_ms": int(_time.monotonic() * 1000) + 60_000,
        }
    )

    reply = result["messages"][0].content or ""
    assert NOTHING_CHANGED not in reply.lower(), (
        "The closing agent was told its own true summary was a fabrication."
    )
    assert "colleague" not in reply.lower(), (
        "The denial invented a transfer on the closing turn."
    )
    assert "1:45" in reply


# ── Slot handles must never be speakable ─────────────────────────────────────
#
# A caller was read this, as "your reference":
#
#   czF8ZW1lcmdlbmN5fDIwMjYtMDgtMTBUMTM6NDU6MDArMDA6MDB8MDE5ZmRjNGYt...
#
# 200 characters of base64 carrying an internal UTC instant and two resource
# UUIDs. The prompt had asked the model not to say it. An instruction was
# standing where a mechanism was needed.


@pytest.mark.parametrize(
    "reply",
    [
        "Your reference is [slot_a3f9c1d2].",
        "I'll book slot_deadbeef for you now.",
        "Booking slot_00112233 — one moment.",
        "The reference is slot_ffffffff, see you then.",
    ],
)
def test_a_slot_handle_never_survives_into_a_reply(reply: str) -> None:
    """The filter, not the instruction.

    Asserted on the guard's output rather than by reading a transcript, because
    this failure looked like a perfectly ordinary confirmation until someone
    base64-decoded it.
    """
    guard = _UnverifiedClaimGuard(
        succeeded={"booking.book_appointment"}, previously=set()
    )
    out = "".join(guard.feed(char) for char in reply) + guard.flush()

    assert "slot_" not in out, (
        f"An internal slot handle reached the caller in {out!r}. It carries the "
        f"engine's own instant and resource ids and is never speakable."
    )
    assert guard.leaked_handle, "the scrub must be reported so it can be alerted on"


def test_scrubbing_is_reported_only_when_it_happens() -> None:
    guard = _UnverifiedClaimGuard(succeeded=set(), previously=set())
    ordinary = "I have Tuesday at 9:00 or 9:15. Which would you like?"
    out = "".join(guard.feed(char) for char in ordinary) + guard.flush()
    assert out == ordinary
    assert not guard.leaked_handle


def test_a_handle_split_across_tokens_is_still_scrubbed() -> None:
    """Tokens split anywhere, including through the middle of a handle."""
    guard = _UnverifiedClaimGuard(
        succeeded={"booking.book_appointment"}, previously=set()
    )
    reply = "Booked, reference [slot_a3f9c1d2]. See you at 2pm."
    out = "".join(guard.feed(char) for char in reply) + guard.flush()
    assert "slot_" not in out
    assert "a3f9c1d2" not in out


# ── A suppressed claim must not leave a contradictory reply ──────────────────


def test_a_suppressed_claim_discards_the_whole_reply() -> None:
    """The caller saw all three of these sentences, in one reply:

        "I'll book that now. I'm sorry — I wasn't able to complete that just
         now, so nothing has changed. Could I grab your full name?"

    A sentence-level swap leaves the rest standing, and the rest was written on
    the assumption the claim was true. A reply caught lying cannot be repaired a
    clause at a time.
    """
    guard = _UnverifiedClaimGuard(succeeded=set(), previously=set())
    reply = (
        "I'll book that now. Your appointment is confirmed for 2pm with "
        "Dr Okafor. Could I grab your full name and a contact number?"
    )
    out = "".join(guard.feed(char) for char in reply) + guard.flush()

    assert out.strip().endswith(_UNVERIFIED_REPLACEMENT), (
        f"A suppressed claim must end the reply honestly: {out!r}"
    )
    assert "confirmed for 2pm" not in out, (
        "The false claim itself reached the caller."
    )
    assert "Could I grab your full name" not in out, (
        "Everything after the suppressed claim was written on the assumption it "
        "was true, so it goes too — that is the contradiction being prevented."
    )


def test_the_replacement_does_not_itself_promise_a_human() -> None:
    """The guard's own fallback must not trip the guard's own rule.

    An earlier version said "let me put you through to a colleague", inventing
    the very handoff the guard exists to prevent.
    """
    second = _UnverifiedClaimGuard(succeeded=set(), previously=set())
    out = "".join(second.feed(c) for c in _UNVERIFIED_REPLACEMENT) + second.flush()
    assert out.strip() == _UNVERIFIED_REPLACEMENT, (
        "The replacement text is itself an unbacked claim."
    )
    assert not second.rewrote


# ── Any promise that a human will act needs a tool ───────────────────────────
#
# Inverted from an allow-list of phrasings, because an allow-list is always one
# phrasing behind: "passed you over" was covered, then "a colleague will be with
# you", then "take a message and have a colleague follow that up" slipped
# through. Each gap was a real caller told help was coming when nobody had been
# told.


@pytest.mark.parametrize(
    "claim",
    [
        "Let me take a message and have a colleague follow that up.",
        "A colleague will be with you shortly.",
        "Someone will call you back this afternoon.",
        "One of the team will contact you.",
        "A member of staff will ring you.",
        "They will confirm the time with you.",
        "I'll take a message for the team.",
        "Somebody will get back to you today.",
    ],
)
def test_any_promise_of_human_action_requires_the_transfer_tool(
    claim: str,
) -> None:
    guard = _UnverifiedClaimGuard(succeeded=set(), previously=set())
    out = "".join(guard.feed(char) for char in claim) + guard.flush()
    assert out.strip() == _UNVERIFIED_REPLACEMENT, (
        f"{claim!r} promised human action with no transfer tool having run. A "
        f"caller told help is coming stops looking for it."
    )


@pytest.mark.parametrize(
    "ordinary",
    [
        "Dr Rahman covers our same-day emergencies.",
        "She can see you at 2pm today.",
        "I have Tuesday at 9:00 or 9:15. Which would you like?",
        "We're open Monday to Friday, eight until six.",
        "He is our hygienist and is very gentle with sensitive gums.",
    ],
)
def test_ordinary_talk_about_people_is_not_a_promise(ordinary: str) -> None:
    """Naming a clinician is not promising a handoff.

    The inverted rule is broad on purpose, so this is the counterweight: an
    agent that cannot mention a dentist by name cannot recommend one, which is
    the feature the whole seed exists to support.
    """
    guard = _UnverifiedClaimGuard(succeeded=set(), previously=set())
    out = "".join(guard.feed(char) for char in ordinary) + guard.flush()
    assert out == ordinary
    assert not guard.rewrote


@pytest.mark.parametrize(
    ("sentence", "suppressed"),
    [
        # U+2019, which models emit as readily as the ASCII apostrophe. Every
        # `'?` in the patterns silently failed to match it, so a legitimate
        # internal handoff was suppressed and the caller got an apology instead
        # of being routed.
        ("I\u2019ll pass you to the scheduler now.", False),
        ("I'll pass you to the scheduler now.", False),
        ("I\u2019ve passed you over to a colleague.", True),
        ("They\u2019ll be with you shortly.", True),
        # Passive future: a claim about what is going to happen, from an agent
        # that is not the one doing it.
        ("This will be booked as an emergency appointment.", True),
        ("It will be moved to Friday.", True),
    ],
)
def test_curly_apostrophes_and_passive_claims(
    sentence: str, suppressed: bool
) -> None:
    """A pattern that only matches ASCII punctuation is a pattern with a hole.

    The observed failure was a *false positive*: triage said "I'll pass you to
    the scheduler" with a curly apostrophe, the internal-handoff exemption did
    not match, and a true sentence was replaced with an apology mid-triage.
    """
    guard = _UnverifiedClaimGuard(succeeded=set(), previously=set())
    "".join(guard.feed(char) for char in sentence)
    guard.flush()
    assert bool(guard.rewrote) is suppressed, (
        f"{sentence!r} was {'not ' if suppressed else ''}suppressed."
    )


def test_every_pattern_in_the_module_sees_normalised_punctuation() -> None:
    """The general form of the curly-apostrophe bug.

    A pattern written with an ASCII apostrophe silently fails on U+2019, and
    models emit both freely. One such hole suppressed a true handoff mid-triage.
    Rather than trusting each pattern to be fixed individually, matching runs
    against a normalised copy — so this asserts the *seam* exists and covers the
    twins a model actually produces.
    """
    from packages.ai.nodes import _normalise

    pairs = [
        ("\u2018", "'"),
        ("\u2019", "'"),
        ("\u02bc", "'"),
        ("\u201c", '"'),
        ("\u201d", '"'),
        ("\u2013", "-"),
        ("\u2014", "-"),
        ("\u2011", "-"),
    ]
    for curly, ascii_form in pairs:
        assert _normalise(f"x{curly}y") == f"x{ascii_form}y", (
            f"U+{ord(curly):04X} is not folded; every pattern matching "
            f"{ascii_form!r} has a hole for it."
        )
    # And it does not mangle ordinary text.
    assert _normalise("I'll pass you to Dr Okafor.") == "I'll pass you to Dr Okafor."


@pytest.mark.parametrize(
    "curly",
    [
        "I\u2019ve passed you over to a colleague.",
        "Your appointment has been booked \u2014 see you then.",
        "They\u2019ll be with you shortly.",
    ],
)
def test_claims_written_with_curly_punctuation_are_still_caught(curly: str) -> None:
    """A false claim must not escape by using a different apostrophe."""
    guard = _UnverifiedClaimGuard(succeeded=set(), previously=set())
    "".join(guard.feed(char) for char in curly)
    guard.flush()
    assert guard.rewrote, f"{curly!r} escaped the guard via unicode punctuation."

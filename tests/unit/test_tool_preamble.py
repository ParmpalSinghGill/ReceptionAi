"""The preamble and the post-tool fast path — two latency fixes, one turn.

Both come from one Langfuse trace of a real turn ("I have a headache and a
little pain in my tooth"):

* Round 0 returned *text and a tool call* — "I'm sorry to hear you're in pain.
  Let me check what emergency slots we have." — and the text was discarded, so
  the caller saw nothing until the whole turn finished.
* Round 1 answered in prose with no tool call, and the loop then regenerated
  the same sentence through ``llm.stream``: a second provider round trip and a
  full prompt's tokens to produce what it already held.

These tests drive the real tool loop with a scripted provider and assert on the
*events the caller would receive*, not on the model's words.
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.ai.streaming import bind_sink, unbind_sink
from packages.core.ports.llm import ToolCall
from tests.unit.test_no_unverified_claims import Harness

pytestmark = pytest.mark.anyio


CALL = ToolCall(
    id="c1", name="booking.book_appointment", arguments={"new_start_time": "x"}
)


async def _run(harness: Harness) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    async def sink(event_type: str, data: dict[str, Any]) -> None:
        events.append((event_type, dict(data)))

    token = bind_sink(sink)
    try:
        reply = await harness.run()
    finally:
        unbind_sink(token)
    return reply, events


def _interim_text(events: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(d["text"] for t, d in events if t == "token" and d.get("interim"))


class TestPreamble:
    async def test_the_sentence_written_with_a_tool_call_is_shown_first(self) -> None:
        harness = Harness(
            rounds=[("I'm sorry to hear that. Let me check the diary.", [CALL])],
            final="That's booked for you. Reference BK-TEST01.",
        )
        _, events = await _run(harness)

        assert _interim_text(events).strip() == (
            "I'm sorry to hear that. Let me check the diary."
        )
        kinds = [t for t, _ in events]
        # Shown BEFORE the tool runs — that is the entire point.
        assert kinds.index("token") < kinds.index("tool.started")

    async def test_a_preamble_claiming_unfinished_work_is_withheld(self) -> None:
        """"I've booked that" written alongside the booking call is a claim
        about a write that has not happened yet. It is not shown, and it is
        not replaced with an apology either — a second later it may be true."""
        harness = Harness(
            rounds=[("I've booked that for you. One moment.", [CALL])],
            final="That's booked for you. Reference BK-TEST01.",
        )
        reply, events = await _run(harness)

        assert _interim_text(events) == ""
        assert "booked" in reply.lower()  # the real answer still arrives

    async def test_a_preamble_that_asks_a_question_is_withheld(self) -> None:
        """One question per reply is a guard rule; a preamble may not spend it."""
        harness = Harness(
            rounds=[("Let me check. What time suits you?", [CALL])],
            final="That's booked for you. Reference BK-TEST01.",
        )
        _, events = await _run(harness)
        assert _interim_text(events) == ""

    async def test_preamble_and_reply_are_one_message_with_a_seam(self) -> None:
        """The preamble is the start of the same message the reply streams
        into, so the persisted turn reads as one utterance, not two glued
        words: '...diary. That's booked' not '...diary.That's booked'."""
        harness = Harness(
            rounds=[("Let me check the diary.", [CALL])],
            final="That's booked for you. Reference BK-TEST01.",
        )
        _, events = await _run(harness)
        streamed = "".join(d["text"] for t, d in events if t == "token")
        assert streamed.startswith("Let me check the diary. That's booked")


class TestPostToolFastPath:
    async def test_prose_after_a_tool_is_replayed_not_regenerated(self) -> None:
        """Round 1 answers in prose with the tool result in hand. That IS the
        reply; a second provider call to say it again is pure waste."""
        harness = Harness(
            rounds=[("", [CALL])],
            final="That's booked for you. Reference BK-TEST01.",
        )
        reply, _ = await _run(harness)

        assert harness.llm.chat_calls == 2  # tool round + the answering round
        assert harness.llm.stream_calls == 0, (
            "The post-tool answer was regenerated through llm.stream instead of "
            "being replayed."
        )
        assert "booked" in reply.lower()

    async def test_an_unbacked_post_tool_claim_is_nudged_then_guarded(self) -> None:
        """The fast path is for REPORTS. A post-tool claim the tool record does
        not back ("that's booked" after a *cancel*) is still an empty promise:
        the loop nudges once (a chance to actually book), and when the model
        only repeats itself the guard rewrites the claim. Neither the nudge nor
        the guard is bypassed by the fast path."""
        wrong = ToolCall(id="c1", name="booking.cancel_booking", arguments={})
        harness = Harness(
            rounds=[("", [wrong])],
            final="That's booked for you. See you Friday.",
        )
        reply, _ = await _run(harness)

        assert harness.llm.chat_calls == 3  # tool round, claim, nudged repeat
        assert harness.llm.stream_calls == 1  # nudged → regenerate → guard
        assert "booked" not in reply.lower()
        assert "nothing has changed" in reply.lower()

    async def test_a_backed_post_tool_report_is_not_nudged(self) -> None:
        """"That's booked" after book_appointment returned ok is a report.
        Before: nudged as a promise (extra round), then regenerated (extra
        stream). Now: two chat calls, zero streams, same words to the caller."""
        harness = Harness(
            rounds=[("", [CALL])],
            final="That's booked for you. Reference BK-TEST01.",
        )
        reply, _ = await _run(harness)
        assert (harness.llm.chat_calls, harness.llm.stream_calls) == (2, 0)
        assert "booked" in reply.lower()

    async def test_a_nudged_turn_still_regenerates(self) -> None:
        """After a narration nudge the prose in hand is the narration itself,
        so the final pass must regenerate — that rule is unchanged."""
        harness = Harness(
            rounds=[("I'll book that now. One moment.", []), ("", [CALL])],
            final="That's booked for you. Reference BK-TEST01.",
        )
        await _run(harness)
        assert harness.llm.stream_calls == 1

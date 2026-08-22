"""The voice→agent bridge: utterance handling that keeps a call coherent.

These cover the text-level logic that runs *between* STT and the agent. The
Pipecat frame plumbing is exercised by constructing the real pipeline in
``tests/unit/test_voice_pipeline_build.py``; here the concern is the merge and
de-duplication rules, which is where a caller's words actually get lost.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "pipecat", reason="voice extra not installed (pip install -e '.[voice]')"
)

from packages.providers.voice.agent_bridge import (
    VoiceAgentProcessor,
    last_user_text,
    merge_utterances,
)
from pipecat.processors.frame_processor import FrameDirection


class TestMergeUtterances:
    def test_joins_two_distinct_fragments(self) -> None:
        assert merge_utterances("oh and", "make it Tuesday") == "oh and make it Tuesday"

    def test_drops_an_exact_repeat(self) -> None:
        """STT re-emits overlapping text; naive concatenation stutters."""
        assert merge_utterances("my number is 07700", "my number is 07700") == (
            "my number is 07700"
        )

    def test_drops_a_contained_fragment(self) -> None:
        assert merge_utterances("book me for Tuesday", "Tuesday") == (
            "book me for Tuesday"
        )

    def test_prefers_the_longer_when_the_first_is_contained(self) -> None:
        assert merge_utterances("Tuesday", "book me for Tuesday") == (
            "book me for Tuesday"
        )

    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [("", "hello", "hello"), ("hello", "", "hello"), ("", "", "")],
    )
    def test_handles_empty_sides(self, left: str, right: str, expected: str) -> None:
        assert merge_utterances(left, right) == expected


class _Ctx:
    def __init__(self, messages: list[object]) -> None:
        self.messages = messages


class TestLastUserText:
    def test_reads_the_newest_user_message(self) -> None:
        ctx = _Ctx(
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "second"},
            ]
        )
        assert last_user_text(ctx) == "second"

    def test_accepts_human_as_a_role_alias(self) -> None:
        assert last_user_text(_Ctx([{"role": "human", "content": "hi"}])) == "hi"

    def test_flattens_multipart_content(self) -> None:
        ctx = _Ctx(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "book"},
                        {"type": "text", "text": "Tuesday"},
                    ],
                }
            ]
        )
        assert last_user_text(ctx) == "book Tuesday"

    def test_empty_context_is_not_an_error(self) -> None:
        """A missing utterance must pass the frame along, not raise mid-call."""
        assert last_user_text(_Ctx([])) == ""
        assert last_user_text(object()) == ""


def _bridge(replies: list[str], *, queue_during: dict[str, str] | None = None):
    """A processor whose graph returns canned replies, with speech captured.

    ``queue_during`` maps a user text to a continuation that "arrives" while
    that turn's inference is running — the caller talking over the silence.
    """
    spoken: list[str] = []
    heard: list[str] = list(replies)
    #: Every call into the runtime, in order — so a test can assert that the
    #: retraction lands BEFORE the follow-up turn reads its history.
    calls: list[str] = []

    proc = VoiceAgentProcessor.__new__(VoiceAgentProcessor)
    proc._on_transcript = None
    proc._state = {}
    proc._busy = True
    proc._last_user_text = ""
    proc._last_user_at = 0.0
    proc._pending_user_text = ""
    proc._phone_buffer = ""
    proc._phone_flush_task = None

    async def run_turn(text: str, **_: object) -> str:
        calls.append(f"run:{text}")
        if queue_during and text in queue_during:
            proc._queue_while_busy(queue_during[text])
        return heard.pop(0)

    async def retract() -> None:
        calls.append("retract")

    async def push(reply: str, direction: FrameDirection) -> None:
        spoken.append(reply)

    proc._run_turn = run_turn  # type: ignore[assignment]
    proc._retract_last_reply = retract  # type: ignore[assignment]
    proc._push_spoken_reply = push  # type: ignore[method-assign]
    proc._calls = calls  # type: ignore[attr-defined]
    return proc, spoken


class TestDrainTurns:
    """Only the final round's reply is spoken — the fix for the voice call
    where the caller heard the same offer twice: their continuation arrived
    while the first reply was still being generated, and both that reply and
    the follow-up's restatement of it were then read out back to back."""

    async def test_continuation_supersedes_the_unheard_reply(self) -> None:
        proc, spoken = _bridge(
            ["offer + ask name", "restated offer + ask name"],
            queue_during={"it can be a general checkup": "no emergency"},
        )
        await proc._drain_turns(
            "it can be a general checkup", FrameDirection.DOWNSTREAM
        )
        assert spoken == ["restated offer + ask name"]

    async def test_unheard_reply_is_retracted_before_the_follow_up_runs(
        self,
    ) -> None:
        """Order is the contract: the follow-up loads its history when it
        starts, so the retraction must already be on disk by then — or the
        model reads "no emergency" as the answer to "may I have your name?"."""
        proc, _ = _bridge(
            ["offer + ask name", "restated offer + ask name"],
            queue_during={"general checkup": "no emergency"},
        )
        await proc._drain_turns("general checkup", FrameDirection.DOWNSTREAM)
        assert proc._calls == [  # type: ignore[attr-defined]
            "run:general checkup",
            "retract",
            "run:no emergency",
        ]

    async def test_nothing_is_retracted_when_the_reply_is_spoken(self) -> None:
        proc, _ = _bridge(["the answer"])
        await proc._drain_turns("a question", FrameDirection.DOWNSTREAM)
        assert "retract" not in proc._calls  # type: ignore[attr-defined]

    async def test_reply_is_spoken_when_nobody_kept_talking(self) -> None:
        proc, spoken = _bridge(["the answer"])
        await proc._drain_turns("a question", FrameDirection.DOWNSTREAM)
        assert spoken == ["the answer"]

    async def test_echo_of_the_same_utterance_does_not_supersede(self) -> None:
        """STT re-emitting the turn's own text is machinery, not new speech."""
        proc, spoken = _bridge(
            ["the answer"], queue_during={"a question": "a question"}
        )
        await proc._drain_turns("a question", FrameDirection.DOWNSTREAM)
        assert spoken == ["the answer"]

    async def test_failed_turn_with_continuation_stays_silent_until_answered(
        self,
    ) -> None:
        """The fallback apology is as unheard as any reply: a caller still
        talking has not been answered yet, so only the follow-up speaks."""
        proc, spoken = _bridge(
            ["", "the real answer"], queue_during={"first": "second"}
        )
        await proc._drain_turns("first", FrameDirection.DOWNSTREAM)
        assert spoken == ["the real answer"]

    async def test_busy_flag_is_cleared_on_exit(self) -> None:
        proc, _ = _bridge(["the answer"])
        await proc._drain_turns("a question", FrameDirection.DOWNSTREAM)
        assert proc._busy is False
        assert proc._state["busy"] is False

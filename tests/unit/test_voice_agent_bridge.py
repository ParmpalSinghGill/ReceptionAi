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
    last_user_text,
    merge_utterances,
)


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

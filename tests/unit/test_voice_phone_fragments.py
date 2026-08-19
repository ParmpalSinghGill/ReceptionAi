"""Reassembling a phone number that arrived in pieces.

The bug these lock down was found by holding a real call: asked for a number,
the harness said "zero seven seven zero zero nine zero zero five five two" with
ordinary pauses, VAD closed a turn on each pause, and the agent answered the
first fragment while the rest were still arriving. The transcript showed the
caller giving the number and the agent asking for it again, twice.
"""

from __future__ import annotations

import pytest
from packages.voice.phone import (
    digits_in,
    is_complete_number,
    looks_like_phone_fragment,
    wants_phone_number,
)


class TestDigitsIn:
    def test_reads_spoken_digits(self) -> None:
        assert digits_in("zero seven seven double zero nine") == "077009"

    def test_oh_is_zero(self) -> None:
        """Callers say "oh" far more often than "zero"."""
        assert digits_in("oh seven seven oh oh") == "07700"

    def test_triple_repeats_three_times(self) -> None:
        assert digits_in("triple four") == "444"

    def test_reads_written_numerals(self) -> None:
        assert digits_in("07700 900552") == "07700900552"

    def test_ignores_surrounding_words(self) -> None:
        assert digits_in("my number is 07700900552 thanks") == "07700900552"

    def test_no_digits_is_empty(self) -> None:
        assert digits_in("no idea sorry") == ""


class TestIsCompleteNumber:
    @pytest.mark.parametrize("digits", ["07700900552", "0770090055"])
    def test_accepts_a_full_number(self, digits: str) -> None:
        assert is_complete_number(digits)

    @pytest.mark.parametrize("digits", ["", "077", "077009"])
    def test_rejects_a_partial_number(self, digits: str) -> None:
        assert not is_complete_number(digits)


class TestWantsPhoneNumber:
    @pytest.mark.parametrize(
        "reply",
        [
            "May I take your name and a contact number?",
            "What's the best phone number for you?",
            "Could I get your mobile?",
        ],
    )
    def test_detects_the_request(self, reply: str) -> None:
        assert wants_phone_number(reply)

    def test_ordinary_replies_do_not_trigger_buffering(self) -> None:
        """Buffering outside this question would make every call feel laggy."""
        assert not wants_phone_number("I can offer Tuesday at ten. Does that suit?")


class TestLooksLikePhoneFragment:
    @pytest.mark.parametrize(
        "fragment", ["zero seven seven", "zero zero nine", "five five two", "077"]
    )
    def test_bare_digit_runs_are_fragments(self, fragment: str) -> None:
        assert looks_like_phone_fragment(fragment)

    def test_digits_with_filler_are_still_fragments(self) -> None:
        assert looks_like_phone_fragment("my number is zero seven seven")

    def test_a_complete_number_is_not_held_back(self) -> None:
        """A caller who says it all at once must not wait out the timer."""
        assert not looks_like_phone_fragment("07700900552")
        assert not looks_like_phone_fragment(
            "zero seven seven zero zero nine zero zero five five two"
        )

    def test_a_sentence_is_not_a_fragment(self) -> None:
        assert not looks_like_phone_fragment("Tuesday at ten works for me")

    def test_a_lone_digit_is_not_a_fragment(self) -> None:
        """"I need it at 3" must reach the agent immediately."""
        assert not looks_like_phone_fragment("three")

    def test_digits_followed_by_real_words_are_not_a_fragment(self) -> None:
        assert not looks_like_phone_fragment("zero seven seven is my old number")

    def test_empty_is_not_a_fragment(self) -> None:
        assert not looks_like_phone_fragment("")


def test_the_observed_failure_reassembles() -> None:
    """The exact fragment sequence from the failing call, joined back up."""
    fragments = ["zero seven seven", "zero zero nine", "zero zero", "five five two"]
    for fragment in fragments:
        assert looks_like_phone_fragment(fragment), fragment

    joined = " ".join(fragments)
    assert digits_in(joined) == "07700900552"
    assert is_complete_number(digits_in(joined))

"""A suppressed claim explains the obstacle instead of apologising generically.

**The failure.** A caller typed a ten-digit phone number. `_normalise_phone`
refused it and the claim guard caught the model announcing a booking that had
not happened. Both mechanisms worked as designed. What the caller heard was:

    "Thank you, Raman. I'm sorry — I wasn't able to complete that just now,
     so nothing has changed. Shall I try again?"

"Shall I try again?" invites them to retype the identical number and fail
identically. The one fact needed to recover — *what was wrong with the number* —
was the one fact discarded, because the guard emitted a fixed constant with no
access to why the tool had failed.

(That particular number is now accepted outright: `_normalise_phone` takes a
plausible-but-unusual length rather than interrogating a caller about dialling
plans. The suppression bug it exposed is what this file guards, and it applies
to every `business_error`, not just phone numbers.)

A guard that turns a specific, recoverable error into a generic one destroys the
information the caller needs. That is worse than the over-claim it prevents,
because the over-claim was at least actionable by someone.
"""

from __future__ import annotations

import pytest
from packages.ai.nodes import _UNVERIFIED_REPLACEMENT, _UnverifiedClaimGuard
from packages.ai.tools.builtin.booking import _normalise_phone
from packages.core.errors import ValidationError

CLAIM = "I've booked your emergency appointment with Dr Okafor for 9:30am."


def test_a_suppressed_claim_carries_the_tools_own_explanation() -> None:
    """**The regression test.**

    The tool already produced a sentence written to be spoken; it simply never
    reached the replacement.
    """
    explanation = (
        "That number looks 1 digit short — tell the customer that and ask them "
        "to read it back."
    )
    guard = _UnverifiedClaimGuard(set(), set(), explanation)
    out = "".join(guard.feed(char) for char in CLAIM) + guard.flush()

    assert explanation in out, (
        f"The suppressed claim was replaced with a generic apology instead of "
        f"the tool's explanation: {out!r}"
    )
    assert _UNVERIFIED_REPLACEMENT not in out
    assert guard.rewrote


def test_the_generic_fallback_survives_for_a_bare_fabrication() -> None:
    """No tool failed, so there is genuinely nothing to explain.

    A model that claims a booking having called nothing at all gets the generic
    sentence, which is honest: the obstacle is not something the caller can act
    on, because there was no obstacle.
    """
    guard = _UnverifiedClaimGuard(set(), set())
    out = "".join(guard.feed(char) for char in CLAIM) + guard.flush()
    assert _UNVERIFIED_REPLACEMENT in out


def test_a_verified_claim_is_untouched_even_with_a_prior_failure() -> None:
    """An earlier failure must not suppress a later success.

    A caller who gives a bad number, corrects it, and is then booked should hear
    about the booking — not about the number they already fixed.
    """
    guard = _UnverifiedClaimGuard(
        {"booking.book_appointment"}, set(), "That number looked short."
    )
    out = "".join(guard.feed(char) for char in CLAIM) + guard.flush()
    assert out == CLAIM
    assert not guard.rewrote


@pytest.mark.parametrize(
    ("raw", "expected_clue"),
    [
        # Far too long to be a subscriber number anywhere.
        ("077009003211234567", "long"),
        ("abcdef", "did not catch a number"),
    ],
)
def test_the_phone_advice_names_the_actual_problem(
    raw: str, expected_clue: str
) -> None:
    """"I did not catch that number" is polite and useless.

    A caller who typed the wrong number of digits repeats the same ones.
    Counting them is something a person can check against what they typed;
    "must match E.164" is not.

    Note what is NOT tested here: a number that is merely the wrong length for
    this region is deliberately ACCEPTED rather than refused (see the lenient
    branch in `_normalise_phone`), on the reasoning that interrogating a caller
    about dialling plans is worse than storing a number staff will find wrong on
    the first call-back. These cases are the ones no country could explain.
    """
    with pytest.raises(ValidationError) as caught:
        _normalise_phone(raw, "GB")

    spoken = caught.value.model_message
    assert expected_clue in spoken.lower(), (
        f"{raw!r} produced {spoken!r}, which does not tell the caller what is "
        f"wrong with their number."
    )
    for jargon in ("e.164", "region", "gb'", "regex"):
        assert jargon not in spoken.lower()


def test_the_diagnostic_message_keeps_the_detail_for_the_log() -> None:
    """Two audiences, still. The log needs the value and the region."""
    with pytest.raises(ValidationError) as caught:
        _normalise_phone("077009003211234567", "GB")
    assert "077009003211234567" in caught.value.message
    assert caught.value.message != caught.value.model_message


def test_the_tool_loop_captures_a_business_error_for_the_guard() -> None:
    """The wiring, asserted at the source.

    A perfect guard that is never handed the explanation is the failure this
    fixes, and it is invisible in a unit test of the guard alone.
    """
    import inspect

    from packages.ai import nodes

    source = inspect.getsource(nodes.NodeFactory.llm_agent)
    assert 'status == "business_error"' in source, (
        "The tool loop does not capture an expected failure, so the guard has "
        "nothing to explain with."
    )
    assert "failure=failure_message" in source, (
        "The captured failure is not passed to the reply guard."
    )

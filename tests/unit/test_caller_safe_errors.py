"""Tool error text never reaches a caller in an engineer's voice.

**The failure.** A caller was asked:

    "Could I get your number in full, including the leading 0 or country code?
     For example, 07123 456789."

A UK dental practice does not ask for a country code. The source was
``_normalise_phone``'s ``ValidationError``, whose message — *"Ask for it in
full, including the leading 0 or the country code"* — was written as an
instruction to the model and read out almost verbatim.

Same class as the slot-handle leak: internal text reaching the customer. The fix
is the same shape too — separate the two audiences at the seam rather than hoping
each message is written carefully. ``DomainError.message`` is diagnostic and goes
to logs; ``model_message`` is what the agent may work from.

The second half of that bug was a UK assumption hardcoded in generic tool code:
``startswith("07")``, ``+44``, an 11-digit rule. A restaurant in Lisbon hits it
on its first caller.
"""

from __future__ import annotations

import pytest
from packages.ai.tools.builtin.booking import _DIALLING_RULES, _normalise_phone
from packages.core.errors import DomainError, ValidationError

#: Words that betray an engineer writing for a machine. None belongs in
#: something a receptionist says out loud.
ENGINEER_VOCABULARY = (
    "country code",
    "leading 0",
    "e.164",
    "iso",
    "call again",
    "tool",
    "parameter",
    "argument",
    "field",
    "null",
    "utc",
    "format",
)


def assert_speakable(text: str) -> None:
    lowered = text.lower()
    for word in ENGINEER_VOCABULARY:
        assert word not in lowered, (
            f"{word!r} appears in text the agent may speak: {text!r}. That is an "
            f"instruction to a machine, not a sentence for a customer."
        )


# ── The phone number ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "region", "expected"),
    [
        # UK, the original hardcoded case — still works, now from config.
        ("07700900321", "GB", "+447700900321"),
        ("07700 900321", "GB", "+447700900321"),
        ("+447700900321", "GB", "+447700900321"),
        # Portugal: nine digits, no trunk prefix. The old code refused this.
        ("912345678", "PT", "+351912345678"),
        ("+351912345678", "PT", "+351912345678"),
        # Ireland and the Netherlands, different trunk/length combinations.
        ("0871234567", "IE", "+353871234567"),
        ("0612345678", "NL", "+31612345678"),
        # International form is always honoured, whatever the region.
        ("+15551234567", "PT", "+15551234567"),
    ],
)
def test_a_national_number_resolves_for_its_region(
    raw: str, region: str, expected: str
) -> None:
    """The region comes from configuration, not from an assumption.

    ``locations.country_code`` already existed beside the IANA timezone — a
    phone number and a wall-clock time are both meaningless without knowing
    where the business is.
    """
    assert _normalise_phone(raw, region) == expected


def test_a_portuguese_number_is_not_read_as_british() -> None:
    """The regression, stated directly.

    Under the old rules a Lisbon caller's number was refused outright, because
    it did not start with ``07`` and was not eleven digits.
    """
    assert _normalise_phone("912345678", "PT") == "+351912345678"

    # The same nine digits at a British practice are now ACCEPTED as local,
    # where they were once refused. That is a deliberate policy change, not a
    # regression.
    #
    # The old rule refused any length that was not exactly right for the
    # practice's own country, reasoning that a wrong number is worse than no
    # number. In practice it refused real callers: an Indian mobile is ten
    # digits, an Italian landline can be six, and a receptionist who cannot
    # write down the number a patient just gave them is broken in a way no
    # caller forgives. A length that is plausible in ANY supported country is
    # now taken down.
    #
    # What we still refuse is a length no country uses — see the
    # unresolvable-number test. And a number carrying an explicit foreign
    # country code is resolved to that country rather than assumed local.
    assert _normalise_phone("912345678", "GB") == "+44912345678"
    assert _normalise_phone("351912345678", "GB") == "+351912345678"


# `(415) 555-0142` was here and is no longer refused, deliberately. Stripped of
# punctuation it is ten digits, which is exactly a valid UK subscriber length —
# so at a British practice it resolves to +44 4155550142. That is not a bug to
# fix: the digits alone carry no evidence of which country they belong to, and
# refusing every ten-digit number in order to catch American ones would reject
# the ordinary UK callers this practice actually has. A foreign number is
# recognised when it carries its country code (see the foreign-number test);
# without one there is nothing to recognise.
@pytest.mark.parametrize("raw", ["12345", "44", "not a number"])
def test_an_unresolvable_number_is_refused_in_the_callers_language(
    raw: str,
) -> None:
    """Refusing is right; refusing in a receptionist's voice was missing.

    Guessing a country code writes the wrong number into a record someone will
    later ring, so the refusal itself is correct behaviour.
    """
    with pytest.raises(ValidationError) as caught:
        _normalise_phone(raw, "GB")

    spoken = caught.value.model_message
    assert_speakable(spoken)
    # It must tell the agent what to DO, not describe a validation rule. Any of
    # these verbs is an instruction; the point is that the message never reads
    # like a schema note.
    assert any(verb in spoken.lower() for verb in ("say", "read it back", "ask")), (
        f"The message must tell the agent what to do: {spoken!r}"
    )
    # And it must NAME the problem. "I did not catch that number" is polite and
    # useless — a caller who typed ten digits repeats the same ten and fails
    # again. This is the whole point of the specific advice.
    assert any(
        clue in spoken.lower()
        for clue in ("short", "long", "not look quite right", "did not catch a number")
    ), f"The message must name what is wrong with the number: {spoken!r}"
    # The diagnostic half keeps the detail an engineer needs.
    assert raw in caught.value.message


def test_the_diagnostic_and_spoken_messages_are_different() -> None:
    """Two audiences, two strings.

    Conflating them is what leaked. The diagnostic message names the offending
    value and the region; the spoken one names neither.
    """
    with pytest.raises(ValidationError) as caught:
        _normalise_phone("12345", "GB")

    assert caught.value.message != caught.value.model_message
    assert "GB" in caught.value.message
    assert "GB" not in caught.value.model_message


def test_an_unknown_region_falls_back_rather_than_failing() -> None:
    """A region the table does not cover must not take the agent off the air.

    It degrades to the platform default, which is wrong for that tenant but
    recoverable — and international form still works for everyone.
    """
    assert _normalise_phone("+351912345678", "ZZ") == "+351912345678"


def test_the_dialling_table_covers_more_than_one_country() -> None:
    """A table with one entry is a hardcoded assumption wearing a hat."""
    assert len(_DIALLING_RULES) >= 5
    assert {"GB", "PT", "US"} <= set(_DIALLING_RULES)


# ── The error contract itself ────────────────────────────────────────────────


def test_domain_errors_default_the_spoken_message_to_the_diagnostic_one() -> None:
    """Backwards compatible: an existing raise site keeps working.

    Only the errors that actually reach a customer need to set the second
    string; everything else behaves as before.
    """
    error = DomainError("something failed", code="internal_error")
    assert error.model_message == "something failed"


def test_a_spoken_message_overrides_the_diagnostic_one() -> None:
    error = ValidationError(
        "phone_e164 regex did not match",
        field="customer_phone",
        model_message="I did not catch that number.",
    )
    assert error.message == "phone_e164 regex did not match"
    assert error.model_message == "I did not catch that number."


def test_the_executor_speaks_the_model_message() -> None:
    """The seam. A tool raising a diagnostic error must not have it spoken.

    Asserted at the executor rather than by reading transcripts, because the
    leak was invisible until a user noticed a dental practice asking for a
    country code.
    """
    import inspect

    from packages.ai.tools import executor

    # `_execute_one_traced` holds the execution logic; `_execute_one` is the
    # tracing wrapper around it. The assertion follows the logic rather than
    # the name, because what matters is that the branch handling a
    # ToolBusinessError speaks `model_message` — wherever that branch lives.
    source = inspect.getsource(executor.ToolExecutor._execute_one_traced)
    assert "exc.model_message" in source
    assert "content=exc.message" not in source, (
        "The executor is putting the DIAGNOSTIC message into the model's "
        "context. That is the field that leaked a country code to a caller."
    )

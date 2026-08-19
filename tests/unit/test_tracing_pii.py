"""Tracing must not leak PII — contract §8, 05 §D14.5.

The failure this guards against is specific and unrecoverable: a patient's
phone number or name written into a trace store. Self-hosting Langfuse means
the data stays inside our network, but "it's local" is not a reason to capture
raw PII — a local dashboard is still read by people with no clinical
relationship to the caller, and a trace store is still a copy of the
conversation that the erasure path (05 §B6.4) has to reach.

So the tests below assert on **output**, not on branches: given content that
contains a name and an Indian mobile number, does that string appear anywhere
in what would be sent? That question stays meaningful when the implementation
is refactored, which "was the mask function called" does not.
"""

from __future__ import annotations

import json
from typing import Any

from packages.core.ports.llm import Message
from packages.core.ports.tracing import Capture, NoOpTracer, TracingPolicy
from packages.observability.redaction import mask_content, scrub, summarize

# A realistic turn for the demo tenant: Sunrise Dental, Pune. The phone number
# is in the +91 form a caller in India actually gives.
PATIENT_NAME = "Ananya Deshpande"
PATIENT_PHONE = "+91 98765 43210"
PATIENT_EMAIL = "ananya.deshpande@example.com"

CONVERSATION = [
    Message(role="system", content="You are the receptionist for Sunrise Dental."),
    Message(
        role="user",
        content=(
            f"Hi, this is {PATIENT_NAME}, my number is {PATIENT_PHONE}. "
            f"You can also email me at {PATIENT_EMAIL}. I have a chipped molar "
            f"and it's been aching since Tuesday."
        ),
    ),
]


def _blob(value: Any) -> str:
    """Everything the value would serialize to, as one searchable string."""
    return json.dumps(value, default=str)


# ── The default ───────────────────────────────────────────────────────────


def test_the_default_policy_masks() -> None:
    """A policy nobody configured must not capture content.

    This is the single most important assertion in the file. Every code path
    that forgets to look up a tenant's setting constructs a bare
    `TracingPolicy()`, and that must be the safe one.
    """
    assert TracingPolicy().capture is Capture.MASKED


def test_masked_capture_hides_name_phone_and_email() -> None:
    masked = mask_content(CONVERSATION, TracingPolicy())
    blob = _blob(masked)

    assert PATIENT_NAME not in blob
    assert PATIENT_PHONE not in blob
    assert PATIENT_EMAIL not in blob
    assert "chipped molar" not in blob
    # Structure survives, because a trace with no shape is unreadable.
    assert masked["messages"] == 2
    assert masked["roles"] == {"system": 1, "user": 1}


def test_masked_capture_keeps_the_shape_an_engineer_needs() -> None:
    """Masking is not the same as recording nothing.

    A latency investigation needs to know the prompt was large; it does not
    need to know what it said.
    """
    masked = summarize(CONVERSATION)
    assert masked["chars"] > 100
    assert masked["redacted"] is True


def test_none_capture_records_nothing_at_all() -> None:
    assert mask_content(CONVERSATION, TracingPolicy(capture=Capture.NONE)) is None


# ── Opt-in capture ────────────────────────────────────────────────────────


def test_full_capture_still_scrubs_phone_and_email() -> None:
    """Opting in is consent to read the conversation, not to store identifiers.

    A tenant who turns on prompt capture wants to debug their agent's wording.
    That is not a reason to keep a payment instrument or a phone number in a
    trace store, so the regex backstop runs even here.
    """
    captured = mask_content(CONVERSATION, TracingPolicy(capture=Capture.FULL))
    blob = _blob(captured)

    assert PATIENT_PHONE not in blob
    assert PATIENT_EMAIL not in blob
    assert "[phone]" in blob
    assert "[email]" in blob
    # The clinical content IS present — that is what was opted into.
    assert "chipped molar" in blob


def test_full_capture_truncates_to_the_policy_limit() -> None:
    long_prompt = "a" * 50_000
    captured = mask_content(
        long_prompt, TracingPolicy(capture=Capture.FULL, max_chars=1_000)
    )
    assert len(captured) < 1_200
    assert "truncated" in captured


def test_scrub_handles_the_indian_mobile_formats() -> None:
    for raw in (
        "+91 98765 43210",
        "+919876543210",
        "098765-43210",
        "9876543210",
    ):
        assert raw not in scrub(f"call me on {raw} please"), raw


def test_tool_arguments_are_reduced_to_names() -> None:
    """05 §D14.5 forbids `ai.tool.arguments` outright.

    A tool call's arguments are whatever the model extracted from the
    conversation — for a dental intake, that is health data.
    """
    message = Message(
        role="assistant",
        content="Booking that now.",
        tool_calls=(
            type(
                "Call",
                (),
                {
                    "id": "c1",
                    "name": "book_appointment",
                    "arguments": {
                        "patient_name": PATIENT_NAME,
                        "phone": PATIENT_PHONE,
                    },
                },
            )(),
        ),
    )
    captured = mask_content([message], TracingPolicy(capture=Capture.FULL))
    blob = _blob(captured)

    assert "book_appointment" in blob
    assert PATIENT_NAME not in blob
    assert PATIENT_PHONE not in blob


# ── The no-op ─────────────────────────────────────────────────────────────


def test_the_noop_tracer_absorbs_content_without_storing_it() -> None:
    """With no backend configured, handing content to a span must be inert.

    This is the path every test run and every un-configured deployment takes,
    so it is also the path where a leak would be least likely to be noticed.
    """
    tracer = NoOpTracer()
    assert tracer.enabled is False

    with tracer.start_generation("llm.chat", model="m", provider="p") as span:
        span.set_content(input=CONVERSATION, output="Booked for Tuesday.")
        span.set_attribute("tenant.id", "abc")
        span.set_error(ValueError("boom"))

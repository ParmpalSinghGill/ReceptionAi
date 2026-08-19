"""The model is told what day it is — and told loudly when it gets it wrong.

**A model does not know the date.** It infers "today" from training data that is
months or years stale. Without an explicit date in the prompt, every relative
date a caller uses resolves to the wrong day, and relative dates are how people
book appointments: "Tuesday morning", "tomorrow afternoon", "next week".

The observed failure was not graceful. The agent asked for a Tuesday that had
already passed; ``_resolve_window`` clamped the start up to now while leaving the
end in the past, producing an inverted window; the error said "the end must be
after its start", which is *false from the model's point of view* because the end
it sent genuinely was after the start it sent. Told something untrue and
uncorrectable, it retried the identical call six times and gave up.

So there are two properties here and both are needed. Grounding alone leaves the
tool silently unhelpful when a date is still wrong for some other reason; a good
error alone leaves the model guessing the date on every single turn.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from packages.ai.prompts import PromptAssembler
from packages.verticals import load_manifest_file
from packages.verticals.manifests import manifest_path

#: A fixed instant, so every assertion below is exact rather than "today-ish".
#: Friday, 7 August 2026, 14:30 UTC — which is 15:30 in Europe/London (BST).
FRIDAY = datetime(2026, 8, 7, 14, 30, tzinfo=UTC)


@pytest.fixture(scope="module")
def assembler() -> PromptAssembler:
    return PromptAssembler(manifest=load_manifest_file(manifest_path("medical")))


def _system_text(assembler: PromptAssembler, **kwargs: object) -> str:
    manifest = assembler.manifest
    prompt = assembler.assemble(
        agent=manifest.graph.agent("scheduler"),
        history=[],
        **kwargs,  # type: ignore[arg-type]
    )
    return prompt.messages[0].content or ""


def test_the_prompt_states_todays_date(assembler: PromptAssembler) -> None:
    """The fix for the whole class of bug, in one assertion.

    Spelled out with the weekday name rather than as an ISO date: a model
    resolves "the next Tuesday after Friday" far more reliably than it resolves
    "the next Tuesday after 2026-08-07", because the second requires it to work
    out the weekday first — which is the step it gets wrong.
    """
    text = _system_text(assembler, timezone="Europe/London", now=FRIDAY)
    assert "Friday 7 August 2026" in text
    assert "Today is" in text


def test_the_date_is_rendered_in_the_business_timezone(
    assembler: PromptAssembler,
) -> None:
    """The clock the caller and the practice share, not UTC.

    Just after midnight local in a positive-offset zone, UTC is still on the
    previous day. An agent working from UTC would offer "tomorrow" as the day it
    is already — and would file the booking under the wrong local date.
    """
    just_after_midnight_bst = datetime(2026, 8, 7, 23, 30, tzinfo=UTC)  # 00:30 on the 8th
    text = _system_text(
        assembler, timezone="Europe/London", now=just_after_midnight_bst
    )
    assert "Saturday 8 August 2026" in text, (
        "The date must be the business's local date. UTC still says the 7th here."
    )
    assert "Europe/London" in text


def test_the_prompt_says_a_weekday_means_the_next_one(
    assembler: PromptAssembler,
) -> None:
    """The specific ambiguity that produced the bug.

    "Tuesday" said on a Friday is genuinely ambiguous in English and models
    resolve it inconsistently. One sentence removes the entire class of wrong
    answer, so it is worth asserting that the sentence is actually there.
    """
    text = _system_text(assembler, timezone="Europe/London", now=FRIDAY)
    assert "NEXT occurrence" in text
    assert "never one that has passed" in text.replace("\n", " ")
    assert "Tomorrow" in text and "Saturday 8 August 2026" in text


def test_the_temporal_layer_is_above_everything_truncatable(
    assembler: PromptAssembler,
) -> None:
    """Ordering is the security and correctness model here (03 §7.1).

    The date is not context, it is a precondition for reading any instruction
    that mentions a day. Sitting below a truncatable layer would let a long
    tenant customization push it out of the prompt on exactly the conversations
    that are complex enough to need it.
    """
    manifest = assembler.manifest
    prompt = assembler.assemble(
        agent=manifest.graph.agent("scheduler"),
        history=[],
        timezone="Europe/London",
        now=FRIDAY,
    )
    layers = list(prompt.layer_names)
    assert layers[0] == "platform"
    assert layers[1] == "temporal"
    assert layers.index("temporal") < layers.index("role")


def test_an_unknown_timezone_degrades_rather_than_failing(
    assembler: PromptAssembler,
) -> None:
    """A bad zone in one tenant's config must not take their agent off the air.

    It degrades that tenant's date handling to UTC and logs; the booking engine
    still refuses anything genuinely out of hours, so the blast radius is "times
    are offered in the wrong clock", not "the receptionist does not answer".
    """
    text = _system_text(assembler, timezone="Mars/Olympus_Mons", now=FRIDAY)
    assert "Today is" in text
    assert "UTC" in text


# ── The tool-side half: a past window fails loudly ───────────────────────────


class _FakeRepo:
    async def default_location_id(self) -> str:
        return "loc"

    async def location_timezone(self, _location: str) -> str:
        return "Europe/London"


class _FakeService:
    repository = _FakeRepo()


async def test_a_window_entirely_in_the_past_says_so_and_names_today() -> None:
    """The error the model can actually act on.

    Three things must be in the message, and each was missing before:

    * that the window has **passed** — the real condition, not a derived
      "inverted range" that reads as false to the caller who sent it;
    * **today's date**, so the model can compute the right one without another
      round trip;
    * the weekday rule, because a past weekday is overwhelmingly the cause.
    """
    from packages.ai.tools.builtin.booking import _resolve_window
    from packages.core.errors import ValidationError

    with pytest.raises(ValidationError) as caught:
        await _resolve_window(
            _FakeService(),
            {
                # The Tuesday that already happened — exactly what the model sent.
                "window_start": "2026-08-04T08:00",
                "window_end": "2026-08-04T12:00",
            },
            ctx=None,  # type: ignore[arg-type]
        )

    # `model_message`, not `message`: the first is what the agent works from,
    # the second is diagnostic. They were one field until a phone-format
    # instruction written for the model was read to a caller verbatim.
    message = caught.value.model_message
    assert "already passed" in message
    assert "today is" in message.lower()
    # The date is present and spoken, so the model can use it directly.
    today = datetime.now(tz=UTC).astimezone().strftime("%A")
    assert today in message or "day" in message.lower()
    assert "NEXT" in message, (
        "The weekday rule must be restated in the error. A past weekday is the "
        "overwhelmingly common cause, and the correction has to arrive with the "
        "failure or the model repeats the same call."
    )


async def test_the_past_window_error_is_distinct_from_an_inverted_one() -> None:
    """Two different mistakes, two different fixes, two different messages.

    Collapsing them is what made the original failure unrecoverable: a model told
    "the end must be after the start" about a request whose end *was* after its
    start has no way to work out what is actually wrong.
    """
    from packages.ai.tools.builtin.booking import _resolve_window
    from packages.core.errors import ValidationError

    future = datetime.now(tz=UTC) + timedelta(days=3)
    later = future + timedelta(hours=2)

    with pytest.raises(ValidationError) as caught:
        await _resolve_window(
            _FakeService(),
            {
                "window_start": later.strftime("%Y-%m-%dT%H:%M"),
                "window_end": future.strftime("%Y-%m-%dT%H:%M"),
            },
            ctx=None,  # type: ignore[arg-type]
        )

    message = caught.value.model_message
    assert "back to front" in message
    assert "already passed" not in message
    # Today is named here too — it costs nothing and it is always useful.
    assert "Today is" in message


async def test_a_future_window_is_accepted_unchanged() -> None:
    """The happy path still works. A guard that rejects everything is not a fix."""
    from packages.ai.tools.builtin.booking import _resolve_window

    future = datetime.now(tz=UTC) + timedelta(days=3)
    start, end, tz = await _resolve_window(
        _FakeService(),
        {
            "window_start": future.strftime("%Y-%m-%dT%H:%M"),
            "window_end": (future + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M"),
        },
        ctx=None,  # type: ignore[arg-type]
    )
    assert end > start
    assert tz == "Europe/London"


async def test_omitting_the_window_searches_forward_from_now() -> None:
    """"Have you got anything?" must not be an error.

    No start means "as soon as possible"; no end means "the next week". Both
    defaulting to one instant would make every unqualified request return
    nothing, which reads to a caller as "we are fully booked forever".
    """
    from packages.ai.tools.builtin.booking import _resolve_window

    before = datetime.now(tz=UTC)
    start, end, _ = await _resolve_window(_FakeService(), {}, ctx=None)  # type: ignore[arg-type]
    assert start >= before - timedelta(seconds=5)
    assert end > start + timedelta(days=5)

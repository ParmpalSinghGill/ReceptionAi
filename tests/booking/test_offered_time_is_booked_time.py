"""The time offered is the time booked — exactly, to the second.

**The bug this file exists to prevent, in full.** ``check_availability`` offered
"1:30 this afternoon". The model composed ``2026-08-10T13:30:00+00:00`` and
``book_appointment`` stored it verbatim. That is a well-formed instant, and in
BST it is **2:30pm London**. An emergency patient with a broken tooth would have
arrived an hour late, or the practice would have held a slot nobody came to.

Nothing reported a failure. The engine did exactly what it was told; the
*interface* let the model tell it something wrong, by accepting a UTC offset the
model had to compute from a wall clock it had just read aloud. A model doing
timezone arithmetic is right most of the time — and "silently wrong sometimes"
is worse than "always wrong", because only the second one gets noticed.

The fix makes the mistake impossible rather than unlikely: the engine mints a
reference for every slot it offers, and the model books that. These tests assert
the property directly against the database row, in both UTC and practice-local
terms, because the whole failure was invisible in the transcript.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from packages.ai.tools.builtin.booking import (
    _decode_slot_ref,
    _mint_slot_ref,
    _to_utc,
)
from packages.booking.service import BookingService
from packages.persistence.repositories.booking import BookingRepository
from sqlalchemy import text

from tests.booking.conftest import TZ, Fixture

pytestmark = [pytest.mark.integration]

LONDON = ZoneInfo(TZ)


async def test_the_offered_slot_is_the_booked_slot_to_the_second(
    fixture: Fixture,
) -> None:
    """**The regression test.** Offer a slot, book its reference, compare rows.

    Asserted three ways, because the failure looked correct in two of them:

    * the stored UTC instant equals the offered UTC instant;
    * the stored instant rendered in the practice's zone equals the wall-clock
      time the caller was told;
    * the local hour is the one a human would say — the check that would have
      caught "1:30 offered, 2:30 booked" on its own.
    """
    day = Fixture.next_weekday(2)
    window_start = Fixture.local(day, time(9, 0))
    window_end = Fixture.local(day, time(17, 0))

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        slots, spec, _location, _tz, _resources = await service.find_slots(
            service_code="standard",
            window_start=window_start,
            window_end=window_end,
            limit=5,
        )
        assert slots, "the fixture practice must have availability to offer"

        offered = slots[0]
        # What the caller is told, and what the engine actually computed.
        offered_local = offered.start_at.astimezone(LONDON)
        reference = _mint_slot_ref(offered, spec.code)

        # The model hands the reference back rather than retyping a time.
        decoded = _decode_slot_ref(reference)
        assert decoded is not None
        assert decoded.start_at == offered.start_at, (
            "The reference must carry the engine's own instant. If it round-trips "
            "to anything else, the model is booking a time nobody offered."
        )

        booking = await service.book(
            service_code=spec.code,
            start_at=decoded.start_at,
            customer_name="Slot Reference Test",
        )

    async with fixture.unit_of_work() as session:
        row = (
            await session.execute(
                text(
                    "SELECT starts_at, "
                    "       starts_at AT TIME ZONE 'Europe/London' AS local_start "
                    "  FROM bookings WHERE tenant_id = :tid AND id = :bid"
                ),
                {"tid": fixture.tenant_id, "bid": booking.id},
            )
        ).mappings().one()

    assert row["starts_at"] == offered.start_at, (
        f"UTC drift: offered {offered.start_at.isoformat()}, "
        f"stored {row['starts_at'].isoformat()}."
    )
    assert row["local_start"].hour == offered_local.hour, (
        f"The caller was offered {offered_local:%H:%M} local and the row says "
        f"{row['local_start']:%H:%M} local. This is the one-hour error that sends "
        f"an emergency patient to the wrong time."
    )
    assert row["local_start"].minute == offered_local.minute
    assert booking.local_start.hour == offered_local.hour


async def test_the_offered_clinician_is_the_booked_clinician(
    fixture: Fixture,
) -> None:
    """Offering one resource and assigning another is a lie caught at reception.

    The reported failure offered Dr Okafor and booked Dr Rahman. The reference
    carries the allocation the solver chose, so the resource named in the offer
    is the resource on the row.
    """
    day = Fixture.next_weekday(2)

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        slots, spec, _location, _tz, resources = await service.find_slots(
            service_code="standard",
            window_start=Fixture.local(day, time(9, 0)),
            window_end=Fixture.local(day, time(17, 0)),
            limit=5,
        )
        offered = slots[0]
        offered_names = {
            resources[r].name for r in offered.resource_ids if r in resources
        }
        reference = _mint_slot_ref(offered, spec.code)
        decoded = _decode_slot_ref(reference)
        assert decoded is not None
        assert set(decoded.resource_ids) == set(offered.resource_ids), (
            "The reference must carry the resources that were offered, or the "
            "booking can land on a different person than the one named."
        )

        booking = await service.book(
            service_code=spec.code,
            start_at=decoded.start_at,
            customer_name="Resource Continuity Test",
        )

    assert set(booking.resource_names) == offered_names, (
        f"Offered {sorted(offered_names)} but booked "
        f"{sorted(booking.resource_names)}. A caller told they are seeing one "
        f"clinician and booked with another finds out at reception."
    )


def test_a_utc_offset_on_a_spoken_time_is_read_as_local() -> None:
    """The safety net for the free-text path.

    A model that has just said "1:30 this afternoon" and then emits
    ``13:30:00+00:00`` has not converted anything — it appended a zone it did not
    compute. Honouring that literally is what booked an hour late. Since no
    caller ever means UTC when they say "half one", local is the only reading
    that is ever right.
    """
    as_local = _to_utc("2026-08-10T13:30:00+00:00", TZ)
    assert as_local.astimezone(LONDON).hour == 13, (
        "A +00:00 offset on a spoken time must be read as practice-local. "
        "Taking it literally shifts every BST booking an hour."
    )
    # And it agrees with the bare form, which is the same assertion by the model.
    assert as_local == _to_utc("2026-08-10T13:30:00", TZ)


def test_a_real_offset_is_still_honoured() -> None:
    """A non-zero offset is a deliberate assertion, not an accident.

    Nothing produces ``+05:30`` by mistake, so a channel that resolved the zone
    itself is trusted. Only the ``+00:00`` case is treated as unconverted.
    """
    parsed = _to_utc("2026-08-10T13:30:00+05:30", TZ)
    assert parsed == datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


def test_a_reference_from_another_process_is_refused_not_guessed() -> None:
    """A reference that fails its digest must not decode.

    Refusing sends the agent back to ``check_availability``, which it knows how
    to recover from. Guessing would book a plausible-looking wrong instant, which
    is the exact class of silent failure this mechanism removes.
    """
    assert _decode_slot_ref("not-a-reference") is None
    assert _decode_slot_ref("") is None
    # Correct shape, wrong signature.
    assert _decode_slot_ref("aGVsbG8.deadbeef") is None


async def test_a_dst_boundary_slot_round_trips_exactly(
    fixture: Fixture,
) -> None:
    """The case where an hour genuinely goes missing.

    Across a DST transition a wall-clock time and a UTC instant disagree by an
    amount that changes mid-week — which is precisely when a model's arithmetic
    fails and precisely when nobody notices. The reference carries the instant,
    so the transition is irrelevant to it.
    """
    # The next transition, found rather than hardcoded so this keeps testing a
    # real boundary after the date passes.
    cursor = datetime.now(tz=LONDON).date() + timedelta(days=4)
    for _ in range(400):
        today_offset = datetime.combine(cursor, time(12), tzinfo=LONDON).utcoffset()
        next_offset = datetime.combine(
            cursor + timedelta(days=1), time(12), tzinfo=LONDON
        ).utcoffset()
        if today_offset != next_offset:
            break
        cursor += timedelta(days=1)

    after = cursor + timedelta(days=1)
    while after.isoweekday() > 5:
        after += timedelta(days=1)

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        slots, spec, _loc, _tz, _res = await service.find_slots(
            service_code="standard",
            window_start=Fixture.local(after, time(9, 0)),
            window_end=Fixture.local(after, time(17, 0)),
            limit=3,
        )
        if not slots:
            pytest.skip("no availability past the DST boundary in this window")

        offered = slots[0]
        offered_local = offered.start_at.astimezone(LONDON)
        decoded = _decode_slot_ref(_mint_slot_ref(offered, spec.code))
        assert decoded is not None

        booking = await service.book(
            service_code=spec.code,
            start_at=decoded.start_at,
            customer_name="DST Round Trip",
        )

    assert booking.start_at == offered.start_at
    assert booking.local_start.hour == offered_local.hour
    assert booking.local_start.minute == offered_local.minute


# ── Customer-level duplicates ────────────────────────────────────────────────
#
# One phone number accumulated thirty-five confirmed bookings, including pairs
# at the SAME instant with different clinicians. The exclusion constraint cannot
# see that: it protects a resource's timeline, and two clinicians at 11:00 is
# not a resource conflict. A customer booking themselves twice is a different
# question, and nothing was asking it.


async def test_an_existing_booking_is_found_by_phone(fixture: Fixture) -> None:
    """The lookup the agent uses before making a second booking."""
    day = Fixture.next_weekday(1)

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        await service.book(
            service_code="standard",
            start_at=Fixture.local(day, time(9, 0)),
            customer_name="Repeat Caller",
            customer_phone_e164="+447700900777",
        )

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        found = await service.existing_for_phone(
            phone_e164="+447700900777", within_days=3
        )

    assert len(found) == 1
    assert found[0].customer_phone_e164 == "+447700900777"


async def test_a_booking_beyond_the_window_is_not_flagged(
    fixture: Fixture,
) -> None:
    """The window is what makes this a duplicate check rather than a history.

    A check-up booked for next month is not a reason to question today's
    emergency appointment.
    """
    far = Fixture.next_weekday(20)

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        await service.book(
            service_code="standard",
            start_at=Fixture.local(far, time(9, 0)),
            customer_name="Distant Caller",
            customer_phone_e164="+447700900778",
        )

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        assert (
            await service.existing_for_phone(
                phone_e164="+447700900778", within_days=3
            )
            == []
        )


async def test_a_cancelled_booking_is_not_a_duplicate(fixture: Fixture) -> None:
    """Someone rebooking after cancelling is not booking twice."""
    day = Fixture.next_weekday(1)

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        booking = await service.book(
            service_code="standard",
            start_at=Fixture.local(day, time(9, 0)),
            customer_name="Cancelled Caller",
            customer_phone_e164="+447700900779",
        )
        await service.cancel(booking_id=booking.id, accept_fee=True)

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        assert (
            await service.existing_for_phone(
                phone_e164="+447700900779", within_days=3
            )
            == []
        )


async def test_the_check_can_be_disabled(fixture: Fixture) -> None:
    """``within_days = 0`` turns it off.

    Correct for a business where repeat bookings are the norm — a restaurant may
    legitimately take three reservations in a week from one number.
    """
    day = Fixture.next_weekday(1)

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        await service.book(
            service_code="standard",
            start_at=Fixture.local(day, time(9, 0)),
            customer_name="Frequent Caller",
            customer_phone_e164="+447700900780",
        )
        assert (
            await service.existing_for_phone(
                phone_e164="+447700900780", within_days=0
            )
            == []
        )


async def test_the_check_never_blocks_a_second_booking(
    fixture: Fixture,
) -> None:
    """**"Keep both" must actually work.**

    The easy wrong answer is a hard block. Someone with a broken tooth on
    Tuesday and a hygienist visit on Thursday is normal, and refusing them is
    worse than the duplicate. The check reports; it does not refuse.
    """
    day = Fixture.next_weekday(1)

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        first = await service.book(
            service_code="standard",
            start_at=Fixture.local(day, time(9, 0)),
            customer_name="Two Appointments",
            customer_phone_e164="+447700900781",
        )
        second = await service.book(
            service_code="standard",
            start_at=Fixture.local(day, time(14, 0)),
            customer_name="Two Appointments",
            customer_phone_e164="+447700900781",
        )

    assert first.id != second.id
    assert second.status == "confirmed", (
        "A second booking for the same number must still be possible. The "
        "check informs the conversation; it is not a constraint."
    )

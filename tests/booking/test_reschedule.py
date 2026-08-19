"""Reschedule is atomic — the original survives a failed move.

**The failure this file exists to prevent.** The obvious way to move a booking is
cancel-then-rebook. It has a window — however short — in which the original slot
is released and the new one has not been claimed. If the new one is taken in that
window, the caller ends up with *nothing*, having started the call with
something. That is the worst outcome a booking system can produce and it is
entirely avoidable.

The engine instead moves in place: one ``UPDATE`` on ``bookings.service_period``,
whose ``trg_bookings_cascade_period`` trigger carries the new geometry onto every
child item, where ``ex_booking_items__no_overlap`` re-evaluates it. Either the
update commits — booking moved, old slot released, new slot held, at one instant
— or it raises and rolls back, leaving the original completely intact. There is
no third outcome, and the tests below assert exactly that.
"""

from __future__ import annotations

from datetime import time, timedelta

import pytest
from packages.booking.service import BookingService, SlotUnavailableError
from packages.core.errors import ValidationError
from packages.persistence.repositories.booking import BookingRepository
from sqlalchemy import text

from tests.booking.conftest import Fixture

pytestmark = [pytest.mark.integration]


async def _narrow(fixture: Fixture) -> None:
    async with fixture.unit_of_work() as session:
        await session.execute(
            text(
                "UPDATE resources SET is_bookable = FALSE "
                " WHERE tenant_id = :tid AND code IN ('p-two','space-two')"
            ),
            {"tid": fixture.tenant_id},
        )


async def test_a_successful_reschedule_moves_the_same_booking(
    fixture: Fixture,
) -> None:
    """The reference does not change, because the appointment did not.

    A caller who was read a reference an hour ago must still be able to quote it.
    Issuing a new one on every move is how a customer ends up with three
    references for one appointment and no idea which is live.
    """
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))
    new_start = Fixture.local(day, time(14, 0))

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        booking = await service.book(
            service_code="standard", start_at=start, customer_name="Mover"
        )
        moved = await service.reschedule(
            booking_id=booking.id, new_start_at=new_start
        )

    assert moved.id == booking.id
    assert moved.reference == booking.reference
    assert moved.start_at == new_start
    assert moved.status == "confirmed"

    # And the items moved with it — the cascade trigger, not application code.
    async with fixture.unit_of_work() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT lower(occupies) AS starts FROM booking_items "
                    " WHERE tenant_id = :tid AND booking_id = :bid"
                ),
                {"tid": fixture.tenant_id, "bid": booking.id},
            )
        ).scalars().all()
    assert rows, "the booking must still have its allocations"
    assert all(row == new_start for row in rows), (
        "Every item's occupancy must follow the booking. If it does not, the "
        "exclusion constraint is now protecting the OLD interval and the new "
        "one is unguarded."
    )


async def test_a_failed_reschedule_leaves_the_original_completely_intact(
    fixture: Fixture,
) -> None:
    """**The test this file is for.**

    The target time is occupied by a second booking on the only available
    resource, so the move must fail. Afterwards the original must be exactly as
    it was — same time, same status, same items, still holding its own slot.

    Every assertion below is about the *original*, deliberately: a reschedule
    that fails cleanly but silently drops the booking's items would pass a test
    that only checked the raised exception.
    """
    await _narrow(fixture)
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))
    occupied = Fixture.local(day, time(14, 0))

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        original = await service.book(
            service_code="standard", start_at=start, customer_name="Original"
        )

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        blocker = await service.book(
            service_code="standard", start_at=occupied, customer_name="Blocker"
        )

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        with pytest.raises(SlotUnavailableError):
            await service.reschedule(
                booking_id=original.id, new_start_at=occupied
            )

    async with fixture.unit_of_work() as session:
        repo = BookingRepository(session)
        after = await repo.get_booking(original.id)
        items = (
            await session.execute(
                text(
                    "SELECT role, lower(occupies) AS starts, is_blocking "
                    "  FROM booking_items "
                    " WHERE tenant_id = :tid AND booking_id = :bid ORDER BY role"
                ),
                {"tid": fixture.tenant_id, "bid": original.id},
            )
        ).mappings().all()
        blocker_after = await repo.get_booking(blocker.id)

    assert after is not None
    assert after.start_at == start, (
        "The original booking moved despite the reschedule failing. The move "
        "must be one UPDATE that either commits or rolls back whole."
    )
    assert after.status == "confirmed"
    assert after.reference == original.reference
    assert len(items) == 2, "the original lost an allocation on a failed move"
    assert all(row["starts"] == start for row in items)
    assert all(row["is_blocking"] for row in items), (
        "The original's items stopped blocking, so its slot is now free — the "
        "caller would keep their booking but lose their time."
    )
    # And the booking that caused the failure is untouched too.
    assert blocker_after is not None
    assert blocker_after.start_at == occupied


async def test_a_failed_reschedule_offers_alternatives(fixture: Fixture) -> None:
    """The caller is not left at a dead end.

    "That time has gone, but I have 2:30 or 3" is the answer; "that didn't work"
    is not. The alternatives are gathered from a week's window, because a caller
    who asked for Tuesday afternoon will usually take Wednesday morning.
    """
    await _narrow(fixture)
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))
    occupied = Fixture.local(day, time(14, 0))

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        original = await service.book(
            service_code="standard", start_at=start, customer_name="Original"
        )
    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        await service.book(
            service_code="standard", start_at=occupied, customer_name="Blocker"
        )

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        with pytest.raises(SlotUnavailableError) as caught:
            await service.reschedule(
                booking_id=original.id, new_start_at=occupied
            )

    assert caught.value.alternatives
    assert "unchanged" in caught.value.message.lower(), (
        "The message must tell the caller their existing appointment survived, "
        "or they will assume they have lost it."
    )


async def test_moving_a_booking_by_less_than_its_own_duration_works(
    fixture: Fixture,
) -> None:
    """"Shift my 9am to 9:15" must not conflict with itself.

    The new interval overlaps the old one, so a naive availability check sees the
    booking's own items as an obstacle and reports the slot taken. The engine
    excludes the booking being moved from its own conflict check
    (``exclude_booking_id``), which is the only way this common request works.
    """
    await _narrow(fixture)
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        booking = await service.book(
            service_code="standard", start_at=start, customer_name="Shifter"
        )
        moved = await service.reschedule(
            booking_id=booking.id, new_start_at=start + timedelta(minutes=15)
        )

    assert moved.start_at == start + timedelta(minutes=15)


async def test_a_cancelled_booking_cannot_be_rescheduled(fixture: Fixture) -> None:
    """Moving a cancelled appointment would resurrect it silently.

    The caller thinks they cancelled; the diary would show them booked. Refusing
    with a clear message lets the agent offer to make a new booking instead,
    which is what they actually want.
    """
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        booking = await service.book(
            service_code="standard", start_at=start, customer_name="Gone"
        )
        await service.cancel(booking_id=booking.id, accept_fee=True)

        with pytest.raises(ValidationError) as caught:
            await service.reschedule(
                booking_id=booking.id, new_start_at=start + timedelta(hours=2)
            )
    assert "cancelled" in str(caught.value).lower()

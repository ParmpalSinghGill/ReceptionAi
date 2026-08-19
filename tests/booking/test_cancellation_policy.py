"""Cancellation policy — inside the notice window versus outside.

Two properties, and the second is the one that protects a customer relationship:

* the fee is computed from the values **copied onto the booking**, never from the
  service's current ones, so a practice raising its fee on Monday does not
  thereby raise it for an appointment agreed on Friday (01 §5.9's copy-on-write);
* a fee is never charged without the caller having been told the amount. The
  engine refuses a late cancellation that has not passed ``accept_fee``, so the
  agent's next sentence is "there's a £25 fee — shall I go ahead?" rather than an
  apology afterwards.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest
from packages.booking.service import BookingService, quote_cancellation
from packages.core.errors import ValidationError
from packages.core.ids import uuid7
from packages.persistence.repositories.booking import BookingRepository
from sqlalchemy import text

from tests.booking.conftest import Fixture

pytestmark = [pytest.mark.integration]


async def _book(fixture: Fixture, *, at: time, offset_days: int = 3):
    day = Fixture.next_weekday(offset_days)
    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        return await service.book(
            service_code="standard",
            start_at=Fixture.local(day, at),
            customer_name="Policy Tester",
        )


async def test_cancelling_outside_the_notice_window_is_free(
    fixture: Fixture,
) -> None:
    """Three days out, against a 24-hour window. No fee, no consent needed.

    The overwhelmingly common case, and it must be frictionless: asking a caller
    to confirm a zero-pound fee is the kind of thing that makes an automated
    receptionist feel worse than a person.
    """
    booking = await _book(fixture, at=time(10, 0), offset_days=3)

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        cancelled, quote = await service.cancel(
            booking_id=booking.id, reason="changed my mind"
        )

    assert cancelled.status == "cancelled"
    assert cancelled.cancelled_at is not None
    assert quote.allowed
    assert not quote.within_notice_period
    assert not quote.fee_applies
    assert quote.reason == "outside_notice_period"
    assert cancelled.cancellation_fee_charged is None


async def test_cancelling_inside_the_notice_window_quotes_a_fee_and_refuses(
    fixture: Fixture,
) -> None:
    """The consent gate. A late cancellation costs money and must be agreed to.

    Without ``accept_fee`` the engine raises with the amount attached rather than
    cancelling — so a model that infers "they want to cancel" cannot charge
    someone who has not heard the figure. The booking is still live afterwards,
    which is what makes the refusal safe to retry.
    """
    # An hour from now: inside the 24-hour window by a wide margin.
    start = datetime.now(tz=UTC) + timedelta(hours=1)
    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        booking = await service.book(
            service_code="standard",
            start_at=_snap(start),
            customer_name="Late Canceller",
        )

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        with pytest.raises(ValidationError) as caught:
            await service.cancel(booking_id=booking.id, reason="too late")

    assert "fee" in str(caught.value).lower()
    assert caught.value.extensions.get("cancellation_fee") == "25.0000"
    assert caught.value.extensions.get("currency") == "GBP"

    # And it is still booked — the refusal did not half-cancel it.
    async with fixture.unit_of_work() as session:
        still = await BookingRepository(session).get_booking(booking.id)
    assert still is not None
    assert still.status == "confirmed"


async def test_cancelling_inside_the_window_with_consent_charges_the_fee(
    fixture: Fixture,
) -> None:
    """With ``accept_fee``, it goes through and the amount is recorded.

    ``cancellation_fee_charged`` is written, not just computed: a fee that is
    only ever derived cannot be reconciled against what was actually taken, and
    a later policy change would retroactively rewrite history.
    """
    start = datetime.now(tz=UTC) + timedelta(hours=1)
    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        booking = await service.book(
            service_code="standard",
            start_at=_snap(start),
            customer_name="Consenting Canceller",
        )

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        cancelled, quote = await service.cancel(
            booking_id=booking.id, reason="unwell", accept_fee=True
        )

    assert cancelled.status == "cancelled"
    assert quote.within_notice_period
    assert quote.fee_applies
    assert quote.fee == Decimal("25.0000")
    assert cancelled.cancellation_fee_charged == Decimal("25.0000")


async def test_the_slot_is_free_again_the_instant_a_booking_is_cancelled(
    fixture: Fixture,
) -> None:
    """The cascade trigger flips ``is_blocking``, and the partial constraint
    then ignores the row.

    No sweeper, no cleanup job: the slot is bookable in the very next
    transaction. This is what lets a practice fill a late cancellation from the
    waiting list while the caller is still on the phone.
    """
    day = Fixture.next_weekday(3)
    start = Fixture.local(day, time(11, 0))

    async with fixture.unit_of_work() as session:
        await session.execute(
            text(
                "UPDATE resources SET is_bookable = FALSE "
                " WHERE tenant_id = :tid AND code IN ('p-two','space-two')"
            ),
            {"tid": fixture.tenant_id},
        )
        service = BookingService(BookingRepository(session))
        booking = await service.book(
            service_code="standard", start_at=start, customer_name="First"
        )

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        await service.cancel(booking_id=booking.id, accept_fee=True)

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        replacement = await service.book(
            service_code="standard", start_at=start, customer_name="Second"
        )

    assert replacement.status == "confirmed"
    assert replacement.id != booking.id


async def test_an_already_cancelled_booking_cannot_be_cancelled_again(
    fixture: Fixture,
) -> None:
    """Refused with a message the agent can say, not a duplicate write.

    Silently succeeding would let a caller be charged a second fee for one
    cancellation.
    """
    booking = await _book(fixture, at=time(10, 0), offset_days=3)

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        await service.cancel(booking_id=booking.id)
        with pytest.raises(ValidationError) as caught:
            await service.cancel(booking_id=booking.id)

    assert "already cancelled" in str(caught.value).lower()


def test_the_quote_reads_the_bookings_own_policy_not_the_services_current_one(
    fixture: Fixture,
) -> None:
    """01 §5.9's copy-on-write, asserted on the pure function.

    A ``BookingRecord`` carries its own ``cancellation_window_minutes`` and
    ``cancellation_fee``, and :func:`quote_cancellation` reads *those*. If it
    reached back to the service instead, an admin editing the fee this afternoon
    would change what every existing booking agreed to this morning.
    """

    from packages.booking.domain import BookingRecord

    now = datetime.now(tz=UTC)
    booking = BookingRecord(
        id=uuid7(),
        tenant_id=fixture.tenant_id,
        reference="BK-TEST01",
        status="confirmed",
        service_id=uuid7(),
        service_code="standard",
        service_name="Standard Service",
        location_id=fixture.location_id,
        start_at=now + timedelta(hours=2),
        end_at=now + timedelta(hours=2, minutes=30),
        local_date=(now + timedelta(hours=2)).date(),
        timezone="Europe/London",
        # The booking's OWN policy, deliberately different from the fixture
        # service's, so a quote reading the wrong source is visible.
        cancellation_window_minutes=60,
        cancellation_fee=Decimal("99.0000"),
        currency="GBP",
    )

    quote = quote_cancellation(booking, now=now)
    assert quote.allowed
    # Two hours out against a 60-minute window: outside, so no fee.
    assert not quote.within_notice_period
    assert quote.fee is None

    late = quote_cancellation(booking, now=now + timedelta(minutes=90))
    assert late.within_notice_period
    assert late.fee == Decimal("99.0000"), (
        "The quote must use the booking's own fee. Reading the service's "
        "current value would let a policy change rewrite an agreement already "
        "made with a customer."
    )


def _snap(moment: datetime) -> datetime:
    """Round up to the next 15-minute grid point.

    The fixture's resource type declares a 15-minute granularity, so a start at
    an arbitrary second is not an offered slot and ``book`` would (correctly)
    refuse it. Snapping keeps these tests about the cancellation policy rather
    than about grid alignment.
    """
    minute = (moment.minute // 15 + 1) * 15
    if minute >= 60:
        return moment.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return moment.replace(minute=minute, second=0, microsecond=0)

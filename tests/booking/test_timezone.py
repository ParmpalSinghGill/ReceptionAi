"""Timezone correctness, and specifically DST — contract §6, 01 §5.6.

Rules are stored as **local wall-clock time plus a weekday mask**, never as UTC,
because "we open at nine" must mean nine o'clock in January and nine o'clock in
July. A stored UTC offset is wrong for half the year, and it is wrong silently:
nothing errors, the diary just opens an hour early every summer.

These tests pin the two transitions that a wrong implementation gets wrong, using
Europe/London (GMT/BST) because the fixture practice is in Manchester and because
its transitions are the ones a UK developer can sanity-check by eye:

* **2026-03-29** — clocks go forward at 01:00 GMT. 01:00-01:59 local does not
  exist.
* **2026-10-25** — clocks go back at 02:00 BST. 01:00-01:59 local happens twice.

Most of this is asserted against the pure expander rather than the database,
because the expander is where the conversion happens and a unit test can cover
both sides of a transition in milliseconds.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from packages.booking.availability import expand_holiday, expand_rule
from packages.booking.service import BookingService
from packages.persistence.repositories.booking import BookingRepository

from tests.booking.conftest import Fixture

pytestmark = [pytest.mark.integration]

LONDON = ZoneInfo("Europe/London")

#: British Summer Time begins. 01:00 GMT -> 02:00 BST.
SPRING_FORWARD = date(2026, 3, 29)
#: British Summer Time ends. 02:00 BST -> 01:00 GMT.
FALL_BACK = date(2026, 10, 25)


def _next_transition() -> date:
    """The next DST transition at least a few days away, in Europe/London.

    Found by walking forward and watching the UTC offset change, rather than
    hardcoded: a fixed date makes the end-to-end test silently stop exercising a
    transition once it is in the past, and `booking_horizon_days` refuses to
    book there anyway.
    """
    cursor = datetime.now(tz=LONDON).date() + timedelta(days=4)
    limit = cursor + timedelta(days=400)
    while cursor < limit:
        today_offset = datetime.combine(cursor, time(12, 0), tzinfo=LONDON).utcoffset()
        next_offset = datetime.combine(
            cursor + timedelta(days=1), time(12, 0), tzinfo=LONDON
        ).utcoffset()
        if today_offset != next_offset:
            return cursor + timedelta(days=1)
        cursor += timedelta(days=1)
    raise AssertionError("no DST transition found within a year")


def test_a_nine_am_rule_is_nine_am_on_both_sides_of_a_dst_boundary() -> None:
    """The core claim, and the reason rules are not stored in UTC.

    The same rule — "open 09:00 to 17:00, every day" — must produce 09:00 *local*
    on the Saturday before the clocks change and 09:00 *local* on the Sunday
    after, even though those are different UTC instants (09:00 GMT is 09:00Z;
    09:00 BST is 08:00Z).

    An implementation that computed one UTC offset and reused it would produce
    two identical UTC times and therefore two *different* local times, one of
    which is an hour wrong. That is what this asserts against.
    """
    windows = expand_rule(
        weekdays=[1, 2, 3, 4, 5, 6, 7],
        start_time=time(9, 0),
        end_time=time(17, 0),
        effective_from=None,
        effective_until=None,
        tz=LONDON,
        from_date=SPRING_FORWARD - timedelta(days=1),
        to_date=SPRING_FORWARD + timedelta(days=1),
    )
    by_date = {w.local_date: w for w in windows}

    before = by_date[SPRING_FORWARD - timedelta(days=1)]
    after = by_date[SPRING_FORWARD]

    assert before.start.astimezone(LONDON).hour == 9
    assert after.start.astimezone(LONDON).hour == 9, (
        "The local opening time drifted across the DST boundary. Rules must be "
        "expanded from local wall-clock into UTC per date, not offset once."
    )

    # And the UTC instants genuinely differ, which is what proves the conversion
    # happened rather than the assertion being vacuous.
    assert before.start.astimezone(UTC).hour == 9
    assert after.start.astimezone(UTC).hour == 8


def test_the_spring_forward_day_is_an_hour_shorter_in_real_time() -> None:
    """A 09:00-17:00 window is 8 hours of wall clock but not always of elapsed time.

    On a normal day it is 8 hours. This test picks a window that *contains* the
    transition (00:00-06:00 on the spring-forward Sunday) and asserts it is 5
    real hours, because 01:00-01:59 local never happened.

    A practice open through the transition is genuinely open for one hour less,
    and the engine must agree — otherwise it offers a slot at 01:30 on a morning
    where 01:30 does not exist.
    """
    windows = expand_rule(
        weekdays=[SPRING_FORWARD.isoweekday()],
        start_time=time(0, 0),
        end_time=time(6, 0),
        effective_from=None,
        effective_until=None,
        tz=LONDON,
        from_date=SPRING_FORWARD,
        to_date=SPRING_FORWARD,
    )
    window = next(w for w in windows if w.local_date == SPRING_FORWARD)
    assert window.end - window.start == timedelta(hours=5), (
        "The spring-forward morning must be one real hour shorter. If it is 6 "
        "hours, the expander did arithmetic in UTC and invented an hour that "
        "does not exist."
    )


def test_the_fall_back_day_is_an_hour_longer_in_real_time() -> None:
    """And the reverse: 01:00-01:59 happens twice, so the window is 7 real hours.

    Both directions are asserted because an implementation can easily be right
    about one and wrong about the other — ``fold`` handling is a separate code
    path from nonexistent-time handling in every date library.
    """
    windows = expand_rule(
        weekdays=[FALL_BACK.isoweekday()],
        start_time=time(0, 0),
        end_time=time(6, 0),
        effective_from=None,
        effective_until=None,
        tz=LONDON,
        from_date=FALL_BACK,
        to_date=FALL_BACK,
    )
    window = next(w for w in windows if w.local_date == FALL_BACK)
    assert window.end - window.start == timedelta(hours=7)


def test_a_whole_day_holiday_covers_the_whole_local_day() -> None:
    """A holiday is a *local* day, which is 23 or 25 hours twice a year.

    Adding ``timedelta(hours=24)`` to a UTC instant would leave an hour of the
    spring-forward Sunday bookable — on a day the practice is shut. Local
    midnight to local midnight gets it right.
    """
    closed = expand_holiday(
        holiday_date=SPRING_FORWARD,
        start_time=None,
        end_time=None,
        tz=LONDON,
        reason="Test closure",
    )
    assert closed.end - closed.start == timedelta(hours=23)

    closed_autumn = expand_holiday(
        holiday_date=FALL_BACK,
        start_time=None,
        end_time=None,
        tz=LONDON,
        reason="Test closure",
    )
    assert closed_autumn.end - closed_autumn.start == timedelta(hours=25)


async def test_a_booking_near_a_dst_boundary_lands_at_the_right_local_time(
    fixture: Fixture,
) -> None:
    """End to end, through the database, across the real transition.

    Two bookings either side of the same *local* time, spanning a real DST
    transition. Both must read back as 10:00 local, and their stored UTC
    instants must differ by an hour — which is the observable signature of the
    conversion having been done per-date rather than once.

    The **next upcoming** transition is used rather than a hardcoded date,
    because ``services.booking_horizon_days`` refuses a booking in the past and
    a fixed date turns this test into one that quietly stops running the year
    after it was written.

    ``local_date`` is asserted too: it is stored, not derived, precisely because
    deriving it needs the location timezone that a generated column cannot reach
    (01 §5.9), so a bug there produces a booking filed under the wrong day in
    every day-view query.
    """
    transition = _next_transition()
    before_day = transition - timedelta(days=2)
    after_day = transition + timedelta(days=2)

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        first = await service.book(
            service_code="standard",
            start_at=Fixture.local(before_day, time(10, 0)),
            customer_name="Before The Change",
        )
        second = await service.book(
            service_code="standard",
            start_at=Fixture.local(after_day, time(10, 0)),
            customer_name="After The Change",
        )

    assert first.local_start.hour == 10
    assert second.local_start.hour == 10, (
        "The same local time produced a different local time after the clocks "
        "changed. Business-local time must be derived from the stored UTC "
        "instant and the location zone at render time (contract §6)."
    )

    # The stored instants differ by exactly the one-hour shift, which is what
    # proves the conversion happened per-date rather than once.
    assert first.start_at.astimezone(UTC).hour != second.start_at.astimezone(UTC).hour
    assert (
        abs(
            first.start_at.astimezone(UTC).hour
            - second.start_at.astimezone(UTC).hour
        )
        == 1
    )
    assert first.local_date == before_day
    assert second.local_date == after_day


async def test_local_date_is_the_business_day_not_the_utc_day(
    fixture: Fixture,
) -> None:
    """A late-evening booking in a positive-offset zone belongs to the local day.

    In BST the UTC instant of a late-evening booking is an hour earlier, so this
    particular case agrees — but the assertion is written against
    ``local_start.date()`` rather than against the UTC date, so it stays correct
    for a tenant east of Greenwich where the two genuinely diverge.

    21:00 rather than later: the fixture practice closes at 22:00 and the service
    is 30 minutes plus a 10-minute buffer, so a later start would be refused for
    being outside opening hours — which is correct behaviour and a different
    test.
    """
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(21, 0))

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        booking = await service.book(
            service_code="standard", start_at=start, customer_name="Late One"
        )

    assert booking.local_date == booking.local_start.date()
    assert booking.local_date == day


def test_a_window_crossing_midnight_belongs_to_the_day_it_starts_on() -> None:
    """18:00-02:00 is one window, not two, and it is filed under the first day.

    A bar, a late clinic, a 24-hour service. ``end_time <= start_time`` is the
    signal, and the expander must produce an 8-hour interval rather than an empty
    or inverted one — an inverted range would be rejected by
    ``ck_booking_items__occupies_bounded`` at write time, but only after the
    solver had already offered slots inside it.
    """
    day = date(2026, 6, 10)
    windows = expand_rule(
        weekdays=[day.isoweekday()],
        start_time=time(18, 0),
        end_time=time(2, 0),
        effective_from=None,
        effective_until=None,
        tz=LONDON,
        from_date=day,
        to_date=day,
    )
    window = next(w for w in windows if w.local_date == day)
    assert window.end > window.start
    assert window.end - window.start == timedelta(hours=8)
    assert window.start.astimezone(LONDON).date() == day
    assert window.end.astimezone(LONDON).date() == day + timedelta(days=1)

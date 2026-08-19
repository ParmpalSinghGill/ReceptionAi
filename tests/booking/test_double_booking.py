"""Double-booking is impossible — 01 §13.1, proved under real concurrency.

The single most important property in the engine, and the one that is easiest to
appear to have without having. A sequential test — book, then try to book again,
assert the second fails — passes with **no constraint at all** if the code does a
``SELECT`` before inserting. It proves the check exists, not that it holds.

So the test that matters here runs two transactions genuinely in parallel, both
past the point where a naive check-then-act would have seen an empty slot, and
asserts exactly one commits. Under ``READ COMMITTED`` — the default, and what we
run — that is precisely the case an application-level check gets wrong.
"""

from __future__ import annotations

import asyncio
from datetime import time, timedelta

import pytest
from packages.booking.service import BookingService, SlotUnavailableError
from packages.core.errors import ResourceConflictError
from packages.persistence.repositories.booking import BookingRepository
from sqlalchemy import text

from tests.booking.conftest import Fixture

pytestmark = [pytest.mark.integration]


async def _book(fixture: Fixture, *, start, service: str = "standard", **kwargs):
    async with fixture.unit_of_work() as session:
        service_obj = BookingService(BookingRepository(session))
        return await service_obj.book(
            service_code=service,
            start_at=start,
            customer_name=kwargs.pop("customer_name", "Test Customer"),
            **kwargs,
        )


async def test_two_concurrent_bookings_for_one_slot_leave_exactly_one_winner(
    fixture: Fixture,
) -> None:
    """The proof. Two real transactions, one slot, one survivor.

    Both coroutines are started before either commits, so both are inside the
    window where a ``SELECT ... WHERE NOT EXISTS`` would have returned "free".
    Only the storage engine can break the tie, and it does: the loser gets
    SQLSTATE 23P01 from ``ex_booking_items__no_overlap`` (or, if it lost the
    advisory lock race first, a clean availability conflict) and rolls back
    whole — no orphan booking row with no resources attached.

    **The resource pool is deliberately narrowed to one practitioner and one
    space.** With the fixture's default two of each, the engine would correctly
    allocate the *second* practitioner to the second booking and both would
    succeed — which is right behaviour and a useless test. Contention needs
    scarcity.
    """
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))

    # Leave exactly one practitioner and one space bookable.
    async with fixture.unit_of_work() as session:
        await session.execute(
            text(
                "UPDATE resources SET is_bookable = FALSE "
                " WHERE tenant_id = :tid AND code IN ('p-two','space-two')"
            ),
            {"tid": fixture.tenant_id},
        )

    results = await asyncio.gather(
        _book(fixture, start=start, customer_name="Racer A"),
        _book(fixture, start=start, customer_name="Racer B"),
        return_exceptions=True,
    )

    booked = [r for r in results if not isinstance(r, BaseException)]
    failed = [r for r in results if isinstance(r, BaseException)]

    assert len(booked) == 1, (
        f"Exactly one booking must win a contested slot; {len(booked)} did. "
        f"Results: {results}"
    )
    assert len(failed) == 1

    loser = failed[0]
    assert isinstance(loser, ResourceConflictError), (
        f"The loser must get a clean domain conflict, not {type(loser).__name__}: "
        f"{loser}"
    )
    # `slot_unavailable`, not the generic `conflict`: PHASE1-API-CONTRACT's 409
    # shape is what a client special-cases, and a caller told "conflict" cannot
    # tell a taken slot from a duplicate reference.
    assert loser.code == "slot_unavailable", (
        f"A lost slot must carry the frozen `slot_unavailable` code so the API "
        f"renders the documented 409; got {loser.code!r}."
    )

    # And the database agrees: one booking, and its items are all present.
    async with fixture.unit_of_work() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM bookings "
                    " WHERE tenant_id = :tid AND status <> 'cancelled'"
                ),
                {"tid": fixture.tenant_id},
            )
        ).scalar_one()
        orphans = (
            await session.execute(
                text(
                    "SELECT count(*) FROM bookings b "
                    " WHERE b.tenant_id = :tid AND NOT EXISTS ("
                    "   SELECT 1 FROM booking_items bi "
                    "    WHERE bi.booking_id = b.id AND bi.tenant_id = b.tenant_id)"
                ),
                {"tid": fixture.tenant_id},
            )
        ).scalar_one()

    assert count == 1
    assert orphans == 0, (
        "A rolled-back booking left an aggregate with no resources attached. "
        "The insert of the booking and of its items must be one transaction."
    )


async def test_the_loser_gets_alternatives_it_can_offer(fixture: Fixture) -> None:
    """A 409 that says only "no" wastes the caller's turn.

    The conflict carries the next free times, so the agent's very next sentence
    is "that's gone, but I have 9:45 or 10:00" rather than an apology and a
    re-query. The alternatives ride on the exception because the engine has
    already loaded everything needed to compute them.
    """
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))

    async with fixture.unit_of_work() as session:
        await session.execute(
            text(
                "UPDATE resources SET is_bookable = FALSE "
                " WHERE tenant_id = :tid AND code IN ('p-two','space-two')"
            ),
            {"tid": fixture.tenant_id},
        )

    await _book(fixture, start=start, customer_name="First")

    with pytest.raises(SlotUnavailableError) as caught:
        await _book(fixture, start=start, customer_name="Second")

    exc = caught.value
    assert exc.alternatives, "A taken slot must come back with somewhere else to go."
    assert all(slot.start_at != start for slot in exc.alternatives)
    # And the RFC 9457 extension the API renders carries them too.
    assert exc.extensions.get("alternatives"), (
        "The alternatives must reach the problem body, or the HTTP caller sees "
        "a bare 409."
    )


async def test_back_to_back_bookings_at_the_boundary_succeed(
    fixture: Fixture,
) -> None:
    """The half-open range check — 01 §13.1's non-negotiable detail.

    With inclusive ``[]`` ranges, ``[09:00, 09:30]`` and ``[09:30, 10:00]``
    overlap at the instant ``09:30``, so **every back-to-back booking would be
    rejected** and a diary could never be filled. With ``[)`` they abut exactly.

    The unbuffered service is used so the boundary being tested is the range
    bound itself and not the buffer arithmetic around it — those are two
    different mechanisms and a test that exercises both proves neither.
    """
    day = Fixture.next_weekday(2)
    first = Fixture.local(day, time(9, 0))
    second = first + timedelta(minutes=30)  # exactly when the first ends

    async with fixture.unit_of_work() as session:
        await session.execute(
            text(
                "UPDATE resources SET is_bookable = FALSE "
                " WHERE tenant_id = :tid AND code IN ('p-two','space-two')"
            ),
            {"tid": fixture.tenant_id},
        )

    a = await _book(fixture, start=first, service="plain", customer_name="Earlier")
    b = await _book(fixture, start=second, service="plain", customer_name="Later")

    assert a.end_at == b.start_at, (
        "The two bookings must abut exactly; if they do not, this test is no "
        "longer exercising the boundary."
    )
    assert a.reference != b.reference
    assert {a.status, b.status} == {"confirmed"}

    # Both are on the same resource, which is the only interesting case.
    assert a.resource_names == b.resource_names


async def test_a_booking_one_minute_into_the_previous_one_is_rejected(
    fixture: Fixture,
) -> None:
    """The other half of the boundary. Abutting is fine; overlapping is not.

    Without this, ``test_back_to_back...`` would also pass against a schema with
    no constraint at all — the two tests are only meaningful as a pair.
    """
    day = Fixture.next_weekday(2)
    first = Fixture.local(day, time(9, 0))

    async with fixture.unit_of_work() as session:
        await session.execute(
            text(
                "UPDATE resources SET is_bookable = FALSE "
                " WHERE tenant_id = :tid AND code IN ('p-two','space-two')"
            ),
            {"tid": fixture.tenant_id},
        )

    await _book(fixture, start=first, service="plain", customer_name="Earlier")

    with pytest.raises(ResourceConflictError):
        await _book(
            fixture,
            start=first + timedelta(minutes=29),
            service="plain",
            customer_name="Overlapper",
        )


async def test_the_buffer_blocks_the_space_but_not_the_practitioner(
    fixture: Fixture,
) -> None:
    """``respects_buffers`` per requirement — 01 §5.5, §14.1.

    The service is 30 minutes with a 10-minute after-buffer. So at 09:30 the
    practitioner is free (their occupancy is the *service* range) while the space
    is not (its occupancy is the *blocking* range, through 09:40).

    This is asserted at the database rather than through the solver, because the
    trigger that derives the two ranges is the thing under test — the solver
    merely reads what it produced.
    """
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))

    booking = await _book(fixture, start=start, service="standard")

    async with fixture.unit_of_work() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT bi.role, lower(bi.occupies) AS starts, "
                    "       upper(bi.occupies) AS ends "
                    "  FROM booking_items bi "
                    " WHERE bi.tenant_id = :tid AND bi.booking_id = :bid "
                    " ORDER BY bi.role"
                ),
                {"tid": fixture.tenant_id, "bid": booking.id},
            )
        ).mappings().all()

    occupancy = {row["role"]: (row["starts"], row["ends"]) for row in rows}

    assert occupancy["primary"][1] == start + timedelta(minutes=30), (
        "The practitioner must be free the moment the appointment ends; a "
        "buffer applied to them would block someone for a turnaround they have "
        "nothing to do with."
    )
    assert occupancy["space"][1] == start + timedelta(minutes=40), (
        "The space must be blocked through the buffer, or a booking would be "
        "accepted into a room that has not been turned around."
    )


async def test_blocking_period_is_generated_and_cannot_be_written(
    fixture: Fixture,
) -> None:
    """01 §5.9: buffers can never desync from the indexed range.

    ``blocking_period`` is ``GENERATED ALWAYS ... STORED``, so it is a function
    of ``service_period`` and the two buffer columns and the storage engine
    enforces that. If it were application-maintained, a bug or a hand-written
    ``UPDATE`` could punch a hole in double-booking protection that nothing would
    report — the exclusion constraint would still be there, indexing a value that
    no longer means what it says.

    Also asserts the substitution documented in migration 0003 is exact:
    ``booking_shift_minutes`` must produce the same instant as
    ``+ make_interval(mins => n)``, which is the whole justification for
    deviating from 01 §5.9's literal DDL.
    """
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))
    booking = await _book(fixture, start=start, service="standard")

    async with fixture.unit_of_work() as session:
        row = (
            await session.execute(
                text(
                    "SELECT lower(blocking_period) AS b_start, "
                    "       upper(blocking_period) AS b_end, "
                    "       lower(service_period)  AS s_start, "
                    "       upper(service_period)  AS s_end, "
                    "       buffer_after_minutes, "
                    "       upper(service_period) "
                    "         + make_interval(mins => buffer_after_minutes) AS expected "
                    "  FROM bookings WHERE tenant_id = :tid AND id = :bid"
                ),
                {"tid": fixture.tenant_id, "bid": booking.id},
            )
        ).mappings().one()

        assert row["b_start"] == row["s_start"]
        assert row["b_end"] == row["expected"], (
            "booking_shift_minutes must be arithmetically identical to "
            "`+ make_interval(mins => n)`. If it is not, migration 0003's "
            "substitution for 01 §5.9's non-immutable expression is wrong."
        )

        # And it refuses to be written.
        with pytest.raises(Exception) as caught:
            await session.execute(
                text(
                    "UPDATE bookings SET blocking_period = "
                    "  tstzrange(now(), now() + interval '1 hour', '[)') "
                    " WHERE tenant_id = :tid AND id = :bid"
                ),
                {"tid": fixture.tenant_id, "bid": booking.id},
            )
        assert "generated" in str(caught.value).lower()


# ── Site-wide concurrency (01 §5.6's capacity_override) ──────────────────────
#
# Distinct from the exclusion constraint, which asks "is THIS clinician free?".
# This asks "how many appointments does the practice run at once?" — the
# restaurant that seats forty, or the single-surgery practice that sees one
# patient at a time however many clinicians are rostered.


async def test_a_site_wide_cap_stops_a_second_concurrent_booking(
    fixture: Fixture,
) -> None:
    """With the cap at one, a taken slot is taken for the whole practice.

    Both clinicians are free — the exclusion constraint would happily allow
    this, and does allow it when no cap is set. The cap is what makes 10:00 mean
    10:00 for everyone.
    """
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(10, 0))

    async with fixture.unit_of_work() as session:
        await session.execute(
            text(
                "UPDATE availability_rules SET capacity_override = 1 "
                " WHERE tenant_id = :tid"
            ),
            {"tid": fixture.tenant_id},
        )

    await _book(fixture, start=start, service="plain", customer_name="First In")

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        slots, *_ = await service.find_slots(
            service_code="plain",
            window_start=start,
            window_end=start + timedelta(minutes=1),
            limit=5,
        )

    assert slots == [], (
        "A slot the practice cannot serve must not be OFFERED. Rejecting it at "
        "booking time means the caller picks a time, hears yes, and is then "
        "told no."
    )


async def test_without_a_cap_concurrent_bookings_are_allowed(
    fixture: Fixture,
) -> None:
    """The default is unchanged: no site-wide limit.

    Every existing tenant keeps per-resource behaviour, which is what makes this
    configuration rather than a new rule in the engine.
    """
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(10, 0))

    async with fixture.unit_of_work() as session:
        await session.execute(
            text(
                "UPDATE availability_rules SET capacity_override = NULL "
                " WHERE tenant_id = :tid"
            ),
            {"tid": fixture.tenant_id},
        )

    first = await _book(fixture, start=start, service="plain", customer_name="One")
    second = await _book(fixture, start=start, service="plain", customer_name="Two")

    assert first.id != second.id
    assert first.resource_names != second.resource_names, (
        "The two bookings must be on different resources — otherwise the "
        "exclusion constraint, not the cap, is what allowed this."
    )


def test_a_cap_of_two_allows_exactly_two() -> None:
    """The cap is a number, not a boolean. A busier practice sets it higher.

    Asserted against the solver directly rather than through the database,
    because the fixture practice has only two of each resource — so a second
    concurrent booking exhausts the POOL before a cap of two could bind, and a
    database-level test would pass by proving the exclusion constraint works.
    Testing the two mechanisms separately is what keeps each assertion about the
    thing it names.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from packages.booking.availability import AvailabilityEngine, Busy, OpenWindow
    from packages.core.ids import uuid7

    engine = AvailabilityEngine()
    start = _dt(2026, 8, 12, 9, 0, tzinfo=_UTC)
    end = start + timedelta(minutes=30)
    window = OpenWindow(
        start=_dt(2026, 8, 12, 8, 0, tzinfo=_UTC),
        end=_dt(2026, 8, 12, 18, 0, tzinfo=_UTC),
        capacity_override=2,
    )

    def occupancy(count: int) -> list[Busy]:
        return [
            Busy(
                resource_id=uuid7(),
                start=start,
                end=end,
                booking_id=uuid7(),
            )
            for _ in range(count)
        ]

    assert engine._within_location_capacity(
        start=start, end=end, open_windows=[window], busy=occupancy(0)
    )
    assert engine._within_location_capacity(
        start=start, end=end, open_windows=[window], busy=occupancy(1)
    )
    assert not engine._within_location_capacity(
        start=start, end=end, open_windows=[window], busy=occupancy(2)
    ), "a cap of two must refuse the third concurrent appointment"


def test_one_appointment_on_two_resources_counts_once() -> None:
    """A booking consuming a clinician AND a room is one appointment.

    Counting occupancy rows would make every two-resource service look like two
    appointments, so a cap of one would refuse the very first booking.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from packages.booking.availability import AvailabilityEngine, Busy, OpenWindow
    from packages.core.ids import uuid7

    engine = AvailabilityEngine()
    start = _dt(2026, 8, 12, 9, 0, tzinfo=_UTC)
    end = start + timedelta(minutes=30)
    one_booking = uuid7()

    assert not engine._within_location_capacity(
        start=start,
        end=end,
        open_windows=[
            OpenWindow(
                start=_dt(2026, 8, 12, 8, 0, tzinfo=_UTC),
                end=_dt(2026, 8, 12, 18, 0, tzinfo=_UTC),
                capacity_override=1,
            )
        ],
        # Two rows, ONE appointment: a clinician and a room.
        busy=[
            Busy(resource_id=uuid7(), start=start, end=end, booking_id=one_booking),
            Busy(resource_id=uuid7(), start=start, end=end, booking_id=one_booking),
        ],
    )

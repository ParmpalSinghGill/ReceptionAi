"""Hold TTL semantics — 01 §13.2, §13.3.

Three properties, and they are in tension with each other, which is why the
design needs three layers:

* a live hold **blocks** a competing booking, or it is not a hold;
* an expired hold **stops blocking the instant its TTL passes**, with no
  background process involved, or a crashed worker can make a resource
  permanently unbookable;
* the sweeper is **janitorial**, not load-bearing — correctness must not depend
  on it having run.

The middle one is the interesting test, because it is the one a naive
implementation gets wrong: it expires holds in a cron job and is therefore
correct only as often as the job runs.
"""

from __future__ import annotations

from datetime import time, timedelta

import pytest
from packages.booking.holds import (
    MAX_HOLD_TTL_SECONDS,
    MIN_HOLD_TTL_SECONDS,
    HoldManager,
)
from packages.booking.service import BookingService
from packages.core.errors import ResourceConflictError
from packages.persistence.repositories.booking import BookingRepository
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.booking.conftest import Fixture

pytestmark = [pytest.mark.integration]


async def _only_one_of_each(fixture: Fixture) -> None:
    """Narrow the pool so a hold on one resource is a hold on the only resource."""
    async with fixture.unit_of_work() as session:
        await session.execute(
            text(
                "UPDATE resources SET is_bookable = FALSE "
                " WHERE tenant_id = :tid AND code IN ('p-two','space-two')"
            ),
            {"tid": fixture.tenant_id},
        )


async def test_a_live_hold_blocks_a_competing_booking(fixture: Fixture) -> None:
    """The point of a hold: the slot is reserved while the caller decides.

    The competing attempt is a *booking*, not another hold, which is the case
    the exclusion constraints cannot cover — they are single-table, so
    hold-vs-booking is exactly the collision that needs
    ``booking_check_conflicts`` and its advisory lock (01 §13.2).
    """
    await _only_one_of_each(fixture)
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))
    end = start + timedelta(minutes=30)

    async with fixture.unit_of_work() as session:
        repo = BookingRepository(session)
        hold = await HoldManager(repo).place(
            location_id=fixture.location_id,
            service_id=fixture.services["plain"],
            resource_id=fixture.resources["p-one"],
            start=start,
            end=end,
            ttl_seconds=600,
        )
        assert hold.status == "active"

    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        slots, *_ = await service.find_slots(
            service_code="plain",
            window_start=start,
            window_end=end,
            limit=5,
        )

    assert not slots, (
        "A live hold must make the slot unavailable. If it does not, the hold "
        "is decorative and two callers can be sold the same time."
    )


async def test_an_expired_hold_frees_the_slot_without_the_sweeper(
    fixture: Fixture,
) -> None:
    """**Layer 1 is authoritative** — 01 §13.3.

    The hold row is left ``status = 'active'`` and only its ``expires_at`` is
    pushed into the past, which is exactly the state a crashed worker leaves
    behind. The sweeper is deliberately *not* run. The slot must still be
    bookable, because every conflict check carries ``expires_at > now()``.

    If this test fails, the system's correctness depends on a background process
    being alive — and a dead worker becomes a resource nobody can book.
    """
    await _only_one_of_each(fixture)
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))
    end = start + timedelta(minutes=30)

    async with fixture.unit_of_work() as session:
        repo = BookingRepository(session)
        hold = await HoldManager(repo).place(
            location_id=fixture.location_id,
            service_id=fixture.services["plain"],
            resource_id=fixture.resources["p-one"],
            start=start,
            end=end,
            ttl_seconds=600,
        )

    async with fixture.unit_of_work() as session:
        # Expire it in place. Status stays 'active' — this is a straggler, not a
        # tidy release, and it is the state the sweeper exists to clean up
        # *later*.
        await session.execute(
            text(
                "UPDATE booking_holds SET expires_at = now() - interval '1 second' "
                " WHERE tenant_id = :tid AND id = :hid"
            ),
            {"tid": fixture.tenant_id, "hid": hold.id},
        )

    async with fixture.unit_of_work() as session:
        still_active = (
            await session.execute(
                text(
                    "SELECT status FROM booking_holds "
                    " WHERE tenant_id = :tid AND id = :hid"
                ),
                {"tid": fixture.tenant_id, "hid": hold.id},
            )
        ).scalar_one()
        assert still_active == "active", (
            "The test must run against an unswept hold, or it proves the "
            "sweeper works rather than that it is unnecessary."
        )

        service = BookingService(BookingRepository(session))
        booking = await service.book(
            service_code="plain",
            start_at=start,
            customer_name="After Expiry",
        )

    assert booking.status == "confirmed", (
        "An expired-but-unswept hold must not block a booking. Layer 1 (the "
        "read-time `expires_at > now()` predicate) is what makes this true."
    )


async def test_the_sweeper_tidies_stragglers_without_being_needed_for_correctness(
    fixture: Fixture, ops_engine: AsyncEngine
) -> None:
    """Layer 3 — janitorial. It keeps the partial index small and admin views honest.

    ``ex_booking_holds__no_overlap``'s predicate is ``status = 'active'`` and,
    being a partial index predicate, **cannot** reference ``now()`` — a partial
    index predicate must be IMMUTABLE and ``now()`` is STABLE. So the constraint
    alone would let an expired-but-unswept hold block a competing *hold*. That is
    what the sweeper fixes, and it is the only thing it fixes.
    """
    await _only_one_of_each(fixture)
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))

    async with fixture.unit_of_work() as session:
        repo = BookingRepository(session)
        hold = await HoldManager(repo).place(
            location_id=fixture.location_id,
            service_id=fixture.services["plain"],
            resource_id=fixture.resources["p-one"],
            start=start,
            end=start + timedelta(minutes=30),
            ttl_seconds=600,
        )
        await session.execute(
            text(
                "UPDATE booking_holds SET expires_at = now() - interval '1 minute' "
                " WHERE tenant_id = :tid AND id = :hid"
            ),
            {"tid": fixture.tenant_id, "hid": hold.id},
        )

    # `ops_expire_stale_holds` is cross-tenant by design and is granted to
    # ``receptionai_ops`` ONLY — the app role gets `permission denied`, which is
    # the grant model working: a cross-tenant write has no business on a request
    # path. Asserting that first, then running it as ops, is what makes this test
    # about the sweeper rather than about who may call it.
    async with fixture.unit_of_work() as session:
        with pytest.raises(Exception) as denied:
            await session.execute(text("SELECT ops_expire_stale_holds(1000)"))
        assert "permission denied" in str(denied.value).lower()

    async with ops_engine.begin() as conn:
        swept = (
            await conn.execute(text("SELECT ops_expire_stale_holds(1000)"))
        ).scalar_one()
        assert swept >= 1

    async with fixture.unit_of_work() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, release_reason FROM booking_holds "
                    " WHERE tenant_id = :tid AND id = :hid"
                ),
                {"tid": fixture.tenant_id, "hid": hold.id},
            )
        ).mappings().one()

    assert row["status"] == "expired"
    assert row["release_reason"] == "ttl_swept"


async def test_two_holds_cannot_overlap_on_one_resource(fixture: Fixture) -> None:
    """``ex_booking_holds__no_overlap``, the hold-vs-hold backstop."""
    await _only_one_of_each(fixture)
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))
    end = start + timedelta(minutes=30)

    async with fixture.unit_of_work() as session:
        holds = HoldManager(BookingRepository(session))
        await holds.place(
            location_id=fixture.location_id,
            service_id=fixture.services["plain"],
            resource_id=fixture.resources["p-one"],
            start=start,
            end=end,
            ttl_seconds=600,
        )

    async with fixture.unit_of_work() as session:
        holds = HoldManager(BookingRepository(session))
        with pytest.raises(ResourceConflictError):
            await holds.place(
                location_id=fixture.location_id,
                service_id=fixture.services["plain"],
                resource_id=fixture.resources["p-one"],
                start=start + timedelta(minutes=15),
                end=end + timedelta(minutes=15),
                ttl_seconds=600,
            )


async def test_releasing_a_hold_is_idempotent(fixture: Fixture) -> None:
    """Three things race to release a hold; two of them losing is not an error.

    The agent's explicit release, the workflow timer, and the sweeper all target
    the same row. If a second release raised, the timer would log an exception
    every time the caller had already hung up politely — which is most of them.
    """
    await _only_one_of_each(fixture)
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))

    async with fixture.unit_of_work() as session:
        manager = HoldManager(BookingRepository(session))
        hold = await manager.place(
            location_id=fixture.location_id,
            service_id=fixture.services["plain"],
            resource_id=fixture.resources["p-one"],
            start=start,
            end=start + timedelta(minutes=30),
            ttl_seconds=600,
        )
        assert await manager.release(hold.id, reason="caller_declined") is True
        assert await manager.release(hold.id, reason="timer") is False


def test_the_ttl_is_clamped_rather_than_rejected() -> None:
    """A manifest asking for two hours gets fifteen minutes, not an error.

    An over-long hold is not generosity, it is an outage: one abandoned call per
    resource per two hours makes a diary look full while it is empty. And a
    sub-minute hold expires while the agent is still reading the time back.
    Clamping keeps both from happening without failing an install over a value
    that is merely optimistic.
    """
    assert HoldManager.clamp_ttl(7200) == MAX_HOLD_TTL_SECONDS
    assert HoldManager.clamp_ttl(1) == MIN_HOLD_TTL_SECONDS
    assert HoldManager.clamp_ttl(300) == 300
    assert HoldManager.clamp_ttl(None) > 0

"""Bookings are invisible cross-tenant — contract §2, 01 §11.

A booking is the most sensitive row the platform holds after a secret: it carries
a name, a phone number, and — through ``intake_data`` — what someone said was
wrong with them. Contract §2.7 says cross-tenant access is impossible *by
construction*, and this file is what turns that claim into something a build can
fail on.

**Everything here runs as ``receptionai_app``**, which is not the table owner and
does not hold ``BYPASSRLS``. A test that ran as a superuser would pass with RLS
entirely absent, which is worse than no test because it reports green.

The negative assertions matter as much as the positive ones. "Tenant B cannot see
tenant A's booking" is only meaningful alongside "tenant A *can*" — otherwise a
broken connection string would satisfy every isolation test in the suite.
"""

from __future__ import annotations

from datetime import time
from uuid import UUID

import pytest
from packages.booking.service import BookingService
from packages.core.ids import uuid7
from packages.core.tenancy import TenantContext
from packages.persistence.repositories.booking import BookingRepository
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.booking.conftest import TZ, Fixture, _set_force

pytestmark = [pytest.mark.integration, pytest.mark.rls]


@pytest.fixture
async def intruder(owner_engine: AsyncEngine, session_factory):
    """A second, empty tenant. Its only job is to fail to see anything.

    Given a location, because a tenant with no location cannot even ask the
    booking questions — and a test that fails because the *query* could not be
    formed would prove nothing about isolation.
    """
    tenant_id = uuid7()
    async with owner_engine.begin() as conn:
        await _set_force(conn, enabled=False)
        await conn.execute(
            text(
                "INSERT INTO tenants (id, slug, legal_name, display_name, status, "
                "  default_timezone, default_currency) "
                "VALUES (:id, :slug, 'Intruder Ltd', 'Intruder', 'active', :tz, 'GBP')"
            ),
            {"id": tenant_id, "slug": f"in-{str(tenant_id)[:12]}", "tz": TZ},
        )
        await conn.execute(
            text(
                "INSERT INTO locations (id, tenant_id, code, name, timezone) "
                "VALUES (:id, :tid, 'main', 'Main', :tz)"
            ),
            {"id": uuid7(), "tid": tenant_id, "tz": TZ},
        )
        await _set_force(conn, enabled=True)

    yield tenant_id

    async with owner_engine.begin() as conn:
        await _set_force(conn, enabled=False)
        await conn.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
        )
        await _set_force(conn, enabled=True)


async def test_a_booking_is_invisible_to_another_tenant(
    fixture: Fixture, intruder: UUID, session_factory
) -> None:
    """The whole claim, in one test.

    Tenant A books. Tenant B — a real tenant, with a real session, through the
    real session factory — cannot see the row by id, by reference, or by counting
    the table. And tenant A still can, which is what stops this passing for the
    wrong reason.
    """
    day = Fixture.next_weekday(2)
    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        booking = await service.book(
            service_code="standard",
            start_at=Fixture.local(day, time(9, 0)),
            customer_name="Private Patient",
            customer_phone_e164="+447700900123",
        )

    # ── The intruder's session. Same factory, same pool, different tenant. ──
    async with session_factory.unit_of_work(
        TenantContext(tenant_id=intruder)
    ) as session:
        repo = BookingRepository(session)

        assert await repo.get_booking(booking.id) is None, (
            "A booking was readable by id from another tenant. RLS is not "
            "filtering `bookings`."
        )
        assert await repo.booking_by_reference(booking.reference) is None, (
            "A booking was readable by reference from another tenant. The "
            "reference is quoted aloud on the phone, so this is the lookup an "
            "attacker would actually try."
        )
        assert await repo.bookings_by_phone("+447700900123") == []

        visible = (
            await session.execute(text("SELECT count(*) FROM bookings"))
        ).scalar_one()
        assert visible == 0, (
            f"The intruder sees {visible} bookings. A bare SELECT with no "
            f"tenant predicate must return zero rows — that is what the RLS "
            f"policy is for, and the repository's own predicate is only "
            f"defence in depth."
        )

        # And the child rows, which carry the resource allocations.
        items = (
            await session.execute(text("SELECT count(*) FROM booking_items"))
        ).scalar_one()
        assert items == 0

    # The owner can still see it — otherwise this test would pass against a
    # broken connection.
    async with fixture.unit_of_work() as session:
        assert await BookingRepository(session).get_booking(booking.id) is not None


async def test_a_tenant_cannot_write_a_booking_stamped_with_another_tenants_id(
    fixture: Fixture, intruder: UUID, session_factory
) -> None:
    """``WITH CHECK``, which is the clause whose absence is invisible.

    Without it a tenant can ``INSERT`` a row carrying someone else's
    ``tenant_id``. The row is immediately invisible to the writer — so nothing
    looks wrong — but it is *there*, corrupting the other tenant's data. That is
    why 01 §11.2 requires both ``USING`` and ``WITH CHECK`` on every policy.
    """
    day = Fixture.next_weekday(2)
    start = Fixture.local(day, time(9, 0))

    async with session_factory.unit_of_work(
        TenantContext(tenant_id=intruder)
    ) as session:
        with pytest.raises(Exception) as caught:
            await session.execute(
                text(
                    "INSERT INTO bookings (id, tenant_id, location_id, service_id, "
                    "  reference, status, service_period, local_date) "
                    "VALUES (:id, :victim, :location_id, :service_id, 'BK-EVIL01', "
                    "        'pending', tstzrange(:start, :end, '[)'), :day)"
                ),
                {
                    "id": uuid7(),
                    "victim": fixture.tenant_id,
                    "location_id": fixture.location_id,
                    "service_id": fixture.services["standard"],
                    "start": start,
                    "end": start,
                    "day": day,
                },
            )

    message = str(caught.value).lower()
    assert "row-level security" in message or "violates" in message, (
        f"A cross-tenant INSERT must be refused by the policy's WITH CHECK "
        f"clause; got {caught.value}"
    )


async def test_the_catalogue_is_invisible_cross_tenant_too(
    fixture: Fixture, intruder: UUID, session_factory
) -> None:
    """Resources and services, not only bookings.

    A competitor learning a practice's staff list, its price list and its
    opening hours is a real disclosure even without a single patient record.
    Every table migration 0003 creates is registered in the §11.3 loop, and this
    asserts the loop actually ran for the ones a booking flow reads.
    """
    async with session_factory.unit_of_work(
        TenantContext(tenant_id=intruder)
    ) as session:
        for table in (
            "resource_types",
            "resources",
            "services",
            "service_resource_requirements",
            "availability_rules",
            "booking_holds",
            "waitlist_entries",
        ):
            # `table` is a literal from this tuple, never request data.
            count = (
                await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            ).scalar_one()
            assert count == 0, (
                f"{table} leaks {count} rows across tenants. Check it is in the "
                f"TENANT_OWNED_TABLES array of the migration that created it."
            )

        # And the intruder genuinely has a session that works, so the zeros
        # above are isolation rather than a broken connection.
        assert (
            await session.execute(text("SELECT count(*) FROM locations"))
        ).scalar_one() == 1


async def test_a_session_with_no_tenant_context_sees_nothing(
    fixture: Fixture, session_factory
) -> None:
    """Fail-closed — 01 §11.2's fourth deliberate property.

    ``app_current_tenant()`` returns NULL when the GUC is unset, and
    ``tenant_id = NULL`` is never true. So a code path that forgets to set
    context sees an **empty table** rather than everything. The ops session is
    exactly that path: it deliberately does not set ``app.tenant_id``.
    """
    day = Fixture.next_weekday(2)
    async with fixture.unit_of_work() as session:
        service = BookingService(BookingRepository(session))
        await service.book(
            service_code="standard",
            start_at=Fixture.local(day, time(9, 0)),
            customer_name="Someone",
        )

    async with session_factory.ops_unit_of_work() as ops:
        count = (
            await ops.execute(text("SELECT count(*) FROM bookings"))
        ).scalar_one()

    assert count == 0, (
        "A session without tenant context saw rows. `app_current_tenant()` must "
        "return NULL when unset so the predicate is never true — fail-closed by "
        "construction."
    )

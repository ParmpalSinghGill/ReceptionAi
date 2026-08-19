"""Fixtures for the booking-engine proofs.

**These tests run against a real PostgreSQL, as ``receptionai_app``.** That is
not thoroughness for its own sake — nearly every property under test *is* a
database property:

* the exclusion constraint is enforced by the storage engine, so a mocked
  repository proves only that the mock returns what it was told to;
* the concurrency test needs two genuinely concurrent transactions, which needs
  two connections;
* RLS invisibility needs a role that is not the owner and does not hold
  ``BYPASSRLS``.

They use the local dev stack rather than spinning a container, because they are
run constantly during development and a 40-second container start per run is the
difference between a suite people use and one they skip. They skip cleanly when
the stack is down.

Each test gets its **own tenant**, created and torn down around it. Sharing one
would make the exclusion-constraint tests order-dependent: a leftover booking
from an earlier test is indistinguishable from the one the current test is
asserting about.
"""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from packages.core.ids import uuid7
from packages.core.tenancy import TenantContext
from packages.persistence.engine import DEFAULT_SHARD, ConnectionRouter, ShardConfig
from packages.persistence.session import SessionFactory, TenantSession
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.integration

POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "55432"))
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "receptionai")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "localpass")

#: The runtime role. Not the owner, no BYPASSRLS — the properties these tests
#: assert are meaningless as a superuser.
APP_DSN = (
    f"postgresql+asyncpg://receptionai_app:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)
#: The privileged connection, used ONLY to create and drop test tenants —
#: something a tenant genuinely cannot do (01 §11.5), and to lift ``FORCE`` for
#: the moment that takes. The migrator credential is the local equivalent of the
#: ``SECURITY DEFINER`` provisioning function production would use; the same
#: reasoning ``tools/seed_demo.py`` documents at length.
OWNER_DSN = (
    f"postgresql+asyncpg://receptionai_migrator:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)

#: Tables the fixture writes that are ``FORCE``'d. FORCE applies policies even to
#: the owner, and the policies name only the runtime roles — so the migrator
#: matches none and can write nothing until FORCE is lifted. Lifted and restored
#: inside one transaction, which PostgreSQL makes transactional, so a crashed
#: test cannot leave the window open.
_FORCED_TABLES: tuple[str, ...] = (
    "tenants",
    "locations",
    "resource_types",
    "resources",
    "services",
    "service_resource_requirements",
    "availability_rules",
    "availability_exceptions",
    "holidays",
)

TZ = "Europe/London"
ZONE = ZoneInfo(TZ)


def _reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_STACK_UP = _reachable(POSTGRES_HOST, POSTGRES_PORT)

requires_stack = pytest.mark.skipif(
    not _STACK_UP,
    reason=(
        f"needs Postgres on {POSTGRES_HOST}:{POSTGRES_PORT}. Run `make up && "
        f"make migrate`."
    ),
)

pytestmark = [pytest.mark.integration, requires_stack]


@pytest_asyncio.fixture(scope="session")
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(OWNER_DSN, poolclass=None)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def ops_engine(owner_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """A connection as ``receptionai_ops``, for the sweeper.

    ``ops_expire_stale_holds`` is cross-tenant and is granted to this role and no
    other — the app role gets ``permission denied``, deliberately, because a
    cross-tenant write has no business on a request path (01 §11.4). Testing the
    sweeper therefore needs the credential the scheduled job will use, not a
    convenient superuser.

    The role is created ``NOLOGIN`` by migration 0001, so the test grants it a
    password and nothing else — in particular not ``BYPASSRLS``, which would make
    every assertion in this directory meaningless.
    """
    password = "ops_test_password"
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(f"ALTER ROLE receptionai_ops LOGIN PASSWORD '{password}'")
        )

    dsn = (
        f"postgresql+asyncpg://receptionai_ops:{password}"
        f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
    )
    engine = create_async_engine(dsn, poolclass=None)
    try:
        yield engine
    finally:
        await engine.dispose()
        async with owner_engine.begin() as conn:
            await conn.execute(text("ALTER ROLE receptionai_ops NOLOGIN"))


@pytest_asyncio.fixture(scope="session")
async def session_factory() -> AsyncIterator[SessionFactory]:
    router = ConnectionRouter(
        {DEFAULT_SHARD: ShardConfig(key=DEFAULT_SHARD, dsn=APP_DSN, pool_size=8)}
    )
    try:
        yield SessionFactory(router)
    finally:
        await router.dispose_all()


class Fixture:
    """A tenant with a full booking catalogue, ready to book against."""

    def __init__(
        self,
        *,
        tenant_id: UUID,
        location_id: UUID,
        services: dict[str, UUID],
        resources: dict[str, UUID],
        session_factory: SessionFactory,
    ) -> None:
        self.tenant_id = tenant_id
        self.location_id = location_id
        self.services = services
        self.resources = resources
        self._sessions = session_factory

    @property
    def context(self) -> TenantContext:
        return TenantContext(tenant_id=self.tenant_id)

    def unit_of_work(self) -> Any:
        return self._sessions.unit_of_work(self.context)

    @staticmethod
    def local(day: date, at: time) -> datetime:
        """A UTC instant from a local wall-clock time.

        Every test time is written the way a person says it — "next Tuesday at
        nine" — and converted here. A test that hardcoded a UTC instant would
        pass in winter and fail in summer, which is the exact class of bug the
        engine is built to avoid.
        """
        return datetime.combine(day, at, tzinfo=ZONE).astimezone(UTC)

    @staticmethod
    def next_weekday(offset_days: int = 1) -> date:
        """The next Mon-Fri at least ``offset_days`` away.

        Tests must land on a day the fixture practice is open, or every
        assertion becomes "no slots" and proves nothing about the constraint
        under test.
        """
        day = datetime.now(tz=ZONE).date() + timedelta(days=offset_days)
        while day.isoweekday() > 5:
            day += timedelta(days=1)
        return day


async def _set_force(conn: Any, *, enabled: bool) -> None:
    """Lift or restore ``FORCE ROW LEVEL SECURITY`` on the fixture's tables.

    Identifiers cannot be bound as parameters, and these names are module-level
    constants that never see request data. Restored inside the same transaction,
    so a failure rolls the lift back rather than leaving the window open.
    """
    keyword = "FORCE" if enabled else "NO FORCE"
    for table in _FORCED_TABLES:
        await conn.execute(text(f"ALTER TABLE {table} {keyword} ROW LEVEL SECURITY"))


async def _build_catalogue(
    conn: Any, tenant_id: UUID, location_id: UUID
) -> tuple[dict[str, UUID], dict[str, UUID]]:
    """One resource type, two resources, two services.

    Deliberately minimal and deliberately generic — a ``practitioner`` and a
    ``space``, not a dentist and a surgery. These tests assert engine behaviour,
    and naming them after a vertical here would be the same mistake invariant I4
    forbids in the engine itself.
    """
    type_ids: dict[str, UUID] = {}
    for code, name, granularity in (
        ("practitioner", "Practitioner", 15),
        ("space", "Space", 15),
    ):
        type_ids[code] = (
            await conn.execute(
                text(
                    "INSERT INTO resource_types (id, tenant_id, code, name, kind, "
                    "  is_capacity_shared, time_unit, slot_granularity_minutes) "
                    "VALUES (:id, :tid, :code, :name, "
                    "        CAST(:kind AS resource_kind), FALSE, 'minute', :grid) "
                    "RETURNING id"
                ),
                {
                    "id": uuid7(),
                    "tid": tenant_id,
                    "code": code,
                    "name": name,
                    "kind": "human" if code == "practitioner" else "space",
                    "grid": granularity,
                },
            )
        ).scalar_one()

    resource_ids: dict[str, UUID] = {}
    for code, name, type_code, order in (
        ("p-one", "Practitioner One", "practitioner", 1),
        ("p-two", "Practitioner Two", "practitioner", 2),
        ("space-one", "Space One", "space", 1),
        ("space-two", "Space Two", "space", 2),
    ):
        resource_ids[code] = (
            await conn.execute(
                text(
                    "INSERT INTO resources (id, tenant_id, location_id, "
                    "  resource_type_id, code, name, capacity, sort_order) "
                    "VALUES (:id, :tid, :lid, :type_id, :code, :name, 1, :order) "
                    "RETURNING id"
                ),
                {
                    "id": uuid7(),
                    "tid": tenant_id,
                    "lid": location_id,
                    "type_id": type_ids[type_code],
                    "code": code,
                    "name": name,
                    "order": order,
                },
            )
        ).scalar_one()

    service_ids: dict[str, UUID] = {}
    # `standard` has a 10-minute after-buffer so the buffered-occupancy and
    # back-to-back cases have something to bite on; `plain` has none, so the
    # half-open boundary can be tested without the buffer confounding it.
    for code, name, duration, buffer_after, cancel_window, fee in (
        ("standard", "Standard Service", 30, 10, 1440, "25.0000"),
        ("plain", "Unbuffered Service", 30, 0, 1440, "25.0000"),
    ):
        service_id = (
            await conn.execute(
                text(
                    "INSERT INTO services (id, tenant_id, location_id, code, name, "
                    "  duration_minutes, buffer_after_minutes, price, currency, "
                    "  lead_time_minutes, cancellation_window_minutes, "
                    "  cancellation_fee) "
                    "VALUES (:id, :tid, :lid, :code, :name, :duration, :buffer, "
                    "        50.0000, 'GBP', 0, :window, :fee) "
                    "RETURNING id"
                ),
                {
                    "id": uuid7(),
                    "tid": tenant_id,
                    "lid": location_id,
                    "code": code,
                    "name": name,
                    "duration": duration,
                    "buffer": buffer_after,
                    "window": cancel_window,
                    "fee": fee,
                },
            )
        ).scalar_one()
        service_ids[code] = service_id

        # Two requirements with DIFFERENT respects_buffers, exactly as 01 §14.1
        # does: the practitioner is free when the appointment ends, the space is
        # blocked through the turnaround.
        for role, type_code, respects in (
            ("primary", "practitioner", False),
            ("space", "space", True),
        ):
            await conn.execute(
                text(
                    "INSERT INTO service_resource_requirements (id, tenant_id, "
                    "  service_id, resource_type_id, role, quantity, "
                    "  units_per_resource, allocation_strategy, respects_buffers) "
                    "VALUES (:id, :tid, :sid, :type_id, :role, 1, 1, "
                    "        'first_available', :respects)"
                ),
                {
                    "id": uuid7(),
                    "tid": tenant_id,
                    "sid": service_id,
                    "type_id": type_ids[type_code],
                    "role": role,
                    "respects": respects,
                },
            )

    # Open every day, all day, so a test never fails because it picked a Sunday.
    await conn.execute(
        text(
            "INSERT INTO availability_rules (id, tenant_id, location_id, effect, "
            "  weekdays, start_time, end_time, effective_period, label) "
            "VALUES (:id, :tid, :lid, 'open', :weekdays, :opens, :closes, "
            "        daterange(:from_date, NULL, '[)'), 'test_hours')"
        ),
        {
            "id": uuid7(),
            "tid": tenant_id,
            "lid": location_id,
            "weekdays": [1, 2, 3, 4, 5, 6, 7],
            "opens": time(6, 0),
            "closes": time(22, 0),
            "from_date": date(2020, 1, 1),
        },
    )

    return service_ids, resource_ids


@pytest_asyncio.fixture
async def fixture(
    owner_engine: AsyncEngine, session_factory: SessionFactory
) -> AsyncIterator[Fixture]:
    """A throwaway tenant with a catalogue. Dropped afterwards.

    Created through the privileged connection because a tenant cannot create a
    tenant — ``tenants``' policy is ``id = app_current_tenant()``, so the app
    role cannot insert the first row by construction. Everything the *tests*
    then do runs as ``receptionai_app``.
    """
    tenant_id = uuid7()
    location_id = uuid7()

    async with owner_engine.begin() as conn:
        await _set_force(conn, enabled=False)
        await conn.execute(
            text(
                "INSERT INTO tenants (id, slug, legal_name, display_name, status, "
                "  default_timezone, default_currency) "
                "VALUES (:id, :slug, 'Test Ltd', 'Test', 'active', :tz, 'GBP')"
            ),
            {"id": tenant_id, "slug": f"bk-{str(tenant_id)[:12]}", "tz": TZ},
        )
        await conn.execute(
            text(
                "INSERT INTO locations (id, tenant_id, code, name, timezone, "
                "  currency, country_code) "
                "VALUES (:id, :tid, 'main', 'Main', :tz, 'GBP', 'GB')"
            ),
            {"id": location_id, "tid": tenant_id, "tz": TZ},
        )
        services, resources = await _build_catalogue(conn, tenant_id, location_id)
        await _set_force(conn, enabled=True)

    yield Fixture(
        tenant_id=tenant_id,
        location_id=location_id,
        services=services,
        resources=resources,
        session_factory=session_factory,
    )

    async with owner_engine.begin() as conn:
        await _set_force(conn, enabled=False)
        # CASCADE from `tenants` takes the whole catalogue and every booking
        # with it, which is what `ON DELETE CASCADE` on tenant children is for.
        await conn.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
        )
        await _set_force(conn, enabled=True)


@pytest_asyncio.fixture
async def uow(fixture: Fixture) -> AsyncIterator[TenantSession]:
    """One tenant-scoped transaction for a test that only needs one."""
    async with fixture.unit_of_work() as session:
        yield session

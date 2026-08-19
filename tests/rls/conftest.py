"""Fixtures for the RLS proofs.

These tests are the ones that gate merges, so the fixture layer deliberately
does the hard thing: it spins a real PostgreSQL 16 with pgvector, runs the real
Alembic migration as the real privileged role, and then connects as
``receptionai_app`` — a role that is not the owner and does not have
``BYPASSRLS``. Anything less proves nothing. A test that runs as the superuser
passes with RLS entirely absent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from packages.core.ids import uuid7
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from testcontainers.community.postgres import PostgresContainer

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The runtime role every isolation assertion runs as. Not the owner, no
#: BYPASSRLS — see `test_app_role_cannot_bypass_rls`.
APP_ROLE = "receptionai_app"
APP_PASSWORD = "app_test_password"


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """PostgreSQL 16 with pgvector, per contract §1's locked stack."""
    container = PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="postgres",
        password="postgres",
        dbname="receptionai",
        driver="asyncpg",
    )
    with container:
        yield container


@pytest.fixture(scope="session")
def migrated_dsn(postgres_container: PostgresContainer) -> str:
    """Run the real migration as the privileged role; return its DSN.

    ``alembic upgrade head`` in a subprocess rather than an in-process API call:
    ``migrations/env.py`` calls ``asyncio.run`` at module scope, which cannot run
    inside an already-running event loop. Shelling out also exercises the exact
    entry point a deploy uses, including the ``MIGRATION_DATABASE_URL`` guard.
    """
    dsn = postgres_container.get_connection_url()

    env = dict(os.environ)
    env["MIGRATION_DATABASE_URL"] = dsn
    env["PYTHONPATH"] = str(REPO_ROOT)

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"alembic upgrade head failed:\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return dsn


@pytest_asyncio.fixture(scope="session")
async def owner_engine(migrated_dsn: str) -> AsyncIterator[AsyncEngine]:
    """A privileged connection, used only for setup and for catalogue reads."""
    engine = create_async_engine(migrated_dsn, poolclass=None)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def app_dsn(migrated_dsn: str, owner_engine: AsyncEngine) -> str:
    """Give ``receptionai_app`` a password and LOGIN, and return its DSN.

    The migration creates the role ``NOLOGIN`` deliberately — a definition that
    cannot itself be used to connect. Tests need it to log in, so they grant
    exactly that and nothing else. In particular they do NOT grant ``BYPASSRLS``
    or ownership, which is what makes every assertion below meaningful.
    """
    from sqlalchemy import text

    async with owner_engine.begin() as conn:
        # `ALTER ROLE ... PASSWORD $1` is a syntax error for the same reason
        # `SET LOCAL app.tenant_id = $1` is: a utility statement is parsed
        # before parameters are bound. That is precisely why the session factory
        # goes through `set_config()` rather than `SET LOCAL` — see
        # `test_tenant_id_is_bound_not_interpolated`.
        #
        # Here both the role and the password are fixed module constants
        # matching ^[a-z_]+$, asserted below so this cannot quietly become an
        # interpolation site if someone parameterizes them later. Production
        # provisions these roles out-of-band with real secrets.
        assert APP_ROLE.replace("_", "").isalnum()
        assert APP_PASSWORD.replace("_", "").isalnum()
        await conn.execute(
            text(f"ALTER ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'")
        )

    # Rewrite user:password in the container DSN, leaving host/port/db intact.
    scheme, _, rest = migrated_dsn.partition("://")
    _, _, host_part = rest.partition("@")
    return f"{scheme}://{APP_ROLE}:{APP_PASSWORD}@{host_part}"


@pytest_asyncio.fixture(scope="session")
async def app_engine(app_dsn: str) -> AsyncIterator[AsyncEngine]:
    """The engine every isolation assertion runs through."""
    engine = create_async_engine(app_dsn, poolclass=None)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def two_tenants(owner_engine: AsyncEngine) -> tuple[UUID, UUID]:
    """Two tenants with a location and a conversation each.

    Seeded through the owner connection because a tenant cannot create a tenant
    (01 §11.5: provisioning goes through a ``SECURITY DEFINER`` function granted
    to a dedicated signup role). Ids are minted application-side, per
    amendment A2.
    """
    from sqlalchemy import text

    tenant_a, tenant_b = uuid7(), uuid7()

    async with owner_engine.begin() as conn:
        for tenant_id, slug in ((tenant_a, "alpha"), (tenant_b, "beta")):
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, legal_name, display_name) "
                    "VALUES (:id, :slug, :name, :name)"
                ),
                {"id": tenant_id, "slug": f"{slug}-{str(tenant_id)[:8]}", "name": slug},
            )
            await conn.execute(
                text(
                    "INSERT INTO locations (id, tenant_id, code, name, timezone) "
                    "VALUES (:id, :tid, 'main', 'Main', 'UTC')"
                ),
                {"id": uuid7(), "tid": tenant_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, tenant_id, channel, contact_identifier) "
                    "VALUES (:id, :tid, 'web_chat', :contact)"
                ),
                {"id": uuid7(), "tid": tenant_id, "contact": f"{slug}@example.com"},
            )

    yield tenant_a, tenant_b

    async with owner_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM tenants WHERE id = ANY(:ids)"),
            {"ids": [tenant_a, tenant_b]},
        )


async def set_tenant(conn: AsyncConnection, tenant_id: UUID | None) -> None:
    """Bind ``app.tenant_id`` exactly as the session factory does.

    Through ``set_config(name, value, is_local => true)`` with a BOUND
    PARAMETER, never an f-string (contract §2.4). The tests use the same
    mechanism as production so a change that breaks one breaks the other.
    """
    from sqlalchemy import text

    await conn.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id) if tenant_id is not None else ""},
    )

"""Tenant isolation, proved against a real PostgreSQL as the real app role.

01 §11.7: *"RLS is the kind of control that silently stops working."* Every
assertion here runs as ``receptionai_app`` — not the owner, no ``BYPASSRLS`` —
because a test that runs as the superuser passes with RLS entirely absent, which
is the exact failure mode these tests exist to catch.

Contract §2.7: cross-tenant access is impossible **by construction**, not by
convention. These are the tests that make "by construction" a claim rather than
an aspiration.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from packages.core.ids import uuid7
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.rls.conftest import APP_ROLE, set_tenant

pytestmark = [pytest.mark.rls, pytest.mark.integration]


async def test_cross_tenant_select_returns_zero_rows(
    app_engine: AsyncEngine, two_tenants: tuple[UUID, UUID]
) -> None:
    """Tenant A cannot see tenant B's rows. The base case.

    Note what is asserted: A sees its *own* row and exactly that. An assertion
    that only checked "B's row is absent" would also pass against a table that
    returned nothing at all, which is a different bug wearing the same result.
    """
    tenant_a, tenant_b = two_tenants

    async with app_engine.connect() as conn:
        await conn.begin()
        await set_tenant(conn, tenant_a)

        visible = (
            await conn.execute(text("SELECT tenant_id FROM conversations"))
        ).scalars().all()
        assert visible == [tenant_a], (
            f"tenant A must see exactly its own conversations; saw {visible}"
        )

        b_rows = (
            await conn.execute(
                text("SELECT count(*) FROM conversations WHERE tenant_id = :tid"),
                {"tid": tenant_b},
            )
        ).scalar_one()
        assert b_rows == 0, "tenant A saw tenant B's conversations"

        # Same story on every other tenant-owned table.
        for table in ("locations", "tenant_settings", "user_tenant_roles", "turns"):
            leaked = (
                await conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id = :tid"),  # noqa: S608
                    {"tid": tenant_b},
                )
            ).scalar_one()
            assert leaked == 0, f"{table} leaked rows across tenants"

        # `tenants` itself: its own id IS the scope.
        tenants_visible = (
            await conn.execute(text("SELECT id FROM tenants"))
        ).scalars().all()
        assert tenants_visible == [tenant_a]


async def test_cross_tenant_update_affects_zero_rows(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, two_tenants: tuple[UUID, UUID]
) -> None:
    """An UPDATE naming another tenant's rows touches nothing.

    This is the ``USING`` clause doing its second job: it filters not only what
    a SELECT returns but which rows an UPDATE or DELETE may touch. Without it, A
    could not *read* B's row but could still overwrite it — a blind write is
    still a write.

    The post-check reads back through the owner connection, because verifying
    "B's row is unchanged" from inside A's session would be checking a row A
    cannot see, which trivially "passes".
    """
    tenant_a, tenant_b = two_tenants

    async with app_engine.connect() as conn:
        await conn.begin()
        await set_tenant(conn, tenant_a)

        result = await conn.execute(
            text(
                "UPDATE conversations SET contact_name = 'hijacked' "
                "WHERE tenant_id = :tid"
            ),
            {"tid": tenant_b},
        )
        assert result.rowcount == 0, "cross-tenant UPDATE modified rows"

        # And an unqualified UPDATE, which is the more realistic accident: a
        # forgotten WHERE clause must still be confined to the bound tenant.
        result = await conn.execute(
            text("UPDATE conversations SET contact_name = 'own-tenant-edit'")
        )
        assert result.rowcount == 1, "unqualified UPDATE should hit A's row only"

        await conn.commit()

    async with owner_engine.connect() as conn:
        untouched = (
            await conn.execute(
                text(
                    "SELECT contact_name FROM conversations WHERE tenant_id = :tid"
                ),
                {"tid": tenant_b},
            )
        ).scalar_one()
        assert untouched is None, "tenant B's row was modified by tenant A"


async def test_cross_tenant_delete_affects_zero_rows(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, two_tenants: tuple[UUID, UUID]
) -> None:
    """A DELETE naming another tenant's rows removes nothing."""
    tenant_a, tenant_b = two_tenants

    async with app_engine.connect() as conn:
        await conn.begin()
        await set_tenant(conn, tenant_a)

        result = await conn.execute(
            text("DELETE FROM conversations WHERE tenant_id = :tid"),
            {"tid": tenant_b},
        )
        assert result.rowcount == 0, "cross-tenant DELETE removed rows"

        result = await conn.execute(text("DELETE FROM locations"))
        assert result.rowcount == 1, "unqualified DELETE should hit A's row only"

        await conn.commit()

    async with owner_engine.connect() as conn:
        surviving = (
            await conn.execute(
                text("SELECT count(*) FROM locations WHERE tenant_id = :tid"),
                {"tid": tenant_b},
            )
        ).scalar_one()
        assert surviving == 1, "tenant B's location was deleted by tenant A"


async def test_insert_stamped_with_another_tenant_is_rejected(
    app_engine: AsyncEngine, two_tenants: tuple[UUID, UUID]
) -> None:
    """``WITH CHECK`` refuses a row stamped with someone else's ``tenant_id``.

    This is the clause whose omission is subtle and catastrophic: with only
    ``USING``, the INSERT succeeds, the row becomes immediately invisible to the
    tenant who wrote it, and it sits in the *other* tenant's data corrupting
    their reports. Both clauses, always.
    """
    tenant_a, tenant_b = two_tenants

    async with app_engine.connect() as conn:
        await conn.begin()
        await set_tenant(conn, tenant_a)

        with pytest.raises(DBAPIError) as exc_info:
            await conn.execute(
                text(
                    "INSERT INTO conversations (id, tenant_id, channel) "
                    "VALUES (:id, :tid, 'web_chat')"
                ),
                {"id": uuid7(), "tid": tenant_b},
            )
        assert "row-level security" in str(exc_info.value).lower()
        await conn.rollback()


async def test_insert_naming_another_tenants_fk_is_rejected(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, two_tenants: tuple[UUID, UUID]
) -> None:
    """The composite-FK argument, proved. 01 §2.2.

    RLS filters *reads*. It does **not** stop an INSERT that names a foreign
    ``id`` the current tenant cannot see, because the FK check runs with RLS
    bypassed for referential integrity — so with a plain
    ``REFERENCES locations(id)``, a cross-tenant reference would be **accepted**
    and then dangle invisibly.

    ``fk_tenant_settings__locations`` references ``(tenant_id, id)`` against
    ``uq_locations__tenant_id_id`` instead. The row this test writes carries A's
    ``tenant_id`` (so RLS's ``WITH CHECK`` is satisfied and cannot be what
    rejects it) and B's ``location_id``. The pair ``(A, B's location)`` does not
    exist in ``locations``, so the FK fires.

    The assertion checks the SQLSTATE is 23503 specifically: if this ever failed
    with 42501 (insufficient privilege) instead, the test would still "pass" a
    naive `raises` check while proving something entirely different.
    """
    tenant_a, tenant_b = two_tenants

    async with owner_engine.connect() as conn:
        b_location = (
            await conn.execute(
                text("SELECT id FROM locations WHERE tenant_id = :tid"),
                {"tid": tenant_b},
            )
        ).scalar_one()

    async with app_engine.connect() as conn:
        await conn.begin()
        await set_tenant(conn, tenant_a)

        with pytest.raises(DBAPIError) as exc_info:
            await conn.execute(
                text(
                    "INSERT INTO tenant_settings "
                    "(id, tenant_id, location_id, namespace, key, value) "
                    "VALUES (:id, :tid, :loc, 'booking', 'lead_time', '60'::jsonb)"
                ),
                {"id": uuid7(), "tid": tenant_a, "loc": b_location},
            )

        sqlstate = getattr(exc_info.value.orig, "sqlstate", None)
        assert sqlstate == "23503", (
            f"expected foreign_key_violation (23503) from the composite FK; "
            f"got {sqlstate}. If this is 23514 or 42501, the composite FK is "
            f"not what rejected the row and the guarantee is not proved."
        )
        await conn.rollback()


async def test_app_role_cannot_bypass_rls(owner_engine: AsyncEngine) -> None:
    """The runtime role is not the owner and has no ``BYPASSRLS``.

    Contract §2.5. ``FORCE ROW LEVEL SECURITY`` is the second half of the same
    control: it defeats even a ``BYPASSRLS`` holder *for the table owner*, so
    accidentally running the app as ``receptionai_owner`` still applies policies.
    Both properties are asserted, because either alone leaves a hole.
    """
    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT rolbypassrls, rolsuper, rolcreaterole, rolcreatedb "
                    "FROM pg_roles WHERE rolname = :role"
                ),
                {"role": APP_ROLE},
            )
        ).one()
        bypassrls, is_super, createrole, createdb = row
        assert not bypassrls, f"{APP_ROLE} has BYPASSRLS — contract §2.5 violated"
        assert not is_super, f"{APP_ROLE} is a superuser"
        assert not createrole, f"{APP_ROLE} can create roles"
        assert not createdb, f"{APP_ROLE} can create databases"

        owned = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN pg_roles r ON r.oid = c.relowner "
                    "WHERE n.nspname = 'public' AND c.relkind IN ('r','p') "
                    "  AND r.rolname = :role"
                ),
                {"role": APP_ROLE},
            )
        ).scalar_one()
        assert owned == 0, (
            f"{APP_ROLE} owns {owned} tables. The owner is exempt from RLS "
            f"unless FORCE is set, so the runtime role must own nothing."
        )


async def test_unset_tenant_context_returns_zero_rows_not_everything(
    app_engine: AsyncEngine, two_tenants: tuple[UUID, UUID]
) -> None:
    """A session that forgets ``app.tenant_id`` sees nothing. 01 §1.6.

    ``app_current_tenant()`` uses ``current_setting(..., true)`` — the
    ``missing_ok`` form — so an unset GUC yields NULL rather than raising, and
    ``tenant_id = NULL`` is never true. Fail-closed by construction.

    Both states are tested: never-set, and explicitly set to the empty string
    (which is what a ``set_config`` call with a stringified ``None`` would
    produce). ``NULLIF(..., '')`` is what makes the second one behave like the
    first instead of raising an invalid-uuid cast error at some random later
    query.
    """
    async with app_engine.connect() as conn:
        await conn.begin()
        # Deliberately no set_tenant() call.
        for table in ("conversations", "locations", "tenant_settings", "tenants"):
            count = (
                await conn.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            ).scalar_one()
            assert count == 0, (
                f"{table} returned {count} rows with app.tenant_id unset. "
                f"Unset context must fail closed, not open."
            )

        current = (
            await conn.execute(text("SELECT app_current_tenant()"))
        ).scalar_one()
        assert current is None

        # The empty string, which is what a stringified None would set.
        await set_tenant(conn, None)
        current = (
            await conn.execute(text("SELECT app_current_tenant()"))
        ).scalar_one()
        assert current is None, "empty-string GUC must normalize to NULL"

        count = (
            await conn.execute(text("SELECT count(*) FROM conversations"))
        ).scalar_one()
        assert count == 0
        await conn.rollback()


async def test_set_local_does_not_leak_across_transactions(
    app_engine: AsyncEngine, two_tenants: tuple[UUID, UUID]
) -> None:
    """``SET LOCAL`` is discarded at COMMIT — THE multi-tenancy bug, closed.

    Connections are pooled. If a request issued a session-level
    ``SET app.tenant_id = 'A'`` and the connection returned to the pool without
    a reset, the next request would inherit A's context and — because RLS
    policies are doing exactly what they were told — read A's data with no error
    anywhere.

    This test reuses **the same connection** across two transactions on purpose:
    a fresh connection would reset the GUC for uninteresting reasons and prove
    nothing about the pooled case.
    """
    tenant_a, _ = two_tenants

    async with app_engine.connect() as conn:
        await conn.begin()
        await set_tenant(conn, tenant_a)
        assert (
            await conn.execute(text("SELECT app_current_tenant()"))
        ).scalar_one() == tenant_a
        await conn.commit()

        # Same physical connection, new transaction, no set_config call.
        await conn.begin()
        leaked = (
            await conn.execute(text("SELECT app_current_tenant()"))
        ).scalar_one()
        assert leaked is None, (
            "app.tenant_id survived COMMIT. The context must be transaction-"
            "scoped (set_config's is_local => true), or a pooled connection "
            "carries one tenant's scope into the next tenant's request."
        )
        count = (
            await conn.execute(text("SELECT count(*) FROM conversations"))
        ).scalar_one()
        assert count == 0
        await conn.rollback()


async def test_tenant_id_is_bound_not_interpolated(
    app_engine: AsyncEngine, two_tenants: tuple[UUID, UUID]
) -> None:
    """A hostile "tenant id" travels as a parameter and cannot break out.

    Contract §2.4: the tenant id is never interpolated into SQL by hand.
    ``SET LOCAL app.tenant_id = $1`` is a syntax error (``SET`` takes a literal,
    not a placeholder), so the factory goes through ``set_config()`` — an
    ordinary function call, which does take bind parameters.

    If the value were ever f-string'd in, the payload below would terminate the
    statement and run the injected one. Bound, it is just an invalid uuid, and
    the failure is a cast error inside ``app_current_tenant()`` — loud, local,
    and harmless.
    """
    from sqlalchemy import text as sql_text

    payload = "'); DROP TABLE conversations; --"

    async with app_engine.connect() as conn:
        await conn.begin()
        await conn.execute(
            sql_text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": payload},
        )
        with pytest.raises(DBAPIError):
            # The cast to uuid fails; nothing was executed as SQL.
            await conn.execute(sql_text("SELECT app_current_tenant()"))
        await conn.rollback()

    async with app_engine.connect() as conn:
        still_there = (
            await conn.execute(
                sql_text("SELECT to_regclass('public.conversations') IS NOT NULL")
            )
        ).scalar_one()
        assert still_there, "the injected DROP TABLE executed"

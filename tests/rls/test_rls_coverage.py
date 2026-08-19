"""Structural RLS coverage — 01 §11.7, the assertions that fail the build.

The isolation tests in ``test_tenant_isolation.py`` prove that the tables they
name are protected. These prove that *every* table is — including the one
someone adds next month and forgets to register. That difference is the whole
point: an isolation test enumerated by hand only ever covers the tables its
author remembered.
"""

from __future__ import annotations

import pytest
from packages.persistence.models.base import tenant_table_names
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.rls.migration_registry import TENANT_OWNED_TABLES

pytestmark = [pytest.mark.rls, pytest.mark.integration]


#: Tables that carry a ``tenant_id`` column but are NOT tenant-owned, so they
#: are covered by the narrower §11.5 policies rather than by the §11.3 loop.
#: Each is listed with the reason, because "it's an exception" is how an
#: unprotected table eventually gets added.
NON_TENANT_OWNED_WITH_TENANT_ID = {
    # Template roles have tenant_id IS NULL and are visible to every tenant, so
    # the predicate is `tenant_id IS NULL OR tenant_id = app_current_tenant()`
    # rather than plain equality (01 §11.5).
    "roles",
    "role_permissions",
    # A session exists before a tenant is chosen; its policy scopes on user_id.
    "sessions",
    # Its own id IS the tenant scope.
    "tenants",
}

#: Tables that are registered in the RLS loop and protected in the database, but
#: have no SQLAlchemy model — so the mapper registry cannot see them. Listing
#: them here keeps the *forward* check (every model is registered) strict while
#: admitting that the reverse direction has a legitimate blind spot.
#:
#: The booking engine is accessed through
#: ``packages/persistence/repositories/booking.py``, which is raw SQL by design:
#: ``service_period``/``occupies`` are ``TSTZRANGE`` whose every useful predicate
#: (``&&``, ``@>``) is written as SQL anyway, ``blocking_period`` is
#: ``GENERATED ALWAYS`` and must never be mappable, and the conflict path is two
#: ``SECURITY DEFINER`` functions that exist to hold an advisory lock. Mapping
#: them would add an attractive nuisance (an assignable ``blocking_period``) for
#: no benefit.
#:
#: They are NOT unprotected: ``test_every_tenant_table_has_rls_enabled_and_forced``
#: reads ``pg_class`` and covers every table with a ``tenant_id`` column
#: regardless of whether a model exists, and
#: ``test_every_registered_table_actually_has_a_policy`` reads ``pg_policies``.
#: This set only exempts them from the model-registry cross-check.
UNMAPPED_BY_DESIGN = {
    "resource_types",
    "resources",
    "services",
    "service_resource_requirements",
    "availability_rules",
    "availability_exceptions",
    "holidays",
    "bookings",
    "booking_items",
    "booking_holds",
    "waitlist_entries",
}

#: Indexes that legitimately do not lead with ``tenant_id`` (01 §11.7). Each is
#: listed explicitly so adding one is a deliberate act rather than drift.
INDEX_EXCEPTIONS = {
    "ix_user_tenant_roles__user_tenant",  # §3.4: pre-context login path
    "ix_user_tenant_roles__expires",  # expiry sweeper
    "ix_sessions__expires",
    "ix_sessions__user_live",
    "ix_sessions__family",
    "uq_sessions__refresh_token_hash",
    "ix_sessions__tenant_user",  # sessions.tenant_id is nullable, not owning
    "ix_turns__safety",  # GIN on an array cannot lead with a scalar
    "ix_roles__global_code",
    "uq_roles__global_code",
    "uq_roles__tenant_code",
    "uq_roles__id_tenant",
    "ix_role_permissions__permission",
    "pk_roles",
    "pk_role_permissions",
    "pk_sessions",
    "pk_tenants",
    # ── Single-column primary keys on tenant-owned tables ────────────────
    # 01 §1.4's standard column set is `id UUID PRIMARY KEY`, which by
    # construction cannot lead with tenant_id. Contract §2.6's rule exists so
    # that RLS-filtered queries can use an index; a unique index on `id` alone
    # already resolves a point lookup in one probe, and the RLS predicate is
    # then a filter on the single row fetched — so the rule's *purpose* is met
    # even though its letter is not.
    #
    # This is a genuine tension between 01 §1.4 and contract §2.6 that the
    # documents do not reconcile. Making the PK `(tenant_id, id)` instead would
    # satisfy both, at the cost of widening every FK that references these
    # tables. The partitioned tables already do exactly that, for an unrelated
    # reason (PostgreSQL requires the partition key in a unique constraint), so
    # the pattern is not foreign to the schema. Flagged for a decision rather
    # than silently exempted.
    "pk_locations",
    "pk_tenant_settings",
    "pk_user_tenant_roles",
    "pk_conversations",  # already (tenant_id, created_at, id) — listed for symmetry
    "pk_turns",
    "uq_tenants__slug",
    "ix_tenants__shard_key",
    "ix_tenants__status",
    # ── Booking engine (migration 0003) ──────────────────────────────────
    #
    # Single-column primary keys, the same 01 §1.4-versus-contract-§2.6 tension
    # already documented above for `pk_locations` and friends. A unique index on
    # `id` alone resolves a point lookup in one probe and the RLS predicate then
    # filters the single row fetched, so the rule's purpose is met.
    "pk_resource_types",
    "pk_resources",
    "pk_services",
    "pk_service_resource_requirements",
    "pk_availability_rules",
    "pk_availability_exceptions",
    "pk_holidays",
    "pk_bookings",
    "pk_booking_items",
    "pk_booking_holds",
    "pk_waitlist_entries",
    # ── Deliberately cross-tenant sweeper indexes ────────────────────────
    #
    # `ops_expire_stale_holds` and the waitlist offer-timeout job both scan "what
    # has expired, across every tenant" — that is the whole point of a sweeper,
    # and it runs as `receptionai_ops` through a SECURITY DEFINER function
    # (01 §11.4), never on a request path. Leading with tenant_id would make the
    # index useless for the only query it exists to serve.
    "ix_booking_holds__expiry_sweep",
    "ix_waitlist_entries__offer_expiry",
    # ── GIN indexes, which cannot lead with a scalar ─────────────────────
    #
    # The same structural exception already granted to `ix_turns__safety`: a GIN
    # index over a JSONB or array column has no leading scalar key to give.
    # `tenant_id` is still applied as a filter by the RLS predicate.
    "ix_resources__attributes",
    "ix_resources__tags",
    "ix_availability_rules__weekdays",
}


async def test_every_tenant_table_has_rls_enabled_and_forced(
    owner_engine: AsyncEngine,
) -> None:
    """01 §11.7's first assertion: zero rows = pass.

    Partitions are excluded because RLS on a partitioned parent propagates to
    every partition at query time, including partitions created later —
    ``relrowsecurity`` on the child is false and *should* be. Defining policies
    on individual partitions would create a second, divergent policy set, which
    is worse than none.
    """
    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                      FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = 'public'
                       AND c.relkind IN ('r', 'p')
                       AND NOT c.relispartition
                       AND EXISTS (
                           SELECT 1 FROM pg_attribute a
                            WHERE a.attrelid = c.oid
                              AND a.attname = 'tenant_id'
                              AND a.attnum > 0
                              AND NOT a.attisdropped
                       )
                       AND NOT (c.relrowsecurity AND c.relforcerowsecurity)
                    """
                )
            )
        ).all()

    assert rows == [], (
        "Tables with a tenant_id column but without RLS enabled AND forced: "
        f"{[r[0] for r in rows]}. Both are required (contract §2.2): ENABLE "
        f"applies policies to non-owners, FORCE applies them to the owner too."
    )


async def test_every_tenant_owned_table_is_registered_in_the_rls_loop() -> None:
    """The migration's registry array covers every tenant-owned model.

    Compares the ``DO $$`` loop's array against the mapper registry, which
    enumerates tables by introspection rather than by hand. A new tenant-owned
    model that nobody added to the array fails here — at build time, with a name
    — instead of shipping a table with no policy on it.
    """
    registered = set(TENANT_OWNED_TABLES)
    mapped = set(tenant_table_names()) - NON_TENANT_OWNED_WITH_TENANT_ID

    unregistered = mapped - registered
    assert not unregistered, (
        f"Tenant-owned tables missing from the RLS loop array: "
        f"{sorted(unregistered)}. Add them to TENANT_OWNED_TABLES in the "
        f"migration that creates them, or — if they are genuinely not "
        f"tenant-owned — to NON_TENANT_OWNED_WITH_TENANT_ID with a reason."
    )

    phantom = registered - mapped - UNMAPPED_BY_DESIGN
    assert not phantom, (
        f"RLS loop registers tables with no corresponding model: "
        f"{sorted(phantom)}. Either the model was deleted and the array was "
        f"not updated, or the name is misspelled — in which case the real "
        f"table has no policy."
    )


async def test_every_registered_table_actually_has_a_policy(
    owner_engine: AsyncEngine,
) -> None:
    """The array is not merely present; the loop actually ran.

    A registry that lists a table proves nothing if the DDL that consumes it was
    skipped, so this reads ``pg_policies`` rather than trusting the Python list.
    """
    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename, policyname, cmd, qual, with_check "
                    "FROM pg_policies WHERE schemaname = 'public'"
                )
            )
        ).all()

    policies_by_table: dict[str, list[tuple[str, str, str | None, str | None]]] = {}
    for tablename, policyname, cmd, qual, with_check in rows:
        policies_by_table.setdefault(tablename, []).append(
            (policyname, cmd, qual, with_check)
        )

    for table in TENANT_OWNED_TABLES:
        entries = policies_by_table.get(table, [])
        assert entries, f"{table} is registered but has no policy"

        expected_name = f"rls_{table}__all"
        match = [e for e in entries if e[0] == expected_name]
        assert match, f"{table} lacks the expected policy {expected_name}"

        _, cmd, qual, with_check = match[0]
        assert cmd == "ALL", (
            f"{expected_name} is FOR {cmd}, not FOR ALL. Multiple permissive "
            f"policies are OR-ed, so splitting one into several can only ever "
            f"widen access (01 §11.2)."
        )
        assert qual and "app_current_tenant()" in qual, (
            f"{expected_name} USING clause does not call app_current_tenant(): "
            f"{qual!r}"
        )
        # WITH CHECK is the clause whose absence lets a tenant write a row
        # stamped with someone else's id.
        assert with_check and "app_current_tenant()" in with_check, (
            f"{expected_name} has no WITH CHECK clause. Without it a tenant can "
            f"INSERT a row carrying another tenant's id — invisible to them, "
            f"but present and corrupting the other tenant's data (01 §11.2)."
        )


async def test_no_policy_applies_to_public(owner_engine: AsyncEngine) -> None:
    """Every policy names its roles.

    A policy without a ``TO`` clause applies to ``PUBLIC``, which includes roles
    added later for entirely unrelated reasons. Naming the roles means a new
    role gets *no* access until someone deliberately grants it (01 §11.2).
    """
    async with owner_engine.connect() as conn:
        offenders = (
            await conn.execute(
                text(
                    "SELECT tablename, policyname, roles FROM pg_policies "
                    "WHERE schemaname = 'public' AND '0' = ANY(roles::text[])"
                )
            )
        ).all()

    assert offenders == [], (
        f"Policies applying to PUBLIC: {offenders}. Name the roles explicitly."
    )


async def test_indexes_on_tenant_tables_lead_with_tenant_id(
    owner_engine: AsyncEngine,
) -> None:
    """Contract §2.6, with its exceptions declared rather than discovered.

    The rule exists so that RLS-filtered queries — which always carry
    ``tenant_id = app_current_tenant()`` — can use an index. An index that does
    not lead with ``tenant_id`` is either a deliberate pre-context access path
    (login, API-key resolution) or a mistake, and the difference has to be
    written down somewhere a reviewer will look.
    """
    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT i.indexrelid::regclass::text AS offending_index,
                           i.indrelid::regclass::text   AS on_table
                      FROM pg_index i
                      JOIN pg_class c ON c.oid = i.indrelid
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                      JOIN pg_attribute a
                        ON a.attrelid = c.oid AND a.attname = 'tenant_id'
                     WHERE n.nspname = 'public'
                       AND c.relkind IN ('r','p')
                       AND NOT c.relispartition
                       AND i.indkey[0] <> a.attnum
                    """
                )
            )
        ).all()

    offenders = {index for index, _ in rows} - INDEX_EXCEPTIONS
    assert not offenders, (
        f"Indexes on tenant tables not leading with tenant_id: "
        f"{sorted(offenders)}. Either lead with tenant_id or add the index to "
        f"INDEX_EXCEPTIONS with the pre-context access path it serves "
        f"(contract §2.6, 01 §11.7)."
    )


async def test_all_persisted_ids_are_uuidv7(
    owner_engine: AsyncEngine, two_tenants: tuple[object, object]
) -> None:
    """Contract A2's SQL assertion: no persisted id is a v4.

    A silent ``uuid4()`` fallback satisfies every type signature and every other
    test in this suite while destroying the index locality UUIDv7 was chosen
    for — and making ``uuid7_timestamp()`` return a plausible-looking lie, which
    breaks partition pruning on exactly the tables that need it. This is the
    assertion that catches it.

    Version is nibble 13 of the hex form (``xxxxxxxx-xxxx-Vxxx-...``).
    """
    checks = [
        ("tenants", "id"),
        ("locations", "id"),
        ("conversations", "id"),
    ]
    async with owner_engine.connect() as conn:
        for table, column in checks:
            bad = (
                await conn.execute(
                    text(
                        f"SELECT count(*) FROM {table} "
                        f"WHERE substring({column}::text FROM 15 FOR 1) <> '7'"
                    )
                )
            ).scalar_one()
            assert bad == 0, (
                f"{bad} rows in {table}.{column} are not UUIDv7. Either a "
                f"uuid4() fallback crept in (contract A2 forbids it) or "
                f"uuid_generate_v7() is producing the wrong version nibble."
            )


async def test_db_side_generator_matches_application_side(
    owner_engine: AsyncEngine,
) -> None:
    """``uuid_generate_v7()`` and ``packages.core.ids.uuid7()`` agree.

    Contract A2 makes the DB-side function the fallback for migrations,
    backfills, and ``INSERT ... SELECT``, on the condition that it produces
    values indistinguishable from the application's. Two properties are checked:
    the version/variant nibbles, and that the embedded timestamp round-trips
    through both ``uuid_v7_timestamp`` (SQL) and ``uuid7_timestamp`` (Python).
    """
    from datetime import UTC, datetime

    from packages.core.ids import uuid7, uuid7_timestamp

    async with owner_engine.connect() as conn:
        db_id = (await conn.execute(text("SELECT uuid_generate_v7()"))).scalar_one()
        assert db_id.version == 7, f"DB generator produced v{db_id.version}"
        assert db_id.variant == "specified in RFC 4122"

        db_ts = (
            await conn.execute(
                text("SELECT uuid_v7_timestamp(:u)"), {"u": db_id}
            )
        ).scalar_one()
        py_ts = uuid7_timestamp(db_id)
        # The SQL function returns a timestamptz; compare instants, allowing a
        # millisecond of representation slack.
        assert abs((db_ts - py_ts).total_seconds()) < 0.002, (
            f"uuid_v7_timestamp({db_id}) = {db_ts} but uuid7_timestamp() = "
            f"{py_ts}; the two extractors disagree."
        )

        # And the reverse: the SQL extractor understands an application-minted id.
        app_id = uuid7()
        sql_ts = (
            await conn.execute(
                text("SELECT uuid_v7_timestamp(:u)"), {"u": app_id}
            )
        ).scalar_one()
        assert abs((sql_ts - uuid7_timestamp(app_id)).total_seconds()) < 0.002
        assert abs((sql_ts - datetime.now(tz=UTC)).total_seconds()) < 60

"""The models and the migration describe the same database.

Two hand-written descriptions of one schema drift, and the drift is silent: the
ORM keeps working against the columns it knows about, so the first symptom is a
query that references a column the migration never created — in production, at
runtime.

Alembic's ``--autogenerate`` is the usual guard, but it cannot see RLS policies,
partitioning, or ``DO $$`` blocks, so this project writes migrations by hand.
That makes an explicit parity check the thing standing between the two
descriptions.
"""

from __future__ import annotations

import pytest
from packages.persistence.models.base import Base
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.rls, pytest.mark.integration]


async def test_every_model_table_exists_in_the_database(
    owner_engine: AsyncEngine,
) -> None:
    async with owner_engine.connect() as conn:
        actual = set(
            await conn.run_sync(lambda sync: inspect(sync).get_table_names(schema="public"))
        )

    expected = set(Base.metadata.tables)
    missing = expected - actual
    assert not missing, (
        f"Models declare tables the migration never created: {sorted(missing)}. "
        f"Every model needs a migration; autogenerate cannot be relied on here "
        f"because RLS and partitioning are hand-written."
    )


async def test_model_columns_match_the_database(owner_engine: AsyncEngine) -> None:
    """Column names and nullability agree, table by table.

    Types are deliberately not compared: SQLAlchemy reflects ``TEXT`` as
    ``TEXT`` but ``CHAR(3)`` as ``CHAR(3)`` and enums as their own type objects,
    and a strict comparison turns into a pile of special cases that nobody
    maintains. Names and nullability are where the drift that actually breaks a
    query lives.
    """
    async with owner_engine.connect() as conn:
        for table_name, table in sorted(Base.metadata.tables.items()):
            reflected = await conn.run_sync(
                lambda sync, t=table_name: inspect(sync).get_columns(
                    t, schema="public"
                )
            )
            actual = {c["name"]: c for c in reflected}
            expected = {c.name: c for c in table.columns}

            missing = set(expected) - set(actual)
            assert not missing, (
                f"{table_name}: model declares columns the database lacks: "
                f"{sorted(missing)}"
            )

            extra = set(actual) - set(expected)
            assert not extra, (
                f"{table_name}: database has columns no model declares: "
                f"{sorted(extra)}. The ORM will never write them, so a NOT NULL "
                f"one would break every insert."
            )

            for name, column in expected.items():
                # Computed columns reflect as nullable regardless; skip them.
                if column.computed is not None:
                    continue
                assert column.nullable == actual[name]["nullable"], (
                    f"{table_name}.{name}: model says "
                    f"nullable={column.nullable}, database says "
                    f"nullable={actual[name]['nullable']}"
                )


async def test_partitioned_tables_are_actually_partitioned(
    owner_engine: AsyncEngine,
) -> None:
    """``conversations`` and ``turns`` are RANGE-partitioned on ``created_at``.

    The models declare ``postgresql_partition_by``, but that kwarg only affects
    ``metadata.create_all`` — the real DDL is hand-written in the migration, so
    nothing but this test connects the two. A ``turns`` table that silently came
    out unpartitioned would work fine until retention needed a ``DROP TABLE``
    and found a hundred million rows to ``DELETE`` instead.
    """
    async with owner_engine.connect() as conn:
        for table in ("conversations", "turns"):
            strategy = (
                await conn.execute(
                    text(
                        # ::text because partstrat is PostgreSQL's "char"
                        # type, which asyncpg hands back as raw bytes.
                        "SELECT p.partstrat::text FROM pg_partitioned_table p "
                        "JOIN pg_class c ON c.oid = p.partrelid "
                        "WHERE c.relname = :t"
                    ),
                    {"t": table},
                )
            ).scalar_one_or_none()
            assert strategy == "r", (
                f"{table} is not RANGE-partitioned (partstrat={strategy!r})"
            )

            key_column = (
                await conn.execute(
                    text(
                        "SELECT a.attname FROM pg_partitioned_table p "
                        "JOIN pg_class c ON c.oid = p.partrelid "
                        "JOIN pg_attribute a "
                        "  ON a.attrelid = c.oid AND a.attnum = p.partattrs[0] "
                        "WHERE c.relname = :t"
                    ),
                    {"t": table},
                )
            ).scalar_one()
            assert key_column == "created_at"

            partitions = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_inherits i "
                        "JOIN pg_class c ON c.oid = i.inhparent "
                        "WHERE c.relname = :t"
                    ),
                    {"t": table},
                )
            ).scalar_one()
            # Current month plus three ahead. Three months of headroom means an
            # outage in the partition scheduler has to last a very long time
            # before an insert fails — and there is deliberately no DEFAULT
            # partition to swallow the failure quietly.
            assert partitions >= 4, (
                f"{table} has only {partitions} partitions; expected the "
                f"current month and three ahead"
            )


async def test_no_default_partition_exists(owner_engine: AsyncEngine) -> None:
    """The loud failure is the design (01 §17.2).

    A default partition would silently accept rows outside every defined range,
    then make *adding* the correct partition require an ``ACCESS EXCLUSIVE``
    scan of the default. An insert with no matching partition raises instead,
    pages someone, and gets fixed.
    """
    async with owner_engine.connect() as conn:
        defaults = (
            await conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_inherits i ON i.inhrelid = c.oid "
                    "WHERE pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT'"
                )
            )
        ).scalars().all()
    assert defaults == [], f"default partitions present: {defaults}"


async def test_updated_at_trigger_fires(owner_engine: AsyncEngine) -> None:
    """``set_updated_at()`` is attached and works — and does not touch version.

    ``version`` is bumped explicitly by the repository so a lost update surfaces
    as ``rowcount = 0``. A trigger that also incremented it would make the
    optimistic-concurrency check unfalsifiable, because every writer's expected
    version would already be stale.
    """
    from packages.core.ids import uuid7

    tenant_id = uuid7()
    # Two SEPARATE transactions, deliberately. `now()` is fixed for the whole
    # transaction in PostgreSQL, so an INSERT and an UPDATE in one transaction
    # produce identical timestamps and the assertion below would fail against a
    # perfectly working trigger.
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, slug, legal_name, display_name) "
                "VALUES (:id, :slug, 'T', 'T')"
            ),
            {"id": tenant_id, "slug": f"trig-{str(tenant_id)[:8]}"},
        )

    async with owner_engine.begin() as conn:
        before = (
            await conn.execute(
                text(
                    "SELECT updated_at, version FROM tenants WHERE id = :id"
                ),
                {"id": tenant_id},
            )
        ).one()

        await conn.execute(
            text("UPDATE tenants SET display_name = 'T2' WHERE id = :id"),
            {"id": tenant_id},
        )
        after = (
            await conn.execute(
                text(
                    "SELECT updated_at, version FROM tenants WHERE id = :id"
                ),
                {"id": tenant_id},
            )
        ).one()

        assert after[0] > before[0], "updated_at was not bumped by the trigger"
        assert after[1] == before[1], (
            "version was bumped by a trigger. It must be incremented only by "
            "the repository's explicit UPDATE, or a lost update cannot be "
            "detected."
        )

        await conn.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
        )

"""Shared migration helpers — 01 §16.3.

Alembic autogenerate does **not** detect RLS policies. They must be written by
hand, and the discipline that makes that safe is this shared helper plus the CI
check in ``tests/rls/`` that fails the build if any tenant-owned table lacks
coverage.
"""

from __future__ import annotations

from alembic import op

__all__ = [
    "TENANT_ROLES",
    "add_updated_at_trigger",
    "disable_tenant_rls",
    "drop_updated_at_trigger",
    "enable_tenant_rls",
    "enable_tenant_rls_bulk",
]

#: The roles a tenant-table policy names. A policy without a ``TO`` clause
#: applies to ``PUBLIC``, which includes roles added later for unrelated
#: reasons; naming them means a new role gets *no* access until someone
#: deliberately grants it.
TENANT_ROLES = "receptionai_app, receptionai_reporting, receptionai_ops"


def enable_tenant_rls(table: str) -> None:
    """Apply the 01 §11.2 pattern to one table.

    Called at the END of every migration that creates a tenant-owned table.
    Four things about the emitted policy are deliberate:

    1. ``USING`` *and* ``WITH CHECK``. ``USING`` filters what you can see and
       which rows ``UPDATE``/``DELETE`` can touch; ``WITH CHECK`` validates rows
       you *write*. Omitting ``WITH CHECK`` would let a tenant ``INSERT`` a row
       stamped with another tenant's id — invisible to them, but *there*,
       corrupting the other tenant's data.
    2. ``FOR ALL`` rather than four separate policies. Multiple permissive
       policies on one table are OR-ed, so adding one can only ever *widen*
       access. One ``FOR ALL`` policy per table means the predicate is stated
       once and cannot be loosened by an addition.
    3. ``TO <explicit roles>``. See :data:`TENANT_ROLES`.
    4. ``app_current_tenant()`` returns NULL when the GUC is unset, and
       ``tenant_id = NULL`` is never true — so a code path that forgets to set
       context sees an empty table. Fail-closed.
    """
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE  ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY rls_{table}__all ON {table}
            FOR ALL
            TO {TENANT_ROLES}
            USING      (tenant_id = app_current_tenant())
            WITH CHECK (tenant_id = app_current_tenant())
        """
    )


def enable_tenant_rls_bulk(tables: list[str]) -> None:
    """Register a whole set of tenant-owned tables in one ``DO $$`` loop.

    This is 01 §11.3's pattern, and the loop is the point rather than an
    optimization: the array is a single, greppable registry of every
    tenant-owned table. A new table that its author forgets to add here is
    caught by ``tests/rls/test_rls_coverage.py``, which compares this array
    against every table in ``pg_class`` carrying a ``tenant_id`` column — so the
    omission fails CI instead of shipping an unprotected table.

    ``format(%I)`` quotes each identifier, so a table name is never
    concatenated raw into DDL.
    """
    array_literal = ", ".join(f"'{t}'" for t in tables)
    op.execute(
        f"""
        DO $$
        DECLARE
            t TEXT;
            tenant_tables TEXT[] := ARRAY[{array_literal}];
        BEGIN
            FOREACH t IN ARRAY tenant_tables LOOP
                EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
                EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
                EXECUTE format($f$
                    CREATE POLICY %I ON %I
                        FOR ALL
                        TO {TENANT_ROLES}
                        USING      (tenant_id = app_current_tenant())
                        WITH CHECK (tenant_id = app_current_tenant())
                $f$, 'rls_' || t || '__all', t);
            END LOOP;
        END;
        $$;
        """
    )


def disable_tenant_rls(table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS rls_{table}__all ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def add_updated_at_trigger(table: str) -> None:
    """Attach ``set_updated_at()``.

    Note it does not bump ``version``: that is incremented explicitly by the
    repository (``... SET version = version + 1 WHERE version = :expected``) so
    a lost update surfaces as ``rowcount = 0`` rather than being silently
    papered over. A trigger cannot report that the caller's expectation was
    violated.
    """
    op.execute(
        f"""
        CREATE TRIGGER trg_{table}_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def drop_updated_at_trigger(table: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")

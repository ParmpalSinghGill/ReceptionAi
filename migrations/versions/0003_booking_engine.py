"""The generic booking engine — 01 §5 and §13, for real.

Creates, in dependency order:

* the booking enum types (01 §1.7)
* ``resource_types``, ``resources``, ``services``,
  ``service_resource_requirements``, ``availability_rules``,
  ``availability_exceptions``, ``holidays``, ``bookings``, ``booking_items``,
  ``booking_holds``, ``waitlist_entries`` (01 §5.2–§5.12)
* the exclusion constraints that make double-booking impossible (01 §13.1)
* the occupancy-sync and period-cascade triggers (01 §5.10)
* ``booking_check_conflicts`` / ``booking_convert_hold`` — the advisory-locked
  hold-vs-booking path (01 §13.2, §13.3)
* ``ops_expire_stale_holds`` — layer 3 of the hold TTL (01 §13.3)
* RLS enabled + forced with policies on every one of them, registered in the
  §11.3 ``DO $$`` loop array

**The three details this migration exists to get right**, each of which is a
silent correctness hole if it is wrong:

1. **Half-open ``[)`` ranges, everywhere.** With inclusive ``[]`` ranges
   ``[10:00, 10:30]`` and ``[10:30, 11:00]`` overlap at the instant ``10:30``,
   so *every* back-to-back booking is rejected. The generated
   ``blocking_period`` expression spells ``'[)'`` explicitly rather than relying
   on ``tstzrange``'s default, and ``ck_booking_items__occupies_half_open``
   refuses a row whose ``occupies`` was constructed any other way — so a
   hand-written ``INSERT`` cannot reintroduce the bug the generated column
   avoids.

2. **``blocking_period`` is ``GENERATED ... STORED``.** The exclusion constraint
   indexes the *buffered* interval. If buffers were applied in application code
   writing a separate column, a bug or a direct SQL fix could desynchronise them
   and punch a hole in double-booking protection that nothing would report. A
   generated column makes the widened range a function of the stored inputs,
   enforced by the storage engine.

3. **``EXCLUDE USING gist`` needs ``btree_gist``** for the ``uuid WITH =``
   operators. Migration 0001 already creates it, deliberately and ahead of need
   — see its ``upgrade()`` comment.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from migrations.helpers import add_updated_at_trigger, enable_tenant_rls_bulk

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


# ── The RLS registry (01 §11.3) ──────────────────────────────────────────────
# Every tenant-owned table this migration creates. `tests/rls/test_rls_coverage`
# asserts this array plus 0001's covers every table in pg_class carrying a
# `tenant_id` column, so a table added without being registered here fails CI
# rather than shipping unprotected.
TENANT_OWNED_TABLES: list[str] = [
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
]

#: Every table this migration creates, for the ownership reassignment below.
#: 0002 established that `receptionai_owner` must own the tables for the default
#: privileges to apply and for SECURITY DEFINER functions to reach them; a table
#: created here is created *by the migrator*, so it needs the same treatment.
_ALL_TABLES: tuple[str, ...] = tuple(TENANT_OWNED_TABLES)


def _enum(name: str) -> pg.ENUM:
    """Reference an existing type without re-creating it."""
    return pg.ENUM(name=name, create_type=False)


def _create_shift_function() -> None:
    """``booking_shift_minutes(timestamptz, integer)`` — an IMMUTABLE instant shift.

    **This function exists because 01 §5.9's DDL as written does not compile.**
    That section defines ``blocking_period`` as::

        GENERATED ALWAYS AS (
            tstzrange(lower(service_period) - make_interval(mins => buffer_before_minutes),
                      upper(service_period) + make_interval(mins => buffer_after_minutes),
                      '[)')) STORED

    PostgreSQL rejects it with ``generation expression is not immutable``, and it
    is right to. ``make_interval`` and ``tstzrange`` are both IMMUTABLE, but the
    ``timestamptz + interval`` operator (``timestamptz_pl_interval``) is
    **STABLE**: an ``interval`` can carry months and days, whose length in real
    time depends on the session ``TimeZone`` — adding ``1 day`` across a DST
    boundary is 23 or 25 hours, not 24. The planner cannot know at DDL time that
    this particular interval is minutes-only, so the whole expression inherits
    STABLE and a generated column refuses it.

    That is a genuine defect in 01 §5.9's DDL, not a local deviation — the
    document's own worked example in §14.1 could not be executed as printed. It
    is flagged rather than silently patched.

    The fix preserves the semantics *exactly*. A ``timestamptz`` is an absolute
    instant; its epoch-second representation is timezone-independent by
    definition. Shifting by a fixed number of *seconds* in epoch space is
    therefore a pure function of its inputs, which is what IMMUTABLE means, and
    it produces byte-identical results to ``+ make_interval(mins => n)`` for
    every input — because a minutes-only interval has no months or days to
    resolve, so there was never any timezone dependence to lose.

    Why a function rather than inlining ``to_timestamp(extract(epoch ...))``: the
    generated expression is the load-bearing detail of the whole engine (01 §5.9)
    and it needs to read as "widen this range by the buffers", not as epoch
    arithmetic. The behaviour is also asserted directly, in
    ``tests/booking/test_double_booking.py``.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION booking_shift_minutes(
            p_at TIMESTAMPTZ, p_minutes INTEGER
        )
        RETURNS TIMESTAMPTZ
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        STRICT
        AS $$
            SELECT to_timestamp(extract(epoch FROM p_at) + p_minutes * 60)
        $$;
        """
    )
    op.execute(
        "ALTER FUNCTION booking_shift_minutes(TIMESTAMPTZ, INTEGER) "
        "OWNER TO receptionai_owner"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION booking_shift_minutes(TIMESTAMPTZ, INTEGER) "
        "TO receptionai_app, receptionai_reporting, receptionai_ops"
    )
    op.execute(
        "COMMENT ON FUNCTION booking_shift_minutes(TIMESTAMPTZ, INTEGER) IS "
        "'IMMUTABLE minute shift for bookings.blocking_period. 01 §5.9 uses "
        "`timestamptz + make_interval(mins => n)`, which is STABLE (an interval "
        "may carry months/days whose length depends on TimeZone) and is "
        "therefore rejected in a generated column. Epoch-second arithmetic on an "
        "absolute instant is equivalent and genuinely immutable.'"
    )


def _create_enums() -> None:
    """01 §1.7's booking vocabularies.

    Enums rather than TEXT+CHECK because each is a closed vocabulary the
    *platform* owns and each appears in an indexed predicate — ``is_blocking``
    is derived from ``booking_status``, the hold sweeper filters on
    ``hold_status``, and the availability materialiser filters on
    ``availability_effect``. 01 §1.7's rule is TEXT+CHECK only for vocabularies
    a vertical manifest might extend; none of these is.

    ``time_unit``, ``price_model``, ``allocation_strategy`` and ``source``
    deliberately stay TEXT+CHECK — those *are* extensible by a vertical.
    """
    op.execute(
        "CREATE TYPE resource_kind AS ENUM ('human','space','asset','virtual')"
    )
    op.execute(
        "CREATE TYPE booking_status AS ENUM "
        "('draft','pending','confirmed','checked_in','in_progress','completed',"
        "'cancelled','no_show')"
    )
    op.execute(
        "CREATE TYPE hold_status AS ENUM "
        "('active','converted','released','expired')"
    )
    op.execute(
        "CREATE TYPE waitlist_status AS ENUM "
        "('waiting','offered','converted','expired','cancelled')"
    )
    op.execute("CREATE TYPE availability_effect AS ENUM ('open','closed')")
    op.execute(
        "CREATE TYPE notification_channel AS ENUM "
        "('email','sms','push','webhook','voice')"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Catalogue: resource types, resources, services, requirements
# ─────────────────────────────────────────────────────────────────────────────


def _create_catalogue_tables() -> None:
    # ── resource_types (01 §5.2) ─────────────────────────────────────────
    #
    # The three switches that make one schema cover every vertical live here:
    # `is_capacity_shared` (exclusive vs shared), `time_unit` (minute / night /
    # day), and `slot_granularity_minutes` (grid vs free-floating). None of them
    # is a column a vertical adds; they are values a vertical *sets*.
    op.create_table(
        "resource_types",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", _enum("resource_kind"), nullable=False),
        # 01 §5.2 declares a composite FK to `tenant_vertical_installs`. That
        # table is 01 §4.3 and does not exist in this build — Phase 1 installs a
        # manifest into `tenant_settings` (apps/api/services/manifests.py) and
        # 01 §4 is not yet migrated. The column is kept, unconstrained, so the
        # FK can be added by the migration that creates `tenant_vertical_installs`
        # without a data change. Documented rather than silently dropped: this is
        # a real, deliberate deviation from 01 §5.2's DDL.
        sa.Column("vertical_install_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "is_capacity_shared", sa.Boolean(), nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column(
            "time_unit", sa.Text(), nullable=False, server_default=sa.text("'minute'")
        ),
        sa.Column("slot_granularity_minutes", sa.SmallInteger(), nullable=True),
        sa.Column(
            "attribute_schema", pg.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "filterable_attributes", pg.ARRAY(sa.Text()), nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "sort_order", sa.SmallInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_resource_types"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_resource_types__tenants",
        ),
        # The composite-FK target every child references. RLS filters *reads*;
        # it does not stop an INSERT naming a foreign id, because the FK check
        # runs with RLS bypassed for referential integrity. This unique key is
        # what closes that hole at the storage layer (contract §2, 01 §2.2).
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_resource_types__tenant_id_id"
        ),
        sa.CheckConstraint(
            r"code ~ '^[a-z][a-z0-9_]{1,47}$'", name="ck_resource_types__code"
        ),
        sa.CheckConstraint(
            "time_unit IN ('minute','hour','day','night')",
            name="ck_resource_types__time_unit",
        ),
        sa.CheckConstraint(
            "slot_granularity_minutes IS NULL "
            "OR slot_granularity_minutes BETWEEN 1 AND 1440",
            name="ck_resource_types__granularity",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(attribute_schema) = 'object'",
            name="ck_resource_types__attr_schema_object",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_resource_types__tenant_code "
        "ON resource_types (tenant_id, code)"
    )
    op.execute(
        "CREATE INDEX ix_resource_types__tenant_active "
        "ON resource_types (tenant_id, kind) WHERE is_active"
    )
    add_updated_at_trigger("resource_types")

    # ── resources (01 §5.3) ──────────────────────────────────────────────
    op.create_table(
        "resources",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_type_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "capacity", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column(
            "overbook_allowance", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "attributes", pg.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "tags", pg.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("external_ref", sa.Text(), nullable=True),
        sa.Column("external_system", sa.Text(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")
        ),
        sa.Column(
            "is_bookable", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")
        ),
        sa.Column(
            "sort_order", sa.SmallInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_resources"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_resources__tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.id"],
            ondelete="RESTRICT",
            name="fk_resources__locations",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "resource_type_id"],
            ["resource_types.tenant_id", "resource_types.id"],
            ondelete="RESTRICT",
            name="fk_resources__resource_types",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="SET NULL", name="fk_resources__users"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_resources__tenant_id_id"),
        sa.CheckConstraint("capacity > 0", name="ck_resources__capacity_positive"),
        sa.CheckConstraint(
            "overbook_allowance >= 0", name="ck_resources__overbook_nonneg"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(attributes) = 'object'", name="ck_resources__attributes_object"
        ),
        sa.CheckConstraint(
            "timezone IS NULL OR now() AT TIME ZONE timezone IS NOT NULL",
            name="ck_resources__timezone_valid",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_resources__tenant_location_code ON resources "
        "(tenant_id, location_id, code) WHERE deleted_at IS NULL"
    )
    # The primary availability query: "bookable resources of type T at
    # location L". INCLUDE carries the capacity columns so the planner does not
    # need a heap fetch to evaluate the shared-capacity path.
    op.execute(
        "CREATE INDEX ix_resources__tenant_type_location ON resources "
        "(tenant_id, resource_type_id, location_id) "
        "INCLUDE (capacity, overbook_allowance) "
        "WHERE is_active AND is_bookable AND deleted_at IS NULL"
    )
    # jsonb_path_ops is ~3x smaller and faster than the default opclass for pure
    # containment (@>), which is all the attribute predicate needs.
    op.execute(
        "CREATE INDEX ix_resources__attributes ON resources "
        "USING gin (attributes jsonb_path_ops) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_resources__tags ON resources USING gin (tags) "
        "WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_resources__external ON resources "
        "(tenant_id, external_system, external_ref) "
        "WHERE external_ref IS NOT NULL AND deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_resources__user ON resources (tenant_id, user_id) "
        "WHERE user_id IS NOT NULL AND deleted_at IS NULL"
    )
    add_updated_at_trigger("resources")

    # ── services (01 §5.4) ───────────────────────────────────────────────
    op.create_table(
        "services",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        # NULL = offered at every location.
        sa.Column("location_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("vertical_install_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("duration_units", sa.Integer(), nullable=True),
        sa.Column(
            "is_duration_variable", sa.Boolean(), nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column("min_duration_units", sa.Integer(), nullable=True),
        sa.Column("max_duration_units", sa.Integer(), nullable=True),
        sa.Column(
            "buffer_before_minutes", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "buffer_after_minutes", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "min_party_size", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column(
            "max_party_size", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column(
            "lead_time_minutes", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "booking_horizon_days", sa.Integer(), nullable=False,
            server_default=sa.text("90"),
        ),
        sa.Column(
            "cancellation_window_minutes", sa.Integer(), nullable=False,
            server_default=sa.text("1440"),
        ),
        sa.Column(
            "requires_approval", sa.Boolean(), nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column(
            "allow_waitlist", sa.Boolean(), nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column(
            "allow_double_booking", sa.Boolean(), nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column("price", sa.Numeric(12, 4), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=True),
        sa.Column(
            "price_model", sa.Text(), nullable=False, server_default=sa.text("'flat'")
        ),
        sa.Column("deposit_amount", sa.Numeric(12, 4), nullable=True),
        sa.Column("unit_price", sa.Numeric(12, 4), nullable=True),
        # Cancellation-policy fee, copied onto the booking at creation time. Not
        # in 01 §5.4's column list, which expresses the policy only as
        # `cancellation_window_minutes`; the manifest's cancellation block
        # (02 §1.10) also carries a fee, and storing it here rather than reading
        # it from the manifest at cancel time is the same copy-on-write
        # discipline 01 §5.9 applies to buffers — an admin editing the fee must
        # not retroactively change what an existing booking agreed to.
        sa.Column("cancellation_fee", sa.Numeric(12, 4), nullable=True),
        sa.Column(
            "intake_schema", pg.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("color", sa.Text(), nullable=True),
        sa.Column(
            "sort_order", sa.SmallInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")
        ),
        sa.Column(
            "is_bookable_online", sa.Boolean(), nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_services"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_services__tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.id"],
            ondelete="CASCADE",
            name="fk_services__locations",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_services__tenant_id_id"),
        sa.CheckConstraint(
            r"code ~ '^[a-z0-9][a-z0-9_-]{0,63}$'", name="ck_services__code"
        ),
        sa.CheckConstraint(
            "duration_minutes IS NOT NULL OR duration_units IS NOT NULL "
            "OR is_duration_variable",
            name="ck_services__duration_present",
        ),
        sa.CheckConstraint(
            "(duration_minutes IS NULL OR duration_minutes > 0) "
            "AND (duration_units IS NULL OR duration_units > 0)",
            name="ck_services__duration_positive",
        ),
        sa.CheckConstraint(
            "NOT is_duration_variable OR ("
            "  min_duration_units IS NOT NULL AND max_duration_units IS NOT NULL "
            "  AND min_duration_units >= 1 "
            "  AND max_duration_units >= min_duration_units)",
            name="ck_services__variable_bounds",
        ),
        sa.CheckConstraint(
            "buffer_before_minutes >= 0 AND buffer_after_minutes >= 0",
            name="ck_services__buffers_nonneg",
        ),
        sa.CheckConstraint(
            "min_party_size >= 1 AND max_party_size >= min_party_size",
            name="ck_services__party_bounds",
        ),
        sa.CheckConstraint(
            "booking_horizon_days BETWEEN 0 AND 3650", name="ck_services__horizon"
        ),
        sa.CheckConstraint(
            "price_model IN "
            "('flat','per_unit','per_person','per_unit_per_person','free','quote')",
            name="ck_services__price_model",
        ),
        sa.CheckConstraint(
            "(price IS NULL AND unit_price IS NULL) OR currency IS NOT NULL",
            name="ck_services__price_currency_paired",
        ),
        sa.CheckConstraint(
            "(price IS NULL OR price >= 0) "
            "AND (unit_price IS NULL OR unit_price >= 0) "
            "AND (deposit_amount IS NULL OR deposit_amount >= 0) "
            "AND (cancellation_fee IS NULL OR cancellation_fee >= 0)",
            name="ck_services__price_nonneg",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(intake_schema) = 'array'", name="ck_services__intake_array"
        ),
    )
    # Two partial uniques rather than one: NULL never equals NULL in a unique
    # index, so without the split a tenant could insert two global rows for the
    # same code.
    op.execute(
        "CREATE UNIQUE INDEX uq_services__tenant_code_global ON services "
        "(tenant_id, code) WHERE location_id IS NULL AND deleted_at IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_services__tenant_code_loc ON services "
        "(tenant_id, location_id, code) "
        "WHERE location_id IS NOT NULL AND deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_services__tenant_location_active ON services "
        "(tenant_id, location_id, sort_order) "
        "WHERE is_active AND deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_services__tenant_category ON services "
        "(tenant_id, category) WHERE is_active AND deleted_at IS NULL"
    )
    add_updated_at_trigger("services")

    # ── service_resource_requirements (01 §5.5) ──────────────────────────
    #
    # The join that makes the model generic. A service declares what KINDS of
    # resource it needs, how many, and with what attributes — it never names a
    # specific resource and it never hard-codes a vertical.
    op.create_table(
        "service_resource_requirements",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("service_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_type_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "role", sa.Text(), nullable=False, server_default=sa.text("'primary'")
        ),
        sa.Column(
            "quantity", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        # NULL = "derive from booking.party_size" (the restaurant/rental case).
        sa.Column("units_per_resource", sa.Integer(), nullable=True),
        sa.Column(
            "attribute_predicate", pg.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "is_required", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")
        ),
        sa.Column(
            "allocation_strategy", sa.Text(), nullable=False,
            server_default=sa.text("'best_fit'"),
        ),
        # Does this requirement consume the resource for the booking's blocking
        # range (true) or only its service range (false)? An operating theatre
        # is blocked through cleanup; a consulting surgeon is not.
        sa.Column(
            "respects_buffers", sa.Boolean(), nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_service_resource_requirements"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_service_resource_requirements__tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "service_id"],
            ["services.tenant_id", "services.id"],
            ondelete="CASCADE",
            name="fk_service_resource_requirements__services",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "resource_type_id"],
            ["resource_types.tenant_id", "resource_types.id"],
            ondelete="RESTRICT",
            name="fk_service_resource_requirements__resource_types",
        ),
        # Referenced by booking_items.requirement_id, which must not be able to
        # name a requirement in another tenant.
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_service_resource_requirements__tenant_id_id"
        ),
        sa.CheckConstraint(
            "quantity BETWEEN 1 AND 100", name="ck_service_resource_requirements__quantity"
        ),
        sa.CheckConstraint(
            "units_per_resource IS NULL OR units_per_resource >= 1",
            name="ck_service_resource_requirements__units",
        ),
        sa.CheckConstraint(
            r"role ~ '^[a-z][a-z0-9_]{0,31}$'",
            name="ck_service_resource_requirements__role",
        ),
        sa.CheckConstraint(
            "allocation_strategy IN "
            "('first_available','least_utilised','best_fit','round_robin','explicit')",
            name="ck_service_resource_requirements__strategy",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(attribute_predicate) = 'object'",
            name="ck_service_resource_requirements__predicate_object",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_srr__service_type_role "
        "ON service_resource_requirements "
        "(tenant_id, service_id, resource_type_id, role)"
    )
    op.execute(
        "CREATE INDEX ix_srr__tenant_service "
        "ON service_resource_requirements (tenant_id, service_id)"
    )
    op.execute(
        "CREATE INDEX ix_srr__tenant_resource_type "
        "ON service_resource_requirements (tenant_id, resource_type_id)"
    )
    add_updated_at_trigger("service_resource_requirements")


# ─────────────────────────────────────────────────────────────────────────────
# Availability: rules, exceptions, holidays
# ─────────────────────────────────────────────────────────────────────────────


def _create_availability_tables() -> None:
    # ── availability_rules (01 §5.6) ─────────────────────────────────────
    #
    # LOCAL WALL-CLOCK time plus a weekday mask, because "we open at 9 am" must
    # survive a DST transition. The engine materialises these into UTC tstzrange
    # candidate windows at query time using the location timezone — storing UTC
    # here would make a rule written in March wrong in November.
    op.create_table(
        "availability_rules",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_type_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("resource_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("service_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "effect", _enum("availability_effect"), nullable=False,
            server_default=sa.text("'open'"),
        ),
        # ISO-8601 weekday numbering (1=Monday … 7=Sunday). An array rather than
        # seven booleans: GIN-indexable, trivially expressible in a manifest, and
        # it reads naturally.
        sa.Column("weekdays", pg.ARRAY(sa.SmallInteger()), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column(
            "crosses_midnight",
            sa.Boolean(),
            sa.Computed("end_time <= start_time", persisted=True),
            nullable=False,
        ),
        # DATERANGE not TSTZRANGE: these are wall-clock calendar dates, not
        # instants.
        sa.Column(
            "effective_period", pg.DATERANGE(), nullable=False,
            server_default=sa.text("daterange(CURRENT_DATE, NULL, '[)')"),
        ),
        sa.Column(
            "priority", sa.SmallInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("capacity_override", sa.Integer(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_availability_rules"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_availability_rules__tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.id"],
            ondelete="CASCADE",
            name="fk_availability_rules__locations",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "resource_id"],
            ["resources.tenant_id", "resources.id"],
            ondelete="CASCADE",
            name="fk_availability_rules__resources",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "resource_type_id"],
            ["resource_types.tenant_id", "resource_types.id"],
            ondelete="CASCADE",
            name="fk_availability_rules__resource_types",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "service_id"],
            ["services.tenant_id", "services.id"],
            ondelete="CASCADE",
            name="fk_availability_rules__services",
        ),
        sa.CheckConstraint(
            "weekdays <@ ARRAY[1,2,3,4,5,6,7]::smallint[] "
            "AND array_length(weekdays, 1) BETWEEN 1 AND 7",
            name="ck_availability_rules__weekdays_valid",
        ),
        # Scope is a strict hierarchy: a resource rule must not also name a type.
        sa.CheckConstraint(
            "NOT (resource_id IS NOT NULL AND resource_type_id IS NOT NULL)",
            name="ck_availability_rules__scope_exclusive",
        ),
        sa.CheckConstraint(
            "capacity_override IS NULL OR capacity_override >= 0",
            name="ck_availability_rules__capacity",
        ),
        sa.CheckConstraint(
            "priority BETWEEN 0 AND 1000", name="ck_availability_rules__priority"
        ),
    )
    # The materialiser's query: "all active rules for this location, in this
    # date window, ordered by priority". GiST because `effective_period` is a
    # range; btree_gist makes the mixed opclass legal.
    op.execute(
        "CREATE INDEX ix_availability_rules__tenant_location_period "
        "ON availability_rules USING gist "
        "(tenant_id, location_id, effective_period) WHERE is_active"
    )
    op.execute(
        "CREATE INDEX ix_availability_rules__tenant_resource "
        "ON availability_rules (tenant_id, resource_id, priority DESC) "
        "WHERE resource_id IS NOT NULL AND is_active"
    )
    op.execute(
        "CREATE INDEX ix_availability_rules__tenant_type "
        "ON availability_rules (tenant_id, resource_type_id, priority DESC) "
        "WHERE resource_type_id IS NOT NULL AND is_active"
    )
    op.execute(
        "CREATE INDEX ix_availability_rules__tenant_service "
        "ON availability_rules (tenant_id, service_id) "
        "WHERE service_id IS NOT NULL AND is_active"
    )
    op.execute(
        "CREATE INDEX ix_availability_rules__weekdays "
        "ON availability_rules USING gin (weekdays) WHERE is_active"
    )
    add_updated_at_trigger("availability_rules")

    # ── availability_exceptions (01 §5.7) ────────────────────────────────
    op.create_table(
        "availability_exceptions",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_type_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("resource_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("service_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "effect", _enum("availability_effect"), nullable=False,
            server_default=sa.text("'closed'"),
        ),
        # Absolute UTC interval, half-open [) so back-to-back exceptions do not
        # collide. Unlike a rule, an exception is a concrete instant range.
        sa.Column("period", pg.TSTZRANGE(), nullable=False),
        sa.Column("capacity_override", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "source", sa.Text(), nullable=False, server_default=sa.text("'manual'")
        ),
        sa.Column("external_ref", sa.Text(), nullable=True),
        sa.Column("connector_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_availability_exceptions"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_availability_exceptions__tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.id"],
            ondelete="CASCADE",
            name="fk_availability_exceptions__locations",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "resource_id"],
            ["resources.tenant_id", "resources.id"],
            ondelete="CASCADE",
            name="fk_availability_exceptions__resources",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "resource_type_id"],
            ["resource_types.tenant_id", "resource_types.id"],
            ondelete="CASCADE",
            name="fk_availability_exceptions__resource_types",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "service_id"],
            ["services.tenant_id", "services.id"],
            ondelete="CASCADE",
            name="fk_availability_exceptions__services",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL",
            name="fk_availability_exceptions__users",
        ),
        sa.CheckConstraint(
            "lower(period) IS NOT NULL AND upper(period) IS NOT NULL "
            "AND NOT isempty(period)",
            name="ck_availability_exceptions__period_bounded",
        ),
        sa.CheckConstraint(
            "NOT (resource_id IS NOT NULL AND resource_type_id IS NOT NULL)",
            name="ck_availability_exceptions__scope_exclusive",
        ),
        sa.CheckConstraint(
            "source IN ('manual','calendar_sync','holiday','system','pms_sync')",
            name="ck_availability_exceptions__source",
        ),
    )
    op.execute(
        "CREATE INDEX ix_availability_exceptions__tenant_location_period "
        "ON availability_exceptions USING gist (tenant_id, location_id, period)"
    )
    op.execute(
        "CREATE INDEX ix_availability_exceptions__tenant_resource_period "
        "ON availability_exceptions USING gist (tenant_id, resource_id, period) "
        "WHERE resource_id IS NOT NULL"
    )
    # Idempotent re-sync from an external calendar: upsert on external identity.
    op.execute(
        "CREATE UNIQUE INDEX uq_availability_exceptions__external "
        "ON availability_exceptions (tenant_id, connector_id, external_ref) "
        "WHERE external_ref IS NOT NULL"
    )
    add_updated_at_trigger("availability_exceptions")

    # ── holidays (01 §5.8) ───────────────────────────────────────────────
    #
    # Kept separate from availability_exceptions because holidays are
    # DECLARATIVE and recurring ("UK bank holidays, observed") while exceptions
    # are concrete instants.
    op.create_table(
        "holidays",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        # NULL = applies to every location in the tenant.
        sa.Column("location_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        # The calendar date in LOCAL terms. Expansion to UTC uses the location tz.
        sa.Column("holiday_date", sa.Date(), nullable=False),
        sa.Column(
            "effect", _enum("availability_effect"), nullable=False,
            server_default=sa.text("'closed'"),
        ),
        # Partial-day holiday: NULL/NULL means the whole day.
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column(
            "recurrence", sa.Text(), nullable=False, server_default=sa.text("'none'")
        ),
        sa.Column("calendar_code", sa.Text(), nullable=True),
        sa.Column(
            "source", sa.Text(), nullable=False, server_default=sa.text("'manual'")
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_holidays"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_holidays__tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.id"],
            ondelete="CASCADE",
            name="fk_holidays__locations",
        ),
        sa.CheckConstraint(
            "recurrence IN ('none','annual')", name="ck_holidays__recurrence"
        ),
        sa.CheckConstraint(
            "(start_time IS NULL) = (end_time IS NULL)", name="ck_holidays__times_paired"
        ),
        sa.CheckConstraint(
            "source IN ('manual','imported','vertical')", name="ck_holidays__source"
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_holidays__tenant_loc_date_name_global "
        "ON holidays (tenant_id, holiday_date, name) WHERE location_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_holidays__tenant_loc_date_name "
        "ON holidays (tenant_id, location_id, holiday_date, name) "
        "WHERE location_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_holidays__tenant_date ON holidays "
        "(tenant_id, holiday_date) WHERE is_active"
    )
    # Annual recurrence lookup by month/day, ignoring the stored year.
    op.execute(
        "CREATE INDEX ix_holidays__annual ON holidays "
        "(tenant_id, (extract(month FROM holiday_date)::smallint), "
        " (extract(day FROM holiday_date)::smallint)) "
        "WHERE is_active AND recurrence = 'annual'"
    )
    add_updated_at_trigger("holidays")


# ─────────────────────────────────────────────────────────────────────────────
# Bookings, items, holds, waitlist
# ─────────────────────────────────────────────────────────────────────────────


def _create_booking_tables() -> None:
    # ── bookings (01 §5.9) ───────────────────────────────────────────────
    #
    # Written as raw DDL rather than op.create_table: `blocking_period` is a
    # GENERATED column whose expression references two other columns and
    # `make_interval`, which does not round-trip through SQLAlchemy's Computed()
    # cleanly with a range type, and the expression is important enough to be
    # visible verbatim.
    op.execute(
        """
        CREATE TABLE bookings (
            id                  UUID NOT NULL DEFAULT uuid_generate_v7()
                                CONSTRAINT pk_bookings PRIMARY KEY,
            tenant_id           UUID NOT NULL
                                REFERENCES tenants(id) ON DELETE CASCADE,
            location_id         UUID NOT NULL,
            service_id          UUID NOT NULL,

            -- Human-facing reference read aloud on the phone: 'BK-7F3K2Q'.
            reference           TEXT NOT NULL,

            status              booking_status NOT NULL DEFAULT 'pending',

            -- ── The two time ranges ──────────────────────────────────────
            -- service_period : what the customer experiences and is billed for.
            -- blocking_period: what the resource is actually unavailable for,
            --   i.e. service_period widened by the buffers. GENERATED, never
            --   hand-maintained, so the two can never disagree.
            --
            -- Both half-open [) so a 10:00-10:30 and a 10:30-11:00 booking do
            -- NOT overlap. This is the single most important detail in the whole
            -- engine: with inclusive ranges every back-to-back booking would
            -- collide, and the '[)' below is spelled out rather than left to
            -- tstzrange's default so it cannot be lost in a later edit.
            --
            -- `booking_shift_minutes` rather than 01 §5.9's
            -- `- make_interval(mins => ...)`: that operator is STABLE and
            -- PostgreSQL refuses it in a generated column. See
            -- _create_shift_function for the full reasoning and why the
            -- substitution is exact rather than approximate.
            service_period      TSTZRANGE NOT NULL,
            buffer_before_minutes INTEGER NOT NULL DEFAULT 0,
            buffer_after_minutes  INTEGER NOT NULL DEFAULT 0,
            blocking_period     TSTZRANGE GENERATED ALWAYS AS (
                tstzrange(
                    booking_shift_minutes(lower(service_period),
                                          -buffer_before_minutes),
                    booking_shift_minutes(upper(service_period),
                                          buffer_after_minutes),
                    '[)'
                )
            ) STORED,

            starts_at           TIMESTAMPTZ
                                GENERATED ALWAYS AS (lower(service_period)) STORED,
            ends_at             TIMESTAMPTZ
                                GENERATED ALWAYS AS (upper(service_period)) STORED,

            -- The local calendar date the booking "belongs to". Stored because
            -- deriving it needs the location tz, which a generated column has no
            -- access to (the tz lives in another table).
            local_date          DATE NOT NULL,

            party_size          INTEGER NOT NULL DEFAULT 1,
            duration_units      INTEGER NOT NULL DEFAULT 1,

            -- Deliberately denormalized rather than a customers FK: a
            -- receptionist takes bookings from people who are not registered
            -- customers, and the identity captured at booking time must not
            -- mutate when a customer record is later edited.
            customer_id         UUID,
            customer_name       TEXT,
            customer_phone_e164 TEXT,
            customer_email      TEXT,
            customer_locale     TEXT,

            intake_data         JSONB NOT NULL DEFAULT '{}'::jsonb,
            notes               TEXT,
            internal_notes      TEXT,

            -- FK to conversations is deliberately omitted: conversations is
            -- partitioned and may be pruned before bookings are.
            conversation_id     UUID,
            channel             conversation_channel,
            created_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
            booked_by           TEXT NOT NULL DEFAULT 'agent',

            price_total         NUMERIC(12,4),
            deposit_paid        NUMERIC(12,4),
            currency            CHAR(3),
            payment_status      TEXT NOT NULL DEFAULT 'none',
            external_payment_ref TEXT,

            -- Cancellation-policy values, copied on at creation for the same
            -- reason as the buffers: an admin editing the policy must not
            -- retroactively change what an existing booking agreed to.
            cancellation_window_minutes INTEGER NOT NULL DEFAULT 1440,
            cancellation_fee    NUMERIC(12,4),
            cancellation_fee_charged NUMERIC(12,4),

            -- 01 §5.13's recurrence_rules is not created by this migration
            -- (recurring bookings are not in this slice), so these columns exist
            -- unconstrained and the FK lands with that table.
            recurrence_rule_id  UUID,
            recurrence_instance_start TIMESTAMPTZ,
            is_recurrence_exception BOOLEAN NOT NULL DEFAULT FALSE,

            workflow_id         TEXT,
            workflow_run_id     TEXT,

            confirmed_at        TIMESTAMPTZ,
            checked_in_at       TIMESTAMPTZ,
            completed_at        TIMESTAMPTZ,
            cancelled_at        TIMESTAMPTZ,
            cancellation_reason TEXT,
            cancelled_by        TEXT,
            no_show_at          TIMESTAMPTZ,

            -- Reschedule chain: a new row points at the row it replaced.
            rescheduled_from_id UUID,

            external_ref        TEXT,
            external_system     TEXT,

            -- Idempotency: an agent retrying a create must not double-book.
            idempotency_key     TEXT,

            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            version             INTEGER NOT NULL DEFAULT 1,

            CONSTRAINT uq_bookings__tenant_id_id UNIQUE (tenant_id, id),

            CONSTRAINT fk_bookings__locations
                FOREIGN KEY (tenant_id, location_id)
                REFERENCES locations (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_bookings__services
                FOREIGN KEY (tenant_id, service_id)
                REFERENCES services (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_bookings__rescheduled_from
                FOREIGN KEY (tenant_id, rescheduled_from_id)
                REFERENCES bookings (tenant_id, id) ON DELETE SET NULL,

            CONSTRAINT ck_bookings__reference_shape
                CHECK (reference ~ '^[A-Z0-9-]{4,24}$'),
            CONSTRAINT ck_bookings__period_bounded
                CHECK (lower(service_period) IS NOT NULL
                       AND upper(service_period) IS NOT NULL
                       AND NOT isempty(service_period)),
            CONSTRAINT ck_bookings__period_order
                CHECK (upper(service_period) > lower(service_period)),
            -- The half-open guarantee, asserted rather than assumed. A row whose
            -- service_period was built '[]' or '(]' is rejected here, so a hand
            -- written INSERT cannot reintroduce the back-to-back collision the
            -- generated blocking_period expression avoids.
            CONSTRAINT ck_bookings__period_half_open
                CHECK (service_period
                       = tstzrange(lower(service_period), upper(service_period), '[)')),
            CONSTRAINT ck_bookings__buffers_nonneg
                CHECK (buffer_before_minutes >= 0 AND buffer_after_minutes >= 0),
            CONSTRAINT ck_bookings__party_positive CHECK (party_size >= 1),
            CONSTRAINT ck_bookings__units_positive CHECK (duration_units >= 1),
            CONSTRAINT ck_bookings__money_paired
                CHECK ((price_total IS NULL AND deposit_paid IS NULL)
                       OR currency IS NOT NULL),
            CONSTRAINT ck_bookings__money_nonneg
                CHECK ((price_total IS NULL OR price_total >= 0)
                       AND (deposit_paid IS NULL OR deposit_paid >= 0)
                       AND (cancellation_fee IS NULL OR cancellation_fee >= 0)
                       AND (cancellation_fee_charged IS NULL
                            OR cancellation_fee_charged >= 0)),
            CONSTRAINT ck_bookings__cancellation_window_nonneg
                CHECK (cancellation_window_minutes >= 0),
            CONSTRAINT ck_bookings__payment_status
                CHECK (payment_status IN ('none','pending','authorized','paid',
                                          'partially_refunded','refunded','failed')),
            CONSTRAINT ck_bookings__booked_by
                CHECK (booked_by IN ('agent','staff','customer','api','import','sync')),
            CONSTRAINT ck_bookings__phone_e164
                CHECK (customer_phone_e164 IS NULL
                       OR customer_phone_e164 ~ '^\\+[1-9]\\d{6,14}$'),

            -- Terminal-state coherence: a status implies its timestamp and vice
            -- versa. Prevents the classic "status says cancelled but the
            -- timestamp is null" inconsistency that makes reports lie.
            CONSTRAINT ck_bookings__cancelled_coherent
                CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL)),
            CONSTRAINT ck_bookings__completed_coherent
                CHECK (status <> 'completed' OR completed_at IS NOT NULL),
            CONSTRAINT ck_bookings__noshow_coherent
                CHECK ((status = 'no_show') = (no_show_at IS NOT NULL)),
            CONSTRAINT ck_bookings__confirmed_coherent
                CHECK (status NOT IN ('confirmed','checked_in','in_progress','completed')
                       OR confirmed_at IS NOT NULL),
            CONSTRAINT ck_bookings__intake_object
                CHECK (jsonb_typeof(intake_data) = 'object')
        );
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_bookings__tenant_reference "
        "ON bookings (tenant_id, reference)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_bookings__tenant_idempotency "
        "ON bookings (tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_bookings__external "
        "ON bookings (tenant_id, external_system, external_ref) "
        "WHERE external_ref IS NOT NULL"
    )
    # 01 §12.5's day-view and agenda queries.
    op.execute(
        "CREATE INDEX ix_bookings__tenant_local_date "
        "ON bookings (tenant_id, local_date, starts_at) "
        "WHERE status <> 'cancelled'"
    )
    op.execute(
        "CREATE INDEX ix_bookings__tenant_starts_at "
        "ON bookings (tenant_id, starts_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_bookings__tenant_service_status "
        "ON bookings (tenant_id, service_id, status, starts_at)"
    )
    # "Find my appointment" from the phone number the caller is speaking from —
    # the single hottest lookup on the voice path.
    op.execute(
        "CREATE INDEX ix_bookings__tenant_phone "
        "ON bookings (tenant_id, customer_phone_e164, starts_at DESC) "
        "WHERE customer_phone_e164 IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_bookings__tenant_conversation "
        "ON bookings (tenant_id, conversation_id) "
        "WHERE conversation_id IS NOT NULL"
    )
    add_updated_at_trigger("bookings")

    op.execute(
        "COMMENT ON COLUMN bookings.customer_name IS "
        "'pii_class=medium; masked in logs and traces'"
    )
    op.execute(
        "COMMENT ON COLUMN bookings.customer_phone_e164 IS "
        "'pii_class=medium; masked in logs and traces'"
    )
    op.execute(
        "COMMENT ON COLUMN bookings.intake_data IS "
        "'pii_class per field, from services.intake_schema; may contain PHI'"
    )

    # ── booking_items (01 §5.10) — where double-booking is prevented ─────
    op.execute(
        """
        CREATE TABLE booking_items (
            id                  UUID NOT NULL DEFAULT uuid_generate_v7()
                                CONSTRAINT pk_booking_items PRIMARY KEY,
            tenant_id           UUID NOT NULL
                                REFERENCES tenants(id) ON DELETE CASCADE,
            booking_id          UUID NOT NULL,
            resource_id         UUID NOT NULL,

            role                TEXT NOT NULL DEFAULT 'primary',
            requirement_id      UUID,

            quantity            INTEGER NOT NULL DEFAULT 1,

            -- Copied from the parent booking (service or blocking range,
            -- depending on the requirement's respects_buffers flag) BY TRIGGER,
            -- so the exclusion constraint can be a single-table check with no
            -- joins. This denormalization is not an optimisation: an EXCLUDE
            -- constraint cannot reference another table, so it is the only way
            -- to enforce the rule declaratively.
            occupies            TSTZRANGE NOT NULL,

            -- Mirrors bookings.status so the constraint can ignore cancelled
            -- rows. Maintained by trigger; never written by the application.
            is_blocking         BOOLEAN NOT NULL DEFAULT TRUE,
            is_capacity_shared  BOOLEAN NOT NULL DEFAULT FALSE,

            price_component     NUMERIC(12,4),
            currency            CHAR(3),
            external_event_ref  TEXT,

            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT fk_booking_items__bookings
                FOREIGN KEY (tenant_id, booking_id)
                REFERENCES bookings (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_booking_items__resources
                FOREIGN KEY (tenant_id, resource_id)
                REFERENCES resources (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_booking_items__requirements
                FOREIGN KEY (tenant_id, requirement_id)
                REFERENCES service_resource_requirements (tenant_id, id)
                ON DELETE SET NULL,
            CONSTRAINT ck_booking_items__quantity CHECK (quantity >= 1),
            CONSTRAINT ck_booking_items__occupies_bounded
                CHECK (lower(occupies) IS NOT NULL AND upper(occupies) IS NOT NULL
                       AND NOT isempty(occupies)),
            -- Half-open, asserted. See ck_bookings__period_half_open.
            CONSTRAINT ck_booking_items__occupies_half_open
                CHECK (occupies = tstzrange(lower(occupies), upper(occupies), '[)')),
            CONSTRAINT ck_booking_items__money_paired
                CHECK (price_component IS NULL OR currency IS NOT NULL)
        );
        """
    )

    # ════════════════════════════════════════════════════════════════════
    # THE DOUBLE-BOOKING CONSTRAINT (01 §13.1)
    # ════════════════════════════════════════════════════════════════════
    #
    # Read it as: "no two blocking allocations of the same exclusive resource,
    # within the same tenant, may have overlapping occupancy intervals."
    #
    #   tenant_id   WITH =  : same tenant   (needs btree_gist)
    #   resource_id WITH =  : same resource (needs btree_gist)
    #   occupies    WITH && : overlapping ranges
    #
    # All three must hold simultaneously for the constraint to fire; that is
    # exactly the definition of a conflict. The WHERE clause makes it partial so
    # cancelled bookings and shared-capacity resources are exempt.
    #
    # Why this and not an application-level SELECT-then-INSERT: under READ
    # COMMITTED — the default, and what we run — two concurrent transactions both
    # see no conflict, both insert, and both commit. Two people are booked into
    # the same chair. The exclusion constraint pushes the guarantee into the
    # storage engine, where NO code path can forget it, including a psql session
    # or a data-fix script.
    #
    # DEFERRABLE INITIALLY IMMEDIATE, not INITIALLY DEFERRED: immediate checking
    # gives the failure at the offending INSERT with the correct row in the error
    # context, which is what the booking service needs in order to retry against
    # a different resource. DEFERRABLE is still declared so a rare bulk operation
    # (an A->B/B->A room swap) can SET CONSTRAINTS ... DEFERRED and pass through a
    # transiently-overlapping intermediate state.
    op.execute(
        """
        ALTER TABLE booking_items
            ADD CONSTRAINT ex_booking_items__no_overlap
            EXCLUDE USING gist (
                tenant_id   WITH =,
                resource_id WITH =,
                occupies    WITH &&
            )
            WHERE (is_blocking AND NOT is_capacity_shared)
            DEFERRABLE INITIALLY IMMEDIATE
        """
    )

    op.execute(
        "CREATE INDEX ix_booking_items__tenant_booking "
        "ON booking_items (tenant_id, booking_id)"
    )
    # Resource timeline: "what is on this resource's calendar this week". The
    # GiST index backing the EXCLUDE constraint only covers blocking, non-shared
    # rows, so a second one is needed for the general case (including cancelled,
    # for history).
    op.execute(
        "CREATE INDEX ix_booking_items__tenant_resource_occupies "
        "ON booking_items USING gist (tenant_id, resource_id, occupies)"
    )
    op.execute(
        "CREATE INDEX ix_booking_items__shared_capacity "
        "ON booking_items USING gist (tenant_id, resource_id, occupies) "
        "WHERE is_capacity_shared AND is_blocking"
    )
    add_updated_at_trigger("booking_items")

    # ── booking_holds (01 §5.11) ─────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE booking_holds (
            id                  UUID NOT NULL DEFAULT uuid_generate_v7()
                                CONSTRAINT pk_booking_holds PRIMARY KEY,
            tenant_id           UUID NOT NULL
                                REFERENCES tenants(id) ON DELETE CASCADE,
            location_id         UUID NOT NULL,
            service_id          UUID NOT NULL,
            resource_id         UUID NOT NULL,

            status              hold_status NOT NULL DEFAULT 'active',

            -- Same geometry as booking_items.occupies, and deliberately the same
            -- column name and semantics so the two participate in one conflict
            -- check (01 §13.2).
            occupies            TSTZRANGE NOT NULL,
            quantity            INTEGER NOT NULL DEFAULT 1,
            is_capacity_shared  BOOLEAN NOT NULL DEFAULT FALSE,

            -- The moment the hold stops blocking. Enforced three ways (§13.3):
            --   1. the read-time predicate in booking_check_conflicts,
            --   2. a Temporal timer that releases it,
            --   3. ops_expire_stale_holds(), the janitorial sweeper.
            -- Layer 1 is what makes this CORRECT: an expired hold stops blocking
            -- the moment its TTL passes, so a dead worker cannot make a resource
            -- permanently unbookable.
            expires_at          TIMESTAMPTZ NOT NULL,

            conversation_id     UUID,
            session_token       TEXT,
            created_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
            workflow_id         TEXT,

            booking_id          UUID,
            converted_at        TIMESTAMPTZ,
            released_at         TIMESTAMPTZ,
            release_reason      TEXT,

            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT fk_booking_holds__locations
                FOREIGN KEY (tenant_id, location_id)
                REFERENCES locations (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_booking_holds__services
                FOREIGN KEY (tenant_id, service_id)
                REFERENCES services (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_booking_holds__resources
                FOREIGN KEY (tenant_id, resource_id)
                REFERENCES resources (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_booking_holds__bookings
                FOREIGN KEY (tenant_id, booking_id)
                REFERENCES bookings (tenant_id, id) ON DELETE SET NULL,

            CONSTRAINT ck_booking_holds__occupies_bounded
                CHECK (lower(occupies) IS NOT NULL AND upper(occupies) IS NOT NULL
                       AND NOT isempty(occupies)),
            CONSTRAINT ck_booking_holds__occupies_half_open
                CHECK (occupies = tstzrange(lower(occupies), upper(occupies), '[)')),
            CONSTRAINT ck_booking_holds__quantity CHECK (quantity >= 1),
            CONSTRAINT ck_booking_holds__converted_coherent
                CHECK ((status = 'converted')
                       = (booking_id IS NOT NULL AND converted_at IS NOT NULL)),
            CONSTRAINT ck_booking_holds__released_coherent
                CHECK (status <> 'released' OR released_at IS NOT NULL)
        );
        """
    )
    # Holds do not overlap each other on an exclusive resource.
    #
    # Note what this constraint CANNOT express: `expires_at > now()`. A partial
    # index predicate must be IMMUTABLE and now() is STABLE, so PostgreSQL
    # rejects it. That is precisely why the TTL needs three layers rather than
    # one — the constraint can only know about `status`, so an expired-but-
    # unswept hold still blocks a competing HOLD at the constraint level. That is
    # acceptable and self-correcting within one sweep, because holds are
    # short-lived and the BOOKING path (layer 1) already ignores them.
    op.execute(
        """
        ALTER TABLE booking_holds
            ADD CONSTRAINT ex_booking_holds__no_overlap
            EXCLUDE USING gist (
                tenant_id   WITH =,
                resource_id WITH =,
                occupies    WITH &&
            )
            WHERE (status = 'active' AND NOT is_capacity_shared)
        """
    )
    # Sweeper: "expire everything past its TTL". A tiny partial index, so the
    # sweeper's cost is proportional to live holds, not to history.
    op.execute(
        "CREATE INDEX ix_booking_holds__expiry_sweep "
        "ON booking_holds (expires_at) WHERE status = 'active'"
    )
    op.execute(
        "CREATE INDEX ix_booking_holds__tenant_resource_active "
        "ON booking_holds USING gist (tenant_id, resource_id, occupies) "
        "WHERE status = 'active'"
    )
    op.execute(
        "CREATE INDEX ix_booking_holds__conversation "
        "ON booking_holds (tenant_id, conversation_id) "
        "WHERE conversation_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_booking_holds__booking "
        "ON booking_holds (tenant_id, booking_id) WHERE booking_id IS NOT NULL"
    )
    add_updated_at_trigger("booking_holds")

    # ── waitlist_entries (01 §5.12) ──────────────────────────────────────
    op.execute(
        """
        CREATE TABLE waitlist_entries (
            id                  UUID NOT NULL DEFAULT uuid_generate_v7()
                                CONSTRAINT pk_waitlist_entries PRIMARY KEY,
            tenant_id           UUID NOT NULL
                                REFERENCES tenants(id) ON DELETE CASCADE,
            location_id         UUID NOT NULL,
            service_id          UUID NOT NULL,

            status              waitlist_status NOT NULL DEFAULT 'waiting',

            -- A range rather than an instant, because a waitlist is inherently
            -- "anything in this window works".
            desired_period      TSTZRANGE NOT NULL,
            preferred_start     TIMESTAMPTZ,

            party_size          INTEGER NOT NULL DEFAULT 1,
            duration_units      INTEGER NOT NULL DEFAULT 1,

            resource_id         UUID,
            resource_type_id    UUID,
            attribute_predicate JSONB NOT NULL DEFAULT '{}'::jsonb,

            -- Lower number = earlier offer. Default is FIFO by created_at;
            -- priority lets staff bump an urgent case.
            priority            SMALLINT NOT NULL DEFAULT 100,

            customer_id         UUID,
            customer_name       TEXT,
            customer_phone_e164 TEXT,
            customer_email      TEXT,
            notify_channels     notification_channel[] NOT NULL DEFAULT '{sms}',

            conversation_id     UUID,

            offered_booking_id  UUID,
            offered_at          TIMESTAMPTZ,
            offer_expires_at    TIMESTAMPTZ,
            offer_attempts      SMALLINT NOT NULL DEFAULT 0,

            converted_booking_id UUID,
            converted_at        TIMESTAMPTZ,
            expires_at          TIMESTAMPTZ,
            cancelled_at        TIMESTAMPTZ,

            notes               TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT fk_waitlist_entries__locations
                FOREIGN KEY (tenant_id, location_id)
                REFERENCES locations (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_waitlist_entries__services
                FOREIGN KEY (tenant_id, service_id)
                REFERENCES services (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_waitlist_entries__resources
                FOREIGN KEY (tenant_id, resource_id)
                REFERENCES resources (tenant_id, id) ON DELETE SET NULL,
            CONSTRAINT fk_waitlist_entries__resource_types
                FOREIGN KEY (tenant_id, resource_type_id)
                REFERENCES resource_types (tenant_id, id) ON DELETE SET NULL,
            CONSTRAINT fk_waitlist_entries__converted
                FOREIGN KEY (tenant_id, converted_booking_id)
                REFERENCES bookings (tenant_id, id) ON DELETE SET NULL,

            CONSTRAINT ck_waitlist_entries__period_bounded
                CHECK (lower(desired_period) IS NOT NULL
                       AND upper(desired_period) IS NOT NULL),
            CONSTRAINT ck_waitlist_entries__preferred_inside
                CHECK (preferred_start IS NULL
                       OR desired_period @> preferred_start),
            CONSTRAINT ck_waitlist_entries__party CHECK (party_size >= 1),
            CONSTRAINT ck_waitlist_entries__priority
                CHECK (priority BETWEEN 0 AND 1000),
            CONSTRAINT ck_waitlist_entries__converted_coherent
                CHECK ((status = 'converted') = (converted_booking_id IS NOT NULL)),
            -- A waitlist entry nobody can be reached on is not a waitlist entry.
            CONSTRAINT ck_waitlist_entries__contactable
                CHECK (customer_phone_e164 IS NOT NULL
                       OR customer_email IS NOT NULL)
        );
        """
    )
    # The matcher's query, run whenever a booking is cancelled: "who is waiting
    # for a slot that overlaps the one that just freed up".
    op.execute(
        "CREATE INDEX ix_waitlist_entries__match "
        "ON waitlist_entries USING gist (tenant_id, service_id, desired_period) "
        "WHERE status = 'waiting'"
    )
    op.execute(
        "CREATE INDEX ix_waitlist_entries__tenant_priority "
        "ON waitlist_entries (tenant_id, service_id, priority, created_at) "
        "WHERE status = 'waiting'"
    )
    op.execute(
        "CREATE INDEX ix_waitlist_entries__offer_expiry "
        "ON waitlist_entries (offer_expires_at) WHERE status = 'offered'"
    )
    op.execute(
        "CREATE INDEX ix_waitlist_entries__tenant_customer "
        "ON waitlist_entries (tenant_id, customer_phone_e164) "
        "WHERE customer_phone_e164 IS NOT NULL"
    )
    add_updated_at_trigger("waitlist_entries")


# ─────────────────────────────────────────────────────────────────────────────
# Triggers: the constraint's inputs are maintained by the database
# ─────────────────────────────────────────────────────────────────────────────


def _create_occupancy_triggers() -> None:
    """01 §5.10's two triggers.

    ``occupies``, ``is_blocking`` and ``is_capacity_shared`` are the exclusion
    constraint's entire input, so the application is never allowed to set them.
    Whatever a caller passes for ``occupies`` is overwritten here; the value only
    has to satisfy ``NOT NULL``.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sync_booking_item_occupancy()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            b        bookings%ROWTYPE;
            r_shared BOOLEAN;
            respects BOOLEAN;
        BEGIN
            SELECT * INTO b FROM bookings
             WHERE id = NEW.booking_id AND tenant_id = NEW.tenant_id;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'booking_items: parent booking % not found',
                    NEW.booking_id;
            END IF;

            SELECT rt.is_capacity_shared INTO r_shared
              FROM resources res
              JOIN resource_types rt
                ON rt.id = res.resource_type_id AND rt.tenant_id = res.tenant_id
             WHERE res.id = NEW.resource_id AND res.tenant_id = NEW.tenant_id;

            NEW.is_capacity_shared := COALESCE(r_shared, FALSE);

            SELECT COALESCE(srr.respects_buffers, TRUE) INTO respects
              FROM service_resource_requirements srr
             WHERE srr.id = NEW.requirement_id AND srr.tenant_id = NEW.tenant_id;

            NEW.occupies := CASE
                WHEN COALESCE(respects, TRUE) THEN b.blocking_period
                ELSE b.service_period
            END;

            -- A booking only blocks its resources while it is in a live state.
            NEW.is_blocking := b.status IN
                ('draft','pending','confirmed','checked_in','in_progress','completed');

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_booking_items_sync_occupancy
            BEFORE INSERT OR UPDATE OF booking_id, resource_id, requirement_id
            ON booking_items
            FOR EACH ROW EXECUTE FUNCTION sync_booking_item_occupancy()
        """
    )

    # A reschedule changes bookings.service_period; every child item's
    # `occupies` must follow, and the exclusion constraint must re-evaluate at
    # that moment. This is what makes "move the booking" a single UPDATE that is
    # either legal or rejected, rather than a delete-then-insert that can lose
    # the slot in between.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cascade_booking_period_to_items()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.service_period IS DISTINCT FROM OLD.service_period
               OR NEW.buffer_before_minutes IS DISTINCT FROM OLD.buffer_before_minutes
               OR NEW.buffer_after_minutes  IS DISTINCT FROM OLD.buffer_after_minutes
               OR NEW.status IS DISTINCT FROM OLD.status THEN

                UPDATE booking_items bi
                   SET occupies = CASE
                           WHEN COALESCE(
                                    (SELECT srr.respects_buffers
                                       FROM service_resource_requirements srr
                                      WHERE srr.id = bi.requirement_id
                                        AND srr.tenant_id = bi.tenant_id),
                                    TRUE)
                           THEN NEW.blocking_period
                           ELSE NEW.service_period
                       END,
                       is_blocking = NEW.status IN
                           ('draft','pending','confirmed','checked_in',
                            'in_progress','completed'),
                       updated_at = now()
                 WHERE bi.booking_id = NEW.id
                   AND bi.tenant_id = NEW.tenant_id;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_bookings_cascade_period
            AFTER UPDATE ON bookings
            FOR EACH ROW EXECUTE FUNCTION cascade_booking_period_to_items()
        """
    )

    # ── Shared capacity (01 §5.10) ───────────────────────────────────────
    #
    # The EXCLUDE constraint deliberately skips shared resources — "no overlap at
    # all" is the wrong rule for a 20-person studio. The right rule ("summed
    # quantity over any instant must not exceed capacity + overbook_allowance")
    # is not expressible as an exclusion constraint, because it is an aggregate
    # over a moving window. A constraint trigger enforces it, taking a row lock
    # on the resource first so concurrent inserts serialise — a row lock, not a
    # table lock: bookings of DIFFERENT resources proceed in parallel.
    #
    # It raises exclusion_violation (23P01) deliberately, so the application's
    # conflict handler catches one error class for both the exclusive and the
    # shared path.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_shared_capacity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            cap  INTEGER;
            over INTEGER;
            peak INTEGER;
        BEGIN
            IF NOT NEW.is_capacity_shared OR NOT NEW.is_blocking THEN
                RETURN NULL;
            END IF;

            SELECT r.capacity, r.overbook_allowance INTO cap, over
              FROM resources r
             WHERE r.id = NEW.resource_id AND r.tenant_id = NEW.tenant_id
               FOR UPDATE;

            -- Peak concurrent consumption is found by evaluating the running sum
            -- at every interval start point that overlaps the new item: the
            -- maximum of a step function can only occur at a step boundary.
            WITH overlapping AS (
                SELECT bi.quantity, bi.occupies
                  FROM booking_items bi
                 WHERE bi.tenant_id = NEW.tenant_id
                   AND bi.resource_id = NEW.resource_id
                   AND bi.is_blocking
                   AND bi.is_capacity_shared
                   AND bi.occupies && NEW.occupies
            ),
            boundaries AS (
                SELECT DISTINCT lower(occupies) AS t FROM overlapping
            )
            SELECT COALESCE(MAX(load), 0) INTO peak
              FROM (
                SELECT b.t, SUM(o.quantity) AS load
                  FROM boundaries b
                  JOIN overlapping o ON o.occupies @> b.t
                 GROUP BY b.t
              ) s;

            IF peak > cap + over THEN
                RAISE EXCEPTION
                    'capacity exceeded on resource %: peak % > capacity % (+% overbook)',
                    NEW.resource_id, peak, cap, over
                    USING ERRCODE = 'exclusion_violation';
            END IF;

            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_booking_items_shared_capacity
            AFTER INSERT OR UPDATE ON booking_items
            DEFERRABLE INITIALLY IMMEDIATE
            FOR EACH ROW EXECUTE FUNCTION check_shared_capacity()
        """
    )


# ─────────────────────────────────────────────────────────────────────────────
# The hold-vs-booking path (01 §13.2, §13.3)
# ─────────────────────────────────────────────────────────────────────────────


def _create_conflict_functions() -> None:
    """``booking_check_conflicts`` and ``booking_convert_hold``.

    PostgreSQL exclusion constraints are single-table, so ``booking_items``
    cannot conflict with ``booking_holds`` via a constraint. 01 §13.2 considered
    three designs and chose separate tables plus an explicit conflict check under
    a **transaction-scoped advisory lock keyed by (tenant_id, resource_id)**.

    The advisory lock is what makes it safe, and it is deliberately narrow: one
    ``(tenant, resource)`` pair, released automatically at transaction end.
    ``pg_advisory_xact_lock``, never ``pg_advisory_lock`` — the session variant
    leaks locks on a crashed connection. Bookings for different resources never
    contend, so concurrency is per-resource, which is exactly the granularity the
    business problem has.

    The exclusion constraints remain in place as the **backstop**: the lock
    provides *coordination*, the constraint provides *correctness*.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION booking_check_conflicts(
            p_tenant_id         UUID,
            p_resource_id       UUID,
            p_occupies          TSTZRANGE,
            p_ignore_hold_id    UUID DEFAULT NULL,
            p_ignore_booking_id UUID DEFAULT NULL
        )
        RETURNS TABLE (conflict_kind TEXT, conflict_id UUID, conflict_range TSTZRANGE)
        LANGUAGE plpgsql
        AS $$
        DECLARE
            lock_key BIGINT;
        BEGIN
            -- Serialise all conflict evaluation for this (tenant, resource). The
            -- key is a 64-bit hash of both ids; collisions are harmless (they
            -- only cause two unrelated resources to serialise briefly), and
            -- hashtextextended is stable across sessions.
            lock_key := hashtextextended(
                p_tenant_id::text || ':' || p_resource_id::text, 0);
            PERFORM pg_advisory_xact_lock(lock_key);

            RETURN QUERY
            SELECT 'booking'::TEXT, bi.booking_id, bi.occupies
              FROM booking_items bi
             WHERE bi.tenant_id = p_tenant_id
               AND bi.resource_id = p_resource_id
               AND bi.is_blocking
               AND NOT bi.is_capacity_shared
               AND bi.occupies && p_occupies
               AND (p_ignore_booking_id IS NULL
                    OR bi.booking_id <> p_ignore_booking_id)

            UNION ALL

            SELECT 'hold'::TEXT, h.id, h.occupies
              FROM booking_holds h
             WHERE h.tenant_id = p_tenant_id
               AND h.resource_id = p_resource_id
               AND h.status = 'active'
               -- ── LAYER 1 of the hold TTL, and the reason the design is
               --    correct rather than merely tidy: an expired hold stops
               --    blocking the MOMENT its TTL passes, with no sweeper
               --    involvement. A dead worker cannot make a resource
               --    permanently unbookable.
               AND h.expires_at > now()
               AND NOT h.is_capacity_shared
               AND h.occupies && p_occupies
               AND (p_ignore_hold_id IS NULL OR h.id <> p_ignore_hold_id);
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION booking_convert_hold(
            p_tenant_id  UUID,
            p_hold_id    UUID,
            p_booking_id UUID,
            p_role       TEXT DEFAULT 'primary',
            p_requirement_id UUID DEFAULT NULL
        )
        RETURNS VOID
        LANGUAGE plpgsql
        AS $$
        DECLARE
            h booking_holds%ROWTYPE;
        BEGIN
            -- Lock the hold row. If another transaction is converting or
            -- releasing it, we block here rather than racing.
            SELECT * INTO h
              FROM booking_holds
             WHERE id = p_hold_id AND tenant_id = p_tenant_id
               FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'hold % not found', p_hold_id
                    USING ERRCODE = 'no_data_found';
            END IF;

            IF h.status <> 'active' THEN
                RAISE EXCEPTION 'hold % is %, not active', p_hold_id, h.status
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            IF h.expires_at <= now() THEN
                -- The TTL passed while the caller was talking. Fail LOUDLY; the
                -- agent must re-check availability and re-offer, not silently
                -- double-book.
                UPDATE booking_holds
                   SET status = 'expired', released_at = now(),
                       release_reason = 'ttl_expired_at_conversion'
                 WHERE id = p_hold_id;
                RAISE EXCEPTION 'hold % expired at %', p_hold_id, h.expires_at
                    USING ERRCODE = 'lock_not_available';
            END IF;

            -- Ignore THIS hold when checking conflicts — it is the one being
            -- converted.
            IF EXISTS (
                SELECT 1 FROM booking_check_conflicts(
                    p_tenant_id, h.resource_id, h.occupies, p_hold_id, NULL)
            ) THEN
                RAISE EXCEPTION 'conflict converting hold %', p_hold_id
                    USING ERRCODE = 'exclusion_violation';
            END IF;

            -- Release the hold in the SAME transaction that inserts the item.
            -- Order matters: releasing first means the hold no longer blocks,
            -- and the exclusion constraint on booking_items then guards the
            -- insert. Either the hold becomes a booking or nothing changes;
            -- there is no window in which the slot is unprotected.
            UPDATE booking_holds
               SET status = 'converted', booking_id = p_booking_id,
                   converted_at = now(), updated_at = now()
             WHERE id = p_hold_id;

            INSERT INTO booking_items
                (tenant_id, booking_id, resource_id, role, requirement_id,
                 quantity, occupies)
            VALUES (p_tenant_id, p_booking_id, h.resource_id, p_role,
                    p_requirement_id, h.quantity, h.occupies);
            -- occupies / is_blocking / is_capacity_shared are then overwritten by
            -- trg_booking_items_sync_occupancy from the parent booking. The value
            -- passed here only satisfies NOT NULL.
        END;
        $$;
        """
    )

    # ── LAYER 3 of the hold TTL: the janitorial sweeper (01 §13.3) ───────
    #
    # Its purpose is NOT correctness — layer 1 already gives that. It exists so
    # that (a) ex_booking_holds__no_overlap's partial index stays small, since its
    # predicate is `status = 'active'` and cannot reference now(), and (b) admin
    # views show accurate state.
    #
    # SECURITY DEFINER and cross-tenant by design: the sweeper legitimately
    # operates across every tenant, and 01 §11.4 says that access goes through an
    # explicitly-granted definer function, never through BYPASSRLS.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ops_expire_stale_holds(p_limit INTEGER DEFAULT 5000)
        RETURNS INTEGER
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
            affected INTEGER;
        BEGIN
            WITH stale AS (
                SELECT id FROM booking_holds
                 WHERE status = 'active' AND expires_at <= now()
                 ORDER BY expires_at
                 LIMIT p_limit
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE booking_holds h
               SET status = 'expired', released_at = now(),
                   release_reason = 'ttl_swept', updated_at = now()
              FROM stale
             WHERE h.id = stale.id;
            GET DIAGNOSTICS affected = ROW_COUNT;
            RETURN affected;
        END;
        $$;
        """
    )

    # A SECURITY DEFINER function needs a policy to reach a FORCE'd table at all
    # — the same reason migration 0002 documents for the auth lookups. This one
    # is narrow: booking_holds only, and only for the NOLOGIN owner role, so the
    # reachable surface is exactly ops_expire_stale_holds' signature.
    op.execute(
        """
        CREATE POLICY rls_booking_holds__owner_sweeper ON booking_holds
            FOR ALL
            TO receptionai_owner
            USING (true)
            WITH CHECK (true)
        """
    )

    for signature in (
        "booking_check_conflicts(UUID, UUID, TSTZRANGE, UUID, UUID)",
        "booking_convert_hold(UUID, UUID, UUID, TEXT, UUID)",
        "ops_expire_stale_holds(INTEGER)",
    ):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO receptionai_owner")
        # Default-deny, then grant deliberately: a table is granted by default
        # privileges, a function never is, because a function is a
        # privilege-escalation surface.
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")

    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "booking_check_conflicts(UUID, UUID, TSTZRANGE, UUID, UUID) "
        "TO receptionai_app"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "booking_convert_hold(UUID, UUID, UUID, TEXT, UUID) TO receptionai_app"
    )
    # The sweeper is an ops job, not an application call. Granting it to the app
    # role too would put a cross-tenant write on the request path.
    op.execute(
        "GRANT EXECUTE ON FUNCTION ops_expire_stale_holds(INTEGER) TO receptionai_ops"
    )

    op.execute(
        "COMMENT ON FUNCTION booking_check_conflicts"
        "(UUID, UUID, TSTZRANGE, UUID, UUID) IS "
        "'Advisory-locked hold-vs-booking conflict check (01 §13.2). Takes "
        "pg_advisory_xact_lock on (tenant, resource) then reads both tables. "
        "Layer 1 of the hold TTL lives in its expires_at > now() predicate.'"
    )
    op.execute(
        "COMMENT ON FUNCTION ops_expire_stale_holds(INTEGER) IS "
        "'Layer 3 of the hold TTL (01 §13.3) — janitorial, not correctness. "
        "Keeps ex_booking_holds__no_overlap''s partial index small and admin "
        "views accurate. Correctness is layer 1, the read-time predicate.'"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Ownership, RLS, grants
# ─────────────────────────────────────────────────────────────────────────────


def _fix_table_ownership() -> None:
    """Hand the new tables to ``receptionai_owner`` — migration 0002's lesson.

    Tables created here are created *by the migrator*. 0002 established why that
    is wrong: the default privileges are keyed on objects created by
    ``receptionai_owner``, so a table the migrator owns never receives them, and
    a ``SECURITY DEFINER`` function owned by the owner cannot reach it. Doing the
    reassignment in the same migration that creates the tables is the fix 0002
    had to apply retroactively.
    """
    for table in _ALL_TABLES:
        # Identifiers cannot be bound; these are module-level constants.
        op.execute(f"ALTER TABLE {table} OWNER TO receptionai_owner")


def _apply_grants() -> None:
    """Explicit grants, because default privileges only apply to objects created
    *after* they are set — and these tables were created by the migrator, before
    the ownership reassignment above. 01 §16.5's ordering trap, handled rather
    than tripped over.
    """
    tables = ", ".join(_ALL_TABLES)
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tables} TO receptionai_app"
    )
    op.execute(f"GRANT SELECT ON {tables} TO receptionai_reporting")
    op.execute(f"GRANT SELECT, UPDATE ON {tables} TO receptionai_ops")


def upgrade() -> None:
    _create_shift_function()
    _create_enums()
    _create_catalogue_tables()
    _create_availability_tables()
    _create_booking_tables()
    _create_occupancy_triggers()

    _fix_table_ownership()
    # RLS after ownership, so the policies land on tables the owner owns and
    # FORCE means what 01 §11.2 says it means.
    enable_tenant_rls_bulk(TENANT_OWNED_TABLES)
    _create_conflict_functions()
    _apply_grants()


def downgrade() -> None:
    # Reverse creation order so FKs never block a drop.
    op.execute("DROP FUNCTION IF EXISTS ops_expire_stale_holds(INTEGER)")
    op.execute(
        "DROP FUNCTION IF EXISTS booking_convert_hold(UUID, UUID, UUID, TEXT, UUID)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "booking_check_conflicts(UUID, UUID, TSTZRANGE, UUID, UUID)"
    )

    op.execute("DROP TABLE IF EXISTS waitlist_entries CASCADE")
    op.execute("DROP TABLE IF EXISTS booking_holds CASCADE")
    op.execute("DROP TABLE IF EXISTS booking_items CASCADE")
    op.execute("DROP TABLE IF EXISTS bookings CASCADE")
    op.execute("DROP TABLE IF EXISTS holidays CASCADE")
    op.execute("DROP TABLE IF EXISTS availability_exceptions CASCADE")
    op.execute("DROP TABLE IF EXISTS availability_rules CASCADE")
    op.execute("DROP TABLE IF EXISTS service_resource_requirements CASCADE")
    op.execute("DROP TABLE IF EXISTS services CASCADE")
    op.execute("DROP TABLE IF EXISTS resources CASCADE")
    op.execute("DROP TABLE IF EXISTS resource_types CASCADE")

    op.execute(
        "DROP FUNCTION IF EXISTS booking_shift_minutes(TIMESTAMPTZ, INTEGER)"
    )
    op.execute("DROP FUNCTION IF EXISTS check_shared_capacity()")
    op.execute("DROP FUNCTION IF EXISTS cascade_booking_period_to_items()")
    op.execute("DROP FUNCTION IF EXISTS sync_booking_item_occupancy()")

    for type_name in (
        "notification_channel",
        "availability_effect",
        "waitlist_status",
        "hold_status",
        "booking_status",
        "resource_kind",
    ):
        op.execute(f"DROP TYPE IF EXISTS {type_name}")

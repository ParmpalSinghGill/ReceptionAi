"""Phase 1 walking skeleton: extensions, functions, roles, and the core tables.

Creates, in dependency order:

* the extensions (01 §1.1)
* ``uuid_generate_v7()`` / ``uuid_v7_timestamp()`` (01 §1.2) — the DB-side
  fallback generator per contract A2
* ``set_updated_at()``, ``app_current_tenant()``, ``app_is_platform_admin()``
  (01 §1.5, §1.6)
* the role split and default privileges (01 §11.1, §16.1)
* the Phase 1 enum types (01 §1.7)
* ``tenants``, ``locations``, ``tenant_settings``, ``users``, ``permissions``,
  ``roles``, ``role_permissions``, ``user_tenant_roles``, ``sessions``,
  ``conversations``, ``turns``
* RLS enabled + forced with policies on every one of them (01 §11.2–§11.5)
* the first four months of partitions for ``conversations`` and ``turns``

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from migrations.helpers import (
    TENANT_ROLES,
    add_updated_at_trigger,
    enable_tenant_rls_bulk,
)

revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


# ── The RLS registry ─────────────────────────────────────────────────────────
# Every tenant-owned table Phase 1 creates. `tests/rls/test_rls_coverage.py`
# asserts this array covers every table in pg_class carrying a `tenant_id`
# column, so a table added without being registered here fails CI rather than
# shipping unprotected. `roles`, `role_permissions`, `users`, and `sessions`
# have a tenant_id or a user_id but are NOT tenant-owned in the ordinary sense
# — they get the narrower policies in §11.5 instead, applied below.
TENANT_OWNED_TABLES: list[str] = [
    "locations",
    "tenant_settings",
    "user_tenant_roles",
    "conversations",
    "turns",
]

# Roles referenced by policies and grants. Created here (idempotently) so a
# fresh database — a test container, a developer laptop — is a single
# `alembic upgrade head` away from a correct one. Production provisions these
# out-of-band with real passwords; NOLOGIN here means these definitions can
# never themselves be used to connect.
_ROLES = (
    "receptionai_owner",
    "receptionai_app",
    "receptionai_auth",
    "receptionai_reporting",
    "receptionai_ops",
)


def _create_roles() -> None:
    """Create the five roles if absent.

    ``NOBYPASSRLS`` is the whole point (contract §2.5). Note also that even a
    role *with* ``BYPASSRLS`` is defeated by ``FORCE ROW LEVEL SECURITY`` only
    for the table owner — so we do both: the app role lacks ``BYPASSRLS``, *and*
    every table is ``FORCE``d, so that even if someone accidentally runs the app
    as the owner, policies still apply.
    """
    for role in _ROLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = '{role}'
                ) THEN
                    CREATE ROLE {role}
                        NOSUPERUSER NOCREATEDB NOCREATEROLE
                        NOINHERIT NOBYPASSRLS NOLOGIN;
                END IF;
            END;
            $$;
            """
        )


def _create_functions() -> None:
    # ── UUIDv7, DB-side (01 §1.2) ────────────────────────────────────────
    # Contract A2: this is the FALLBACK generator. It exists so column defaults
    # work for rows inserted by migrations, backfills, and INSERT ... SELECT.
    # The normal path is application-side (packages/core/ids.py), which produces
    # byte-identical values.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION uuid_generate_v7()
        RETURNS uuid
        LANGUAGE plpgsql
        VOLATILE
        PARALLEL SAFE
        AS $$
        DECLARE
            unix_ts_ms  bytea;
            uuid_bytes  bytea;
        BEGIN
            unix_ts_ms := substring(
                int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint)
                FROM 3 FOR 6
            );

            -- 10 random bytes fill rand_a + rand_b; version/variant nibbles are
            -- then overwritten in place.
            uuid_bytes := unix_ts_ms || gen_random_bytes(10);

            -- byte 6: set version to 7 (high nibble)
            uuid_bytes := set_byte(uuid_bytes, 6,
                (b'0111'::int << 4) | (get_byte(uuid_bytes, 6) & 15));

            -- byte 8: set variant to RFC 4122 (0b10xxxxxx)
            uuid_bytes := set_byte(uuid_bytes, 8,
                (b'10'::int << 6) | (get_byte(uuid_bytes, 8) & 63));

            RETURN encode(uuid_bytes, 'hex')::uuid;
        END;
        $$;
        """
    )

    # IMMUTABLE, and genuinely so (a pure function of its input), which is what
    # makes it safe in index expressions and partition constraints.
    # `uuid_generate_v7` above is VOLATILE and must never appear in either.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION uuid_v7_timestamp(u uuid)
        RETURNS timestamptz
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        AS $$
            SELECT to_timestamp(
                ('x' || substring(replace(u::text, '-', '') FROM 1 FOR 12))
                    ::bit(48)::bigint / 1000.0
            );
        $$;
        """
    )

    # ── updated_at maintenance (01 §1.5) ─────────────────────────────────
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$;
        """
    )

    # ── The tenant-context helper every RLS policy calls (01 §1.6) ────────
    # STABLE, not IMMUTABLE: the value is constant within a statement but varies
    # across transactions. STABLE lets the planner treat it as a constant for
    # the duration of a query, which is what makes RLS predicates index-usable.
    #
    # `current_setting(..., true)` is the missing_ok form: it returns NULL
    # rather than raising when the GUC is unset. Combined with NULLIF on the
    # empty string, an unset context yields NULL — and `tenant_id = NULL` is
    # never true, so a session that forgot to set context sees ZERO ROWS rather
    # than erroring or, far worse, seeing everything. Fail-closed by
    # construction.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_current_tenant()
        RETURNS uuid
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        AS $$
            SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid;
        $$;
        """
    )

    # Deliberately NOT used in any tenant-table policy (01 §11.4). It gates only
    # the small set of platform-catalogue tables. Once
    # `OR app_is_platform_admin()` exists in every policy, the security of the
    # entire system rests on one boolean GUC never being set incorrectly.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_is_platform_admin()
        RETURNS boolean
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        AS $$
            SELECT coalesce(current_setting('app.platform_admin', true), 'off')
                   = 'on';
        $$;
        """
    )


def _create_enums() -> None:
    """Create the Phase 1 enum types up front, in one place.

    Ahead of the tables rather than lazily at table-create time: the ordering
    under ``metadata.create_all`` differs from the ordering under Alembic, and
    that divergence is exactly the kind of thing that passes in tests and fails
    on deploy.
    """
    op.execute(
        "CREATE TYPE tenant_status AS ENUM "
        "('trial','active','past_due','suspended','cancelled')"
    )
    op.execute(
        "CREATE TYPE user_status AS ENUM ('invited','active','disabled','locked')"
    )
    op.execute(
        "CREATE TYPE conversation_channel AS ENUM "
        "('voice_inbound','voice_outbound','web_chat','sms','whatsapp',"
        "'telegram','email','api')"
    )
    op.execute(
        "CREATE TYPE conversation_status AS ENUM "
        "('active','ended','failed','abandoned')"
    )
    op.execute(
        "CREATE TYPE turn_role AS ENUM ('system','user','assistant','tool','event')"
    )
    op.execute(
        "CREATE TYPE outcome_kind AS ENUM "
        "('resolved','booked','rescheduled','cancelled','transferred',"
        "'abandoned','failed','spam')"
    )


def _enum(name: str) -> pg.ENUM:
    """Reference an existing type without re-creating it."""
    return pg.ENUM(name=name, create_type=False)


# ─────────────────────────────────────────────────────────────────────────────
# Tables
# ─────────────────────────────────────────────────────────────────────────────


def _create_tenancy_tables() -> None:
    op.create_table(
        "tenants",
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("legal_name", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "status", _enum("tenant_status"), nullable=False,
            server_default=sa.text("'trial'"),
        ),
        sa.Column("shard_key", sa.Text(), nullable=True),
        sa.Column(
            "default_timezone", sa.Text(), nullable=False,
            server_default=sa.text("'UTC'"),
        ),
        sa.Column(
            "default_locale", sa.Text(), nullable=False,
            server_default=sa.text("'en-US'"),
        ),
        sa.Column(
            "default_currency", sa.CHAR(3), nullable=False,
            server_default=sa.text("'USD'"),
        ),
        sa.Column(
            "default_text_search_config", pg.REGCONFIG(), nullable=False,
            server_default=sa.text("'simple'"),
        ),
        sa.Column(
            "data_region", sa.Text(), nullable=False,
            server_default=sa.text("'us-east-1'"),
        ),
        sa.Column(
            "compliance_profile", sa.Text(), nullable=False,
            server_default=sa.text("'standard'"),
        ),
        sa.Column("kms_key_id", sa.Text(), nullable=True),
        sa.Column(
            "metadata", pg.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspension_reason", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.CheckConstraint(
            r"slug ~ '^[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])?$'",
            name="ck_tenants__slug_format",
        ),
        sa.CheckConstraint(
            r"default_currency ~ '^[A-Z]{3}$'", name="ck_tenants__currency_format"
        ),
        sa.CheckConstraint(
            "now() AT TIME ZONE default_timezone IS NOT NULL",
            name="ck_tenants__timezone_valid",
        ),
        sa.CheckConstraint(
            "compliance_profile IN ('standard','hipaa','gdpr_strict','pci')",
            name="ck_tenants__compliance_profile",
        ),
        sa.CheckConstraint(
            "(status = 'suspended') = (suspended_at IS NOT NULL)",
            name="ck_tenants__suspension_coherent",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_tenants__slug ON tenants (slug) "
        "WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_tenants__shard_key ON tenants (shard_key) "
        "WHERE shard_key IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_tenants__status ON tenants (status) "
        "WHERE deleted_at IS NULL"
    )
    add_updated_at_trigger("tenants")

    op.create_table(
        "locations",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=True),
        sa.Column("locale", sa.Text(), nullable=True),
        sa.Column("address_line1", sa.Text(), nullable=True),
        sa.Column("address_line2", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("postal_code", sa.Text(), nullable=True),
        sa.Column("country_code", sa.CHAR(2), nullable=True),
        sa.Column("latitude", sa.Numeric(9, 6), nullable=True),
        sa.Column("longitude", sa.Numeric(9, 6), nullable=True),
        sa.Column("phone_e164", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column(
            "settings", pg.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_locations"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_locations__tenants",
        ),
        # THE composite-FK target. RLS filters *reads*; it does not stop an
        # INSERT naming a foreign id, because the FK check runs with RLS
        # bypassed for referential integrity — so a cross-tenant reference would
        # be ACCEPTED and then invisible. This unique key is what lets children
        # reference (tenant_id, id) and closes that hole at the storage layer.
        sa.UniqueConstraint("tenant_id", "id", name="uq_locations__tenant_id_id"),
        sa.CheckConstraint(
            "now() AT TIME ZONE timezone IS NOT NULL",
            name="ck_locations__timezone_valid",
        ),
        sa.CheckConstraint(
            r"currency IS NULL OR currency ~ '^[A-Z]{3}$'",
            name="ck_locations__currency_format",
        ),
        sa.CheckConstraint(
            r"country_code IS NULL OR country_code ~ '^[A-Z]{2}$'",
            name="ck_locations__country_format",
        ),
        sa.CheckConstraint(
            r"phone_e164 IS NULL OR phone_e164 ~ '^\+[1-9]\d{6,14}$'",
            name="ck_locations__phone_e164",
        ),
        sa.CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL)",
            name="ck_locations__geo_paired",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_locations__tenant_code ON locations "
        "(tenant_id, code) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_locations__tenant_active ON locations "
        "(tenant_id, is_active) WHERE deleted_at IS NULL"
    )
    add_updated_at_trigger("locations")

    op.create_table(
        "tenant_settings",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", pg.JSONB(), nullable=False),
        sa.Column(
            "schema_version", sa.Text(), nullable=False, server_default=sa.text("'1'")
        ),
        sa.Column(
            "is_secret_ref", sa.Boolean(), nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("updated_by_user_id", pg.UUID(as_uuid=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_tenant_settings"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_tenant_settings__tenants",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.id"],
            ondelete="CASCADE",
            name="fk_tenant_settings__locations",
        ),
        sa.CheckConstraint(
            r"namespace ~ '^[a-z][a-z0-9_]{0,31}$'",
            name="ck_tenant_settings__namespace",
        ),
        sa.CheckConstraint(
            r"key ~ '^[a-z][a-z0-9_.]{0,63}$'", name="ck_tenant_settings__key"
        ),
    )
    # Two partial uniques rather than one, because NULL never equals NULL in a
    # unique index: without this split, a tenant could insert two tenant-wide
    # rows for the same key.
    op.execute(
        "CREATE UNIQUE INDEX uq_tenant_settings__tenant_ns_key_global "
        "ON tenant_settings (tenant_id, namespace, key) "
        "WHERE location_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_tenant_settings__tenant_loc_ns_key "
        "ON tenant_settings (tenant_id, location_id, namespace, key) "
        "WHERE location_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_tenant_settings__tenant_namespace "
        "ON tenant_settings (tenant_id, namespace, location_id)"
    )
    add_updated_at_trigger("tenant_settings")


def _create_identity_tables() -> None:
    op.create_table(
        "users",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("email", sa.Text(), nullable=False),
        # Generated rather than a functional index so the normalized value is
        # also queryable and joinable.
        sa.Column(
            "email_normalized",
            sa.Text(),
            sa.Computed("lower(btrim(email))", persisted=True),
            nullable=False,
        ),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("password_algo", sa.Text(), nullable=True),
        sa.Column("password_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("full_name", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("phone_e164", sa.Text(), nullable=True),
        sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "locale", sa.Text(), nullable=False, server_default=sa.text("'en-US'")
        ),
        sa.Column(
            "timezone", sa.Text(), nullable=False, server_default=sa.text("'UTC'")
        ),
        sa.Column(
            "status", _enum("user_status"), nullable=False,
            server_default=sa.text("'invited'"),
        ),
        sa.Column(
            "is_platform_staff", sa.Boolean(), nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column(
            "mfa_enforced", sa.Boolean(), nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column(
            "failed_login_count", sa.SmallInteger(), nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.CheckConstraint(
            r"email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'",
            name="ck_users__email_shape",
        ),
        sa.CheckConstraint(
            r"phone_e164 IS NULL OR phone_e164 ~ '^\+[1-9]\d{6,14}$'",
            name="ck_users__phone_e164",
        ),
        sa.CheckConstraint(
            "(password_hash IS NULL) = (password_algo IS NULL)",
            name="ck_users__password_paired",
        ),
        sa.CheckConstraint(
            "now() AT TIME ZONE timezone IS NOT NULL",
            name="ck_users__timezone_valid",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_users__email_normalized ON users "
        "(email_normalized) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_users__email_trgm ON users "
        "USING gin (email_normalized gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_users__locked ON users (locked_until) "
        "WHERE locked_until IS NOT NULL"
    )
    add_updated_at_trigger("users")

    # PII classification travels with the schema so a masking sweep can read it
    # from the catalogue rather than from a document (contract §8).
    op.execute(
        "COMMENT ON COLUMN users.password_hash IS "
        "'pii_class=high; never logged'"
    )
    op.execute(
        "COMMENT ON COLUMN users.email IS 'pii_class=medium; masked in traces'"
    )
    op.execute(
        "COMMENT ON COLUMN users.phone_e164 IS "
        "'pii_class=medium; masked in traces'"
    )

    op.create_table(
        "permissions",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("requires_feature", sa.Text(), nullable=True),
        sa.Column(
            "is_sensitive", sa.Boolean(), nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_permissions"),
        sa.CheckConstraint(
            r"code ~ '^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$'",
            name="ck_permissions__code_shape",
        ),
        sa.CheckConstraint(
            "code = resource || ':' || action",
            name="ck_permissions__code_matches_parts",
        ),
    )
    op.execute("CREATE UNIQUE INDEX uq_permissions__code ON permissions (code)")
    op.execute("CREATE INDEX ix_permissions__resource ON permissions (resource)")

    op.create_table(
        "roles",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        # NULL = platform-defined template role, visible to every tenant.
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_system", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")
        ),
        sa.Column(
            "rank", sa.SmallInteger(), nullable=False, server_default=sa.text("0")
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
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_roles__tenants",
        ),
        sa.CheckConstraint(
            r"code ~ '^[a-z][a-z0-9_]{0,31}$'", name="ck_roles__code_shape"
        ),
        sa.CheckConstraint(
            "NOT is_system OR tenant_id IS NULL", name="ck_roles__system_is_global"
        ),
        sa.CheckConstraint("rank BETWEEN 0 AND 100", name="ck_roles__rank"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_roles__global_code ON roles (code) "
        "WHERE tenant_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_roles__tenant_code ON roles (tenant_id, code) "
        "WHERE tenant_id IS NOT NULL"
    )
    # Needed as the target of the composite FK from role_permissions, which must
    # tolerate a NULL tenant_id on template roles.
    op.execute(
        "CREATE UNIQUE INDEX uq_roles__id_tenant ON roles (id, tenant_id)"
    )
    add_updated_at_trigger("roles")

    op.create_table(
        "role_permissions",
        sa.Column("role_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", pg.UUID(as_uuid=True), nullable=False),
        # Denormalized from roles.tenant_id so RLS on this table is a plain
        # predicate rather than a subquery. An RLS predicate requiring a
        # subquery is evaluated per row and defeats index-only scans.
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "granted_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("granted_by_user_id", pg.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "role_id", "permission_id", name="pk_role_permissions"
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], ondelete="CASCADE",
            name="fk_role_permissions__roles",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"], ["permissions.id"], ondelete="CASCADE",
            name="fk_role_permissions__permissions",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_role_permissions__tenants",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"], ["users.id"], ondelete="SET NULL",
            name="fk_role_permissions__users",
        ),
        # Binds the denormalized tenant_id to the role's own.
        sa.ForeignKeyConstraint(
            ["role_id", "tenant_id"],
            ["roles.id", "roles.tenant_id"],
            ondelete="CASCADE",
            name="fk_role_permissions__role_tenant",
        ),
    )
    op.execute(
        "CREATE INDEX ix_role_permissions__permission ON role_permissions "
        "(permission_id, role_id)"
    )
    op.execute(
        "CREATE INDEX ix_role_permissions__tenant ON role_permissions "
        "(tenant_id) WHERE tenant_id IS NOT NULL"
    )

    op.create_table(
        "user_tenant_roles",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "is_owner", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")
        ),
        sa.Column("invited_by_user_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_user_tenant_roles"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_user_tenant_roles__tenants",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
            name="fk_user_tenant_roles__users",
        ),
        # RESTRICT, not CASCADE: deleting a role people still hold must fail
        # loudly rather than silently stripping their access.
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], ondelete="RESTRICT",
            name="fk_user_tenant_roles__roles",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"], ["users.id"], ondelete="SET NULL",
            name="fk_user_tenant_roles__inviter",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.id"],
            ondelete="CASCADE",
            name="fk_user_tenant_roles__locations",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_user_tenant_roles__tenant_user_role_global "
        "ON user_tenant_roles (tenant_id, user_id, role_id) "
        "WHERE location_id IS NULL AND revoked_at IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_user_tenant_roles__tenant_user_role_loc "
        "ON user_tenant_roles (tenant_id, user_id, role_id, location_id) "
        "WHERE location_id IS NOT NULL AND revoked_at IS NULL"
    )
    # Hot path A: authorization on every request. Also the index the `users`
    # RLS policy's EXISTS lands on.
    op.execute(
        "CREATE INDEX ix_user_tenant_roles__tenant_user "
        "ON user_tenant_roles (tenant_id, user_id) "
        "INCLUDE (role_id, location_id) WHERE revoked_at IS NULL"
    )
    # Hot path B: login. Leads with user_id because at login time there is no
    # tenant context yet. THE deliberate exception to contract §2.6, safe
    # precisely because it is the index that *establishes* tenant context, and
    # reachable only through a SECURITY DEFINER function run as receptionai_auth.
    op.execute(
        "CREATE INDEX ix_user_tenant_roles__user_tenant "
        "ON user_tenant_roles (user_id, tenant_id) WHERE revoked_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_user_tenant_roles__expires "
        "ON user_tenant_roles (expires_at) "
        "WHERE expires_at IS NOT NULL AND revoked_at IS NULL"
    )
    add_updated_at_trigger("user_tenant_roles")

    op.create_table(
        "sessions",
        sa.Column(
            "id", pg.UUID(as_uuid=True), nullable=False,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("refresh_token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("parent_session_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("family_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("ip_address", pg.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("device_fingerprint", sa.Text(), nullable=True),
        sa.Column("mfa_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "auth_method", sa.Text(), nullable=False,
            server_default=sa.text("'password'"),
        ),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_sessions__users"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_sessions__tenants",
        ),
        sa.ForeignKeyConstraint(
            ["parent_session_id"], ["sessions.id"], ondelete="SET NULL",
            name="fk_sessions__parent",
        ),
        sa.CheckConstraint(
            "octet_length(refresh_token_hash) = 32", name="ck_sessions__hash_len"
        ),
        sa.CheckConstraint("expires_at > issued_at", name="ck_sessions__expiry_future"),
        sa.CheckConstraint(
            "auth_method IN "
            "('password','oauth','saml','magic_link','impersonation')",
            name="ck_sessions__auth_method",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sessions__refresh_token_hash ON sessions "
        "(refresh_token_hash)"
    )
    op.execute(
        "CREATE INDEX ix_sessions__user_live ON sessions "
        "(user_id, expires_at DESC) WHERE revoked_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_sessions__family ON sessions (family_id) "
        "WHERE revoked_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_sessions__expires ON sessions (expires_at) "
        "WHERE revoked_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_sessions__tenant_user ON sessions (tenant_id, user_id) "
        "WHERE tenant_id IS NOT NULL AND revoked_at IS NULL"
    )
    # No updated_at trigger: last_seen_at is written on nearly every request and
    # a trigger would double the write amplification for no benefit (01 §3.6).


def _create_conversation_tables() -> None:
    """``conversations`` and ``turns``, partitioned by month on ``created_at``.

    Written as raw DDL rather than ``op.create_table``: Alembic emits
    ``PARTITION BY`` only through dialect kwargs that do not round-trip through
    autogenerate, and the PK ordering ``(tenant_id, created_at, id)`` matters
    enough to be visible verbatim.
    """
    op.execute(
        """
        CREATE TABLE conversations (
            id                  UUID NOT NULL DEFAULT uuid_generate_v7(),
            tenant_id           UUID NOT NULL,
            location_id         UUID,

            channel             conversation_channel NOT NULL,
            status              conversation_status NOT NULL DEFAULT 'active',

            external_ref        TEXT,
            channel_metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,

            contact_identifier  TEXT,
            contact_name        TEXT,
            customer_id         UUID,
            contact_hash        BYTEA,

            vertical_install_id UUID,
            agent_code          TEXT,
            agent_version       TEXT,

            started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            answered_at         TIMESTAMPTZ,
            ended_at            TIMESTAMPTZ,
            duration_ms         INTEGER,

            turn_count          INTEGER NOT NULL DEFAULT 0,
            total_input_tokens  BIGINT NOT NULL DEFAULT 0,
            total_output_tokens BIGINT NOT NULL DEFAULT 0,
            total_cost          NUMERIC(12,4) NOT NULL DEFAULT 0,
            cost_currency       CHAR(3) NOT NULL DEFAULT 'USD',

            first_response_ms   INTEGER,
            p95_response_ms     INTEGER,
            interruption_count  SMALLINT NOT NULL DEFAULT 0,
            escalated           BOOLEAN NOT NULL DEFAULT FALSE,
            escalated_at        TIMESTAMPTZ,
            escalation_reason   TEXT,

            language            TEXT,
            redacted_at         TIMESTAMPTZ,
            correlation_id      UUID,

            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

            -- PostgreSQL requires the partition key in every unique constraint
            -- on a partitioned table, so created_at must appear here. Leading
            -- with tenant_id keeps contract §2.6 intact and makes the PK index
            -- itself serve the dominant query.
            CONSTRAINT pk_conversations PRIMARY KEY (tenant_id, created_at, id),
            CONSTRAINT ck_conversations__duration
                CHECK (duration_ms IS NULL OR duration_ms >= 0),
            CONSTRAINT ck_conversations__end_after_start
                CHECK (ended_at IS NULL OR ended_at >= started_at),
            CONSTRAINT ck_conversations__ended_coherent
                CHECK (status <> 'ended' OR ended_at IS NOT NULL),
            CONSTRAINT ck_conversations__escalation_coherent
                CHECK (escalated = (escalated_at IS NOT NULL)),
            CONSTRAINT ck_conversations__cost_nonneg CHECK (total_cost >= 0)
        ) PARTITION BY RANGE (created_at);
        """
    )
    # Indexes are declared on the parent; PostgreSQL creates them on every
    # partition, existing and future.
    op.execute(
        "CREATE INDEX ix_conversations__tenant_started "
        "ON conversations (tenant_id, started_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_conversations__tenant_channel_started "
        "ON conversations (tenant_id, channel, started_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_conversations__tenant_status "
        "ON conversations (tenant_id, status, started_at DESC) "
        "WHERE status = 'active'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_conversations__tenant_external "
        "ON conversations (tenant_id, channel, external_ref, created_at) "
        "WHERE external_ref IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_conversations__tenant_contact "
        "ON conversations (tenant_id, contact_identifier, started_at DESC) "
        "WHERE contact_identifier IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_conversations__tenant_customer "
        "ON conversations (tenant_id, customer_id, started_at DESC) "
        "WHERE customer_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_conversations__tenant_location "
        "ON conversations (tenant_id, location_id, started_at DESC) "
        "WHERE location_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_conversations__escalated "
        "ON conversations (tenant_id, escalated_at DESC) WHERE escalated"
    )
    add_updated_at_trigger("conversations")

    op.execute(
        "COMMENT ON COLUMN conversations.contact_identifier IS "
        "'pii_class=medium; masked in logs and LLM prompts unless tenant "
        "opted in'"
    )
    op.execute(
        "COMMENT ON COLUMN conversations.contact_name IS 'pii_class=medium'"
    )
    op.execute(
        "COMMENT ON COLUMN conversations.customer_id IS "
        "'Nullable by design (04 §11.2.1): NULL means \"not identified\", the "
        "correct state for an anonymous visitor. Cross-tenant integrity is "
        "enforced by trg_conversations_customer_fk once `customers` exists, "
        "not by a declarative FK — see 01 §6.1.'"
    )

    op.execute(
        """
        CREATE TABLE turns (
            id                  UUID NOT NULL DEFAULT uuid_generate_v7(),
            tenant_id           UUID NOT NULL,
            conversation_id     UUID NOT NULL,

            sequence            INTEGER NOT NULL,
            role                turn_role NOT NULL,

            content             TEXT,
            content_parts       JSONB,

            tool_calls          JSONB,
            tool_call_id        TEXT,
            tool_name           TEXT,
            tool_result         JSONB,
            tool_error          TEXT,
            tool_duration_ms    INTEGER,

            model_provider      TEXT,
            model_name          TEXT,
            input_tokens        INTEGER,
            output_tokens       INTEGER,
            cached_tokens       INTEGER,
            reasoning_tokens    INTEGER,
            cost                NUMERIC(12,6),
            cost_currency       CHAR(3),

            latency_ms          INTEGER,
            time_to_first_token_ms INTEGER,

            finish_reason       TEXT,
            agent_node          TEXT,

            audio_start_ms      INTEGER,
            audio_end_ms        INTEGER,
            is_interruption     BOOLEAN NOT NULL DEFAULT FALSE,

            pii_redacted        BOOLEAN NOT NULL DEFAULT FALSE,
            safety_flags        TEXT[],

            correlation_id      UUID,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

            -- No FK to conversations: both are partitioned by created_at, so
            -- the FK would have to be (tenant_id, conversation_created_at,
            -- conversation_id), forcing the parent's partition key onto every
            -- child row of the hottest table in the system. Enforced in the
            -- repository instead (01 §6.2) — a considered exception, and one
            -- that gives up only a WITHIN-tenant guarantee.
            CONSTRAINT pk_turns PRIMARY KEY (tenant_id, created_at, id),
            CONSTRAINT ck_turns__sequence_nonneg CHECK (sequence >= 0),
            CONSTRAINT ck_turns__tokens_nonneg CHECK (
                COALESCE(input_tokens, 0) >= 0
                AND COALESCE(output_tokens, 0) >= 0
                AND COALESCE(cached_tokens, 0) >= 0
            ),
            CONSTRAINT ck_turns__cost_nonneg CHECK (cost IS NULL OR cost >= 0),
            CONSTRAINT ck_turns__cost_currency_paired
                CHECK ((cost IS NULL) = (cost_currency IS NULL)),
            CONSTRAINT ck_turns__tool_role_shape
                CHECK (role <> 'tool' OR tool_call_id IS NOT NULL),
            CONSTRAINT ck_turns__audio_order
                CHECK (audio_end_ms IS NULL OR audio_start_ms IS NULL
                       OR audio_end_ms >= audio_start_ms),
            CONSTRAINT ck_turns__content_present
                CHECK (content IS NOT NULL OR content_parts IS NOT NULL
                       OR tool_calls IS NOT NULL OR tool_result IS NOT NULL
                       OR role = 'event')
        ) PARTITION BY RANGE (created_at);
        """
    )
    # The dominant query by an enormous margin: "replay this conversation in
    # order". Every agent invocation runs it to rebuild context.
    op.execute(
        "CREATE UNIQUE INDEX uq_turns__conversation_sequence "
        "ON turns (tenant_id, conversation_id, sequence, created_at)"
    )
    op.execute(
        "CREATE INDEX ix_turns__tenant_created ON turns (tenant_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_turns__tenant_tool "
        "ON turns (tenant_id, tool_name, created_at DESC) "
        "WHERE tool_name IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_turns__tenant_model "
        "ON turns (tenant_id, model_provider, model_name, created_at DESC) "
        "WHERE model_name IS NOT NULL"
    )
    # A GIN index on an array cannot lead with tenant_id; declared as an
    # exception in the §11.7 conformance test.
    op.execute(
        "CREATE INDEX ix_turns__safety ON turns USING gin (safety_flags) "
        "WHERE safety_flags IS NOT NULL"
    )
    op.execute(
        "COMMENT ON COLUMN turns.content IS "
        "'pii_class=high; customer utterances; redacted per tenant retention "
        "policy'"
    )


def _create_partitions() -> None:
    """Bootstrap partitions for the current month and the next three.

    There is deliberately **no DEFAULT partition**. A default would silently
    accept rows outside every defined range — precisely the failure we want to
    be loud. An insert with no matching partition raises, pages someone, and
    gets fixed; a default would instead accumulate rows that then make *adding*
    the correct partition require an ``ACCESS EXCLUSIVE`` scan of the default.

    In production this is a daily, idempotent Temporal schedule running three
    months ahead, so a missed run is self-healing.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ops_create_monthly_partitions(p_month DATE)
        RETURNS TEXT[]
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
            parent      TEXT;
            parents     TEXT[] := ARRAY['conversations','turns'];
            part_name   TEXT;
            range_start DATE := date_trunc('month', p_month)::date;
            range_end   DATE := (date_trunc('month', p_month)
                                 + INTERVAL '1 month')::date;
            created     TEXT[] := '{}';
        BEGIN
            FOREACH parent IN ARRAY parents LOOP
                part_name := format('%s_p%s', parent,
                                    to_char(range_start, 'YYYYMM'));

                IF NOT EXISTS (
                    SELECT 1 FROM pg_class WHERE relname = part_name
                ) THEN
                    EXECUTE format(
                        'CREATE TABLE %I PARTITION OF %I '
                        'FOR VALUES FROM (%L) TO (%L)',
                        part_name, parent, range_start, range_end
                    );
                    -- Partitions inherit the parent's indexes and RLS
                    -- automatically. Ownership must be set explicitly so
                    -- default privileges apply.
                    EXECUTE format('ALTER TABLE %I OWNER TO receptionai_owner',
                                   part_name);
                    created := created || part_name;
                END IF;
            END LOOP;

            RETURN created;
        END;
        $$;
        """
    )
    # `SET search_path = public, pg_temp` on every SECURITY DEFINER function is
    # mandatory: without it a caller can prepend a schema containing a malicious
    # table of the same name and hijack the definer's privileges.
    op.execute(
        "REVOKE ALL ON FUNCTION ops_create_monthly_partitions(DATE) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION ops_create_monthly_partitions(DATE) "
        "TO receptionai_ops"
    )
    op.execute(
        """
        SELECT ops_create_monthly_partitions(d::date)
          FROM generate_series(date_trunc('month', now()),
                               date_trunc('month', now()) + INTERVAL '3 months',
                               INTERVAL '1 month') AS d;
        """
    )


# ─────────────────────────────────────────────────────────────────────────────
# RLS
# ─────────────────────────────────────────────────────────────────────────────


def _apply_rls() -> None:
    # The tenant-owned tables, through the DO $$ loop (01 §11.3).
    enable_tenant_rls_bulk(TENANT_OWNED_TABLES)

    # RLS on a partitioned parent propagates to every partition automatically,
    # including partitions created later. We do NOT define policies on
    # individual partitions; doing so would create a second, divergent set.

    # ── tenants: its own id IS the scope (01 §11.5) ──────────────────────
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants FORCE  ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY rls_tenants__self ON tenants
            FOR ALL
            TO {TENANT_ROLES}
            USING      (id = app_current_tenant())
            WITH CHECK (id = app_current_tenant())
        """
    )

    # ── users: visible only to tenants they are a member of (01 §11.5) ───
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users FORCE  ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY rls_users__member_visible ON users
            FOR SELECT
            TO receptionai_app, receptionai_reporting
            USING (
                EXISTS (
                    SELECT 1 FROM user_tenant_roles utr
                     WHERE utr.user_id = users.id
                       AND utr.tenant_id = app_current_tenant()
                       AND utr.revoked_at IS NULL
                )
            )
        """
    )
    op.execute(
        """
        CREATE POLICY rls_users__self_update ON users
            FOR UPDATE
            TO receptionai_app
            USING      (id = NULLIF(current_setting('app.user_id', true), '')::uuid)
            WITH CHECK (id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """
    )
    # Invitations create users. Only the app role, and only rows that are
    # immediately made members in the same transaction (asserted by the service
    # layer).
    op.execute(
        """
        CREATE POLICY rls_users__insert ON users
            FOR INSERT
            TO receptionai_app
            WITH CHECK (true)
        """
    )

    # ── sessions: your own rows only (01 §11.5) ──────────────────────────
    op.execute("ALTER TABLE sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sessions FORCE  ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY rls_sessions__own ON sessions
            FOR ALL
            TO receptionai_app
            USING      (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
            WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """
    )

    # ── roles / role_permissions: templates readable by all (01 §11.5) ───
    op.execute("ALTER TABLE roles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE roles FORCE  ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY rls_roles__read ON roles
            FOR SELECT
            TO receptionai_app, receptionai_reporting
            USING (tenant_id IS NULL OR tenant_id = app_current_tenant())
        """
    )
    op.execute(
        """
        CREATE POLICY rls_roles__write ON roles
            FOR ALL
            TO receptionai_app
            USING      (tenant_id = app_current_tenant() AND NOT is_system)
            WITH CHECK (tenant_id = app_current_tenant() AND NOT is_system)
        """
    )

    op.execute("ALTER TABLE role_permissions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE role_permissions FORCE  ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY rls_role_permissions__read ON role_permissions
            FOR SELECT
            TO receptionai_app, receptionai_reporting
            USING (tenant_id IS NULL OR tenant_id = app_current_tenant())
        """
    )
    op.execute(
        """
        CREATE POLICY rls_role_permissions__write ON role_permissions
            FOR ALL
            TO receptionai_app
            USING      (tenant_id = app_current_tenant())
            WITH CHECK (tenant_id = app_current_tenant())
        """
    )


def _apply_grants() -> None:
    """Grants and default privileges — 01 §16.1.

    **The critical detail is the default privileges.** Without them, every
    migration that creates a table must remember to ``GRANT`` on it — and the
    one that forgets produces a table the application cannot read, discovered in
    production.

    Functions are the deliberate opposite: NOT granted by default, so each
    ``SECURITY DEFINER`` function forces a conscious decision. A function is a
    privilege-escalation surface; a table is not.
    """
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute(
        "GRANT USAGE ON SCHEMA public TO receptionai_app, receptionai_auth, "
        "receptionai_reporting, receptionai_ops"
    )

    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE receptionai_owner IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO receptionai_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE receptionai_owner IN SCHEMA public "
        "GRANT SELECT ON TABLES TO receptionai_reporting"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE receptionai_owner IN SCHEMA public "
        "GRANT SELECT, UPDATE ON TABLES TO receptionai_ops"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE receptionai_owner IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO receptionai_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE receptionai_owner IN SCHEMA public "
        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    )

    # Default privileges apply only to objects created AFTER they are set, and
    # this migration created its tables first (they had to exist before their
    # policies). So the grants are also applied explicitly here. This is 01
    # §16.5's ordering trap, handled rather than tripped over.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
        "TO receptionai_app"
    )
    op.execute(
        "GRANT SELECT ON ALL TABLES IN SCHEMA public TO receptionai_reporting"
    )
    op.execute(
        "GRANT SELECT, UPDATE ON ALL TABLES IN SCHEMA public TO receptionai_ops"
    )

    # Platform catalogue: read-only reference data, no RLS, privilege control
    # instead (01 §11.5).
    op.execute(
        "REVOKE INSERT, UPDATE, DELETE ON permissions "
        "FROM receptionai_app, receptionai_reporting, receptionai_ops"
    )

    # `alembic_version` is owned by the migration role and no runtime role has
    # any business touching it.
    op.execute(
        "REVOKE ALL ON alembic_version "
        "FROM receptionai_app, receptionai_reporting, receptionai_ops"
    )

    op.execute(
        "GRANT EXECUTE ON FUNCTION app_current_tenant() "
        "TO receptionai_app, receptionai_auth, receptionai_reporting, "
        "receptionai_ops"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_is_platform_admin() TO receptionai_app"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION uuid_generate_v7() TO receptionai_app"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION uuid_v7_timestamp(uuid) "
        "TO receptionai_app, receptionai_reporting, receptionai_ops"
    )


def _create_auth_functions() -> None:
    """The ``auth`` role's narrow window — 01 §11.6.

    Login and API-key resolution must read before a tenant context exists.
    Rather than weakening any policy, ``receptionai_auth`` gets ``EXECUTE`` on
    these ``SECURITY DEFINER`` functions and ``SELECT`` on nothing.

    Note what they return: authentication material and nothing else. No booking
    data, no conversation content, no secrets. The blast radius of a compromised
    ``auth`` credential is "can enumerate password hashes", not "can read every
    tenant".
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_resolve_user_by_email(p_email TEXT)
        RETURNS TABLE (
            user_id UUID, email TEXT, password_hash TEXT, password_algo TEXT,
            status user_status, mfa_enforced BOOLEAN, locked_until TIMESTAMPTZ,
            is_platform_staff BOOLEAN
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT u.id, u.email, u.password_hash, u.password_algo, u.status,
                   u.mfa_enforced, u.locked_until, u.is_platform_staff
              FROM users u
             WHERE u.email_normalized = lower(btrim(p_email))
               AND u.deleted_at IS NULL;
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_list_memberships(p_user_id UUID)
        RETURNS TABLE (tenant_id UUID, tenant_slug TEXT,
                       tenant_status tenant_status, role_code TEXT,
                       location_id UUID)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT utr.tenant_id, t.slug, t.status, r.code, utr.location_id
              FROM user_tenant_roles utr
              JOIN tenants t ON t.id = utr.tenant_id AND t.deleted_at IS NULL
              JOIN roles   r ON r.id = utr.role_id
             WHERE utr.user_id = p_user_id
               AND utr.revoked_at IS NULL
               AND (utr.expires_at IS NULL OR utr.expires_at > now());
        $$;
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION auth_resolve_user_by_email(TEXT) FROM PUBLIC"
    )
    op.execute("REVOKE ALL ON FUNCTION auth_list_memberships(UUID) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION auth_resolve_user_by_email(TEXT) "
        "TO receptionai_auth"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION auth_list_memberships(UUID) TO receptionai_auth"
    )


def upgrade() -> None:
    # ── Extensions (01 §1.1) ─────────────────────────────────────────────
    # btree_gist is non-optional: without it,
    # `EXCLUDE USING gist (tenant_id WITH =, ...)` fails, because the default
    # GiST opclass set has no equality operator for uuid. Phase 1 has no
    # exclusion constraint yet, but the booking engine's depends on it and
    # creating an extension is not a thing to leave to the migration that first
    # needs it.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    _create_roles()
    _create_functions()
    _create_enums()

    _create_tenancy_tables()
    _create_identity_tables()
    _create_conversation_tables()
    _create_partitions()

    _apply_rls()
    _create_auth_functions()
    _apply_grants()


def downgrade() -> None:
    # Tables drop their own policies, triggers, and indexes with them. Order is
    # the reverse of creation so FKs never block a drop.
    op.execute("DROP TABLE IF EXISTS turns CASCADE")
    op.execute("DROP TABLE IF EXISTS conversations CASCADE")
    op.execute("DROP TABLE IF EXISTS sessions CASCADE")
    op.execute("DROP TABLE IF EXISTS user_tenant_roles CASCADE")
    op.execute("DROP TABLE IF EXISTS role_permissions CASCADE")
    op.execute("DROP TABLE IF EXISTS roles CASCADE")
    op.execute("DROP TABLE IF EXISTS permissions CASCADE")
    op.execute("DROP TABLE IF EXISTS users CASCADE")
    op.execute("DROP TABLE IF EXISTS tenant_settings CASCADE")
    op.execute("DROP TABLE IF EXISTS locations CASCADE")
    op.execute("DROP TABLE IF EXISTS tenants CASCADE")

    op.execute("DROP FUNCTION IF EXISTS auth_list_memberships(UUID)")
    op.execute("DROP FUNCTION IF EXISTS auth_resolve_user_by_email(TEXT)")
    op.execute("DROP FUNCTION IF EXISTS ops_create_monthly_partitions(DATE)")
    op.execute("DROP FUNCTION IF EXISTS app_is_platform_admin()")
    op.execute("DROP FUNCTION IF EXISTS app_current_tenant()")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    op.execute("DROP FUNCTION IF EXISTS uuid_v7_timestamp(uuid)")
    op.execute("DROP FUNCTION IF EXISTS uuid_generate_v7()")

    for type_name in (
        "outcome_kind",
        "turn_role",
        "conversation_status",
        "conversation_channel",
        "user_status",
        "tenant_status",
    ):
        op.execute(f"DROP TYPE IF EXISTS {type_name}")

    # Roles are deliberately NOT dropped: they may own objects in other
    # databases on the same cluster, and a downgrade that removes the
    # application's login role turns a schema rollback into an outage.

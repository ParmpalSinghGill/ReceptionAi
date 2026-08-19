"""Tenancy models — 01 §2.1, §2.2, §2.3.

``tenants`` is the root aggregate: one of only two tables in the system that is
not tenant-scoped in the usual sense, because its own ``id`` *is* the tenant
scope.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from packages.persistence.models.base import (
    Base,
    SoftDeleteMixin,
    TenantMixin,
    VersionedMixin,
)
from packages.persistence.models.enums import TenantStatus, tenant_status_enum
from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, REGCONFIG
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

__all__ = ["Location", "Tenant", "TenantSetting"]


class Tenant(Base, VersionedMixin, SoftDeleteMixin):
    """The root aggregate. 01 §2.1."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v7()"),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    legal_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[TenantStatus] = mapped_column(
        tenant_status_enum, nullable=False, server_default=text("'trial'")
    )

    # Contract §2 escape hatch. NULL = lives in the shared cluster.
    # A non-null value names a logical shard the ConnectionRouter resolves to a
    # dedicated engine. Adding a shard is a data change, never a schema change.
    #
    # Setting this is a COMPLIANCE COMMITMENT, not a performance tweak: it is
    # set only for tenants whose contract or regulator makes the shared cluster
    # unacceptable. Per contract A8, a non-null value the router cannot resolve
    # to a configuration refuses the session rather than falling back to shared.
    shard_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    default_timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'UTC'")
    )
    default_locale: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'en-US'")
    )
    default_currency: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, server_default=text("'USD'")
    )

    # REGCONFIG rather than TEXT so an invalid config name is rejected at write
    # time and so the value can be passed straight to to_tsvector() without a
    # cast or a lookup table (contract A4).
    default_text_search_config: Mapped[str] = mapped_column(
        REGCONFIG, nullable=False, server_default=text("'simple'")
    )

    data_region: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'us-east-1'")
    )
    compliance_profile: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'standard'")
    )

    kms_key_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Platform metadata. NOT tenant-configurable; see tenant_settings for that.
    # Named `metadata_` in Python because `metadata` is reserved on a
    # DeclarativeBase (it is the MetaData object); the column name is unchanged.
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    trial_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    suspended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    suspension_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            r"slug ~ '^[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])?$'",
            name="slug_format",
        ),
        CheckConstraint(
            r"default_currency ~ '^[A-Z]{3}$'", name="currency_format"
        ),
        # `now() AT TIME ZONE <bad name>` raises invalid_parameter_value, which
        # aborts the statement — so an invalid IANA name becomes a write-time
        # rejection. Technically a non-immutable expression in a CHECK, which
        # PostgreSQL permits in this form; the residual risk (a tzdata update
        # invalidating an existing row) is caught by a nightly validation query
        # rather than at read time.
        CheckConstraint(
            "now() AT TIME ZONE default_timezone IS NOT NULL",
            name="timezone_valid",
        ),
        CheckConstraint(
            "compliance_profile IN ('standard','hipaa','gdpr_strict','pci')",
            name="compliance_profile",
        ),
        CheckConstraint(
            "(status = 'suspended') = (suspended_at IS NOT NULL)",
            name="suspension_coherent",
        ),
        # Slug is the public URL identity; unique among live tenants only, so a
        # deleted tenant does not permanently burn a name.
        Index(
            "uq_tenants__slug",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_tenants__shard_key",
            "shard_key",
            postgresql_where=text("shard_key IS NOT NULL"),
        ),
        Index(
            "ix_tenants__status",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class Location(Base, TenantMixin, VersionedMixin, SoftDeleteMixin):
    """A physical or logical site. Owns the operating calendar and timezone.
    01 §2.2.
    """

    __tablename__ = "locations"

    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)

    # IANA; authoritative for all local rendering. Business-local time is
    # derived from this at render time, never stored (contract §6).
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    locale: Mapped[str | None] = mapped_column(Text, nullable=True)

    address_line1: Mapped[str | None] = mapped_column(Text, nullable=True)
    address_line2: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str | None] = mapped_column(Text, nullable=True)
    postal_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    country_code: Mapped[str | None] = mapped_column(CHAR(2), nullable=True)

    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)

    phone_e164: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)

    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_locations__tenants",
        ),
        CheckConstraint(
            "now() AT TIME ZONE timezone IS NOT NULL",
            name="timezone_valid",
        ),
        CheckConstraint(
            r"currency IS NULL OR currency ~ '^[A-Z]{3}$'",
            name="currency_format",
        ),
        CheckConstraint(
            r"country_code IS NULL OR country_code ~ '^[A-Z]{2}$'",
            name="country_format",
        ),
        CheckConstraint(
            r"phone_e164 IS NULL OR phone_e164 ~ '^\+[1-9]\d{6,14}$'",
            name="phone_e164",
        ),
        CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL)", name="geo_paired"
        ),
        # The composite-FK target. Child tables reference (tenant_id, id)
        # against this rather than plain id, which makes it structurally
        # impossible for a row in tenant A to point at a location in tenant B —
        # an FK violation rather than an RLS-invisible dangling reference.
        # RLS filters *reads*; it does not stop an INSERT naming a foreign id,
        # because the FK check runs with RLS bypassed for referential integrity.
        # This unique index is what closes that hole at the storage layer.
        UniqueConstraint("tenant_id", "id", name="uq_locations__tenant_id_id"),
        Index(
            "uq_locations__tenant_code",
            "tenant_id",
            "code",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_locations__tenant_active",
            "tenant_id",
            "is_active",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class TenantSetting(Base, TenantMixin, VersionedMixin):
    """Namespaced, versioned, typed configuration. 01 §2.3.

    Resolution order (implemented here in Python, not in SQL):
    location row → tenant row → vertical manifest default → platform default.
    The first non-null wins. Kept out of SQL because the manifest layer is a
    JSONB document the database should not have to interpret.
    """

    __tablename__ = "tenant_settings"

    # NULL location_id = tenant-wide default. A non-null row overrides it.
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )

    namespace: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # The JSON Schema fragment this value was validated against, so we can
    # re-validate after a platform upgrade and report drift.
    schema_version: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'1'")
    )

    is_secret_ref: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_tenant_settings__tenants",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.id"],
            ondelete="CASCADE",
            name="fk_tenant_settings__locations",
        ),
        CheckConstraint(
            r"namespace ~ '^[a-z][a-z0-9_]{0,31}$'",
            name="namespace",
        ),
        CheckConstraint(
            r"key ~ '^[a-z][a-z0-9_.]{0,63}$'", name="key"
        ),
        # Two partial uniques rather than one, because NULL never equals NULL in
        # a unique index: without this split, a tenant could insert two
        # tenant-wide rows for the same key.
        Index(
            "uq_tenant_settings__tenant_ns_key_global",
            "tenant_id",
            "namespace",
            "key",
            unique=True,
            postgresql_where=text("location_id IS NULL"),
        ),
        Index(
            "uq_tenant_settings__tenant_loc_ns_key",
            "tenant_id",
            "location_id",
            "namespace",
            "key",
            unique=True,
            postgresql_where=text("location_id IS NOT NULL"),
        ),
        # Settings are read as a whole namespace on nearly every request path,
        # so the namespace is in the index, not just the key.
        Index(
            "ix_tenant_settings__tenant_namespace",
            "tenant_id",
            "namespace",
            "location_id",
        ),
    )

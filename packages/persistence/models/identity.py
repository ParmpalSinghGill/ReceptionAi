"""Identity & access models — 01 §3.

**Users are global; membership is tenant-scoped.** A person may work for two
tenants (an agency managing three restaurants; a contractor covering two
clinics). Modelling ``users`` as tenant-owned forces duplicate accounts with
duplicate credentials and duplicate MFA enrolment — a security and UX
regression. So ``users`` is a global table keyed by email and
``user_tenant_roles`` is the tenant-owned join that grants access.

This is the one deliberate departure from "every table has ``tenant_id``", and
the contract anticipates it: §2.1 says every *tenant-owned* table has
``tenant_id``, and ``users`` is not tenant-owned. Its RLS predicate is therefore
an existence check against the membership table, not a ``tenant_id`` comparison
(01 §11.5).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from packages.persistence.models.base import Base, SoftDeleteMixin, VersionedMixin
from packages.persistence.models.enums import UserStatus, user_status_enum
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

__all__ = [
    "Permission",
    "Role",
    "RolePermission",
    "Session",
    "User",
    "UserTenantRole",
]


class User(Base, VersionedMixin, SoftDeleteMixin):
    """A global account. 01 §3.2.

    PII classification (contract §8): ``password_hash`` is ``pii_class=high``
    and never logged; ``email`` and ``phone_e164`` are ``pii_class=medium`` and
    masked in traces. The ``COMMENT ON COLUMN`` statements carrying these are
    emitted by the migration, so the classification travels with the schema and
    a masking sweep can read it from the catalogue.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v7()"),
    )

    email: Mapped[str] = mapped_column(Text, nullable=False)
    # Generated rather than a functional index so the normalized value is also
    # queryable and joinable. 'Bob@X.com' and 'bob@x.com' are one account.
    email_normalized: Mapped[str] = mapped_column(
        Text,
        Computed("lower(btrim(email))", persisted=True),
        nullable=False,
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # NULL for SSO-only users who never set a local password.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_algo: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    full_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_e164: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    locale: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'en-US'")
    )
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'UTC'")
    )

    status: Mapped[UserStatus] = mapped_column(
        user_status_enum, nullable=False, server_default=text("'invited'")
    )

    # Platform staff. Bypasses nothing in the DB; only unlocks admin routes,
    # which then set app.platform_admin explicitly and audibly. Note that
    # app.platform_admin gates only platform-catalogue tables — it appears in no
    # tenant-table policy (01 §11.4), so this flag can never widen tenant access.
    is_platform_staff: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )

    mfa_enforced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    failed_login_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            r"email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'",
            name="email_shape",
        ),
        CheckConstraint(
            r"phone_e164 IS NULL OR phone_e164 ~ '^\+[1-9]\d{6,14}$'",
            name="phone_e164",
        ),
        CheckConstraint(
            "(password_hash IS NULL) = (password_algo IS NULL)",
            name="password_paired",
        ),
        CheckConstraint(
            "now() AT TIME ZONE timezone IS NOT NULL", name="timezone_valid"
        ),
        Index(
            "uq_users__email_normalized",
            "email_normalized",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Admin user search ("find bob") without a leading-wildcard seq scan.
        Index(
            "ix_users__email_trgm",
            text("email_normalized gin_trgm_ops"),
            postgresql_using="gin",
        ),
        Index(
            "ix_users__locked",
            "locked_until",
            postgresql_where=text("locked_until IS NOT NULL"),
        ),
    )


class Permission(Base):
    """Platform catalogue: a fixed vocabulary shipped with the release. 01 §3.3.

    Not tenant data, so it gets no RLS — it gets privilege control instead
    (SELECT to every runtime role, INSERT/UPDATE/DELETE revoked). That is the
    correct treatment for read-only reference data: an RLS policy on a table
    with no ``tenant_id`` would need a predicate that is either ``true`` (no
    protection) or a subquery (all cost, no benefit).
    """

    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v7()"),
    )

    # '<resource>:<action>' e.g. 'booking:create', 'knowledge:publish'
    code: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Permissions that only make sense with a feature entitlement.
    requires_feature: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Dangerous permissions require a fresh MFA assertion at use time.
    is_sensitive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            r"code ~ '^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$'",
            name="code_shape",
        ),
        # Belt and braces: the denormalized parts cannot disagree with the code.
        CheckConstraint(
            "code = resource || ':' || action",
            name="code_matches_parts",
        ),
        Index("uq_permissions__code", "code", unique=True),
        Index("ix_permissions__resource", "resource"),
    )


class Role(Base, VersionedMixin):
    """Platform template (``tenant_id IS NULL``) or tenant-authored. 01 §3.3."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v7()"),
    )

    # NULL = platform-defined template role, visible to every tenant.
    # Non-null = a role this tenant authored.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )

    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )

    # Ordering for "can this role manage that role" checks. Higher = more
    # powerful. A user may only assign roles at or below their own rank.
    rank: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_roles__tenants",
        ),
        CheckConstraint(r"code ~ '^[a-z][a-z0-9_]{0,31}$'", name="code_shape"),
        CheckConstraint(
            "NOT is_system OR tenant_id IS NULL", name="system_is_global"
        ),
        CheckConstraint("rank BETWEEN 0 AND 100", name="rank"),
        Index(
            "uq_roles__global_code",
            "code",
            unique=True,
            postgresql_where=text("tenant_id IS NULL"),
        ),
        Index(
            "uq_roles__tenant_code",
            "tenant_id",
            "code",
            unique=True,
            postgresql_where=text("tenant_id IS NOT NULL"),
        ),
        # Target of the composite FK from role_permissions, which must tolerate
        # a NULL tenant_id on template roles. A UNIQUE INDEX (not a
        # UniqueConstraint) because a NULL in a unique *constraint* column is
        # still permitted here — id alone is already unique, so this index only
        # ever adds the second column to the FK's reachable key.
        Index("uq_roles__id_tenant", "id", "tenant_id", unique=True),
    )


class RolePermission(Base):
    """Role → permission grants. 01 §3.3.

    ``tenant_id`` is denormalized from ``roles.tenant_id`` so RLS on this table
    is a plain predicate rather than a subquery. An RLS predicate requiring a
    subquery (``EXISTS (SELECT 1 FROM roles r WHERE ...)``) is evaluated per row
    and defeats index-only scans; carrying ``tenant_id`` and binding it with a
    composite FK gives the same integrity guarantee with a predicate the planner
    can push into an index scan. This trade recurs throughout the schema and is
    deliberate.
    """

    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    permission_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False
    )

    # NULL for template roles.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )

    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    granted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        PrimaryKeyConstraint("role_id", "permission_id", name="pk_role_permissions"),
        ForeignKeyConstraint(
            ["role_id"], ["roles.id"], ondelete="CASCADE",
            name="fk_role_permissions__roles",
        ),
        ForeignKeyConstraint(
            ["permission_id"], ["permissions.id"], ondelete="CASCADE",
            name="fk_role_permissions__permissions",
        ),
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_role_permissions__tenants",
        ),
        ForeignKeyConstraint(
            ["granted_by_user_id"], ["users.id"], ondelete="SET NULL",
            name="fk_role_permissions__users",
        ),
        # Binds the denormalized tenant_id to the role's own: a grant cannot
        # claim a tenant the role does not belong to.
        ForeignKeyConstraint(
            ["role_id", "tenant_id"],
            ["roles.id", "roles.tenant_id"],
            ondelete="CASCADE",
            name="fk_role_permissions__role_tenant",
        ),
        # Reverse lookup: "who has knowledge:publish" for an audit report.
        Index("ix_role_permissions__permission", "permission_id", "role_id"),
        Index(
            "ix_role_permissions__tenant",
            "tenant_id",
            postgresql_where=text("tenant_id IS NOT NULL"),
        ),
    )


class UserTenantRole(Base):
    """The membership table — what makes a user "in" a tenant at all. 01 §3.4."""

    __tablename__ = "user_tenant_roles"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v7()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    role_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    # Optional narrowing: a manager scoped to one location only.
    # NULL = all locations in the tenant.
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )

    is_owner: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )

    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    invited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Time-boxed contractor access.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_user_tenant_roles__tenants",
        ),
        ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
            name="fk_user_tenant_roles__users",
        ),
        # RESTRICT, not CASCADE: deleting a role that people still hold must
        # fail loudly rather than silently stripping their access.
        ForeignKeyConstraint(
            ["role_id"], ["roles.id"], ondelete="RESTRICT",
            name="fk_user_tenant_roles__roles",
        ),
        ForeignKeyConstraint(
            ["invited_by_user_id"], ["users.id"], ondelete="SET NULL",
            name="fk_user_tenant_roles__inviter",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.id"],
            ondelete="CASCADE",
            name="fk_user_tenant_roles__locations",
        ),
        # A user holds a given role at a given scope at most once (while live).
        Index(
            "uq_user_tenant_roles__tenant_user_role_global",
            "tenant_id",
            "user_id",
            "role_id",
            unique=True,
            postgresql_where=text("location_id IS NULL AND revoked_at IS NULL"),
        ),
        Index(
            "uq_user_tenant_roles__tenant_user_role_loc",
            "tenant_id",
            "user_id",
            "role_id",
            "location_id",
            unique=True,
            postgresql_where=text("location_id IS NOT NULL AND revoked_at IS NULL"),
        ),
        # Hot path A: authorization on every request — "roles of user U in
        # tenant T". Also the index the `users` RLS policy's EXISTS lands on.
        Index(
            "ix_user_tenant_roles__tenant_user",
            "tenant_id",
            "user_id",
            postgresql_include=["role_id", "location_id"],
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # Hot path B: login — "which tenants can user U enter". Leads with
        # user_id because at login time there is no tenant context yet. This is
        # the deliberate exception to contract §2.6's tenant-first rule, and it
        # is safe precisely because it is the index that *establishes* tenant
        # context. It runs as the dedicated `auth` role against a narrow
        # SECURITY DEFINER function, not as the general runtime role. It is the
        # only such index in the schema, called out so no future reader mistakes
        # it for drift, and listed in the §11.7 conformance exception array.
        Index(
            "ix_user_tenant_roles__user_tenant",
            "user_id",
            "tenant_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # Expiry sweeper.
        Index(
            "ix_user_tenant_roles__expires",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL AND revoked_at IS NULL"),
        ),
    )


class Session(Base):
    """Interactive browser sessions for the admin portal. 01 §3.6.

    Not tenant-owned in the RLS sense: its policy is ``user_id = app.user_id``
    (01 §11.5), because a session exists before a tenant is chosen and a user
    must be able to see their own sessions across every tenant they belong to.

    **No ``updated_at`` trigger.** ``last_seen_at`` is written on nearly every
    request; a trigger would double the write amplification for no benefit. The
    app updates it, throttled to at most once per 60 seconds per session, to
    keep this off the hot path.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v7()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    # The tenant this session is currently "acting as". A user with three
    # memberships switches tenants by minting a new session, not by mutating
    # this — so a stolen cookie cannot be pivoted to another tenant.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )

    # Opaque refresh token, hashed. The short-lived access JWT is never stored.
    refresh_token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # Rotation chain: each refresh mints a new row pointing at its parent.
    # Reuse of a rotated token is a theft signal and revokes the whole family.
    parent_session_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    device_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Which factors were satisfied, and when. Sensitive permissions require a
    # recent mfa_at (step-up auth).
    mfa_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    auth_method: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'password'")
    )

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
            name="fk_sessions__users",
        ),
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_sessions__tenants",
        ),
        ForeignKeyConstraint(
            ["parent_session_id"], ["sessions.id"], ondelete="SET NULL",
            name="fk_sessions__parent",
        ),
        CheckConstraint(
            "octet_length(refresh_token_hash) = 32", name="hash_len"
        ),
        CheckConstraint("expires_at > issued_at", name="expiry_future"),
        CheckConstraint(
            "auth_method IN ('password','oauth','saml','magic_link','impersonation')",
            name="auth_method",
        ),
        Index("uq_sessions__refresh_token_hash", "refresh_token_hash", unique=True),
        Index(
            "ix_sessions__user_live",
            "user_id",
            text("expires_at DESC"),
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # Revoke-the-family on token reuse.
        Index(
            "ix_sessions__family",
            "family_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # Reaper. Partial index keeps it small: only unexpired rows matter.
        Index(
            "ix_sessions__expires",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "ix_sessions__tenant_user",
            "tenant_id",
            "user_id",
            postgresql_where=text("tenant_id IS NOT NULL AND revoked_at IS NULL"),
        ),
    )

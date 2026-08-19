"""The declarative base and the mixins every model composes — 01 §15.8."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, Integer, MetaData, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "SoftDeleteMixin",
    "TenantMixin",
    "VersionedMixin",
    "tenant_table_names",
]

NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s__%(referred_table_name)s",
    "uq": "uq_%(table_name)s__%(column_0_N_name)s",
    "ck": "ck_%(table_name)s__%(constraint_name)s",
    "ix": "ix_%(table_name)s__%(column_0_N_name)s",
}


class Base(DeclarativeBase):
    """Naming conventions here are what make Alembic autogenerate produce
    stable, reviewable diffs. Without them, dropping and recreating a constraint
    yields a random system-assigned name and every migration churns.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # RUF012 reads this as a mutable class default; it is SQLAlchemy's
    # declarative API for mapping Python annotations to column types, and
    # must be a plain dict on the class.
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB}


class TenantMixin:
    """Applied to every tenant-owned model.

    The mixin — rather than repetition — is what lets the RLS conformance test
    enumerate tenant tables by introspecting the model registry and assert
    coverage. A new tenant-owned model that forgets the mixin also forgets
    ``tenant_id``, which the migration's ``DO $$`` loop then refuses to register,
    so the omission fails CI twice over.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v7()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        server_onupdate=func.now(),
    )


class VersionedMixin:
    """Optimistic concurrency.

    ``version`` is bumped explicitly by the repository
    (``UPDATE ... SET version = version + 1 WHERE version = :expected``), never
    by a trigger, so a lost update surfaces as ``rowcount = 0`` and becomes a
    409 — rather than being silently papered over, which is what a trigger would
    do since it cannot report that the caller's expectation was violated.
    """

    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )


class SoftDeleteMixin:
    """Applied only where audit or referential history genuinely requires it.

    Soft deletes complicate every unique constraint (they force partial uniques
    on ``WHERE deleted_at IS NULL``) and every join, so 01 §1.4 restricts them
    to ``resources``, ``services``, ``users``, ``tenant_connectors``,
    ``knowledge_sources`` — plus ``tenants`` and ``locations``, whose slugs and
    codes are public identities that must not be permanently burned by a delete.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


def tenant_table_names() -> frozenset[str]:
    """Every mapped table carrying a ``tenant_id`` column.

    The RLS conformance test compares this against the array in the migration's
    ``DO $$`` loop. Deriving it from the mapper registry rather than a hand-kept
    list is the point: a new tenant-owned model appears here automatically, so
    forgetting to register it in the loop fails the build instead of shipping an
    unprotected table.
    """
    return frozenset(
        table.name
        for table in Base.metadata.tables.values()
        if "tenant_id" in table.columns
    )

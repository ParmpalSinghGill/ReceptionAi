"""The base repository — 01 §15.4."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, ClassVar
from uuid import UUID

from packages.core.errors import (
    ConcurrencyConflictError,
    EntityNotFoundError,
    ResourceConflictError,
)
from packages.core.ids import uuid7_timestamp
from packages.persistence.models.base import Base
from packages.persistence.session import TenantSession
from sqlalchemy import ColumnElement, Select, delete, func, select, update
from sqlalchemy.exc import IntegrityError

__all__ = ["TenantRepository", "uuid7_timestamp"]


class TenantRepository[ModelT: Base]:
    """Base repository for tenant-owned entities.

    Every query issued through this class carries an explicit
    ``tenant_id == session.tenant_ctx.tenant_id`` predicate, even though RLS
    already enforces it. That redundancy is intentional and buys three things:

      1. Defence in depth — contract §8 requires the service layer to re-check
         what the database enforces.
      2. Index usability — the planner sees a literal predicate and picks the
         tenant-leading index, rather than relying on the RLS predicate being
         pushed down (which it usually is, but "usually" is not a plan).
      3. Local testability — repository tests can run without RLS enabled and
         still verify scoping.
    """

    model: ClassVar[type[Any]]
    #: Set True on partitioned models so point lookups add a created_at window.
    partitioned: ClassVar[bool] = False

    def __init__(self, session: TenantSession) -> None:
        self.session = session

    @property
    def tenant_id(self) -> UUID:
        return self.session.tenant_ctx.tenant_id

    # ── Query construction ────────────────────────────────────────────────

    def _base_select(self) -> Select[tuple[ModelT]]:
        stmt: Select[tuple[ModelT]] = select(self.model).where(
            self.model.tenant_id == self.tenant_id
        )
        if hasattr(self.model, "deleted_at"):
            stmt = stmt.where(self.model.deleted_at.is_(None))
        return stmt

    def _partition_window(self, entity_id: UUID) -> tuple[datetime, datetime]:
        """A narrow ``created_at`` range around a UUIDv7's embedded timestamp.

        Partition pruning needs a predicate on ``created_at``. Rather than
        forcing every caller to pass one, we derive it from the id. The ±1
        second margin absorbs clock skew between the app instance that minted
        the id and the database that stamped ``created_at``.

        This is the concrete payoff of amendment A2's "no uuid4 fallback": a v4
        id would produce a window somewhere in the next few thousand years and
        prune to zero partitions, so the lookup would return None for a row that
        exists. :func:`~packages.core.ids.uuid7_timestamp` raises rather than
        letting that happen.

        **Known ceiling.** The generator keeps a monotonic counter, returning
        ``last_ms + 1`` when called twice inside one millisecond so that ordering
        holds. A process minting more than ~1000 ids per millisecond therefore
        drifts the embedded timestamp ahead of ``created_at`` by more than this
        window, and a point lookup would miss its own row. That is ~1M ids per
        second sustained from a single process — far outside anything this
        platform does — but it is a real bound rather than an imaginary one, and
        widening the margin is the fix if it is ever approached.
        """
        ts = uuid7_timestamp(entity_id)
        return ts - timedelta(seconds=1), ts + timedelta(seconds=1)

    # ── Reads ─────────────────────────────────────────────────────────────

    async def get(self, entity_id: UUID) -> ModelT | None:
        stmt = self._base_select().where(self.model.id == entity_id)
        if self.partitioned:
            lo, hi = self._partition_window(entity_id)
            stmt = stmt.where(self.model.created_at >= lo, self.model.created_at < hi)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_raise(self, entity_id: UUID) -> ModelT:
        entity = await self.get(entity_id)
        if entity is None:
            # 404, not 403: under RLS an entity in another tenant is
            # indistinguishable from one that does not exist, and confirming
            # which would make the id space enumerable across tenants.
            raise EntityNotFoundError(
                entity_type=self.model.__tablename__, entity_id=entity_id
            )
        return entity

    async def list(
        self,
        *filters: ColumnElement[bool],
        limit: int = 50,
        offset: int = 0,
        order_by: Any = None,
    ) -> Sequence[ModelT]:
        stmt = self._base_select().where(*filters)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        elif hasattr(self.model, "created_at"):
            stmt = stmt.order_by(self.model.created_at.desc())
        # Hard ceiling regardless of what the caller asked for: an unbounded
        # list against a partitioned table is how one request becomes an outage.
        stmt = stmt.limit(min(limit, 500)).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def count(self, *filters: ColumnElement[bool]) -> int:
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(self.model.tenant_id == self.tenant_id, *filters)
        )
        if hasattr(self.model, "deleted_at"):
            stmt = stmt.where(self.model.deleted_at.is_(None))
        return int((await self.session.execute(stmt)).scalar_one())

    async def exists(self, *filters: ColumnElement[bool]) -> bool:
        stmt = select(
            select(1)
            .select_from(self.model)
            .where(self.model.tenant_id == self.tenant_id, *filters)
            .exists()
        )
        return bool((await self.session.execute(stmt)).scalar_one())

    # ── Writes ────────────────────────────────────────────────────────────

    async def add(self, entity: ModelT) -> ModelT:
        # Stamp rather than trust: the caller cannot set another tenant's id
        # even by accident, and the before_flush guard is a second net.
        entity.tenant_id = self.tenant_id
        self.session.add(entity)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise self._translate(exc) from exc
        return entity

    async def add_all(self, entities: Sequence[ModelT]) -> Sequence[ModelT]:
        for e in entities:
            e.tenant_id = self.tenant_id
        self.session.add_all(list(entities))
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise self._translate(exc) from exc
        return entities

    async def update_versioned(
        self, entity_id: UUID, expected_version: int, **values: Any
    ) -> int:
        """Optimistic concurrency.

        Returns the new version. Raises :class:`ConcurrencyConflictError` if
        another writer moved first — surfaced to the caller as a 412, never
        silently overwritten. This is why ``version`` is bumped here rather than
        in a trigger: a trigger cannot report that the expectation was violated.
        """
        stmt = (
            update(self.model)
            .where(
                self.model.id == entity_id,
                self.model.tenant_id == self.tenant_id,
                self.model.version == expected_version,
            )
            .values(**values, version=self.model.version + 1)
            .returning(self.model.version)
        )
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            raise ConcurrencyConflictError(
                entity_type=self.model.__tablename__,
                entity_id=entity_id,
                expected_version=expected_version,
            )
        return int(row)

    async def soft_delete(self, entity_id: UUID) -> None:
        if not hasattr(self.model, "deleted_at"):
            raise TypeError(f"{self.model.__name__} does not support soft delete")
        stmt = (
            update(self.model)
            .where(
                self.model.id == entity_id,
                self.model.tenant_id == self.tenant_id,
                self.model.deleted_at.is_(None),
            )
            .values(deleted_at=func.now())
        )
        if (await self.session.execute(stmt)).rowcount == 0:
            raise EntityNotFoundError(
                entity_type=self.model.__tablename__, entity_id=entity_id
            )

    async def hard_delete(self, entity_id: UUID) -> None:
        stmt = delete(self.model).where(
            self.model.id == entity_id, self.model.tenant_id == self.tenant_id
        )
        if (await self.session.execute(stmt)).rowcount == 0:
            raise EntityNotFoundError(
                entity_type=self.model.__tablename__, entity_id=entity_id
            )

    # ── Error translation ─────────────────────────────────────────────────

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        """Turn Postgres SQLSTATEs into domain errors.

        23P01 (``exclusion_violation``) is the one that matters: it is how a
        double-booking attempt surfaces, and the booking service must be able to
        catch it specifically in order to retry against another resource. 23503
        also covers the composite-FK rejection of a cross-tenant reference and
        the constraint trigger on ``conversations.customer_id``, which raises
        ``foreign_key_violation`` deliberately so both arrive here identically.
        """
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        constraint = getattr(
            getattr(orig, "__cause__", None), "constraint_name", None
        )
        if sqlstate == "23P01":
            return ResourceConflictError(
                "The requested time is no longer available",
                constraint=constraint,
                kind="overlap",
            )
        if sqlstate == "23505":
            return ResourceConflictError(
                "A record with these values already exists",
                constraint=constraint,
                kind="duplicate",
            )
        if sqlstate == "23503":
            return ResourceConflictError(
                "Referenced record does not exist or belongs to another tenant",
                constraint=constraint,
                kind="foreign_key",
            )
        if sqlstate == "23514":
            return ResourceConflictError(
                "Value violates a business rule",
                constraint=constraint,
                kind="check",
            )
        return exc

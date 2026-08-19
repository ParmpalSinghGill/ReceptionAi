"""Tenancy repositories — ``tenants``, ``locations``, ``tenant_settings``."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from packages.core.errors import EntityNotFoundError
from packages.persistence.models.tenancy import Location, Tenant, TenantSetting
from packages.persistence.repositories.base import TenantRepository
from packages.persistence.session import TenantSession
from sqlalchemy import select

__all__ = ["LocationRepository", "TenantRepo", "TenantSettingRepository"]


class TenantRepo:
    """Reads the *current* tenant's own row.

    Not a :class:`TenantRepository`: ``tenants`` has no ``tenant_id`` column —
    its ``id`` *is* the scope — so the RLS policy is ``id = app_current_tenant()``
    (01 §11.5) and there is no other row this session can see. That is why there
    is no ``get(id)`` here: passing an id would imply a choice the session does
    not have, and a caller who wrote ``get(other_tenant_id)`` would get ``None``
    and quietly conclude the tenant does not exist.

    Tenant *creation* is deliberately absent too. It happens through a
    ``SECURITY DEFINER`` provisioning function granted only to a dedicated
    signup role — a tenant cannot create a tenant.
    """

    def __init__(self, session: TenantSession) -> None:
        self.session = session

    @property
    def tenant_id(self) -> UUID:
        return self.session.tenant_ctx.tenant_id

    async def current(self) -> Tenant | None:
        stmt = select(Tenant).where(
            Tenant.id == self.tenant_id, Tenant.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def current_or_raise(self) -> Tenant:
        tenant = await self.current()
        if tenant is None:
            raise EntityNotFoundError(entity_type="tenants", entity_id=self.tenant_id)
        return tenant

    async def shard_key(self) -> str | None:
        """The tenant's assigned shard, straight from the row.

        Read at authentication to populate ``Principal.shard_key`` and therefore
        ``TenantContext.shard_key``. A non-null value here that the router
        cannot resolve refuses the session (contract A8) — it never falls back.
        """
        stmt = select(Tenant.shard_key).where(Tenant.id == self.tenant_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()


class LocationRepository(TenantRepository[Location]):
    model = Location

    async def by_code(self, code: str) -> Location | None:
        stmt = self._base_select().where(Location.code == code)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def active(self, *, limit: int = 50, offset: int = 0) -> Sequence[Location]:
        return await self.list(
            Location.is_active.is_(True),
            limit=limit,
            offset=offset,
            order_by=Location.name.asc(),
        )


class TenantSettingRepository(TenantRepository[TenantSetting]):
    model = TenantSetting

    async def namespace(
        self, namespace: str, *, location_id: UUID | None = None
    ) -> dict[str, Any]:
        """Resolve a whole namespace, location overriding tenant-wide.

        01 §2.3's resolution order is ``location row → tenant row → vertical
        manifest default → platform default``; the first two live here and the
        latter two are applied by the caller that holds the manifest. Kept out
        of SQL because the manifest layer is a JSONB document the database
        should not have to interpret.

        One query, not two: fetching tenant-wide and location rows separately
        would double the round-trips on a path that runs on nearly every
        request. Ordering ``location_id NULLS FIRST`` means the location row,
        when present, is written last and wins.
        """
        stmt = self._base_select().where(TenantSetting.namespace == namespace)
        if location_id is None:
            stmt = stmt.where(TenantSetting.location_id.is_(None))
        else:
            stmt = stmt.where(
                TenantSetting.location_id.is_(None)
                | (TenantSetting.location_id == location_id)
            )
        stmt = stmt.order_by(TenantSetting.location_id.asc().nullsfirst())

        resolved: dict[str, Any] = {}
        for row in (await self.session.execute(stmt)).scalars():
            resolved[row.key] = row.value
        return resolved

    async def get_value(
        self, namespace: str, key: str, *, location_id: UUID | None = None
    ) -> Any | None:
        values = await self.namespace(namespace, location_id=location_id)
        return values.get(key)

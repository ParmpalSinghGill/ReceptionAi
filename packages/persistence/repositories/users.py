"""User and membership repositories — 01 §3.2, §3.4.

``users`` is global; ``user_tenant_roles`` is tenant-owned. That asymmetry
shapes this module: :class:`UserRepository` is *not* a
:class:`~packages.persistence.repositories.base.TenantRepository`, because there
is no ``users.tenant_id`` to filter on. Visibility comes from the RLS policy
``rls_users__member_visible``, whose predicate is an existence check against the
membership table (01 §11.5) — so a user outside the current tenant is invisible
here for the same structural reason a foreign booking is.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from packages.core.errors import EntityNotFoundError
from packages.persistence.models.identity import (
    Permission,
    Role,
    RolePermission,
    User,
    UserTenantRole,
)
from packages.persistence.repositories.base import TenantRepository
from packages.persistence.session import TenantSession
from sqlalchemy import select

__all__ = ["MembershipRepository", "UserRepository"]


class UserRepository:
    """Members of the current tenant.

    Every method here is readable only because ``rls_users__member_visible``
    already narrowed the table to the current tenant's members. The explicit
    join to ``user_tenant_roles`` in :meth:`members` is therefore belt-and-braces
    in the same spirit as ``TenantRepository``'s redundant ``tenant_id``
    predicate: it makes the intent legible in the query plan and keeps the code
    correct if it is ever run in a test fixture without RLS.
    """

    def __init__(self, session: TenantSession) -> None:
        self.session = session

    @property
    def tenant_id(self) -> UUID:
        return self.session.tenant_ctx.tenant_id

    async def get(self, user_id: UUID) -> User | None:
        stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_raise(self, user_id: UUID) -> User:
        user = await self.get(user_id)
        if user is None:
            raise EntityNotFoundError(entity_type="users", entity_id=user_id)
        return user

    async def by_email(self, email: str) -> User | None:
        """Look up by the normalized form.

        Normalizing in Python to match the generated column rather than calling
        ``lower(btrim(...))`` in SQL: the expression must be identical to the
        one that built ``email_normalized``, or the unique index is not used and
        the lookup becomes a seq scan on a table that grows without bound.

        This is *not* the login path. Login runs as ``receptionai_auth`` through
        ``auth_resolve_user_by_email``, a ``SECURITY DEFINER`` function, because
        it must read before any tenant context exists. This method only ever
        finds users already visible in the current tenant.
        """
        stmt = select(User).where(
            User.email_normalized == email.strip().lower(), User.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def members(self, *, limit: int = 50, offset: int = 0) -> Sequence[User]:
        stmt = (
            select(User)
            .join(UserTenantRole, UserTenantRole.user_id == User.id)
            .where(
                UserTenantRole.tenant_id == self.tenant_id,
                UserTenantRole.revoked_at.is_(None),
                User.deleted_at.is_(None),
            )
            .order_by(User.email_normalized.asc())
            .limit(min(limit, 500))
            .offset(offset)
            .distinct()
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def permissions_of(self, user_id: UUID) -> frozenset[str]:
        """Every permission code this user holds in the current tenant.

        Resolved as the union across all their live, unexpired memberships.
        Computed once per request and carried on the
        :class:`~packages.core.auth.principal.Principal`, never re-derived per
        authorization check — an RBAC check that hits the database is an RBAC
        check that gets skipped under load.
        """
        now = datetime.now(tz=UTC)
        stmt = (
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .join(UserTenantRole, UserTenantRole.role_id == Role.id)
            .where(
                UserTenantRole.tenant_id == self.tenant_id,
                UserTenantRole.user_id == user_id,
                UserTenantRole.revoked_at.is_(None),
                (UserTenantRole.expires_at.is_(None))
                | (UserTenantRole.expires_at > now),
            )
        )
        return frozenset((await self.session.execute(stmt)).scalars().all())


class MembershipRepository(TenantRepository[UserTenantRole]):
    """``user_tenant_roles`` — the table that makes a user "in" a tenant."""

    model = UserTenantRole

    async def for_user(self, user_id: UUID) -> Sequence[UserTenantRole]:
        """Live memberships of one user in the current tenant.

        Hits ``ix_user_tenant_roles__tenant_user``, the tenant-leading index.
        The mirror-image index ``ix_user_tenant_roles__user_tenant`` exists for
        login — where there is no tenant context yet — and is the schema's one
        declared exception to contract §2.6. It is not used here.
        """
        now = datetime.now(tz=UTC)
        stmt = (
            self._base_select()
            .where(
                UserTenantRole.user_id == user_id,
                UserTenantRole.revoked_at.is_(None),
                (UserTenantRole.expires_at.is_(None))
                | (UserTenantRole.expires_at > now),
            )
            .order_by(UserTenantRole.created_at.asc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def is_member(self, user_id: UUID) -> bool:
        now = datetime.now(tz=UTC)
        return await self.exists(
            UserTenantRole.user_id == user_id,
            UserTenantRole.revoked_at.is_(None),
            (UserTenantRole.expires_at.is_(None)) | (UserTenantRole.expires_at > now),
        )

    async def grant(
        self,
        *,
        user_id: UUID,
        role_id: UUID,
        location_id: UUID | None = None,
        invited_by_user_id: UUID | None = None,
        expires_at: datetime | None = None,
        is_owner: bool = False,
    ) -> UserTenantRole:
        membership = UserTenantRole(
            tenant_id=self.tenant_id,
            user_id=user_id,
            role_id=role_id,
            location_id=location_id,
            invited_by_user_id=invited_by_user_id,
            invited_at=datetime.now(tz=UTC),
            expires_at=expires_at,
            is_owner=is_owner,
        )
        return await self.add(membership)

    async def revoke(self, membership_id: UUID) -> None:
        """Revoke rather than delete.

        The partial uniques are all predicated on ``revoked_at IS NULL``, so a
        revoked row does not block re-granting the same role — and the history
        of who had access when survives, which is the whole point of an access
        trail.
        """
        membership = await self.get_or_raise(membership_id)
        membership.revoked_at = datetime.now(tz=UTC)
        await self.session.flush()

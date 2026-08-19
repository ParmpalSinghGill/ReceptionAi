"""The session factory — where ``SET LOCAL app.tenant_id`` happens.

**This is the single most security-critical function in the platform.** It is
the only thing standing between a shared database and a cross-tenant breach.

01 §15.3, amended by A10 (which makes ``TenantSession`` canonical platform-wide
and withdraws 05's ``get_session()`` / bare ``AsyncSession``).
"""

from __future__ import annotations

import contextvars
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from packages.core.errors import CrossTenantAccessError
from packages.core.tenancy import (
    TenantContext,
    get_tenant_context,
    reset_tenant_context,
    set_tenant_context,
)
from packages.persistence.engine import ConnectionRouter
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.orm import UOWTransaction

__all__ = ["SessionFactory", "TenantSession"]


class _TenantSyncSession(SyncSession):
    """The synchronous session ``TenantSession`` proxies.

    ``AsyncSession`` is a facade over a real, synchronous ``Session``; the ORM's
    unit-of-work events — ``before_flush`` among them — are emitted by that
    inner object and cannot be registered on the async facade. 01 §15.3 shows
    the listener attached directly to ``TenantSession``, which raises
    ``InvalidRequestError: No such event 'before_flush'`` at import. The fix is
    this subclass, wired in via ``sync_session_class`` below, carrying a
    back-reference to the context so the guard can still see it.
    """

    tenant_ctx: TenantContext


@event.listens_for(_TenantSyncSession, "before_flush")
def _assert_tenant_on_flush(
    session: _TenantSyncSession,
    flush_context: UOWTransaction,
    instances: Sequence[object] | None,
) -> None:
    """Belt-and-braces: RLS ``WITH CHECK`` would reject a wrong ``tenant_id``
    anyway, but it would do so as an opaque database error deep in a flush.
    Catching it here gives a precise, testable failure with the offending object
    named.
    """
    expected = getattr(session, "tenant_ctx", None)
    if expected is None:
        # An ops session (no tenant bound) or a session built outside the
        # factory. RLS remains the control; there is simply nothing to compare.
        return
    expected_tenant = expected.tenant_id
    for obj in list(session.new) + list(session.dirty):
        tid = getattr(obj, "tenant_id", None)
        if tid is None:
            continue  # non-tenant-owned entity
        if tid != expected_tenant:
            raise CrossTenantAccessError(
                f"{type(obj).__name__} carries tenant_id={tid} but the session "
                f"is bound to {expected_tenant}"
            )


class TenantSession(AsyncSession):
    """An ``AsyncSession`` that knows which tenant it is bound to.

    The binding is set once, at open, and asserted on every flush. It exists so
    that repositories can defensively re-check ``tenant_id`` in Python —
    contract §8's "re-checked in the service layer" — without threading the
    context through every call signature.

    **This is the canonical session type platform-wide (contract A10).**
    05-PLATFORM-SECURITY-OPS.md has withdrawn its ``get_session()`` /
    ``AsyncSession`` pair in favour of ``tenant_context()`` / ``db_session()`` /
    ``TenantSession``, so the properties below are a published interface, not
    internal convenience. Removing or renaming one is a breaking change for
    05's audit writer. The published shape is pinned by
    :class:`packages.core.tenancy.TenantSession`, a Protocol this class
    satisfies structurally — so callers above the persistence layer type against
    the protocol and never import SQLAlchemy.
    """

    tenant_ctx: TenantContext

    #: The ORM's unit-of-work events fire on the inner synchronous session, not
    #: on this facade, so the flush guard is registered against this class.
    sync_session_class = _TenantSyncSession

    def __init__(self, *args: Any, tenant_ctx: TenantContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.tenant_ctx = tenant_ctx
        # The guard runs inside the sync session and has no path back to this
        # object, so the context is copied across at construction.
        self.sync_session.tenant_ctx = tenant_ctx

    # ── Published accessors (contract A10) ────────────────────────────────
    # Flattened off tenant_ctx so callers in `05` need not import
    # TenantContext to write an audit row.

    @property
    def tenant_id(self) -> UUID:
        return self.tenant_ctx.tenant_id

    @property
    def user_id(self) -> UUID | None:
        """The SUBJECT — whose authority the action is taken under."""
        return self.tenant_ctx.user_id

    @property
    def impersonator_user_id(self) -> UUID | None:
        """The ACTOR — the platform staff member driving an impersonated
        session, or None for ordinary access.

        05 stores the subject and needs the actor: an audit row must record both
        "acted as Alice" and "was really Bob from support", or impersonation is
        indistinguishable from the real user in the trail. 01 §11.4 makes
        impersonation use the ordinary tenant-scoped path precisely so the same
        RLS policy protects it — which means the actor is NOT recoverable from
        the database session and must be carried here.
        """
        return self.tenant_ctx.impersonator_user_id

    @property
    def shard_key(self) -> str | None:
        """Which shard this session is bound to. None = the shared cluster.

        Exposed for 05's audit and telemetry: a record written on dedicated
        infrastructure should say so, and A8 makes an unexpected value here a
        signal worth alerting on rather than a benign detail.
        """
        return self.tenant_ctx.shard_key

    @property
    def is_impersonated(self) -> bool:
        return self.tenant_ctx.impersonator_user_id is not None


class SessionFactory:
    """Creates tenant-scoped sessions through the :class:`ConnectionRouter`."""

    def __init__(self, router: ConnectionRouter) -> None:
        self._router = router
        self._makers: dict[int, async_sessionmaker[TenantSession]] = {}

    async def _maker(self, ctx: TenantContext) -> async_sessionmaker[TenantSession]:
        engine = await self._router.engine_for(ctx)
        key = id(engine)
        maker = self._makers.get(key)
        if maker is None:
            maker = async_sessionmaker(
                bind=engine,
                class_=TenantSession,
                expire_on_commit=False,  # objects stay usable after commit
                autoflush=False,  # explicit flushes; no surprise writes
            )
            self._makers[key] = maker
        return maker

    @asynccontextmanager
    async def unit_of_work(
        self,
        ctx: TenantContext | None = None,
        *,
        readonly: bool = False,
    ) -> AsyncIterator[TenantSession]:
        """Open a transaction with RLS context applied.

        Everything about this is deliberate:

        1. ``SET LOCAL`` — not ``SET``. LOCAL is transaction-scoped, so the GUC
           is reset automatically at COMMIT or ROLLBACK. A plain ``SET`` would
           persist on the pooled connection and leak tenant A's context into
           tenant B's next request. This is THE multi-tenancy bug, and
           ``SET LOCAL`` is the fix.

        2. Bound parameters — never f-strings. Contract §2.4: "never
           interpolated into SQL by hand". ``SET LOCAL`` does not accept a bind
           parameter directly (``SET`` takes a literal, not a placeholder — the
           statement is parsed before parameters are bound), so we go through
           ``set_config(name, value, is_local => true)``, which is an ordinary
           function call and therefore does. The tenant id travels as a
           parameter, immune to injection.

        3. The context is set BEFORE any user query runs, inside the same
           transaction, so there is no window in which a query executes without
           RLS scoping.
        """
        ctx = ctx or get_tenant_context()
        maker = await self._maker(ctx)

        token: contextvars.Token[TenantContext | None] = set_tenant_context(ctx)
        session = maker(tenant_ctx=ctx)
        try:
            async with session.begin():
                # ── The bind parameter. NEVER an f-string. Contract §2.4. ──
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :tid, true)"),
                    {"tid": str(ctx.tenant_id)},
                )
                if ctx.user_id is not None:
                    # Drives the self-row policies on `users`, `sessions`, and
                    # `mfa_factors` (01 §11.5). Same discipline: bound, local.
                    await session.execute(
                        text("SELECT set_config('app.user_id', :uid, true)"),
                        {"uid": str(ctx.user_id)},
                    )
                if readonly:
                    # Cheap insurance for report queries: any accidental write
                    # fails at the database rather than being caught in review.
                    await session.execute(text("SET TRANSACTION READ ONLY"))

                yield session
                # `session.begin()` commits on clean exit, rolls back on raise.
        finally:
            await session.close()
            reset_tenant_context(token)

    @asynccontextmanager
    async def ops_unit_of_work(self) -> AsyncIterator[TenantSession]:
        """Cross-tenant maintenance work.

        Deliberately does NOT set ``app.tenant_id``, so every RLS-protected
        table returns zero rows. The only useful thing this session can do is
        call the explicitly-granted ``SECURITY DEFINER`` ``ops_*`` functions
        (01 §11.4). If someone writes a bare SELECT here expecting rows, they
        get none — which is exactly the feedback we want.
        """
        engine = await self._router.default_engine()
        maker = async_sessionmaker(
            bind=engine, class_=TenantSession, expire_on_commit=False
        )
        # The all-zero UUID is a sentinel, not a tenant. It never matches a real
        # `tenants.id` (which is always a UUIDv7), so even if a query slipped
        # through with this bound, it would still see nothing.
        sentinel = TenantContext(tenant_id=UUID(int=0))
        session = maker(tenant_ctx=sentinel)
        try:
            async with session.begin():
                yield session
        finally:
            await session.close()

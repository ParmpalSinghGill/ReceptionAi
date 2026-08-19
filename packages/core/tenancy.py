"""Tenant identity — the published interface of amendment A10.

A10 makes ``tenant_context()`` / ``db_session()`` / ``TenantSession`` from
01 §15 canonical platform-wide and withdraws 05's ``get_session()`` /
bare ``AsyncSession``. That makes the accessors below a **published interface**,
not internal convenience: 05's audit writer reads them, so removing or renaming
one is a breaking change across documents.

Two types live here:

* :class:`TenantContext` — the immutable identity a unit of work executes under.
  Concrete, because it is a value object with no I/O: it is constructed at the
  authentication boundary from the :class:`~packages.core.auth.principal.Principal`
  and carried down. Contract §9 lets core own it; persistence imports it rather
  than redefining it.

* :class:`TenantSession` — a :class:`~typing.Protocol`, because the concrete
  session is a SQLAlchemy ``AsyncSession`` subclass and core imports no I/O
  library. The protocol pins the five accessors A10 published
  (``tenant_id``, ``user_id``, ``impersonator_user_id``, ``shard_key``,
  ``is_impersonated``), so a caller in ``packages/*`` types against this and
  never against the SQLAlchemy class.

**Why ``contextvars`` and not a thread-local.** asyncio tasks inherit context
vars at creation and each request handler runs in its own task, so a value set
at the top of a request is visible to everything it awaits and to nothing else.
A thread-local is shared by every coroutine on the event loop — which in a
worker process serving every tenant is a cross-tenant leak by construction.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from packages.core.errors import ConfigurationError

__all__ = [
    "MissingTenantContextError",
    "TenantContext",
    "TenantSession",
    "get_tenant_context",
    "reset_tenant_context",
    "set_tenant_context",
    "try_get_tenant_context",
]


class MissingTenantContextError(ConfigurationError):
    """A DB session was opened without a bound tenant.

    Fail-closed: there is no "anonymous tenant" state to fall back to. 05 §A3.4
    spells out why — a null tenant that reaches a repository yields either an
    empty result or a confusing 500, and the natural "fix" someone reaches for
    (``COALESCE(current_setting('app.tenant_id', true), ...)``) silently makes
    the system cross-tenant. The state is never produced, so the temptation
    never arises.
    """

    code = "internal_error"
    http_status = 500


@dataclass(frozen=True, slots=True)
class TenantContext:
    """The identity under which a unit of work executes.

    Immutable and frozen: a request's tenant cannot be reassigned mid-flight.
    Constructed once, at the authentication boundary, from the auth token —
    never from a path or query parameter (contract §10).
    """

    tenant_id: UUID
    user_id: UUID | None = None
    shard_key: str | None = None
    # Set only by the impersonation flow; written into every audit record.
    # 01 names the ACTOR here — the platform staff member driving the session.
    impersonator_user_id: UUID | None = None
    # Present for API-key auth; narrows what the session may do.
    api_key_id: UUID | None = None
    location_id: UUID | None = None

    @property
    def is_impersonated(self) -> bool:
        return self.impersonator_user_id is not None


@runtime_checkable
class TenantSession(Protocol):
    """The published accessors of a tenant-bound database session (A10).

    ``packages/persistence`` supplies the concrete implementation (an
    ``AsyncSession`` subclass). Everything above the persistence layer types
    against this protocol, so the domain never imports SQLAlchemy and the five
    accessors below are the whole cross-document contract.

    The accessors are flattened off :class:`TenantContext` deliberately: 05's
    audit writer needs the actor and the shard, and forcing it to reach through
    ``session.tenant_ctx`` would make it import a type it has no other use for.
    """

    @property
    def tenant_id(self) -> UUID:
        """The tenant this session is scoped to. Matches ``app.tenant_id``."""
        ...

    @property
    def user_id(self) -> UUID | None:
        """The SUBJECT — whose authority the action is taken under."""
        ...

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
        ...

    @property
    def shard_key(self) -> str | None:
        """Which shard this session is bound to. None = the shared cluster.

        Exposed for 05's audit and telemetry: a record written on dedicated
        infrastructure should say so, and A8 makes an unexpected value here a
        signal worth alerting on rather than a benign detail.
        """
        ...

    @property
    def is_impersonated(self) -> bool:
        """True when a platform staff member is acting as a tenant user."""
        ...


_tenant_ctx: contextvars.ContextVar[TenantContext | None] = contextvars.ContextVar(
    "receptionai_tenant_ctx", default=None
)


def set_tenant_context(ctx: TenantContext) -> contextvars.Token[TenantContext | None]:
    """Bind ``ctx`` to the current task. Returns the token to reset with."""
    return _tenant_ctx.set(ctx)


def get_tenant_context() -> TenantContext:
    """The bound context, or raise.

    Raises:
        MissingTenantContextError: when nothing is bound. Never returns a
            default — see the class docstring.
    """
    ctx = _tenant_ctx.get()
    if ctx is None:
        raise MissingTenantContextError(
            "No tenant context bound. Every DB access must run inside "
            "`async with session_factory.unit_of_work(ctx)`."
        )
    return ctx


def try_get_tenant_context() -> TenantContext | None:
    """The bound context, or None. For logging and telemetry only.

    Anything that opens a session uses :func:`get_tenant_context` instead: the
    difference between "no rows" and "an error" is the difference between a
    silent isolation failure and a caught bug.
    """
    return _tenant_ctx.get()


def reset_tenant_context(token: contextvars.Token[TenantContext | None]) -> None:
    """Restore the previous context-var value.

    Always called from a ``finally`` so a raised exception cannot leave a tenant
    bound to the calling task — which, in a worker process serving every tenant,
    would be a cross-tenant leak.
    """
    _tenant_ctx.reset(token)

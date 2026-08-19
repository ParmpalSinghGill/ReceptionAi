"""The dependency chain — 05 §A2.4, §A3.6, amendment A10.

``auth → principal → tenant context → RLS session``, and each arrow is a
separate object so each can be tested and reasoned about alone:

* :func:`get_principal` reads what the middleware resolved. It does not
  re-authenticate — the middleware already did, and a second parse would be a
  second place for the two to disagree.
* :func:`requires` is layer L2 (05 §A0): *"is this principal allowed to do this
  verb on this resource type?"* It answers before the request touches the
  database.
* :func:`db_session` is layer L3's entry point: it derives a
  :class:`~packages.core.tenancy.TenantContext` from the principal and opens
  the RLS session through ``packages/persistence/session.py``, which is where
  ``SET LOCAL app.tenant_id`` happens. **This module does not open a
  connection itself** — contract §2 requires every session to come through the
  router, and there is exactly one path to one.

Amendment A10 makes ``TenantSession`` canonical, so :data:`DbSession` is the
annotation every handler uses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import structlog
from apps.api.problems import ProblemException
from apps.api.security.permissions import STEP_UP_REQUIRED, Perm
from apps.api.state import AppState, get_app_state
from fastapi import Depends, Query, Request
from packages.core.auth import Principal
from packages.core.tenancy import TenantContext
from packages.persistence.session import TenantSession

__all__ = [
    "WIDGET_PERMISSIONS",
    "AppStateDep",
    "ConversationAccess",
    "ConversationAccessDep",
    "CurrentPrincipal",
    "DbSession",
    "ReadOnlyDbSession",
    "db_session",
    "get_principal",
    "requires",
    "resolve_conversation_access",
    "tenant_context",
    "widget_principal",
]

log = structlog.get_logger(__name__)

#: 05 §A2.4: a sensitive action needs not just MFA but *recent* MFA. Fifteen
#: minutes is long enough not to re-prompt within one admin session and short
#: enough that a walked-away-from laptop is not a standing authorization.
_MAX_AUTH_AGE_SECONDS = 900


# ─────────────────────────────────────────────────────────────────────────────
# Principal
# ─────────────────────────────────────────────────────────────────────────────


def get_principal(request: Request) -> Principal:
    """The authenticated caller, from the middleware.

    Raises:
        ProblemException: 401 when nothing is attached. That means the
            middleware did not run for this path — a wiring bug — and it fails
            closed. Returning ``None`` and letting the handler decide is how a
            route ends up serving unauthenticated traffic because someone
            forgot a check.
    """
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise ProblemException(status=401, code="missing_credentials")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def requires(*permissions: Perm, mode: str = "all"):
    """Route dependency enforcing RBAC — layer L2.

    Usage::

        @router.get("/api/v1/conversations",
                    dependencies=[Depends(requires(Perm.CONVERSATIONS_READ))])

    or, when the handler needs the principal::

        async def handler(p: Principal = Depends(requires(Perm.CONVERSATIONS_READ))):

    The returned closure carries ``__is_authz_check__``, which is what
    :func:`~apps.api.security.route_audit.assert_all_routes_guarded` looks for
    at startup. A route without one crashes the app.
    """
    if mode not in ("all", "any"):
        raise ValueError("mode must be 'all' or 'any'")

    needed = frozenset(permissions)
    needs_step_up = bool(needed & STEP_UP_REQUIRED)

    async def check(
        request: Request,
        access: Annotated[
            ConversationAccess, Depends(resolve_conversation_access)
        ],
    ) -> Principal:
        # `access` is declared as a sub-dependency rather than the principal
        # being read from `request.state`, and that ordering is load-bearing.
        #
        # FastAPI resolves a dependency's own parameters BEFORE calling it, so
        # naming `ConversationAccessDep` here guarantees the credential — token
        # or ticket — is resolved before this check runs. Reading
        # `request.state.principal` instead would depend on the order FastAPI
        # happens to resolve `dependencies=[...]` against signature parameters,
        # and on those routes it resolves `requires` first, which 401s a widget
        # request that is perfectly entitled.
        #
        # On routes with no `{conversation_id}` the access dependency yields a
        # token-only principal, so this is the single resolution point for
        # every guarded route.
        principal = access.principal
        held = principal.permissions
        wanted = {p.value for p in needed}
        satisfied = held >= wanted if mode == "all" else bool(held & wanted)

        if not satisfied:
            missing = sorted(wanted - held)
            log.info(
                "authz.denied",
                path=request.url.path,
                missing=missing,
                roles=list(principal.roles),
            )
            # We say which permission is missing. Hiding it does not improve
            # security — the caller is already authenticated to this tenant —
            # and it makes integration debugging miserable.
            raise ProblemException(
                status=403,
                code="forbidden",
                detail=f"Requires {', '.join(missing)}.",
                missing_permissions=missing,
            )

        if needs_step_up:
            if principal.kind == "api_key":
                # 05 §A1.7: a machine credential can never present a second
                # factor, so it can never satisfy step-up. Saying so is more
                # useful than a generic 403 the integrator will retry.
                raise ProblemException(
                    status=403,
                    code="step_up_required",
                    detail=(
                        "API keys cannot perform this action; use an "
                        "interactive session."
                    ),
                    required_aal=2,
                )
            if principal.aal < 2:
                raise ProblemException(
                    status=403,
                    code="step_up_required",
                    detail="This action requires multi-factor authentication.",
                    required_aal=2,
                )
            if principal.auth_age_seconds > _MAX_AUTH_AGE_SECONDS:
                raise ProblemException(
                    status=403,
                    code="reauthentication_required",
                    detail="Re-enter your credentials to continue.",
                    max_age=_MAX_AUTH_AGE_SECONDS,
                )

        # Read by the audit writer, so a record says what was checked rather
        # than what the route happens to be named.
        request.state.checked_permissions = wanted
        return principal

    check.__is_authz_check__ = True  # type: ignore[attr-defined]
    return check


# ─────────────────────────────────────────────────────────────────────────────
# Tenant context and the RLS session
# ─────────────────────────────────────────────────────────────────────────────


def tenant_context(principal: CurrentPrincipal) -> TenantContext:
    """Derive the DB-facing identity from the authenticated caller.

    The five fields 05 §A3.6 names as the contract between the auth layer and
    01's persistence layer. ``user_id`` is the **subject** (whose authority the
    action is taken under) and ``impersonator_user_id`` is the **actor** — the
    two differ only under impersonation, and both must be recorded or an
    impersonated action is indistinguishable from the real user's in the audit
    trail.
    """
    return TenantContext(
        tenant_id=principal.tenant_id,
        user_id=principal.subject_user_id,
        shard_key=principal.shard_key,
        impersonator_user_id=principal.impersonator_user_id,
        api_key_id=principal.api_key_id,
    )


async def db_session(
    request: Request,
    ctx: Annotated[TenantContext, Depends(tenant_context)],
) -> AsyncIterator[TenantSession]:
    """An RLS-scoped session for the duration of the request.

    One transaction per request, committed on a clean return and rolled back on
    an exception — which is ``SessionFactory.unit_of_work``'s behaviour, not
    something re-implemented here. ``SET LOCAL`` is transaction-scoped, so the
    GUC is reset at COMMIT or ROLLBACK and a pooled connection can never carry
    one tenant's context into the next request. That is THE multi-tenancy bug,
    and it is closed in ``packages/persistence/session.py``.
    """
    state = get_app_state(request)
    async with state.session_factory.unit_of_work(ctx) as session:
        yield session


async def readonly_db_session(
    request: Request,
    ctx: Annotated[TenantContext, Depends(tenant_context)],
) -> AsyncIterator[TenantSession]:
    """A read-only session. ``SET TRANSACTION READ ONLY`` at the database.

    Cheap insurance on GET routes: an accidental write fails at Postgres rather
    than being caught in review, and it lets the planner and any future replica
    routing treat the transaction correctly.
    """
    state = get_app_state(request)
    async with state.session_factory.unit_of_work(ctx, readonly=True) as session:
        yield session


DbSession = Annotated[TenantSession, Depends(db_session)]
ReadOnlyDbSession = Annotated[TenantSession, Depends(readonly_db_session)]
AppStateDep = Annotated[AppState, Depends(get_app_state)]


# ─────────────────────────────────────────────────────────────────────────────
# The widget credential — PHASE1-API-CONTRACT
# ─────────────────────────────────────────────────────────────────────────────
#
# The contract puts BOTH callers on the same two endpoints:
#
#     POST /api/v1/conversations/{id}/messages
#     GET  /api/v1/conversations/{id}/stream?ticket=<ticket>
#
# with *"`ticket` for the demo widget; admin clients authenticate by cookie and
# omit it."* So a single handler must accept either credential — which is
# exactly the shape an authorization bug hides in, and the reason the resolution
# below is one function with one exit rather than a branch inside each handler.
#
# The safety property is that a ticket never produces a general principal. It
# produces a principal that can touch ONE conversation and nothing else, and
# `requires()` still runs on top of it — so the route's permission check is the
# same code for both callers.


#: What a widget principal may do. Deliberately not `CONVERSATIONS_READ`: that
#: permission lets a caller *list and open any conversation in the tenant*,
#: which is precisely what an anonymous ticket-holder must not have. The
#: narrowing to one conversation is enforced separately, in
#: `resolve_conversation_access` — permissions answer "what verb", the ticket
#: claim answers "which row".
WIDGET_PERMISSIONS: frozenset[str] = frozenset(
    {Perm.CONVERSATIONS_READ.value, Perm.CONVERSATIONS_WRITE.value}
)


@dataclass(frozen=True, slots=True)
class ConversationAccess:
    """An authorized caller plus the conversation they may act on."""

    principal: Principal
    conversation_id: UUID
    #: True when the credential was a stream ticket rather than a token. Drives
    #: the SSE audience decision (06 §3.2): usage is staff-only.
    is_widget: bool

    @property
    def context(self) -> TenantContext:
        return tenant_context(self.principal)


def widget_principal(*, tenant_id: UUID, conversation_id: UUID) -> Principal:
    """A principal derived from a redeemed stream ticket.

    ``kind="service"`` rather than ``"user"``: there is no user behind it, and
    a ``user`` principal with ``user_id=None`` would fail ``Principal``'s own
    ``__post_init__`` invariant. ``service`` is the honest kind for "the
    platform acting on behalf of an anonymous visitor".

    ``aal=1`` and no step-up-eligible permission means it can never satisfy a
    sensitive route, so even if one were reachable it would be refused.

    The tenant comes from the **ticket's Redis payload**, which the server
    wrote when it minted the ticket — never from the request. A caller cannot
    influence which tenant this resolves to (contract §10).
    """
    return Principal(
        kind="service",
        tenant_id=tenant_id,
        tenant_plan="__widget__",
        permissions=WIDGET_PERMISSIONS,
        aal=1,
        # Not a general reader: the widget sees the conversation it holds a
        # ticket for, and `scope_all_conversations=False` keeps the RLS row
        # predicate narrow even if a query somehow escaped the check below.
        scope_all_conversations=False,
    )


#: The cookie the exchanged credential travels in. ``HttpOnly`` so JS cannot
#: read it, ``SameSite=Lax`` so it rides same-site navigations but not
#: cross-site form posts. Accepted from a header too — see below.
SESSION_COOKIE = "rai_demo_session"

#: Header alternative to the cookie. A cookie is the better default (JS cannot
#: exfiltrate an ``HttpOnly`` one), but the widget is embedded cross-origin on
#: a customer's website, where a ``SameSite=Lax`` cookie is NOT sent — Lax
#: covers same-site requests only. So the credential is also accepted here, and
#: the widget uses whichever its embedding allows. Both are checked; neither is
#: a URL, which is the property that matters.
SESSION_HEADER = "X-Conversation-Session"


async def resolve_conversation_access(
    request: Request,
    state: AppStateDep,
    conversation_id: UUID | None = None,
    ticket: Annotated[str | None, Query(max_length=128)] = None,
    session: Annotated[str | None, Query(max_length=256)] = None,
) -> ConversationAccess:
    """Authorize a conversation request by any of three credentials.

    In precedence order, and each ordering decision matters:

    1. **A Bearer token.** Wins over everything. A request carrying both a
       token and a widget credential is an admin client that happened to keep a
       stale parameter, and honouring the weaker one would silently downgrade
       an admin session.
    2. **A conversation session** (cookie or ``X-Conversation-Session``). The
       credential a ticket was exchanged for. Reusable within its conversation
       — this is what lets the widget send a second message.
    3. **A stream ticket.** Single-use, and by design only useful for opening
       the stream, which is where it gets exchanged.

    Checking the session *before* the ticket is deliberate: after the exchange
    the widget holds both, and the ticket is spent. Trying the spent ticket
    first would 401 a request that has a perfectly good session.

    Raises:
        ProblemException: 401 when no credential is present or the presented
            one is expired/spent; **403 when a valid credential names a
            different conversation** than the path. That 403 is the
            load-bearing check — without it a caller obtains a legitimate
            credential for their own conversation and then presents it against
            any id they like, and every other property protects nothing.
    """
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        # The admin path. `requires()` enforces the verb on top of this; RLS
        # scopes the row to the token's tenant.
        return ConversationAccess(
            principal=principal, conversation_id=conversation_id, is_widget=False
        )

    # A route with no `{conversation_id}` (e.g. `/me`) and no token: there is
    # nothing a widget credential could authorize, so fail closed here rather
    # than letting one reach a route it was never scoped to.
    if conversation_id is None:
        raise ProblemException(status=401, code="missing_credentials")

    # Header, then cookie, then query parameter — in decreasing order of how
    # much we like them, and the last one is not optional.
    #
    # `EventSource` cannot set a header and, embedded cross-origin on a tenant's
    # domain, will not send a `SameSite=Lax` cookie either. That is the whole
    # reason the single-use ticket exists for the FIRST connection. But a stream
    # that idles out and reconnects needs a credential too, and by then the
    # ticket is spent — so without this branch every reconnect 401s, the widget
    # exhausts its retries, and the caller is told the connection was lost. It
    # was not: the server refused the only credential the browser could send.
    #
    # A session in a URL is weaker than one in a header: URLs reach access logs,
    # proxy logs and `Referer`. It is accepted because the alternative is a
    # widget that cannot reconnect at all, and it is bounded by the session's
    # own short TTL and single-conversation scope.
    session_token = (
        request.headers.get(SESSION_HEADER)
        or request.cookies.get(SESSION_COOKIE)
        or session
    )

    claim = None
    credential = "none"
    if session_token:
        claim = await state.tickets.resolve_session(session_token)
        credential = "session"
    if claim is None and ticket:
        # Only *peeked* here. The stream handler redeems it, and it is the one
        # place that may, because redemption is what the exchange consumes.
        claim = await state.tickets.peek(ticket)
        credential = "ticket"

    if claim is None:
        if not session_token and not ticket:
            raise ProblemException(
                status=401,
                code="missing_credentials",
                detail=(
                    "Send an Authorization header, a `ticket` query parameter "
                    "from POST /api/v1/public/demo/sessions, or the "
                    f"{SESSION_HEADER} you received on the stream."
                ),
            )
        raise ProblemException(
            status=401,
            code="invalid_token",
            detail=(
                "This credential is expired or has already been used. Stream "
                "tickets are single-use and are exchanged for a conversation "
                "session when the stream opens; request a new one from "
                "POST /api/v1/public/demo/sessions."
            ),
        )

    if claim.conversation_id != conversation_id:
        log.warning(
            "widget.conversation_mismatch",
            credential=credential,
            credential_conversation=str(claim.conversation_id),
            requested=str(conversation_id),
        )
        raise ProblemException(
            status=403,
            code="forbidden",
            detail="This credential is not valid for that conversation.",
        )

    resolved = widget_principal(
        tenant_id=claim.tenant_id, conversation_id=conversation_id
    )
    # Attached so `requires()` — which takes this dependency — sees the widget
    # principal rather than falling back to a 401. One authorization code path
    # for both callers is the whole point.
    request.state.principal = resolved

    return ConversationAccess(
        principal=resolved, conversation_id=conversation_id, is_widget=True
    )


ConversationAccessDep = Annotated[
    ConversationAccess, Depends(resolve_conversation_access)
]

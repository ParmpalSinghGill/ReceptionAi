"""Tenant resolution — 05 §A3. The most security-critical code in the API.

Five rules, from 05 §A3.1, and every line below serves one of them:

1. **The tenant comes from the signed credential and nothing else.** Not a
   header, not a path segment, not a subdomain, not a query parameter
   (contract §10). A request carrying ``X-Tenant-Id`` is *rejected*, not
   ignored — silent ignoring is how a proxy misconfiguration becomes a breach
   later.
2. **Fail closed.** Any error — unknown token, suspended tenant, database
   unavailable, an unexpected exception — yields no tenant context and no
   session. There is no code path that reaches a repository with
   ``app.tenant_id`` unset.
3. ``SET LOCAL``, parameterized, inside the request transaction. Owned by
   ``packages/persistence/session.py``; this middleware supplies the
   ``Principal`` it derives the context from.
4. The session is opened by the router, never a global engine.
5. The GUC is set once and never changed within a transaction.

**There is no anonymous-tenant state** (05 §A3.4). Not "None means public" —
the state is never produced. A null tenant that reaches a repository yields
either an empty result or a confusing 500, and the natural "fix" someone
reaches for (``COALESCE(current_setting('app.tenant_id', true), ...)``)
silently makes the system cross-tenant. Public routes are exempted by path
before resolution runs, and they carry their own controls.
"""

from __future__ import annotations

import contextvars
import re

import structlog
from apps.api.problems import problem_response
from apps.api.security.tokens import TokenService
from packages.core.auth import Principal
from packages.core.errors import AuthenticationError, DomainError, TenantStateError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

__all__ = ["TenantContextMiddleware", "current_principal"]

log = structlog.get_logger(__name__)

#: A ContextVar, not a thread-local. A thread serves many concurrent requests
#: on an event loop, so a thread-local would leak one tenant's identity into
#: another's request. This distinction is the entire ballgame — the same point
#: `packages/core/tenancy.py` makes about `TenantContext`.
current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "receptionai_principal", default=None
)

#: Paths reachable with no credential. Kept in step with
#: `apps/api/security/route_audit.PUBLIC_ROUTES` by a test — the two lists
#: answer different questions (this one "may skip authentication", that one
#: "may skip authorization") but a route on one and not the other is always a
#: mistake in one direction or the other.
_ANON_PATHS = frozenset(
    {"/healthz", "/readyz", "/metrics", "/openapi.json", "/docs", "/redoc"}
)
_ANON_PREFIXES = ("/api/v1/auth/", "/api/v1/public/", "/docs")

#: Routes that accept EITHER a bearer token or a stream ticket
#: (PHASE1-API-CONTRACT). The middleware must not reject them for lacking an
#: Authorization header, because a widget legitimately has none — but it must
#: still resolve a principal when a header IS present, so an admin client gets
#: its full identity rather than being silently downgraded to a ticket path.
#:
#: So these are "optional authentication", not "anonymous": a *malformed* or
#: expired token is still rejected here. Only a completely absent credential
#: is allowed through, to be decided by
#: `apps.api.deps.resolve_conversation_access` — which fails closed if there is
#: no ticket either.
_OPTIONAL_AUTH = re.compile(
    r"^/api/v1/conversations/[^/]+/(messages|stream)$"
)

#: Headers that attempt to select a tenant. Rejected outright (05 §A3.4:
#: 400 `tenant_header_not_permitted`, audited).
_TENANT_HEADERS = ("x-tenant-id", "x-tenant", "x-org-id", "x-tenant-slug")


class TenantContextMiddleware(BaseHTTPMiddleware):
    """Turns a request into an authenticated principal, or rejects it.

    Ordering: runs *after* ``RequestIdMiddleware`` (so a denial can be
    correlated) and *before* ``RateLimitMiddleware`` (which keys on the tenant
    this resolves).
    """

    def __init__(self, app: ASGIApp, *, tokens: TokenService) -> None:
        super().__init__(app)
        self._tokens = tokens

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        if path in _ANON_PATHS or path.startswith(_ANON_PREFIXES):
            request.state.principal = None
            return await call_next(request)

        # Dual-credential routes: no Authorization header is not an error here,
        # it is the widget path. A header that IS present is still verified, so
        # a bad token cannot be laundered into an anonymous request.
        if _OPTIONAL_AUTH.match(path) and not request.headers.get("authorization"):
            request.state.principal = None
            return await call_next(request)

        try:
            principal = self._resolve(request)
        except DomainError as exc:
            return self._deny(request, exc)
        except Exception:
            # An exception here means we could not prove who is calling.
            # Proceeding without a tenant is how shared-schema systems leak.
            # 503 rather than 500 (05 §A3.4): it tells the client "we are
            # broken, retry" and makes SLO alerting treat it as availability
            # rather than as a security event.
            log.exception("auth.resolution_failed", path=path)
            return problem_response(
                request,
                status=503,
                code="authentication_unavailable",
                headers={"Retry-After": "2"},
            )

        request.state.principal = principal
        token = current_principal.set(principal)
        structlog.contextvars.bind_contextvars(
            tenant_id=str(principal.tenant_id),
            # The user id, never the email or name — 05 §D14 keeps PII out of
            # logs and span attributes.
            user_id=str(principal.user_id) if principal.user_id else None,
        )
        try:
            return await call_next(request)
        finally:
            current_principal.reset(token)
            structlog.contextvars.unbind_contextvars("tenant_id", "user_id")

    # ── Resolution ────────────────────────────────────────────────────────

    def _resolve(self, request: Request) -> Principal:
        # Rejected before the credential is even parsed: a client sending one
        # has either misunderstood the API or is probing, and both deserve a
        # hard error rather than being quietly ignored.
        for header in _TENANT_HEADERS:
            if header in request.headers:
                log.warning(
                    "auth.tenant_header_rejected",
                    header=header,
                    path=request.url.path,
                )
                raise AuthenticationError(
                    "The tenant is derived from your credentials only",
                    code="tenant_header_not_permitted",
                    http_status=400,
                )

        authorization = request.headers.get("authorization", "")
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or not credential:
            raise AuthenticationError(
                "Authentication required", code="missing_credentials"
            )

        claims = self._tokens.verify_access(credential)

        return Principal(
            kind="user",
            tenant_id=claims.tid,
            tenant_plan=claims.tsl,
            user_id=claims.sub,
            session_id=claims.sid,
            roles=claims.rol,
            permissions=claims.prm,
            aal=claims.aal,
            auth_time=claims.iat,
            acting_as=claims.act,
            # 05 §A2.6: precomputed once here so the RLS row predicate does not
            # re-derive it per query. An `agent` sees only conversations
            # assigned to them; every other role sees the tenant's.
            scope_all_conversations="agent" not in claims.rol,
        )

    def _deny(self, request: Request, exc: DomainError) -> Response:
        """Render a denial.

        Middleware runs outside the exception-handler stack, so it cannot raise
        and have the installed handler catch it — it calls the same builder
        directly. That is the *only* difference between a middleware denial and
        a handler denial: where the body is constructed, never what it looks
        like on the wire (amendment A9).
        """
        severity = "warning" if isinstance(exc, TenantStateError) else "info"
        log.log(
            30 if severity == "warning" else 20,
            "auth.denied",
            code=exc.code,
            status=exc.http_status,
            path=request.url.path,
        )
        return problem_response(
            request,
            status=exc.http_status,
            code=exc.code,
            title=exc.title,
            detail=exc.message,
            **exc.extensions,
        )

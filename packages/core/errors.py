"""The domain error hierarchy — the vocabulary every layer above raises.

Two things live here and they are deliberately in one file:

1. **The exception hierarchy.** Every error the domain can express, rooted at
   :class:`DomainError`, each carrying a stable ``code`` from the frozen
   vocabulary below.

2. **The frozen code vocabulary** (05 §A3.4, adopted by 06 §16.2 as amendment
   A9). These strings are a compatibility surface: they appear as the ``code``
   field of the RFC 9457 ``application/problem+json`` body and clients switch on
   them. They do not change.

**Why the codes live in `core` rather than in the API layer.** A9 makes the
error envelope uniform across handler-level and middleware-level denials. If the
vocabulary lived in ``apps/api``, then ``packages/*`` — which cannot import
``apps`` (contract §9) — would have to invent parallel strings, and the two sets
would drift. The domain owns the vocabulary; the API layer owns only the HTTP
rendering of it.

**Why titles are asserted, not defaulted.** A9: "A test asserts every frozen
code has a title; a missing one would ``KeyError`` inside the error path,
discoverable only by failing to render an error." :func:`title_for` and
:func:`assert_titles_complete` are the mechanism.

This module imports nothing outside the standard library, per contract §9.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

__all__ = [
    "FROZEN_ERROR_CODES",
    "PROBLEM_TITLES",
    "AuthenticationError",
    "AuthorizationError",
    "ConcurrencyConflictError",
    "ConfigurationError",
    "CrossTenantAccessError",
    "DomainError",
    "EntityNotFoundError",
    "PortUnavailableError",
    "RateLimitedError",
    "ResourceConflictError",
    "StepUpRequiredError",
    "TenantStateError",
    "ValidationError",
    "assert_titles_complete",
    "title_for",
]


# ─────────────────────────────────────────────────────────────────────────────
# The frozen code vocabulary (05 §A3.4 · 06 §16.2 · amendment A9)
# ─────────────────────────────────────────────────────────────────────────────
#
# Grouped by the document that froze them. Adding a code here without adding a
# title below fails `assert_titles_complete`, which the test suite calls.

FROZEN_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        # ── 05 §A3.4: authentication and tenant resolution ──────────────────
        "missing_credentials",
        "invalid_token",
        "token_expired",
        "token_revoked",
        "token_stale",
        "unknown_tenant",
        "tenant_suspended",
        "tenant_deleted",
        "tenant_header_not_permitted",
        "authentication_unavailable",
        # ── 05 §A2.4 / §A1.6: authorization and step-up ─────────────────────
        "forbidden",
        "step_up_required",
        "reauthentication_required",
        # ── 05 §B8: rate limiting ───────────────────────────────────────────
        "rate_limited",
        # ── 06 §16.2: resource and request errors ───────────────────────────
        "bad_request",
        "unauthenticated",
        "not_found",
        "method_not_allowed",
        "conflict",
        "gone",
        "unsupported_media_type",
        "validation_failed",
        "internal_error",
        # ── 06 §16.2: domain-specific conflicts ─────────────────────────────
        "slot_unavailable",
        "version_conflict",
        "precondition_required",
        # ── 01 §15.2 / amendment A8: shard routing ──────────────────────────
        "shard_unavailable",
        # ── contract §2.7: isolation ────────────────────────────────────────
        "cross_tenant_access",
        # ── contract §5: port/adapter availability ──────────────────────────
        "service_unavailable",
        "configuration_error",
    }
)

#: Human-readable titles, one per frozen code. RFC 9457 requires ``title`` to be
#: a short, human-readable summary that does not change from occurrence to
#: occurrence — so it is a lookup, never a formatted string.
PROBLEM_TITLES: Final[dict[str, str]] = {
    "missing_credentials": "Authentication required",
    "invalid_token": "Credentials are not valid",
    "token_expired": "Access token has expired",
    "token_revoked": "Session was revoked",
    "token_stale": "Permissions changed; refresh required",
    "unknown_tenant": "Tenant not found",
    "tenant_suspended": "Tenant is suspended",
    "tenant_deleted": "Tenant has been deleted",
    "tenant_header_not_permitted": "Tenant may not be selected by header",
    "authentication_unavailable": "Authentication is temporarily unavailable",
    "forbidden": "Insufficient permissions",
    "step_up_required": "Additional authentication required",
    "reauthentication_required": "Re-authentication required",
    "rate_limited": "Rate limit exceeded",
    "bad_request": "Malformed request",
    "unauthenticated": "Authentication required",
    "not_found": "Resource not found",
    "method_not_allowed": "Method not allowed",
    "conflict": "Conflicting request",
    "gone": "Resource is gone",
    "unsupported_media_type": "Unsupported media type",
    "validation_failed": "Request validation failed",
    "internal_error": "Internal server error",
    "slot_unavailable": "The requested time is no longer available",
    "version_conflict": "The record changed since you loaded it",
    "precondition_required": "If-Match header is required",
    "shard_unavailable": "Tenant infrastructure is unavailable",
    "cross_tenant_access": "Cross-tenant access denied",
    "service_unavailable": "A dependency is temporarily unavailable",
    "configuration_error": "Service is misconfigured",
}


def title_for(code: str) -> str:
    """The RFC 9457 ``title`` for a frozen code.

    Raises:
        KeyError: for an unknown code. Deliberately not defaulted: a
            ``code.replace("_", " ").capitalize()`` fallback would let a typo
            ship as a plausible-looking title and never be noticed.
    """
    return PROBLEM_TITLES[code]


def assert_titles_complete() -> None:
    """Every frozen code has a title, and no title is orphaned.

    Called from the test suite (A9). Both directions matter: a missing title
    ``KeyError``s inside the error path — discoverable only by failing to render
    an error — and an orphaned title is a code that was removed from the frozen
    set, which is a compatibility break someone needs to have decided on.
    """
    missing = FROZEN_ERROR_CODES - set(PROBLEM_TITLES)
    if missing:
        raise AssertionError(
            f"Frozen error codes with no title: {sorted(missing)}. "
            f"A missing title raises KeyError inside the error-rendering path "
            f"(contract A9)."
        )
    orphaned = set(PROBLEM_TITLES) - FROZEN_ERROR_CODES
    if orphaned:
        raise AssertionError(
            f"Titles for codes not in the frozen vocabulary: {sorted(orphaned)}."
        )


# ─────────────────────────────────────────────────────────────────────────────
# The hierarchy
# ─────────────────────────────────────────────────────────────────────────────


class DomainError(Exception):
    """Root of every error the domain can express.

    Carries the frozen ``code`` and an ``extensions`` mapping that becomes
    RFC 9457 problem extensions verbatim (``missing_permissions``,
    ``required_aal``, ``retry_after_ms``, ``conflict_kind``, …). Subclasses set
    ``code`` and ``http_status`` as class attributes so the API layer maps an
    exception to a response without a registry of isinstance checks.
    """

    #: Frozen vocabulary member. Every concrete subclass overrides this.
    code: str = "internal_error"
    #: The HTTP status the API layer renders. Owned here rather than in the API
    #: layer because the *reason* for the status is a domain fact (409 means
    #: "someone else moved first"), not a transport detail.
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        model_message: str | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        #: What an AI agent may say about this failure, if anything.
        #:
        #: **The two audiences are not the same, and conflating them leaks.** A
        #: caller was told "Could I get your number in full, including the
        #: leading 0 or country code?" because the phone validator's message —
        #: written as an instruction to the model — was read out almost
        #: verbatim. A UK dental practice does not ask for a country code.
        #:
        #: ``message`` is diagnostic and goes to logs. ``model_message`` is what
        #: the agent is allowed to work from, written to be spoken. Defaulting
        #: to ``message`` keeps every existing raise site working; the ones that
        #: reach a customer set it explicitly.
        self.model_message = model_message or message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.extensions: dict[str, Any] = {
            k: v for k, v in extensions.items() if v is not None
        }
        if self.code not in FROZEN_ERROR_CODES:
            raise ValueError(
                f"{type(self).__name__} carries code {self.code!r}, which is not "
                f"in the frozen vocabulary. Add it to FROZEN_ERROR_CODES *and* "
                f"PROBLEM_TITLES, deliberately — clients switch on these strings."
            )

    @property
    def title(self) -> str:
        return title_for(self.code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# ── Validation and request shape ─────────────────────────────────────────────


class ValidationError(DomainError):
    """A value violates a domain rule that the type system cannot express."""

    code = "validation_failed"
    http_status = 422

    def __init__(
        self, message: str, *, field: str | None = None, **extensions: Any
    ) -> None:
        super().__init__(message, field=field, **extensions)
        self.field = field


class ConfigurationError(DomainError):
    """The platform or a tenant is misconfigured.

    Distinct from :class:`ValidationError`: nothing the caller sent is wrong.
    A 500 rather than a 4xx, because it is our fault.
    """

    code = "configuration_error"
    http_status = 500


# ── Lookup and concurrency ───────────────────────────────────────────────────


class EntityNotFoundError(DomainError):
    """A requested entity does not exist *within the current tenant*.

    Note the scoping. Under RLS an entity belonging to another tenant is
    indistinguishable from one that does not exist, and that is the correct
    behaviour: a 404 rather than a 403 refuses to confirm that an id is real
    somewhere else, which would otherwise make the id space enumerable across
    tenants.
    """

    code = "not_found"
    http_status = 404

    def __init__(
        self,
        *,
        entity_type: str,
        entity_id: UUID | str | None = None,
        message: str | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(
            message or f"{entity_type} {entity_id} not found",
            entity_type=entity_type,
            **extensions,
        )
        self.entity_type = entity_type
        self.entity_id = entity_id


class ConcurrencyConflictError(DomainError):
    """An optimistic-concurrency check failed: another writer moved first.

    Surfaced as 412 rather than 409 (06 §16.2): the caller sent a precondition
    — ``If-Match`` / ``expected_version`` — and the precondition failed. A 409
    would say "your request conflicts with the resource state" without telling
    the client that re-reading and retrying is the correct response.
    """

    code = "version_conflict"
    http_status = 412

    def __init__(
        self,
        *,
        entity_type: str,
        entity_id: UUID | str,
        expected_version: int,
        message: str | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(
            message
            or (
                f"{entity_type} {entity_id} was modified by another writer; "
                f"expected version {expected_version}"
            ),
            entity_type=entity_type,
            expected_version=expected_version,
            **extensions,
        )
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.expected_version = expected_version


class ResourceConflictError(DomainError):
    """A write was refused by a uniqueness, exclusion, FK, or check constraint.

    ``kind`` discriminates, and it matters: the booking service catches
    ``kind="overlap"`` specifically in order to retry against another resource,
    which it must not do for ``kind="duplicate"``. It is the Python-side
    counterpart of the SQLSTATE the repository translated (23P01, 23505, 23503,
    23514).
    """

    code = "conflict"
    http_status = 409

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        constraint: str | None = None,
        code: str | None = None,
        **extensions: Any,
    ) -> None:
        # An overlap on a booking is the one conflict clients are told to
        # special-case, so it gets its own frozen code (06 §16.2's worked
        # example) rather than the generic one.
        resolved_code = code or ("slot_unavailable" if kind == "overlap" else "conflict")
        super().__init__(
            message,
            code=resolved_code,
            conflict_kind=kind,
            constraint=constraint,
            **extensions,
        )
        self.kind = kind
        self.constraint = constraint


# ── Identity, isolation, and access ──────────────────────────────────────────


class AuthenticationError(DomainError):
    """The caller is not authenticated, or the credential is unusable."""

    code = "invalid_token"
    http_status = 401


class AuthorizationError(DomainError):
    """The caller is authenticated but lacks a required permission (05 §A2)."""

    code = "forbidden"
    http_status = 403

    def __init__(
        self,
        message: str = "Insufficient permissions",
        *,
        missing_permissions: tuple[str, ...] = (),
        **extensions: Any,
    ) -> None:
        super().__init__(
            message,
            missing_permissions=list(missing_permissions) or None,
            **extensions,
        )
        self.missing_permissions = missing_permissions


class StepUpRequiredError(AuthorizationError):
    """A sensitive action requires a fresh MFA assertion (05 §A1.6, §A2.4).

    Separate from :class:`AuthorizationError` because the remedy is different:
    the caller is not missing a permission, they are missing *recency*. A client
    that treats this as a plain 403 shows "you cannot do this" instead of
    prompting for MFA.
    """

    code = "step_up_required"
    http_status = 403

    def __init__(
        self, message: str = "Additional authentication required", *, required_aal: int = 2
    ) -> None:
        super().__init__(message, required_aal=required_aal)
        self.required_aal = required_aal


class CrossTenantAccessError(DomainError):
    """An operation attempted to touch data outside the bound tenant.

    Contract §2.7 makes cross-tenant access impossible by construction — RLS
    would refuse the write regardless. This error exists because RLS refuses it
    as an opaque database error deep inside a flush, and a precise Python-side
    failure naming the offending object is testable, whereas "0 rows updated"
    is not. Reaching this is always a bug in *our* code, never in a caller's
    request, which is why it renders as a 500-class problem and pages.
    """

    code = "cross_tenant_access"
    http_status = 500


class TenantStateError(DomainError):
    """The tenant exists but is not in a state that permits this request.

    Suspended → 403, deleted → 410 (05 §A3.4). Both carry a distinct code so a
    client can tell "pay your bill" from "this is over".
    """

    code = "tenant_suspended"
    http_status = 403


# ── Availability of a dependency behind a port ───────────────────────────────


class PortUnavailableError(DomainError):
    """An adapter behind a contract §5 port failed or timed out.

    503 with ``Retry-After``, never 500: a 500 tells the client "your request
    was wrong", while a 503 tells it "we are broken, retry" — which drives
    client backoff correctly and makes our SLO alerting treat the event as
    availability rather than security (05 §A3.4).

    :class:`~packages.persistence.engine.UnresolvableShardError` narrows this
    for the A8 fail-closed case; it maps to ``shard_unavailable`` so an alert
    can distinguish "a provider is down" from "a tenant who paid for isolation
    cannot be served", which is a compliance incident in progress.
    """

    code = "service_unavailable"
    http_status = 503

    def __init__(
        self,
        message: str,
        *,
        port: str,
        adapter: str | None = None,
        retry_after_s: int = 2,
        code: str | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(
            message,
            code=code,
            port=port,
            adapter=adapter,
            retry_after_ms=retry_after_s * 1000,
            **extensions,
        )
        self.port = port
        self.adapter = adapter
        self.retry_after_s = retry_after_s


class RateLimitedError(DomainError):
    """A per-tenant or per-endpoint-class rate limit was exceeded (05 §B8)."""

    code = "rate_limited"
    http_status = 429

    def __init__(
        self, message: str = "Rate limit exceeded", *, retry_after_ms: int, scope: str
    ) -> None:
        super().__init__(message, retry_after_ms=retry_after_ms, scope=scope)
        self.retry_after_ms = retry_after_ms
        self.scope = scope

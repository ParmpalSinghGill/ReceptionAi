"""The authenticated caller — 05 §A3.2, normative.

Every construction site in the platform is reconciled against this definition.
``frozen=True, slots=True`` means a typo'd or unknown keyword is a ``TypeError``
at construction rather than a silently-ignored attribute. That strictness is
deliberate: a Principal with a misspelled ``plan=`` that quietly defaulted would
fail **open** in the rate limiter and in plan-gated adapter resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal
from uuid import UUID

__all__ = ["PRINCIPAL_KINDS", "Principal", "PrincipalKind"]

PrincipalKind = Literal["user", "api_key", "service"]

PRINCIPAL_KINDS: Final[frozenset[str]] = frozenset({"user", "api_key", "service"})


@dataclass(frozen=True, slots=True)
class Principal:
    """THE authoritative shape of an authenticated caller.

    This definition is normative: every construction site in the codebase is
    reconciled against it, and ``frozen=True, slots=True`` means a typo'd or
    unknown keyword is a ``TypeError`` at construction rather than a
    silently-ignored attribute. That strictness is deliberate — a Principal with
    a misspelled ``plan=`` that quietly defaulted would fail *open* in the rate
    limiter and the plan-gated adapter resolution.
    """

    kind: PrincipalKind  # 'user' | 'api_key' | 'service'
    tenant_id: UUID
    tenant_plan: str
    permissions: frozenset[str]
    user_id: UUID | None = None
    api_key_id: UUID | None = None
    session_id: UUID | None = None
    roles: tuple[str, ...] = ()  # tuple, not list: the dataclass is frozen
    # and a list would be an unhashable field
    aal: int = 1  # authenticator assurance level (05 §A1.2)
    auth_time: datetime | None = None

    # Support impersonation (RFC 8693 `act`). Non-None means "staff member
    # `user_id` is acting as tenant user `acting_as`". AUDIT-RELEVANT: it is
    # what makes `audit_actor` below emit both identities, so an impersonated
    # action is never recorded as though the customer performed it themselves.
    # Set only from a signed `act` claim; never inferred, never user-supplied.
    #
    # 01-DATA-MODEL.md §15 reads this through `TenantContext.impersonator_user_id`.
    # Same value, opposite naming convention: 01 names the *actor*, 05 names the
    # *subject being acted upon*. The alias property below is the
    # reconciliation — one field, no drift.
    acting_as: UUID | None = None

    # Selects the tenant's database (contract §2 escape hatch). None = the
    # shared cluster; non-None routes to a dedicated engine for an enterprise
    # or HIPAA tenant. Carried on the Principal rather than re-queried per
    # request because it is needed BEFORE a session can be opened — looking it
    # up would require the session it determines. Resolved from the tenant
    # record at authentication and cached with it (05 §A3.3 `TenantCache`).
    shard_key: str | None = None

    # Precomputed authz narrowing, consumed by the RLS session opener (05 §A3.5)
    # to set `app.scope_all_conversations`. False means this principal sees
    # only conversations assigned to them or unassigned (05 §A2.6). Computed
    # once at resolution so the row predicate does not re-derive it per query.
    scope_all_conversations: bool = False

    def __post_init__(self) -> None:
        # Invariants that the type system cannot express. Cheap, and each one
        # corresponds to a real failure mode rather than defensive noise.
        if self.kind not in PRINCIPAL_KINDS:
            raise ValueError(f"unknown principal kind {self.kind!r}")
        if self.kind == "api_key":
            if self.api_key_id is None:
                raise ValueError("api_key principal requires api_key_id")
            if self.aal >= 2:
                # 05 §A1.7: a machine credential must never satisfy step-up.
                # A key cannot present a second factor, so an aal >= 2 on one
                # can only have come from a bug or a forged claim — and it
                # would unlock exactly the routes (rotate key, change billing,
                # export data, delete tenant) that step-up exists to protect.
                raise ValueError("api_key principals cannot hold aal >= 2")
        if self.kind == "user" and self.user_id is None:
            raise ValueError("user principal requires user_id")
        if self.acting_as is not None and self.kind != "user":
            raise ValueError("impersonation is only valid for user principals")

    @property
    def auth_age_seconds(self) -> float:
        """Seconds since the credential was minted; ``inf`` when unknown.

        ``inf`` rather than 0 is the fail-closed choice: an unknown auth time
        must read as "arbitrarily stale", so a freshness check refuses rather
        than admits.
        """
        if self.auth_time is None:
            return float("inf")
        return (datetime.now(UTC) - self.auth_time).total_seconds()

    @property
    def impersonator_user_id(self) -> UUID | None:
        """Alias consumed by ``tenant_context()`` in 01-DATA-MODEL.md §15.

        When impersonating, the *impersonator* is ``user_id`` (the staff member
        holding the session) and ``acting_as`` is the customer being acted upon.
        01 wants the actor, so that is what this returns — and it returns None
        when no impersonation is in play, so an ordinary session records no
        impersonator rather than recording the user as impersonating themselves.
        """
        return self.user_id if self.acting_as is not None else None

    @property
    def subject_user_id(self) -> UUID | None:
        """The user whose authority the action is taken under.

        Under impersonation that is ``acting_as``; otherwise it is ``user_id``.
        This is what ``TenantContext.user_id`` carries, and therefore what
        ``app.user_id`` is set to — so the self-row RLS policies on ``sessions``
        and ``users`` (01 §11.5) scope to the impersonated subject, exactly as
        §11.4 requires: support access must be indistinguishable at the database
        layer from ordinary tenant access, so the same policy protects it.
        """
        return self.acting_as if self.acting_as is not None else self.user_id

    @property
    def is_impersonated(self) -> bool:
        return self.acting_as is not None

    @property
    def audit_actor(self) -> str:
        if self.acting_as:
            return f"staff:{self.user_id}#acting:{self.acting_as}"
        if self.api_key_id:
            return f"apikey:{self.api_key_id}"
        if self.kind == "service":
            return "system"
        return f"user:{self.user_id}"

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def missing_permissions(self, required: frozenset[str]) -> frozenset[str]:
        """Which of ``required`` this principal lacks.

        Returned rather than raised so the caller can put the whole set in one
        problem-extension field; telling a client about one missing permission
        at a time turns a single fix into a round-trip loop.
        """
        return required - self.permissions

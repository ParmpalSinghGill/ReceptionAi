"""Wire schemas — the exact shapes PHASE1-API-CONTRACT freezes.

Conventions the contract fixes, applied uniformly here:

* All ids are UUIDv7 **strings** on the wire. Pydantic serializes ``UUID`` to a
  string, so the models carry ``UUID`` and JSON carries the string.
* All timestamps are RFC 3339 UTC with ``Z``. :func:`rfc3339` enforces the
  ``Z`` — Python's ``isoformat()`` emits ``+00:00``, which is equally valid
  RFC 3339 and *not* what the contract's examples show, and a frontend written
  against the examples with a naive parser breaks on it.
* Money is a decimal **string** plus an ISO-4217 currency, never a float.
  Phase 1 exposes no money field; the helper exists so the first one cannot be
  added as a float.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BookingListResponse",
    "BookingResource",
    "BookingSummary",
    "ConversationDetail",
    "DemoSessionResponse",
    "LoginRequest",
    "MeResponse",
    "MessageAccepted",
    "MessageRequest",
    "Money",
    "RefreshRequest",
    "ResourceListResponse",
    "TenantSummary",
    "TokenResponse",
    "TurnDetail",
    "rfc3339",
]


def rfc3339(value: datetime | None) -> str | None:
    """RFC 3339 UTC with a literal ``Z``.

    ``datetime.isoformat()`` gives ``+00:00``. Both are valid RFC 3339, but the
    contract's examples show ``Z`` and a frontend that string-slices or uses a
    permissive-looking regex will handle one and not the other. Pinning the
    format here means every timestamp in every response is identical.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class Money(BaseModel):
    """A decimal string plus ISO-4217. Never a float (contract §6).

    Floats cannot represent 0.10 exactly, so summing a hundred of them drifts
    from the ledger — and the drift appears in an invoice, months later, in a
    currency someone has to reconcile by hand.
    """

    model_config = ConfigDict(extra="forbid")

    amount: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @classmethod
    def from_decimal(cls, amount: Decimal, currency: str) -> Money:
        return cls(amount=f"{amount:f}", currency=currency)


# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    #: A user in several tenants must say which they are signing into: `tid` is
    #: singular by design (05 §A1.2). Omitted means "my only membership", and
    #: the handler refuses rather than guessing when there is more than one.
    tenant_slug: str | None = None


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=1, max_length=512)


class TokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    refresh_token: str
    # RFC 6750 names the scheme "Bearer"; this is the scheme name on the
    # wire, not a credential. S105 pattern-matches the field name.
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105
    expires_in: int
    tenant_id: UUID
    user_id: UUID


# ─────────────────────────────────────────────────────────────────────────────
# Me
# ─────────────────────────────────────────────────────────────────────────────


class TenantSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    slug: str
    display_name: str
    status: str
    plan: str
    timezone: str
    currency: str


class MeResponse(BaseModel):
    """The current principal — drives RBAC-conditional UI (06 §8.4).

    ``permissions`` is a sorted list rather than a set so the JSON is stable
    across requests: an unstable ordering defeats HTTP caching and makes every
    response look changed to a client diffing it.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: UUID | None
    email: str | None
    display_name: str | None
    kind: str
    roles: list[str]
    permissions: list[str]
    aal: int
    is_impersonated: bool
    tenant: TenantSummary


# ─────────────────────────────────────────────────────────────────────────────
# Public demo
# ─────────────────────────────────────────────────────────────────────────────


class DemoSessionRequest(BaseModel):
    """``POST /api/v1/public/demo/sessions`` ← body. Every field optional.

    The contract's example body is ``{}`` and every existing client sends
    exactly that, so this stays additive: a request that omits ``mode`` behaves
    precisely as it did before.

    ``mode`` exists because the demo now has two front doors. The conversation
    row records which one was used, so a booking made by speaking is
    distinguishable from one made by typing — without it every voice call is
    filed as web chat and the channel column stops meaning anything.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["chat", "voice"] = "chat"


class DemoSessionResponse(BaseModel):
    """``POST /api/v1/public/demo/sessions`` → 201.

    Note what is absent: no tenant id, no manifest version, no internal ids.
    The contract says *"never returns tenant internals"*, and an anonymous
    member of the public receives exactly what the widget needs to render and
    connect.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    ticket: str
    vertical: str
    greeting: str


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=8_000)
    #: Contract: `POST` accepts an idempotency key. Carried in the body here
    #: (per the frozen example) as well as being accepted as a header.
    idempotency_key: str | None = Field(default=None, max_length=128)


class MessageAccepted(BaseModel):
    """``POST .../messages`` → 202. The turn is queued; tokens arrive on SSE."""

    model_config = ConfigDict(extra="forbid")

    turn_id: UUID
    accepted_at: str


# ─────────────────────────────────────────────────────────────────────────────
# Conversations
# ─────────────────────────────────────────────────────────────────────────────


class TurnDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    sequence: int
    role: str
    content: str | None
    agent_node: str | None = None
    created_at: str
    latency_ms: int | None = None
    finish_reason: str | None = None
    #: Present only for a principal holding `usage:read` — 06 §3.2 keeps unit
    #: economics away from any other audience.
    usage: dict[str, Any] | None = None


class ConversationDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    channel: str
    status: str
    started_at: str
    ended_at: str | None
    turn_count: int
    agent_code: str | None
    agent_version: str | None
    escalated: bool
    outcome: str | None = None
    turns: list[TurnDetail]


# ─────────────────────────────────────────────────────────────────────────────
# Bookings — the diary
# ─────────────────────────────────────────────────────────────────────────────


class BookingResource(BaseModel):
    """One resource a booking consumes, and the diary's filter options.

    Generic on purpose (01 §5): a `resource` is a person, a room, or a piece of
    equipment. Which of those a tenant's diary shows is a property of their
    manifest, not of this schema.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    code: str
    name: str
    resource_type_code: str | None = None
    #: The resource's own attributes as the tenant configured them. The portal
    #: reads them for display only; nothing here is interpreted by the API.
    attributes: dict[str, Any] = Field(default_factory=dict)


class BookingSummary(BaseModel):
    """One booking as a day view renders it.

    ``customer_name`` and ``customer_phone`` are **null for a principal without
    ``conversations:read_pii``**, and null rather than absent so a client does
    not have to distinguish "no phone recorded" from "not allowed to see it" by
    key presence — 05 §A2.3's viewer asymmetry, applied to the diary.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    reference: str
    status: str
    starts_at: str
    ends_at: str
    #: The business's own calendar day, `YYYY-MM-DD`. Derived at write time in
    #: the location's zone; contract §6 forbids re-deriving it from the UTC
    #: instant, which is wrong twice a year.
    local_date: str
    #: IANA zone the local time should be rendered in.
    timezone: str
    service_id: UUID
    service_code: str
    service_name: str
    customer_name: str | None = None
    customer_phone: str | None = None
    resources: list[BookingResource] = Field(default_factory=list)
    price: Money | None = None


class BookingListResponse(BaseModel):
    """``GET /api/v1/bookings`` → 200, for a day or a range.

    A day is a bounded set — a practice with forty resources has tens of
    bookings, not thousands — so this returns the whole span rather than a
    cursor page. Contract §2.2's cursor rule governs the unbounded lists; a
    calendar that paginated a Tuesday would be unusable and would still have to
    fetch every page to lay out the grid, and the range is capped server-side
    so it cannot grow without bound either.
    """

    model_config = ConfigDict(extra="forbid")

    #: The single day asked for, or null when a range was. Kept so the day
    #: view's existing contract is unchanged by the range parameters.
    date: str | None = None
    #: Both ends of what was actually queried, inclusive. Always present, so a
    #: client never has to reconstruct the span it asked for.
    from_date: str
    to_date: str
    timezone: str
    items: list[BookingSummary]


class ResourceListResponse(BaseModel):
    """``GET /api/v1/resources`` → 200. The diary's filter options."""

    model_config = ConfigDict(extra="forbid")

    items: list[BookingResource]

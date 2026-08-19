"""The value types the booking engine speaks — 02 §5.1, backed by 01 §5.

Frozen dataclasses rather than ORM rows. The engine's core — slot enumeration,
attribute matching, conflict detection — is pure computation over these, which
is what lets it be tested without a database and what keeps the SQL at the
edges. A repository loads rows and builds these; nothing here knows SQLAlchemy.

Every field name is drawn from the generic vocabulary. If a concept cannot be
expressed as a resource attribute, a requirement predicate, an allocation
strategy, or a capacity/buffer refinement, it is a **model** change (02 §5.6's
"when this test fires"), not a new field with a vertical's name on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

__all__ = [
    "AllocationCandidate",
    "BookingRecord",
    "CancellationQuote",
    "ClosedWindow",
    "HoldRecord",
    "OpenWindow",
    "RequirementSpec",
    "ResourceSpec",
    "ServiceSpec",
    "Slot",
    "TimeUnit",
]

#: 01 §5.2's ``time_unit``: the natural unit of the time axis. It drives UI,
#: pricing, and how the engine snaps boundaries. It does NOT change storage —
#: everything is a ``tstzrange`` either way.
TimeUnit = Literal["minute", "hour", "day", "night"]

#: 01 §5.5's ``allocation_strategy``: how to choose when several resources
#: qualify. ``best_fit`` minimises wasted capacity, ``least_utilised`` spreads
#: load, ``first_available`` fills front-to-back, ``round_robin`` rotates.
AllocationStrategy = Literal[
    "first_available", "least_utilised", "best_fit", "round_robin", "explicit"
]


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """One bookable instance, with the type switches it inherits.

    ``is_capacity_shared``, ``time_unit`` and ``slot_granularity_minutes`` live
    on the resource *type* in the schema (01 §5.2) and are flattened onto the
    instance here, because every engine decision that reads them also reads the
    instance and a second lookup would buy nothing.
    """

    id: UUID
    tenant_id: UUID
    location_id: UUID
    resource_type_id: UUID
    code: str
    name: str
    capacity: int = 1
    overbook_allowance: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    #: Overrides the location timezone. Almost always ``None``.
    timezone: str | None = None
    sort_order: int = 0

    # ── Inherited from the resource type ──────────────────────────────────
    is_capacity_shared: bool = False
    time_unit: TimeUnit = "minute"
    slot_granularity_minutes: int | None = None

    @property
    def sort_key(self) -> tuple[int, str, str]:
        """A stable tiebreaker for every allocation strategy.

        Ranking must be deterministic, or the same query returns different
        resources on successive calls — and a caller offered "Tuesday with the
        person you saw last time" and then booked with someone else has been
        lied to. Explicit ``sort_order`` first (the admin's own preference),
        then code, then id.
        """
        return (self.sort_order, self.code, str(self.id))

    def matches(self, predicate: dict[str, Any], bindings: dict[str, Any]) -> bool:
        """Evaluate 01 §5.5's ``attribute_predicate`` against this resource.

        Three operators, and they are deliberately few:

        * ``match`` — equality, or subset containment when the expected value is
          a list. ``{"match": {"skills": ["scaling"]}}`` is satisfied by a
          resource whose ``skills`` array *contains* scaling, not one that equals
          it exactly, because "can do X" is the question being asked.
        * ``gte`` / ``lte`` — numeric bounds. This is how "a party of four needs
          something that seats at least four" is expressed with no vertical
          knowledge: the requirement says ``{"gte": {"seats": "$party_size"}}``
          and the resource carries ``{"seats": 6}``.
        * ``in`` — membership in an allowed set.

        ``$party_size`` and ``$duration_units`` are the two supported bind
        variables (01 §5.5), substituted from ``bindings``. A ``$name`` the
        caller did not bind evaluates as *unsatisfiable* rather than as the
        literal string — silently comparing ``6 >= "$party_size"`` would be a
        type error at best and a wrong answer at worst.
        """
        for key, expected in (predicate.get("match") or {}).items():
            actual = self.attributes.get(key)
            if isinstance(expected, list):
                have = set(actual) if isinstance(actual, list) else {actual}
                if not set(expected).issubset(have):
                    return False
            elif actual != expected:
                return False

        for key, raw in (predicate.get("gte") or {}).items():
            bound = _bind(raw, bindings)
            actual = self.attributes.get(key)
            if bound is None or not _numeric(actual) or actual < bound:
                return False

        for key, raw in (predicate.get("lte") or {}).items():
            bound = _bind(raw, bindings)
            actual = self.attributes.get(key)
            if bound is None or not _numeric(actual) or actual > bound:
                return False

        for key, allowed in (predicate.get("in") or {}).items():
            if self.attributes.get(key) not in (allowed or []):
                return False

        return True


@dataclass(frozen=True, slots=True)
class RequirementSpec:
    """What a service needs, expressed as a *kind* — never a named instance.

    01 §5.5: "the join that makes the model generic". A service declares what
    kinds of resource it needs, how many, and with what attributes. Two rows with
    different ``role`` values is how a multi-resource booking is expressed, and
    it needs no special handling anywhere in the engine.
    """

    id: UUID
    resource_type_id: UUID
    role: str = "primary"
    quantity: int = 1
    #: ``None`` = derive from the booking's party size.
    units_per_resource: int | None = None
    attribute_predicate: dict[str, Any] = field(default_factory=dict)
    is_required: bool = True
    allocation_strategy: AllocationStrategy = "best_fit"
    #: Does this requirement consume the resource for the booking's *blocking*
    #: range, or only its *service* range? A space is blocked through cleanup;
    #: the person who used it is not.
    respects_buffers: bool = True


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """What a customer books, and the policy attached to it."""

    id: UUID
    tenant_id: UUID
    code: str
    name: str
    #: ``None`` = offered at every location.
    location_id: UUID | None = None
    duration_minutes: int | None = None
    duration_units: int | None = None
    buffer_before_minutes: int = 0
    buffer_after_minutes: int = 0
    min_party_size: int = 1
    max_party_size: int = 1
    lead_time_minutes: int = 0
    booking_horizon_days: int = 90
    cancellation_window_minutes: int = 1440
    cancellation_fee: Decimal | None = None
    price: Decimal | None = None
    currency: str | None = None
    requirements: tuple[RequirementSpec, ...] = ()
    intake_schema: tuple[dict[str, Any], ...] = ()
    allow_waitlist: bool = True

    @property
    def duration(self) -> timedelta:
        """The service length. Minutes when set, else one unit of the time axis.

        A service with neither is refused by ``ck_services__duration_present`` at
        the database, so the fallback here is unreachable in practice — it exists
        so a hand-built spec in a test does not divide by zero.
        """
        if self.duration_minutes:
            return timedelta(minutes=self.duration_minutes)
        return timedelta(days=self.duration_units or 1)


@dataclass(frozen=True, slots=True)
class OpenWindow:
    """One materialised open interval, in UTC.

    Produced by expanding an ``availability_rules`` row over a date range using
    the location's IANA timezone. Local wall-clock is the *storage* form
    (01 §5.6) precisely so that "we open at 9" survives a DST transition; this is
    what it becomes once a concrete date is known.
    """

    start: datetime
    end: datetime
    #: Which resource this window applies to. ``None`` = the whole location.
    resource_id: UUID | None = None
    capacity_override: int | None = None
    priority: int = 0
    #: The local calendar date this window was expanded from. Carried because a
    #: booking's ``local_date`` column must be the *business's* day, and after a
    #: window crosses midnight the UTC instant no longer says which that was.
    local_date: date | None = None


@dataclass(frozen=True, slots=True)
class ClosedWindow:
    """A concrete UTC interval during which nothing may be booked.

    Exceptions and expanded holidays converge on this shape, so the subtraction
    step does not need to know which produced it.
    """

    start: datetime
    end: datetime
    resource_id: UUID | None = None
    resource_type_id: UUID | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AllocationCandidate:
    """One requirement satisfied by a concrete set of resources, for one slot."""

    role: str
    requirement_id: UUID
    resource_ids: tuple[UUID, ...]
    quantity: int
    respects_buffers: bool


@dataclass(frozen=True, slots=True)
class Slot:
    """A bookable start, with the allocation that makes it bookable.

    The allocation is carried rather than recomputed at booking time. Offering a
    slot and then failing to book it because the resource that made it available
    has since been chosen for something else is the single most confusing thing a
    booking system can do to a caller, and carrying the assignment forward means
    the booking path re-checks a *specific* claim rather than re-deriving a new
    one.
    """

    start_at: datetime
    end_at: datetime
    allocations: tuple[AllocationCandidate, ...]
    price: Decimal | None = None
    currency: str | None = None

    @property
    def resource_ids(self) -> tuple[UUID, ...]:
        return tuple(r for a in self.allocations for r in a.resource_ids)


@dataclass(frozen=True, slots=True)
class HoldRecord:
    """A live hold, as the caller sees it."""

    id: UUID
    tenant_id: UUID
    location_id: UUID
    service_id: UUID
    resource_id: UUID
    start_at: datetime
    end_at: datetime
    expires_at: datetime
    quantity: int = 1
    status: str = "active"

    def is_live(self, now: datetime) -> bool:
        """01 §13.3 layer 1, in Python.

        The database predicate is authoritative — this is for the caller that
        wants to know before issuing a write. The two must agree, and they do
        because both express exactly ``status = 'active' AND expires_at > now``.
        """
        return self.status == "active" and self.expires_at > now


@dataclass(frozen=True, slots=True)
class BookingRecord:
    """A booking as the tools and the API render it."""

    id: UUID
    tenant_id: UUID
    reference: str
    status: str
    service_id: UUID
    service_code: str
    service_name: str
    location_id: UUID
    start_at: datetime
    end_at: datetime
    local_date: date
    timezone: str
    party_size: int = 1
    customer_name: str | None = None
    customer_phone_e164: str | None = None
    customer_email: str | None = None
    resource_names: tuple[str, ...] = ()
    #: The allocated resources' ids, in the same order as ``resource_names``.
    #:
    #: Names are what a person reads and ids are what a filter compares, and a
    #: caller that has only the names has to match on a string a tenant can
    #: rename — which silently stops matching the moment they do. Both travel
    #: together so neither side has to look the other up.
    resource_ids: tuple[UUID, ...] = ()
    price_total: Decimal | None = None
    currency: str | None = None
    cancellation_window_minutes: int = 1440
    cancellation_fee: Decimal | None = None
    cancellation_fee_charged: Decimal | None = None
    cancelled_at: datetime | None = None
    cancellation_reason: str | None = None
    rescheduled_from_id: UUID | None = None
    intake_data: dict[str, Any] = field(default_factory=dict)

    @property
    def local_start(self) -> datetime:
        """The start instant rendered in the location's own clock.

        Contract §6: business-local time is *derived* at render time, never
        stored. This is that derivation, in one place, so no caller is tempted to
        do it with a hardcoded offset.
        """
        from zoneinfo import ZoneInfo

        return self.start_at.astimezone(ZoneInfo(self.timezone))

    @property
    def local_end(self) -> datetime:
        from zoneinfo import ZoneInfo

        return self.end_at.astimezone(ZoneInfo(self.timezone))


@dataclass(frozen=True, slots=True)
class CancellationQuote:
    """What cancelling this booking costs, and whether the agent may do it.

    Separate from the act of cancelling because the agent must be able to *tell
    the caller the fee* before taking the decision. A cancel that silently
    charges is a complaint; a cancel that quotes first is a conversation.
    """

    allowed: bool
    within_notice_period: bool
    notice_required_minutes: int
    notice_actual_minutes: int
    fee: Decimal | None
    currency: str | None
    reason: str

    @property
    def fee_applies(self) -> bool:
        return self.fee is not None and self.fee > 0


def _bind(raw: Any, bindings: dict[str, Any]) -> Any:
    """Resolve a ``$name`` bind variable, or pass a literal through.

    An unbound ``$name`` returns ``None``, which every caller treats as "this
    predicate cannot be satisfied". Returning the literal string instead would
    compare a number against ``"$party_size"`` and produce a confident wrong
    answer — the failure mode this function exists to prevent.
    """
    if isinstance(raw, str) and raw.startswith("$"):
        value = bindings.get(raw[1:])
        return value if _numeric(value) else None
    return raw if _numeric(raw) else None


def _numeric(value: Any) -> bool:
    """True for a real number. ``bool`` is excluded deliberately.

    ``isinstance(True, int)`` is True in Python, so without this an attribute
    accidentally stored as a boolean would compare as 1 and satisfy a
    ``gte: 1`` predicate — a silently wrong match rather than a refusal.
    """
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)

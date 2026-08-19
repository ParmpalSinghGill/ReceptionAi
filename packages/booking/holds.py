"""Holds — place, extend, release. 01 §5.11 and §13.2-§13.3.

A hold is a **short-lived, TTL-bounded reservation of capacity** taken while the
agent is still talking to the caller. It has to do two contradictory-sounding
things at once: block competing bookings *and* vanish on its own if the caller
hangs up. Both are achieved without a background process being load-bearing.

**The TTL has three layers, and only the first is correctness** (01 §13.3):

1. **Read-time predicate — authoritative.** Every conflict check carries
   ``expires_at > now()``. An expired hold stops blocking the *moment* its TTL
   passes, with no sweeper involvement. This is what makes the system correct
   rather than merely tidy: a dead worker cannot make a resource permanently
   unbookable.
2. **Timer — timely.** The workflow that created the hold sets a durable timer
   and releases it explicitly when the caller abandons. This is the well-behaved
   path and the one that actually runs almost always.
3. **Sweeper — janitorial.** ``ops_expire_stale_holds()`` flips stragglers to
   ``expired``. Its purpose is *not* correctness — layer 1 already gives that. It
   exists so ``ex_booking_holds__no_overlap``'s partial index stays small (its
   predicate is ``status = 'active'`` and, being a partial index predicate, cannot
   reference ``now()``) and so admin views show accurate state.

**Why the advisory lock, not just the exclusion constraint.** PostgreSQL
exclusion constraints are single-table. ``booking_items`` and ``booking_holds``
each have one, so booking-vs-booking and hold-vs-hold are impossible — but
*hold-vs-booking* is not, and that is the collision that actually happens: one
caller is holding 3pm while another confirms it outright. 01 §13.2 closes it with
``pg_advisory_xact_lock`` keyed on ``(tenant_id, resource_id)``, taken inside
``booking_check_conflicts`` before either table is read. The lock provides
coordination; the constraints remain the backstop that provides correctness.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from packages.booking.domain import HoldRecord
from packages.core.errors import DomainError, ResourceConflictError

__all__ = [
    "DEFAULT_HOLD_TTL_SECONDS",
    "MAX_HOLD_TTL_SECONDS",
    "MIN_HOLD_TTL_SECONDS",
    "HoldExpiredError",
    "HoldManager",
]

log = structlog.get_logger(__name__)

#: How long a hold lives by default. Long enough for a caller to find their
#: diary and say yes; short enough that an abandoned call does not sit on a slot
#: through the next three enquiries. Manifests may narrow it via the booking
#: policy's ``hold_duration``; they may not exceed the ceiling below.
DEFAULT_HOLD_TTL_SECONDS = 600

#: The ceiling a manifest cannot raise. A tenant who sets a two-hour hold has
#: not configured generosity, they have configured an outage: one abandoned call
#: per resource per two hours is enough to make a diary look full while it is
#: empty. Fifteen minutes is past the point where a caller is still on the line.
MAX_HOLD_TTL_SECONDS = 900

#: The floor. Below about half a minute the hold expires while the agent is
#: still reading the time back, and the confirm fails for a slot nobody took.
MIN_HOLD_TTL_SECONDS = 30


class HoldExpiredError(DomainError):
    """The hold's TTL passed before it could be converted.

    A conflict (409, ``slot_unavailable``) rather than a not-found, because the
    caller's next action is the same as for any lost race: re-check availability
    and re-offer. It is a distinct class only so the agent can say "that time was
    held for you but the hold ran out" instead of "someone else took it", which
    is a different and less accurate thing to tell a person.
    """

    code = "slot_unavailable"
    http_status = 409


class HoldManager:
    """Place, extend and release holds through the advisory-locked path.

    Thin by design. Every operation that could race is a single call into the
    repository, which is a single call into a database function that holds the
    lock — so there is no window here for two coroutines to interleave, because
    there is no multi-step state machine here to interleave *in*.
    """

    def __init__(self, repository: object) -> None:
        """
        Args:
            repository: A ``BookingRepository``. Typed as ``object`` so this
                package does not import ``packages.persistence`` — the booking
                engine receives its I/O rather than reaching for it, which is
                what keeps it testable with an in-memory double and what keeps
                the dependency arrow pointing the right way.
        """
        self._repo = repository

    @staticmethod
    def clamp_ttl(requested: int | None) -> int:
        """Bring a requested TTL inside the platform's bounds.

        Clamped rather than rejected. A manifest asking for twenty minutes is
        expressing a real preference and should get fifteen, not an error at
        install time for a value that is merely optimistic. A *model* asking for
        twenty minutes is doing so because a caller stalled, and the same answer
        is right.
        """
        if requested is None:
            return DEFAULT_HOLD_TTL_SECONDS
        return max(MIN_HOLD_TTL_SECONDS, min(int(requested), MAX_HOLD_TTL_SECONDS))

    async def place(
        self,
        *,
        location_id: UUID,
        service_id: UUID,
        resource_id: UUID,
        start: datetime,
        end: datetime,
        ttl_seconds: int | None = None,
        quantity: int = 1,
        conversation_id: UUID | None = None,
    ) -> HoldRecord:
        """Take a hold, or raise ``ResourceConflictError`` if the slot is gone.

        The conflict check inside the repository takes the advisory lock and
        reads *both* tables, so "free when I looked" and "free when I wrote" are
        the same instant for this resource.
        """
        ttl = self.clamp_ttl(ttl_seconds)
        hold = await self._repo.place_hold(  # type: ignore[attr-defined]
            location_id=location_id,
            service_id=service_id,
            resource_id=resource_id,
            start=start,
            end=end,
            ttl_seconds=ttl,
            quantity=quantity,
            conversation_id=conversation_id,
        )
        log.info(
            "booking.hold.placed",
            hold_id=str(hold.id),
            resource_id=str(resource_id),
            ttl_seconds=ttl,
            expires_at=hold.expires_at.isoformat(),
        )
        return hold

    async def extend(self, hold_id: UUID, *, ttl_seconds: int | None = None) -> HoldRecord:
        """Push a live hold's TTL out.

        Only an active, unexpired hold may be extended — see
        ``BookingRepository.extend_hold`` for why reviving a lapsed one is worse
        than refusing.
        """
        ttl = self.clamp_ttl(ttl_seconds)
        hold = await self._repo.extend_hold(hold_id, ttl_seconds=ttl)  # type: ignore[attr-defined]
        log.info(
            "booking.hold.extended",
            hold_id=str(hold_id),
            expires_at=hold.expires_at.isoformat(),
        )
        return hold

    async def release(self, hold_id: UUID, *, reason: str = "released") -> bool:
        """Release a hold. Idempotent — returns False when it was already gone.

        Three independent things can release the same hold (the agent, the
        timer, the sweeper) and two of them losing the race is normal operation,
        not an error worth raising.
        """
        released = await self._repo.release_hold(hold_id, reason=reason)  # type: ignore[attr-defined]
        if released:
            log.info("booking.hold.released", hold_id=str(hold_id), reason=reason)
        return released

    async def require_live(self, hold_id: UUID, *, now: datetime | None = None) -> HoldRecord:
        """Load a hold and assert it can still be converted.

        Called before the work of building a booking, so an expired hold fails
        early with a message the agent can act on, rather than at the conversion
        step where the SQLSTATE would have to be interpreted. The database still
        re-checks in ``booking_convert_hold`` — this is the fast, friendly check,
        not the authoritative one.
        """
        now = now or datetime.now(tz=UTC)
        hold = await self._repo.get_hold(hold_id)  # type: ignore[attr-defined]
        if hold is None:
            raise HoldExpiredError(
                "That time is no longer being held. Let me check what is free.",
            )
        if hold.status == "converted":
            raise ResourceConflictError(
                "That hold has already been turned into a booking.",
                kind="duplicate",
                constraint="booking_holds.converted",
            )
        if not hold.is_live(now):
            raise HoldExpiredError(
                "That time was held but the hold has run out; it may have been "
                "taken. Let me re-check.",
            )
        return hold

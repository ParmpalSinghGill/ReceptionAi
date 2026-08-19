"""Book, cancel, reschedule, look up — the engine's public surface.

Every method here is written against one question: *what happens if this fails
halfway?* The answers are the design:

* **Book** takes a hold, converts it, and attaches the remaining allocations in
  one transaction. A failure at any point rolls the whole thing back, so a
  caller is never told "booked" for a row that does not exist and a slot is never
  left half-claimed.
* **Reschedule** moves the existing booking *in place* — new hold, move, release
  old hold — rather than cancelling and rebooking. Cancel-then-rebook has a
  window in which the original slot is free and the new one may be taken, and a
  caller who wanted to move from Tuesday to Wednesday can end up with neither.
  That is the single most damaging failure mode a booking system has, and the
  reason ``move`` is one UPDATE whose cascade trigger re-evaluates the exclusion
  constraint.
* **Cancel** quotes the policy first and applies it second, so the agent can tell
  the caller what it costs before it costs them anything.

The engine speaks resources and services throughout. Whether a resource is a
person, a room or a machine is a value in ``resource_types.kind``, and nothing
here branches on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from packages.booking.availability import AvailabilityEngine, SlotQuery
from packages.booking.domain import (
    BookingRecord,
    CancellationQuote,
    ResourceSpec,
    ServiceSpec,
    Slot,
)
from packages.booking.holds import HoldManager
from packages.core.errors import (
    EntityNotFoundError,
    ResourceConflictError,
    ValidationError,
)

__all__ = ["BookingService", "SlotUnavailableError", "quote_cancellation"]

log = structlog.get_logger(__name__)

#: How many alternatives to gather when a requested time is taken. Three is
#: enough to give a caller a real choice and few enough to read aloud on a phone
#: call without them losing track of the first one.
_ALTERNATIVE_COUNT = 3


class SlotUnavailableError(ResourceConflictError):
    """The requested time is taken — with alternatives attached.

    This is the shape PHASE1-API-CONTRACT's 409 renders: ``slot_unavailable``
    with the alternatives in the problem's extensions. Carrying them on the
    exception rather than making the caller re-query is deliberate — by the time
    this is raised the engine has already loaded the resources, windows and
    occupancy it needs to answer "what else?", and throwing that away so the
    handler can rebuild it would double the work at the exact moment the caller
    is waiting.
    """

    def __init__(
        self,
        message: str,
        *,
        alternatives: tuple[Slot, ...] = (),
        timezone: str = "UTC",
        constraint: str | None = None,
    ) -> None:
        super().__init__(
            message,
            kind="overlap",
            constraint=constraint,
            alternatives=[
                {
                    "start_at": slot.start_at.isoformat(),
                    "end_at": slot.end_at.isoformat(),
                    "local_start": slot.start_at.astimezone(
                        ZoneInfo(timezone)
                    ).isoformat(),
                }
                for slot in alternatives
            ]
            or None,
        )
        self.alternatives = alternatives
        self.timezone = timezone


def quote_cancellation(
    booking: BookingRecord, *, now: datetime | None = None
) -> CancellationQuote:
    """Evaluate the cancellation policy this booking was created under.

    Reads the values **copied onto the booking**, never the service's current
    ones. A practice that raises its late-cancellation fee on Monday must not
    thereby raise it for an appointment agreed on Friday — the policy is a
    promise made at a moment, and copy-on-write is how the schema records which
    promise was made (01 §5.9).

    "Inside the notice period" means *too late*: the caller is within
    ``cancellation_window_minutes`` of the start, so the fee applies. The naming
    is the one place this reads backwards in English, so it is spelled out on the
    field rather than left to the reader.
    """
    now = now or datetime.now(tz=UTC)
    minutes_until = (booking.start_at - now).total_seconds() / 60.0
    required = booking.cancellation_window_minutes
    too_late = minutes_until < required

    if booking.status == "cancelled":
        return CancellationQuote(
            allowed=False,
            within_notice_period=too_late,
            notice_required_minutes=required,
            notice_actual_minutes=int(minutes_until),
            fee=None,
            currency=booking.currency,
            reason="already_cancelled",
        )

    if booking.status in {"completed", "no_show", "checked_in", "in_progress"}:
        return CancellationQuote(
            allowed=False,
            within_notice_period=too_late,
            notice_required_minutes=required,
            notice_actual_minutes=int(minutes_until),
            fee=None,
            currency=booking.currency,
            reason=f"status_{booking.status}",
        )

    # A booking whose start has passed is not cancelled, it is missed. Allowing a
    # cancellation there would let a no-show be reclassified after the fact,
    # which is precisely what the fee exists to discourage.
    if minutes_until < 0:
        return CancellationQuote(
            allowed=False,
            within_notice_period=True,
            notice_required_minutes=required,
            notice_actual_minutes=int(minutes_until),
            fee=booking.cancellation_fee,
            currency=booking.currency,
            reason="already_started",
        )

    fee: Decimal | None = booking.cancellation_fee if too_late else None
    return CancellationQuote(
        allowed=True,
        within_notice_period=too_late,
        notice_required_minutes=required,
        notice_actual_minutes=int(minutes_until),
        fee=fee,
        currency=booking.currency if fee is not None else None,
        reason="inside_notice_period" if too_late else "outside_notice_period",
    )


class BookingService:
    """Orchestrates availability, holds and writes over one transaction."""

    def __init__(self, repository: Any, *, engine: AvailabilityEngine | None = None) -> None:
        """
        Args:
            repository: A ``BookingRepository`` bound to a tenant-scoped session.
                Typed loosely so this package does not import
                ``packages.persistence`` — the engine is handed its I/O.
        """
        self._repo = repository
        self._engine = engine or AvailabilityEngine()
        self._holds = HoldManager(repository)

    @property
    def holds(self) -> HoldManager:
        return self._holds

    @property
    def repository(self) -> Any:
        """The bound repository.

        Exposed because callers legitimately need the catalogue reads it already
        holds — a tool resolving the default location should not open a second
        object to do it. Read access to a collaborator this service was handed;
        not an invitation to write through it.
        """
        return self._repo

    # ── Availability ──────────────────────────────────────────────────────

    async def find_slots(
        self,
        *,
        service_code: str,
        window_start: datetime,
        window_end: datetime,
        party_size: int = 1,
        location_id: UUID | None = None,
        resource_ids: frozenset[UUID] | None = None,
        limit: int = 10,
        now: datetime | None = None,
    ) -> tuple[list[Slot], ServiceSpec, UUID, str, dict[UUID, ResourceSpec]]:
        """Free slots for a service in a window.

        Returns the slots plus the resolved service, location, timezone, and the
        resources that were considered — every caller needs all of them, and
        re-resolving is several extra round-trips for values this method already
        holds.

        The resource map is what lets a caller say **who** as well as when. A
        :class:`Slot` carries resource *ids*; rendering "with Dr Okafor, who
        covers our same-day emergencies" needs the row. Returning the map the
        solver already built is strictly cheaper than looking the ids back up,
        and it keeps this method the single place that knows which resources were
        in play.

        Note what this does NOT do: interpret those attributes. The engine hands
        back generic resources with whatever attributes a tenant configured;
        deciding that ``scope`` means a speciality worth mentioning is the
        conversation layer's job, and keeping that out of here is invariant I4.
        """
        location = location_id or await self._repo.default_location_id()
        service = await self._repo.service_by_code(service_code, location_id=location)
        if service is None:
            raise EntityNotFoundError(
                entity_type="services",
                entity_id=service_code,
                message=f"No bookable service with code {service_code!r}.",
            )

        tz = await self._repo.location_timezone(location)
        zone = ZoneInfo(tz)
        from_date = window_start.astimezone(zone).date()
        to_date = window_end.astimezone(zone).date()

        resources = await self._repo.resources_for_service(service, location_id=location)
        by_id = {r.id: r for r in resources}
        if not resources:
            return [], service, location, tz, by_id

        open_windows = await self._repo.open_windows(
            location_id=location,
            service_id=service.id,
            tz=tz,
            from_date=from_date,
            to_date=to_date,
        )
        closed_windows = await self._repo.closed_windows(
            location_id=location,
            service_id=service.id,
            tz=tz,
            from_date=from_date,
            to_date=to_date,
        )
        # The busy window is widened by the buffers so an appointment whose
        # *blocking* range reaches into the search window is seen, even though
        # its service range does not. Without the widening a slot butting up
        # against a buffered booking would be offered and then rejected by the
        # exclusion constraint — the solver would be lying by omission.
        pad = timedelta(
            minutes=max(service.buffer_before_minutes, service.buffer_after_minutes)
        )
        busy = await self._repo.busy(
            resource_ids=[r.id for r in resources],
            window_start=window_start - pad,
            window_end=window_end + pad,
        )

        slots = self._engine.solve(
            SlotQuery(
                service=service,
                window_start=window_start,
                window_end=window_end,
                timezone=tz,
                party_size=party_size,
                resource_ids=resource_ids,
                limit=limit,
            ),
            resources=resources,
            open_windows=open_windows,
            closed_windows=closed_windows,
            busy=busy,
            now=now,
        )
        return slots, service, location, tz, by_id

    # ── Booking ───────────────────────────────────────────────────────────

    async def book(
        self,
        *,
        service_code: str,
        start_at: datetime,
        customer_name: str,
        customer_phone_e164: str | None = None,
        customer_email: str | None = None,
        party_size: int = 1,
        intake_data: dict[str, Any] | None = None,
        notes: str | None = None,
        location_id: UUID | None = None,
        conversation_id: UUID | None = None,
        channel: str | None = None,
        idempotency_key: str | None = None,
        hold_id: UUID | None = None,
        now: datetime | None = None,
    ) -> BookingRecord:
        """Create a confirmed booking at an exact start time.

        The flow, and why it is this order:

        1. **Solve for that one instant.** Not "is anything free that day" — the
           caller named a time and the answer must be about that time. Solving
           also produces the *allocation*, so the resources are chosen by the same
           strategy that would have chosen them for a browsed slot.
        2. **Nothing free → raise with alternatives.** The slots already gathered
           are reused, so the 409 carries somewhere else to go rather than a dead
           end.
        3. **Insert the booking, then its items.** The items are where 23P01
           fires. Because they are inserted after the aggregate and in the same
           transaction, a conflict rolls back the whole booking rather than
           leaving an orphan with no resources.

        ``hold_id``, when supplied, converts an existing hold for the primary
        allocation instead of racing for it — which is the path a real
        conversation takes: hold while the caller decides, convert when they say
        yes.
        """
        now = now or datetime.now(tz=UTC)
        window_end = start_at + timedelta(hours=1)

        slots, service, location, tz, _resources = await self.find_slots(
            service_code=service_code,
            window_start=start_at,
            window_end=max(window_end, start_at + timedelta(days=1)),
            party_size=party_size,
            location_id=location_id,
            limit=_ALTERNATIVE_COUNT + 1,
            now=now,
        )

        exact = next((s for s in slots if s.start_at == start_at), None)
        if exact is None:
            raise SlotUnavailableError(
                "That time is not available.",
                alternatives=tuple(slots[:_ALTERNATIVE_COUNT]),
                timezone=tz,
                constraint="availability",
            )

        self._validate_intake(service, intake_data or {})

        zone = ZoneInfo(tz)
        booking_id = await self._repo.create_booking(
            location_id=location,
            service=service,
            start=exact.start_at,
            end=exact.end_at,
            local_date=exact.start_at.astimezone(zone).date(),
            party_size=party_size,
            customer_name=customer_name,
            customer_phone_e164=customer_phone_e164,
            customer_email=customer_email,
            intake_data=intake_data or {},
            notes=notes,
            conversation_id=conversation_id,
            channel=channel,
            idempotency_key=idempotency_key,
        )

        # An idempotent replay returns the original booking untouched: its items
        # already exist and re-attaching them would collide with the very
        # constraint that makes the booking safe.
        existing = await self._repo.get_booking(booking_id)
        if existing is not None and existing.resource_names:
            return existing

        await self._attach_allocations(
            booking_id=booking_id, slot=exact, hold_id=hold_id, timezone=tz
        )

        booking = await self._repo.get_booking(booking_id)
        if booking is None:  # pragma: no cover - just written in this transaction
            raise EntityNotFoundError(entity_type="bookings", entity_id=booking_id)

        log.info(
            "booking.created",
            booking_id=str(booking.id),
            reference=booking.reference,
            service=service.code,
            start_at=booking.start_at.isoformat(),
        )
        return booking

    async def _attach_allocations(
        self,
        *,
        booking_id: UUID,
        slot: Slot,
        hold_id: UUID | None,
        timezone: str,
    ) -> None:
        """Write one ``booking_items`` row per allocated resource.

        When a hold exists it is converted for the resource it covers — and the
        conversion releases it in the same transaction as the insert, so there is
        never an instant where the slot is protected by neither (01 §13.3). Every
        other allocation is inserted directly and relies on the exclusion
        constraint.
        """
        converted_resource: UUID | None = None
        if hold_id is not None:
            hold = await self._holds.require_live(hold_id)
            primary = next(
                (
                    a
                    for a in slot.allocations
                    if hold.resource_id in a.resource_ids
                ),
                None,
            )
            try:
                await self._repo.convert_hold(
                    hold_id=hold_id,
                    booking_id=booking_id,
                    role=primary.role if primary else "primary",
                    requirement_id=primary.requirement_id if primary else None,
                )
            except ResourceConflictError as exc:
                raise SlotUnavailableError(
                    "That time was taken while we were talking.",
                    timezone=timezone,
                    constraint=exc.constraint,
                ) from exc
            converted_resource = hold.resource_id

        for allocation in slot.allocations:
            for resource_id in allocation.resource_ids:
                if resource_id == converted_resource:
                    continue  # already attached by the hold conversion
                try:
                    await self._repo.add_item(
                        booking_id=booking_id,
                        resource_id=resource_id,
                        role=allocation.role,
                        requirement_id=allocation.requirement_id,
                        quantity=allocation.quantity,
                    )
                except ResourceConflictError as exc:
                    if exc.kind != "overlap":
                        raise
                    # The slot was free when we solved and is not now. Re-raise
                    # as the conflict shape the API renders, so the caller gets
                    # a 409 with a recoverable message rather than a 500.
                    raise SlotUnavailableError(
                        "That time was taken while we were booking it.",
                        timezone=timezone,
                        constraint=exc.constraint,
                    ) from exc

    @staticmethod
    def _validate_intake(service: ServiceSpec, intake: dict[str, Any]) -> None:
        """Enforce ``services.intake_schema``'s required fields.

        Checked here rather than only in the tool schema because the schema
        describes what the *model* may send, and this describes what the
        *practice* requires — they are different lists, and the practice's is the
        one a booking is invalid without. A missing field is a 422 the agent can
        recover from by asking one more question.
        """
        missing = [
            field["key"]
            for field in service.intake_schema
            if field.get("required") and not intake.get(field.get("key", ""))
        ]
        if missing:
            raise ValidationError(
                f"This booking still needs: {', '.join(missing)}.",
                field="intake_data",
                missing_fields=missing,
            )

    # ── Reschedule ────────────────────────────────────────────────────────

    async def reschedule(
        self,
        *,
        booking_id: UUID,
        new_start_at: datetime,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> BookingRecord:
        """Move a booking to a new time, atomically.

        **Never cancel-then-rebook.** That sequence has a window — however
        short — in which the original slot is released and the new one has not
        been claimed. If the new one is taken in that window, the caller ends up
        with nothing, having started the call with something. It is the worst
        outcome the system can produce and it is entirely avoidable.

        Instead: solve for the new time, then issue one ``UPDATE`` on
        ``bookings.service_period``. ``trg_bookings_cascade_period`` carries the
        new geometry onto every child item, where
        ``ex_booking_items__no_overlap`` re-evaluates it. The update either
        commits — booking moved, old slot released, new slot held, all at one
        instant — or raises 23P01 and rolls back, leaving the **original booking
        completely intact**. There is no third outcome.

        The exclusion constraint's ``DEFERRABLE INITIALLY IMMEDIATE`` matters
        here: immediate checking means the conflict surfaces on this statement,
        with the offending row in context, rather than at COMMIT where the caller
        cannot tell which of several writes failed.
        """
        now = now or datetime.now(tz=UTC)
        booking = await self._repo.get_booking(booking_id)
        if booking is None:
            raise EntityNotFoundError(entity_type="bookings", entity_id=booking_id)
        if booking.status in {"cancelled", "completed", "no_show"}:
            raise ValidationError(
                f"That appointment is {booking.status} and cannot be moved.",
                field="booking_id",
            )

        service = await self._repo.service_by_id(booking.service_id)
        if service is None:  # pragma: no cover - FK guarantees it exists
            raise EntityNotFoundError(
                entity_type="services", entity_id=booking.service_id
            )

        tz = booking.timezone
        zone = ZoneInfo(tz)
        duration = booking.end_at - booking.start_at
        new_end = new_start_at + duration

        # Solve the new time *excluding this booking's own items*, or it would
        # conflict with itself when moving by less than its own duration —
        # "shift my 3pm to 3:15" would report the 3pm as an obstacle.
        resources = await self._repo.resources_for_service(
            service, location_id=booking.location_id
        )
        pad = timedelta(
            minutes=max(service.buffer_before_minutes, service.buffer_after_minutes)
        )
        busy = await self._repo.busy(
            resource_ids=[r.id for r in resources],
            window_start=new_start_at - pad,
            window_end=new_end + pad,
            exclude_booking_id=booking_id,
        )
        from_date = new_start_at.astimezone(zone).date()
        to_date = new_end.astimezone(zone).date()
        open_windows = await self._repo.open_windows(
            location_id=booking.location_id,
            service_id=service.id,
            tz=tz,
            from_date=from_date,
            to_date=to_date,
        )
        closed_windows = await self._repo.closed_windows(
            location_id=booking.location_id,
            service_id=service.id,
            tz=tz,
            from_date=from_date,
            to_date=to_date,
        )

        slots = self._engine.solve(
            SlotQuery(
                service=service,
                window_start=new_start_at,
                window_end=new_end,
                timezone=tz,
                party_size=booking.party_size,
                limit=1,
            ),
            resources=resources,
            open_windows=open_windows,
            closed_windows=closed_windows,
            busy=busy,
            now=now,
        )
        if not slots or slots[0].start_at != new_start_at:
            alternatives, _, _, _, _ = await self.find_slots(
                service_code=service.code,
                window_start=new_start_at,
                window_end=new_start_at + timedelta(days=7),
                party_size=booking.party_size,
                location_id=booking.location_id,
                limit=_ALTERNATIVE_COUNT,
                now=now,
            )
            raise SlotUnavailableError(
                "That new time is not available; the existing appointment is "
                "unchanged.",
                alternatives=tuple(alternatives),
                timezone=tz,
                constraint="availability",
            )

        try:
            await self._repo.move_booking(
                booking_id=booking_id,
                start=new_start_at,
                end=new_end,
                local_date=new_start_at.astimezone(zone).date(),
            )
        except ResourceConflictError as exc:
            # The constraint rejected the move. The transaction is rolled back to
            # this statement's savepoint by the caller's error handling and the
            # ORIGINAL booking is untouched — which is the property the whole
            # move-in-place design exists to guarantee.
            raise SlotUnavailableError(
                "That time was taken a moment ago; your existing appointment is "
                "unchanged.",
                timezone=tz,
                constraint=exc.constraint,
            ) from exc

        moved = await self._repo.get_booking(booking_id)
        if moved is None:  # pragma: no cover
            raise EntityNotFoundError(entity_type="bookings", entity_id=booking_id)

        log.info(
            "booking.rescheduled",
            booking_id=str(booking_id),
            reference=moved.reference,
            was=booking.start_at.isoformat(),
            now=moved.start_at.isoformat(),
            reason=reason,
        )
        return moved

    # ── Cancel ────────────────────────────────────────────────────────────

    async def cancel(
        self,
        *,
        booking_id: UUID,
        reason: str | None = None,
        cancelled_by: str = "agent",
        accept_fee: bool = False,
        now: datetime | None = None,
    ) -> tuple[BookingRecord, CancellationQuote]:
        """Cancel, enforcing the policy the booking was created under.

        ``accept_fee`` is the consent gate, and it is not ceremony. A late
        cancellation costs the caller money; performing it because a model
        inferred they wanted to, without them having heard the amount, is the
        difference between a service and a complaint. When a fee applies and
        consent is absent this raises with the quote attached, so the agent's
        next utterance is "there's a £30 late-cancellation fee — shall I go
        ahead?" rather than an apology afterwards.
        """
        now = now or datetime.now(tz=UTC)
        booking = await self._repo.get_booking(booking_id)
        if booking is None:
            raise EntityNotFoundError(entity_type="bookings", entity_id=booking_id)

        quote = quote_cancellation(booking, now=now)
        if not quote.allowed:
            raise ValidationError(
                _refusal_message(quote, booking),
                field="booking_id",
                cancellation_reason=quote.reason,
            )

        if quote.fee_applies and not accept_fee:
            raise ValidationError(
                f"Cancelling less than {_hours(quote.notice_required_minutes)} "
                f"before the appointment incurs a "
                f"{_money(quote.fee, quote.currency)} fee. Confirm with the "
                f"customer before cancelling.",
                field="accept_fee",
                cancellation_fee=str(quote.fee),
                currency=quote.currency,
                notice_required_minutes=quote.notice_required_minutes,
            )

        cancelled = await self._repo.cancel_booking(
            booking_id=booking_id,
            reason=reason,
            cancelled_by=cancelled_by,
            fee_charged=quote.fee if quote.fee_applies else None,
        )
        if not cancelled:
            # Lost a race with another cancel. Not an error: the caller's intent
            # is satisfied, and reporting failure would prompt a retry loop for
            # something already done.
            log.info("booking.cancel.already_cancelled", booking_id=str(booking_id))

        record = await self._repo.get_booking(booking_id)
        if record is None:  # pragma: no cover
            raise EntityNotFoundError(entity_type="bookings", entity_id=booking_id)

        log.info(
            "booking.cancelled",
            booking_id=str(booking_id),
            reference=record.reference,
            fee_charged=str(quote.fee) if quote.fee_applies else None,
            within_notice=quote.within_notice_period,
        )
        return record, quote

    # ── Lookup ────────────────────────────────────────────────────────────

    async def existing_for_phone(
        self,
        *,
        phone_e164: str,
        within_days: int = 3,
        now: datetime | None = None,
    ) -> list[BookingRecord]:
        """Live appointments this number already holds in the next few days.

        **Information, never a refusal.** The caller may legitimately want two —
        a broken tooth on Tuesday and a scheduled hygienist visit on Thursday is
        normal, and blocking that is worse than the duplicate it prevents. So
        this answers "what do they already have?" and leaves the decision to the
        conversation.

        The gap it closes: nothing looked at whether a caller already had an
        appointment, so one phone number accumulated thirty-five confirmed
        bookings, including pairs at the *same instant* with different
        clinicians. The exclusion constraint cannot see that — it protects a
        resource's timeline, and two clinicians at 11:00 is not a resource
        conflict. A customer-level duplicate is a different question and needs a
        different check.

        Matched on the **normalised E.164 string**, so a caller who gives their
        number differently the second time still matches the first booking. That
        normalisation happens at the tool boundary; by here it has already been
        done.

        ``within_days = 0`` disables the check, which is the correct setting for
        a business where repeat bookings are the norm.
        """
        if within_days <= 0 or not phone_e164:
            return []

        now = now or datetime.now(tz=UTC)
        horizon = now + timedelta(days=within_days)
        found = await self._repo.bookings_by_phone(phone_e164, limit=10)
        return [
            booking
            for booking in found
            if booking.status not in ("cancelled", "no_show", "completed")
            and booking.start_at <= horizon
        ]

    async def lookup(
        self,
        *,
        reference: str | None = None,
        booking_id: UUID | None = None,
        phone_e164: str | None = None,
        conversation_id: UUID | None = None,
        limit: int = 5,
    ) -> list[BookingRecord]:
        """Find bookings by whatever the caller can actually supply.

        Tried in order of precision. A reference identifies exactly one booking
        and is checked first; a phone number identifies a person and may return
        several; a conversation id catches "the one we just made" without the
        caller repeating a reference they heard thirty seconds ago.

        Returns a list even for a reference lookup, so the caller has one shape
        to handle. An empty list is a legitimate answer — "I can't find that" is
        information, not a failure.
        """
        if booking_id is not None:
            found = await self._repo.get_booking(booking_id)
            return [found] if found else []
        if reference:
            found = await self._repo.booking_by_reference(reference)
            return [found] if found else []
        if phone_e164:
            return await self._repo.bookings_by_phone(phone_e164, limit=limit)
        if conversation_id is not None:
            return await self._repo.bookings_for_conversation(
                conversation_id, limit=limit
            )
        raise ValidationError(
            "Give a reference, a booking id, a phone number, or a conversation.",
            field="reference",
        )


def _refusal_message(quote: CancellationQuote, booking: BookingRecord) -> str:
    match quote.reason:
        case "already_cancelled":
            return f"{booking.reference} is already cancelled."
        case "already_started":
            return (
                f"{booking.reference} has already started or passed; it cannot "
                f"be cancelled."
            )
        case _:
            return (
                f"{booking.reference} is {booking.status} and cannot be cancelled."
            )


def _hours(minutes: int) -> str:
    if minutes % 60 == 0 and minutes >= 60:
        count = minutes // 60
        return f"{count} hour{'s' if count != 1 else ''}"
    return f"{minutes} minutes"


def _money(amount: Decimal | None, currency: str | None) -> str:
    """Render money as a decimal string with its currency.

    Never a float (contract §6) and never a bare number: an amount without a
    currency is how a £30 fee gets read aloud as $30.
    """
    if amount is None:
        return "no"
    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency or "", "")
    return f"{symbol}{amount:.2f}" if symbol else f"{amount:.2f} {currency or ''}".strip()

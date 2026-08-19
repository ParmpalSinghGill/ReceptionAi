"""Free-slot computation — 01 §5.6's materialiser, 02 §5.2's solver.

The pipeline, in the order it runs:

    rules ──expand over dates in the location tz──► open windows (UTC)
      - exceptions
      - holidays (expanded through the same tz)
      = the open envelope
    ──enumerate candidate starts on the granularity grid──►
    ──for each start, satisfy every requirement──►
      - existing booking_items on the chosen resources
      - live holds on the chosen resources
    = slots

**Why availability is computed and not stored.** The obvious alternative — a
``slots`` table with one row per bookable interval per resource — is how many
booking systems start and why they later rewrite. For 200 resources * 90 days *
15-minute slots that is 17 million rows *per tenant*, every one of which must be
regenerated when a rule, a holiday, or an exception changes, and every one of
which is wrong the moment the horizon rolls forward (01 §5.6). The database
stores *intent*; this module derives the facts.

**Timezone correctness is the reason this module is careful rather than short.**
Rules are stored as local wall-clock time plus a weekday mask, because "we open
at 9 am" must mean 09:00 in March and 09:00 in November — a stored UTC offset
would be wrong for half the year. Expansion therefore walks *local calendar
dates*, builds a local naive datetime for each, and only then attaches the zone.
Doing it the other way round — iterating UTC instants and converting — puts the
DST discontinuity inside the loop, where it produces a day with 23 or 25 hours of
slots and nobody notices until a caller is offered 02:30 on the spring-forward
Sunday.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from packages.booking.domain import (
    AllocationCandidate,
    ClosedWindow,
    OpenWindow,
    RequirementSpec,
    ResourceSpec,
    ServiceSpec,
    Slot,
)

__all__ = ["AvailabilityEngine", "Busy", "SlotQuery", "expand_rule"]

log = structlog.get_logger(__name__)

#: Default grid when a resource type declares no ``slot_granularity_minutes``.
#: Fifteen minutes rather than "the service duration" because a 40-minute
#: service on a 40-minute grid can only ever start at 09:00, 09:40, 10:20 — which
#: drifts off every human-friendly time by the third slot and is why free-floating
#: types exist (``slot_granularity_minutes IS NULL`` means "any minute", and the
#: caller supplies the grid it wants to offer).
_DEFAULT_GRID_MINUTES = 15

#: A hard ceiling on enumerated starts per query. A 90-day window on a 1-minute
#: grid is 129,600 candidate starts, each of which would probe every resource.
#: The agent asks for a day or two at a time; anything approaching this is a bug
#: or an attack, and truncating with a warning beats melting a worker.
_MAX_CANDIDATE_STARTS = 20_000


@dataclass(frozen=True, slots=True)
class Busy:
    """One occupied interval on one resource.

    Bookings and live holds both reduce to this before subtraction, so the solver
    has one code path for "already taken" rather than two that can disagree —
    which is the same reasoning that makes ``booking_holds.occupies`` share a
    column name and semantics with ``booking_items.occupies`` (01 §5.11).
    """

    resource_id: UUID
    start: datetime
    end: datetime
    quantity: int = 1
    kind: str = "booking"
    #: Which booking this occupancy belongs to, when it is one.
    #:
    #: Needed to count *appointments* rather than rows for the site-wide
    #: capacity check: one appointment consuming a clinician and a room is one
    #: appointment, and counting items would make a two-resource service look
    #: like two. ``None`` for a hold, which has no booking yet.
    booking_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SlotQuery:
    """One availability question.

    ``window_start`` / ``window_end`` bound the *search*, not the slots: a slot
    is returned when it starts at or after ``window_start`` and *ends* at or
    before ``window_end``, so a 30-minute service asked about a 30-minute window
    gets exactly one answer rather than an offer it cannot honour.
    """

    service: ServiceSpec
    window_start: datetime
    window_end: datetime
    timezone: str
    party_size: int = 1
    duration_units: int = 1
    #: Restrict to these resources. Used by reschedule, which prefers to keep the
    #: caller with the same person, and by "is Tuesday 3pm still free".
    resource_ids: frozenset[UUID] | None = None
    #: Cap on returned slots, so a wide window does not return a thousand times.
    limit: int = 20
    #: Grid override. ``None`` = the resource type's own granularity.
    granularity_minutes: int | None = None


def expand_rule(
    *,
    weekdays: Sequence[int],
    start_time: time,
    end_time: time,
    effective_from: date | None,
    effective_until: date | None,
    tz: ZoneInfo,
    from_date: date,
    to_date: date,
    resource_id: UUID | None = None,
    capacity_override: int | None = None,
    priority: int = 0,
) -> list[OpenWindow]:
    """Turn one recurring rule into concrete UTC intervals over a date range.

    Args:
        weekdays: ISO-8601 numbering, 1=Monday … 7=Sunday, as stored.
        start_time, end_time: local wall-clock. ``end_time <= start_time`` means
            the window crosses midnight (a bar open 18:00-02:00), which is
            handled explicitly rather than producing an empty or inverted range.

    The DST handling is the point of this function, and there are two distinct
    hazards:

    * **The nonexistent local time.** On the spring-forward Sunday, 01:30 local
      does not exist. ``ZoneInfo`` resolves it by shifting forward rather than
      raising, which is the behaviour a business wants — a 01:30 opening on that
      one day becomes 03:00, the shop opens when the clocks say it does.
    * **The ambiguous local time.** On the fall-back Sunday, 01:30 happens twice.
      ``fold=0`` selects the first occurrence, so a window that spans the repeat
      is 25 hours of real time — again, what the business experiences.

    Neither is a special case in the code below, because building a *naive local*
    datetime and attaching the zone is exactly the operation whose semantics
    ``ZoneInfo`` defines. Arithmetic in UTC and conversion afterwards would give
    a different, wrong answer, and would do so silently on two Sundays a year.
    """
    wanted = {int(d) for d in weekdays}
    windows: list[OpenWindow] = []
    crosses_midnight = end_time <= start_time

    cursor = from_date
    # One day of slack on each side: a window that crosses midnight belongs to
    # the local date it *starts* on, so the last day before `from_date` can still
    # contribute an interval inside the search range.
    cursor -= timedelta(days=1)
    last = to_date + timedelta(days=1)

    while cursor <= last:
        if cursor.isoweekday() not in wanted:
            cursor += timedelta(days=1)
            continue
        if effective_from is not None and cursor < effective_from:
            cursor += timedelta(days=1)
            continue
        if effective_until is not None and cursor >= effective_until:
            break

        local_start = datetime.combine(cursor, start_time, tzinfo=tz)
        end_date = cursor + timedelta(days=1) if crosses_midnight else cursor
        local_end = datetime.combine(end_date, end_time, tzinfo=tz)

        windows.append(
            OpenWindow(
                start=local_start.astimezone(UTC),
                end=local_end.astimezone(UTC),
                resource_id=resource_id,
                capacity_override=capacity_override,
                priority=priority,
                local_date=cursor,
            )
        )
        cursor += timedelta(days=1)

    return windows


def expand_holiday(
    *,
    holiday_date: date,
    start_time: time | None,
    end_time: time | None,
    tz: ZoneInfo,
    reason: str | None = None,
) -> ClosedWindow:
    """One holiday row → the concrete UTC interval it closes.

    ``NULL``/``NULL`` times mean the whole local day, which is *not* always 24
    hours: on a DST transition it is 23 or 25. Taking local midnight to local
    midnight of the following day, then converting, gets that right; adding
    ``timedelta(hours=24)`` to a UTC instant would leave an hour of the day
    bookable twice a year, on exactly the days a practice is most likely to be
    shut.
    """
    if start_time is None or end_time is None:
        local_start = datetime.combine(holiday_date, time(0, 0), tzinfo=tz)
        local_end = datetime.combine(
            holiday_date + timedelta(days=1), time(0, 0), tzinfo=tz
        )
    else:
        local_start = datetime.combine(holiday_date, start_time, tzinfo=tz)
        local_end = datetime.combine(holiday_date, end_time, tzinfo=tz)

    return ClosedWindow(
        start=local_start.astimezone(UTC),
        end=local_end.astimezone(UTC),
        reason=reason,
    )


class AvailabilityEngine:
    """Pure slot computation. No I/O — the caller supplies the inputs.

    Keeping this side-effect-free is what makes the DST cases, the half-open
    boundary case, and the multi-resource allocation case testable without a
    database, a clock, or a tenant. The repository that loads rules, exceptions,
    holidays, bookings and holds lives in ``packages/persistence``; this decides
    what they *mean*.
    """

    def solve(
        self,
        query: SlotQuery,
        *,
        resources: Sequence[ResourceSpec],
        open_windows: Sequence[OpenWindow],
        closed_windows: Sequence[ClosedWindow] = (),
        busy: Sequence[Busy] = (),
        now: datetime | None = None,
    ) -> list[Slot]:
        """Compute bookable starts.

        Returns at most ``query.limit`` slots, earliest first. An empty list is a
        legitimate answer and the caller must render it as "nothing available in
        that window", never as an error — which is why this returns a list rather
        than raising when nothing fits.
        """
        now = now or datetime.now(tz=UTC)
        service = query.service

        # ── Lead time and horizon (01 §5.4) ───────────────────────────────
        # A practice that needs two hours' notice must not be offered a slot in
        # ninety minutes, and one that books ninety days out must not be offered
        # next year. Both are clamps on the search window rather than filters on
        # the results, so the grid is never walked over a range that cannot
        # produce anything.
        earliest = max(
            query.window_start, now + timedelta(minutes=service.lead_time_minutes)
        )
        latest = min(
            query.window_end, now + timedelta(days=service.booking_horizon_days)
        )
        if earliest >= latest:
            return []

        if not (service.min_party_size <= query.party_size <= service.max_party_size):
            return []

        duration = service.duration
        bindings = {
            "party_size": query.party_size,
            "duration_units": query.duration_units,
        }

        by_resource = {r.id: r for r in resources}
        candidates = self._enumerate_starts(
            query=query,
            open_windows=open_windows,
            earliest=earliest,
            latest=latest,
            duration=duration,
            resources=resources,
        )

        slots: list[Slot] = []
        for start, _local_date in candidates:
            end = start + duration
            if end > latest:
                continue

            allocations = self._allocate(
                service=service,
                start=start,
                end=end,
                bindings=bindings,
                by_resource=by_resource,
                open_windows=open_windows,
                closed_windows=closed_windows,
                busy=busy,
                party_size=query.party_size,
                restrict_to=query.resource_ids,
            )
            if allocations is None:
                continue

            # ── Location-wide concurrency (01 §5.6's capacity_override) ───
            #
            # Distinct from the per-resource check in `_has_capacity`, which
            # asks "is THIS clinician free?". This asks "how many appointments
            # does the practice run at once?" — the restaurant that seats forty,
            # or the single-surgery practice that sees one patient at a time
            # however many clinicians are on the rota.
            #
            # It belongs here rather than at booking time because a slot that
            # cannot be booked must not be OFFERED. Rejecting at write time
            # would mean the caller picks a time, hears yes, and is then told no.
            if not self._within_location_capacity(
                start=start, end=end, open_windows=open_windows, busy=busy
            ):
                continue

            slots.append(
                Slot(
                    start_at=start,
                    end_at=end,
                    allocations=allocations,
                    price=service.price,
                    currency=service.currency,
                )
            )
            if len(slots) >= query.limit:
                break

        return slots

    @staticmethod
    def _within_location_capacity(
        *,
        start: datetime,
        end: datetime,
        open_windows: Sequence[OpenWindow],
        busy: Sequence[Busy],
    ) -> bool:
        """Does the practice have room for one more appointment at this instant?

        ``capacity_override`` on the governing window is the cap: ``1`` means one
        appointment at a time across the whole site whatever the rota, ``4``
        means four. ``None`` — the default — means no site-wide limit, and only
        the per-resource rules apply, which is what every existing tenant gets.

        Counted over **distinct bookings**, not booking items: one appointment
        consuming a clinician and a room is one appointment, and counting rows
        would make a two-resource service look like two.

        The governing window is the tightest cap covering this interval. A
        location-level window (``resource_id is None``) is the site's own hours,
        which is where a site-wide cap naturally lives; a resource-level one
        overriding it downward is honoured too, since the tighter constraint is
        always the binding one.
        """
        caps = [
            window.capacity_override
            for window in open_windows
            if window.capacity_override is not None
            and window.start <= start
            and end <= window.end
        ]
        if not caps:
            return True

        concurrent = {
            occupied.booking_id
            for occupied in busy
            if occupied.booking_id is not None
            and start < occupied.end
            and occupied.start < end
        }
        return len(concurrent) < min(caps)

    # ── Step 1: candidate starts ──────────────────────────────────────────

    def _enumerate_starts(
        self,
        *,
        query: SlotQuery,
        open_windows: Sequence[OpenWindow],
        earliest: datetime,
        latest: datetime,
        duration: timedelta,
        resources: Sequence[ResourceSpec],
    ) -> list[tuple[datetime, date | None]]:
        """Walk the grid inside every open window.

        Starts are aligned to the grid **from the window's own start**, not from
        midnight UTC. That distinction matters: a practice opening at 08:00 local
        on a 15-minute grid should offer 08:00, 08:15, 08:30 — and if the grid
        were anchored to UTC midnight, a location on a half-hour offset (India,
        Newfoundland, parts of Australia) would be offered 08:07, 08:22, 08:37.
        Anchoring to the window start makes the offered times the ones the
        business actually says out loud, in every timezone.
        """
        grid = query.granularity_minutes or self._grid_for(resources)
        step = timedelta(minutes=max(1, grid))

        out: list[tuple[datetime, date | None]] = []
        seen: set[datetime] = set()

        for window in sorted(open_windows, key=lambda w: w.start):
            window_start = max(window.start, earliest)
            window_end = min(window.end, latest)
            if window_start >= window_end:
                continue

            # Snap forward onto the grid anchored at the window's own start.
            offset = (window_start - window.start) % step
            cursor = window_start if not offset else window_start + (step - offset)

            while cursor + duration <= window_end:
                if cursor not in seen:
                    seen.add(cursor)
                    out.append((cursor, window.local_date))
                cursor += step
                if len(out) >= _MAX_CANDIDATE_STARTS:
                    log.warning(
                        "booking.availability.candidates_truncated",
                        limit=_MAX_CANDIDATE_STARTS,
                        grid_minutes=grid,
                    )
                    return sorted(out, key=lambda pair: pair[0])

        return sorted(out, key=lambda pair: pair[0])

    @staticmethod
    def _grid_for(resources: Sequence[ResourceSpec]) -> int:
        """The finest grid any participating resource type declares.

        The finest rather than the coarsest: a 15-minute resource and a
        free-floating one together should offer 15-minute starts, because the
        coarser constraint is the one that binds and taking the maximum would
        silently hide bookable times. ``None`` (free-floating) contributes the
        default rather than "1 minute", since offering a caller 09:01 is
        technically correct and practically useless.
        """
        declared = [
            r.slot_granularity_minutes
            for r in resources
            if r.slot_granularity_minutes
        ]
        return min(declared) if declared else _DEFAULT_GRID_MINUTES

    # ── Step 2: allocation ────────────────────────────────────────────────

    def _allocate(
        self,
        *,
        service: ServiceSpec,
        start: datetime,
        end: datetime,
        bindings: dict[str, Any],
        by_resource: dict[UUID, ResourceSpec],
        open_windows: Sequence[OpenWindow],
        closed_windows: Sequence[ClosedWindow],
        busy: Sequence[Busy],
        party_size: int,
        restrict_to: frozenset[UUID] | None,
    ) -> tuple[AllocationCandidate, ...] | None:
        """Satisfy every requirement at this instant, or return ``None``.

        ``None`` rather than an empty tuple, and the distinction is load-bearing:
        a service with *no* requirements is trivially satisfiable and yields
        ``()``, while a service whose requirements cannot be met yields ``None``.
        Collapsing the two would make every unsatisfiable slot look bookable.

        A resource already chosen for one requirement is not offered to the next
        (``taken`` below). Without that, a service needing "two of type X" could
        be handed the same resource twice and the exclusion constraint would
        reject the second item — turning a solvable allocation into a 409 at
        write time, which is exactly the failure the solver exists to avoid.
        """
        allocations: list[AllocationCandidate] = []
        taken: set[UUID] = set()

        for requirement in service.requirements:
            consumed_start, consumed_end = self._consumed_interval(
                requirement, service, start, end
            )

            pool = [
                resource
                for resource in by_resource.values()
                if resource.resource_type_id == requirement.resource_type_id
                and resource.id not in taken
                and (restrict_to is None or resource.id in restrict_to)
                and resource.matches(requirement.attribute_predicate, bindings)
                and self._is_open(resource, consumed_start, consumed_end, open_windows)
                and not self._is_closed(
                    resource, consumed_start, consumed_end, closed_windows
                )
                and self._has_capacity(
                    resource,
                    consumed_start,
                    consumed_end,
                    busy,
                    self._units(requirement, party_size),
                )
            ]

            if len(pool) < requirement.quantity:
                if requirement.is_required:
                    return None
                continue

            chosen = self._rank(pool, requirement, party_size, busy)[
                : requirement.quantity
            ]
            taken.update(r.id for r in chosen)
            allocations.append(
                AllocationCandidate(
                    role=requirement.role,
                    requirement_id=requirement.id,
                    resource_ids=tuple(r.id for r in chosen),
                    quantity=self._units(requirement, party_size),
                    respects_buffers=requirement.respects_buffers,
                )
            )

        return tuple(allocations)

    @staticmethod
    def _units(requirement: RequirementSpec, party_size: int) -> int:
        """Capacity units one allocation consumes on each chosen resource.

        ``NULL`` on the requirement means "derive from party size" (01 §5.5) —
        which is how a shared pool serves a party of three by consuming three
        units, with no code that knows what a party is.
        """
        return requirement.units_per_resource or max(1, party_size)

    @staticmethod
    def _consumed_interval(
        requirement: RequirementSpec,
        service: ServiceSpec,
        start: datetime,
        end: datetime,
    ) -> tuple[datetime, datetime]:
        """The interval this requirement actually consumes.

        ``respects_buffers`` decides, and it is per-requirement rather than
        per-service for a concrete reason (01 §14.1): a space is blocked through
        cleanup, but the person who used it is free the moment the appointment
        ends. Applying the buffer to both would make every practitioner
        unavailable during a room turnaround they have nothing to do with.
        """
        if not requirement.respects_buffers:
            return start, end
        return (
            start - timedelta(minutes=service.buffer_before_minutes),
            end + timedelta(minutes=service.buffer_after_minutes),
        )

    @staticmethod
    def _is_open(
        resource: ResourceSpec,
        start: datetime,
        end: datetime,
        open_windows: Sequence[OpenWindow],
    ) -> bool:
        """Is the whole interval inside one open window for this resource?

        One window, not a union of adjacent ones. Two abutting windows
        (09:00-12:00 and 13:00-17:00) genuinely leave a gap, and an appointment
        spanning it would be sold across a closure. Merging first would be a
        correctness bug dressed as an optimisation.

        A window with ``resource_id=None`` is the location's own hours and
        applies to every resource; a resource-specific window overrides nothing —
        both must admit the interval, which is what makes "the practice is open
        AND this person is working" the condition rather than either alone.
        """
        location_ok = False
        resource_specific = [w for w in open_windows if w.resource_id == resource.id]
        location_windows = [w for w in open_windows if w.resource_id is None]

        for window in location_windows:
            if window.start <= start and end <= window.end:
                location_ok = True
                break
        if location_windows and not location_ok:
            return False

        if not resource_specific:
            # No personal schedule declared: the resource follows the location.
            return location_ok or not location_windows

        return any(w.start <= start and end <= w.end for w in resource_specific)

    @staticmethod
    def _is_closed(
        resource: ResourceSpec,
        start: datetime,
        end: datetime,
        closed_windows: Sequence[ClosedWindow],
    ) -> bool:
        """Does any exception or holiday overlap this interval?

        Overlap, not containment. A two-hour maintenance window in the middle of
        a three-hour appointment closes it, even though it does not cover it —
        which is the opposite test from :meth:`_is_open` and deliberately so.

        Half-open comparison throughout (``start < other_end and other_start <
        end``), matching the database's ``&&`` on ``[)`` ranges. Using ``<=``
        here would make a closure ending exactly when the appointment begins
        block it, and the two layers would then disagree about the same instant.
        """
        for window in closed_windows:
            if window.resource_id is not None and window.resource_id != resource.id:
                continue
            if (
                window.resource_type_id is not None
                and window.resource_type_id != resource.resource_type_id
            ):
                continue
            if start < window.end and window.start < end:
                return True
        return False

    @staticmethod
    def _has_capacity(
        resource: ResourceSpec,
        start: datetime,
        end: datetime,
        busy: Sequence[Busy],
        wanted: int,
    ) -> bool:
        """Is there capacity for ``wanted`` units across this interval?

        Two rules, chosen by ``is_capacity_shared`` — the switch that lets one
        schema cover an exclusive chair and a twenty-person studio (01 §5.2):

        * **Exclusive:** any overlap at all disqualifies. This mirrors
          ``ex_booking_items__no_overlap`` exactly, so a slot this method offers
          is a slot the constraint will accept.
        * **Shared:** the *peak* summed quantity over the interval must leave
          room. The peak of a step function can only occur at a step boundary, so
          evaluating at every overlapping interval's start point is exact rather
          than a sample — the same argument ``check_shared_capacity()`` makes in
          SQL, which is why the two agree.
        """
        overlapping = [
            b
            for b in busy
            if b.resource_id == resource.id and start < b.end and b.start < end
        ]
        if not resource.is_capacity_shared:
            return not overlapping

        ceiling = resource.capacity + resource.overbook_allowance
        boundaries = {start} | {b.start for b in overlapping if start <= b.start < end}
        for instant in boundaries:
            load = sum(b.quantity for b in overlapping if b.start <= instant < b.end)
            if load + wanted > ceiling:
                return False
        return True

    @staticmethod
    def _rank(
        pool: list[ResourceSpec],
        requirement: RequirementSpec,
        party_size: int,
        busy: Sequence[Busy],
    ) -> list[ResourceSpec]:
        """Order qualifying resources by the requirement's allocation strategy.

        ``best_fit`` minimises wasted capacity — seat a party of two at a
        two-capacity resource, not a six — which is the strategy that keeps the
        large resources free for the parties that need them. It reads *capacity*
        rather than any named attribute, so the same code does it for tables,
        shuttles and treatment rooms.

        ``least_utilised`` counts current commitments, which spreads load across
        staff instead of hammering whoever sorts first — the difference between a
        fair rota and one person doing every appointment.
        """
        match requirement.allocation_strategy:
            case "best_fit":
                return sorted(pool, key=lambda r: (r.capacity, r.sort_key))
            case "least_utilised":
                load = {r.id: sum(1 for b in busy if b.resource_id == r.id) for r in pool}
                return sorted(pool, key=lambda r: (load[r.id], r.sort_key))
            case "round_robin":
                # Rotating fairly needs the last-assigned instant, which is a
                # property of history rather than of the resource. Approximated
                # by "fewest recent commitments", which converges to the same
                # rota without a second table to keep in step.
                load = {r.id: sum(1 for b in busy if b.resource_id == r.id) for r in pool}
                return sorted(pool, key=lambda r: (load[r.id], r.sort_key))
            case _:  # first_available, explicit
                return sorted(pool, key=lambda r: r.sort_key)


def merge_busy(
    bookings: Iterable[Busy], holds: Iterable[Busy], *, now: datetime
) -> list[Busy]:
    """Combine confirmed occupancy with live holds.

    Holds are filtered by TTL here as well as in SQL. That is not redundancy for
    its own sake: the solver may be handed a cached hold list, and layer 1 of the
    TTL (01 §13.3) says an expired hold stops blocking *the moment it expires*,
    with no sweeper involved. A cached list that still contains it must not make
    a free slot look taken.
    """
    out = list(bookings)
    out.extend(h for h in holds if h.end > now or h.kind != "hold")
    return out

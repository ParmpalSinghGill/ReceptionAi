"""The booking read/write path — 01 §15.5.

**Why this repository is raw SQL rather than ORM models.** Three of the columns
it touches cannot be expressed through the ORM without fighting it, and all three
are the columns that matter:

* ``service_period`` / ``occupies`` are ``TSTZRANGE``. SQLAlchemy can round-trip
  a range, but every predicate the engine needs (``&&``, ``@>``, ``lower()``) is
  written as raw SQL anyway, so the mapped class would buy nothing and hide the
  operator that does the work.
* ``blocking_period`` is ``GENERATED ALWAYS`` and must never be written. A mapped
  attribute is an attractive nuisance: it exists, it is assignable, and assigning
  it fails at flush time with an error that names the ORM rather than the rule.
* ``booking_check_conflicts`` and ``booking_convert_hold`` are ``SECURITY
  DEFINER`` functions whose entire purpose is to hold an advisory lock across
  two tables. They are called, not modelled.

Every statement here still runs inside a :class:`TenantSession`, so ``SET LOCAL
app.tenant_id`` is in force and RLS scopes every row — and every query *also*
carries an explicit ``tenant_id = :tenant_id`` predicate, per 01 §15.4's
defence-in-depth rule and because the planner picks the tenant-leading index from
a literal predicate rather than from an RLS one it has to push down.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from packages.booking.availability import Busy, expand_holiday, expand_rule
from packages.booking.domain import (
    BookingRecord,
    ClosedWindow,
    HoldRecord,
    OpenWindow,
    RequirementSpec,
    ResourceSpec,
    ServiceSpec,
)
from packages.core.errors import EntityNotFoundError, ResourceConflictError
from packages.core.ids import uuid7
from packages.persistence.session import TenantSession
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

__all__ = ["BookingRepository", "new_reference"]

log = structlog.get_logger(__name__)

#: Reference alphabet: uppercase, no ``I``/``O``/``0``/``1``. A reference is read
#: aloud on the phone and typed back by someone who heard it, so the pairs a
#: human confuses are removed rather than being a support ticket each.
_REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_reference(prefix: str = "BK") -> str:
    """A human-quotable booking reference, ``BK-7F3K2Q``.

    ``secrets`` rather than ``random``: a reference is a capability in any flow
    that looks a booking up by reference alone, and a predictable sequence would
    let one caller enumerate another's appointments. Six characters from a
    32-symbol alphabet is 2^30, and uniqueness is guaranteed by
    ``uq_bookings__tenant_reference`` rather than by the entropy — the entropy is
    there to make guessing pointless, not to avoid collisions.
    """
    body = "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))
    return f"{prefix}-{body}"


class BookingRepository:
    """Loads the engine's inputs and performs its writes."""

    def __init__(self, session: TenantSession) -> None:
        self.session = session

    @property
    def tenant_id(self) -> UUID:
        return self.session.tenant_ctx.tenant_id

    # ── Catalogue reads ───────────────────────────────────────────────────

    async def service_by_code(
        self, code: str, *, location_id: UUID | None = None
    ) -> ServiceSpec | None:
        """Resolve a service by its stable code, with its requirements.

        Location-specific rows win over global ones (``location_id IS NULL``
        means "offered everywhere", 01 §5.4), which is what lets a two-site
        practice price the same treatment differently without two codes.
        """
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, code, name, location_id,
                           duration_minutes, duration_units,
                           buffer_before_minutes, buffer_after_minutes,
                           min_party_size, max_party_size,
                           lead_time_minutes, booking_horizon_days,
                           cancellation_window_minutes, cancellation_fee,
                           price, currency, intake_schema, allow_waitlist
                      FROM services
                     WHERE tenant_id = :tenant_id
                       AND code = :code
                       AND is_active
                       AND deleted_at IS NULL
                       AND (location_id IS NULL OR location_id = :location_id
                            OR :location_id IS NULL)
                     ORDER BY (location_id IS NOT NULL) DESC
                     LIMIT 1
                    """
                ),
                {"tenant_id": self.tenant_id, "code": code, "location_id": location_id},
            )
        ).mappings().all()

        if not rows:
            return None
        return await self._build_service(rows[0])

    async def service_by_id(self, service_id: UUID) -> ServiceSpec | None:
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, code, name, location_id,
                           duration_minutes, duration_units,
                           buffer_before_minutes, buffer_after_minutes,
                           min_party_size, max_party_size,
                           lead_time_minutes, booking_horizon_days,
                           cancellation_window_minutes, cancellation_fee,
                           price, currency, intake_schema, allow_waitlist
                      FROM services
                     WHERE tenant_id = :tenant_id AND id = :id
                       AND deleted_at IS NULL
                    """
                ),
                {"tenant_id": self.tenant_id, "id": service_id},
            )
        ).mappings().all()
        if not rows:
            return None
        return await self._build_service(rows[0])

    async def list_services(self) -> list[ServiceSpec]:
        """Every bookable service, for the catalogue the agent is shown."""
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, code, name, location_id,
                           duration_minutes, duration_units,
                           buffer_before_minutes, buffer_after_minutes,
                           min_party_size, max_party_size,
                           lead_time_minutes, booking_horizon_days,
                           cancellation_window_minutes, cancellation_fee,
                           price, currency, intake_schema, allow_waitlist
                      FROM services
                     WHERE tenant_id = :tenant_id
                       AND is_active AND is_bookable_online AND deleted_at IS NULL
                     ORDER BY sort_order, name
                    """
                ),
                {"tenant_id": self.tenant_id},
            )
        ).mappings().all()
        return [await self._build_service(row) for row in rows]

    async def _build_service(self, row: Any) -> ServiceSpec:
        requirements = (
            await self.session.execute(
                text(
                    """
                    SELECT id, resource_type_id, role, quantity,
                           units_per_resource, attribute_predicate, is_required,
                           allocation_strategy, respects_buffers
                      FROM service_resource_requirements
                     WHERE tenant_id = :tenant_id AND service_id = :service_id
                     ORDER BY role
                    """
                ),
                {"tenant_id": self.tenant_id, "service_id": row["id"]},
            )
        ).mappings().all()

        return ServiceSpec(
            id=row["id"],
            tenant_id=row["tenant_id"],
            code=row["code"],
            name=row["name"],
            location_id=row["location_id"],
            duration_minutes=row["duration_minutes"],
            duration_units=row["duration_units"],
            buffer_before_minutes=row["buffer_before_minutes"],
            buffer_after_minutes=row["buffer_after_minutes"],
            min_party_size=row["min_party_size"],
            max_party_size=row["max_party_size"],
            lead_time_minutes=row["lead_time_minutes"],
            booking_horizon_days=row["booking_horizon_days"],
            cancellation_window_minutes=row["cancellation_window_minutes"],
            cancellation_fee=row["cancellation_fee"],
            price=row["price"],
            currency=row["currency"],
            intake_schema=tuple(row["intake_schema"] or ()),
            allow_waitlist=row["allow_waitlist"],
            requirements=tuple(
                RequirementSpec(
                    id=r["id"],
                    resource_type_id=r["resource_type_id"],
                    role=r["role"],
                    quantity=r["quantity"],
                    units_per_resource=r["units_per_resource"],
                    attribute_predicate=dict(r["attribute_predicate"] or {}),
                    is_required=r["is_required"],
                    allocation_strategy=r["allocation_strategy"],
                    respects_buffers=r["respects_buffers"],
                )
                for r in requirements
            ),
        )

    async def resources_for_service(
        self, service: ServiceSpec, *, location_id: UUID
    ) -> list[ResourceSpec]:
        """Every bookable resource of a type the service requires.

        Filtered by type here rather than in Python because the predicate hits
        ``ix_resources__tenant_type_location``, and a practice with forty
        resources should not ship all forty over the wire to discard thirty.
        """
        type_ids = [r.resource_type_id for r in service.requirements]
        if not type_ids:
            return []

        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT r.id, r.tenant_id, r.location_id, r.resource_type_id,
                           r.code, r.name, r.capacity, r.overbook_allowance,
                           r.attributes, r.tags, r.timezone, r.sort_order,
                           rt.is_capacity_shared, rt.time_unit,
                           rt.slot_granularity_minutes
                      FROM resources r
                      JOIN resource_types rt
                        ON rt.id = r.resource_type_id AND rt.tenant_id = r.tenant_id
                     WHERE r.tenant_id = :tenant_id
                       AND r.location_id = :location_id
                       AND r.resource_type_id = ANY(:type_ids)
                       AND r.is_active AND r.is_bookable AND r.deleted_at IS NULL
                       AND rt.is_active
                     ORDER BY r.sort_order, r.code
                    """
                ),
                {
                    "tenant_id": self.tenant_id,
                    "location_id": location_id,
                    "type_ids": type_ids,
                },
            )
        ).mappings().all()

        return [
            ResourceSpec(
                id=row["id"],
                tenant_id=row["tenant_id"],
                location_id=row["location_id"],
                resource_type_id=row["resource_type_id"],
                code=row["code"],
                name=row["name"],
                capacity=row["capacity"],
                overbook_allowance=row["overbook_allowance"],
                attributes=dict(row["attributes"] or {}),
                tags=tuple(row["tags"] or ()),
                timezone=row["timezone"],
                sort_order=row["sort_order"],
                is_capacity_shared=row["is_capacity_shared"],
                time_unit=row["time_unit"],
                slot_granularity_minutes=row["slot_granularity_minutes"],
            )
            for row in rows
        ]

    async def location_timezone(self, location_id: UUID) -> str:
        """The IANA zone every local-time derivation goes through.

        Raises rather than defaulting to UTC when the location is unknown.
        Contract §6 makes local time a *derivation* from this value, so guessing
        it would silently produce bookings an hour or five wrong — visible only
        when a patient arrives at the wrong time.
        """
        tz = (
            await self.session.execute(
                text(
                    "SELECT timezone FROM locations "
                    " WHERE tenant_id = :tenant_id AND id = :id "
                    "   AND deleted_at IS NULL"
                ),
                {"tenant_id": self.tenant_id, "id": location_id},
            )
        ).scalar_one_or_none()
        if tz is None:
            raise EntityNotFoundError(entity_type="locations", entity_id=location_id)
        return str(tz)

    async def location_region(self, location_id: UUID) -> str | None:
        """The location's ISO-3166 country code, for dialling rules.

        Beside the IANA timezone, and for the same reason: a phone number and a
        wall-clock time are both meaningless without knowing where the business
        is. Returns ``None`` when unset, so the caller applies its own default
        rather than this layer inventing a country.
        """
        return (
            await self.session.execute(
                text(
                    "SELECT country_code FROM locations "
                    " WHERE tenant_id = :tenant_id AND id = :id "
                    "   AND deleted_at IS NULL"
                ),
                {"tenant_id": self.tenant_id, "id": location_id},
            )
        ).scalar_one_or_none()

    async def default_location_id(self) -> UUID:
        """The tenant's primary location.

        Single-location is overwhelmingly the common case and the agent should
        not have to name one. ``code = 'main'`` first, then the oldest active
        location — deterministic either way, because a non-deterministic default
        would put two bookings from one conversation at two different sites.
        """
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id FROM locations
                     WHERE tenant_id = :tenant_id AND is_active
                       AND deleted_at IS NULL
                     ORDER BY (code = 'main') DESC, created_at
                     LIMIT 1
                    """
                ),
                {"tenant_id": self.tenant_id},
            )
        ).scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError(
                entity_type="locations",
                message="This tenant has no active location to book against.",
            )
        return row

    # ── Availability inputs ───────────────────────────────────────────────

    async def open_windows(
        self,
        *,
        location_id: UUID,
        service_id: UUID,
        tz: str,
        from_date: date,
        to_date: date,
    ) -> list[OpenWindow]:
        """Expand every applicable ``open`` rule into UTC intervals.

        Rules scoped to a *service* are included only when they name this one;
        rules with ``service_id IS NULL`` apply to everything. A rule naming a
        different service is not merely irrelevant — including it would open a
        window the practice deliberately reserved (01 §5.6's "new patient slots
        only on Tuesdays" is exactly this).

        ``closed`` rules are handled by :meth:`closed_windows`, not here, because
        a closure must subtract from the envelope rather than compete inside it.
        """
        zone = ZoneInfo(tz)
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT resource_id, resource_type_id, weekdays,
                           start_time, end_time, priority, capacity_override,
                           lower(effective_period)  AS effective_from,
                           upper(effective_period)  AS effective_until
                      FROM availability_rules
                     WHERE tenant_id = :tenant_id
                       AND location_id = :location_id
                       AND is_active
                       AND effect = 'open'
                       AND (service_id IS NULL OR service_id = :service_id)
                       AND effective_period && daterange(:from_date, :to_date, '[]')
                    """
                ),
                {
                    "tenant_id": self.tenant_id,
                    "location_id": location_id,
                    "service_id": service_id,
                    "from_date": from_date,
                    "to_date": to_date,
                },
            )
        ).mappings().all()

        # A rule scoped to a resource *type* applies to every resource of that
        # type, so it is expanded once per member. Doing that here rather than in
        # the solver keeps `OpenWindow` a flat "this resource, this interval",
        # which is what makes the containment test in `_is_open` a one-liner.
        type_members = await self._resources_by_type(location_id)

        windows: list[OpenWindow] = []
        for row in rows:
            targets: list[UUID | None]
            if row["resource_id"] is not None:
                targets = [row["resource_id"]]
            elif row["resource_type_id"] is not None:
                targets = list(type_members.get(row["resource_type_id"], []))
            else:
                targets = [None]

            for target in targets:
                windows.extend(
                    expand_rule(
                        weekdays=row["weekdays"],
                        start_time=row["start_time"],
                        end_time=row["end_time"],
                        effective_from=row["effective_from"],
                        effective_until=row["effective_until"],
                        tz=zone,
                        from_date=from_date,
                        to_date=to_date,
                        resource_id=target,
                        capacity_override=row["capacity_override"],
                        priority=row["priority"],
                    )
                )
        return windows

    async def _resources_by_type(
        self, location_id: UUID
    ) -> dict[UUID, list[UUID]]:
        rows = (
            await self.session.execute(
                text(
                    "SELECT resource_type_id, id FROM resources "
                    " WHERE tenant_id = :tenant_id AND location_id = :location_id "
                    "   AND is_active AND is_bookable AND deleted_at IS NULL"
                ),
                {"tenant_id": self.tenant_id, "location_id": location_id},
            )
        ).all()
        out: dict[UUID, list[UUID]] = {}
        for type_id, resource_id in rows:
            out.setdefault(type_id, []).append(resource_id)
        return out

    async def closed_windows(
        self,
        *,
        location_id: UUID,
        service_id: UUID,
        tz: str,
        from_date: date,
        to_date: date,
    ) -> list[ClosedWindow]:
        """Exceptions, ``closed`` rules, and holidays — everything that subtracts.

        Three sources, one output type, because the solver's subtraction step
        must not care which produced an interval. A holiday and a vacation both
        mean "not bookable"; giving them separate handling is two chances for one
        of them to be forgotten.
        """
        zone = ZoneInfo(tz)
        out: list[ClosedWindow] = []

        exceptions = (
            await self.session.execute(
                text(
                    """
                    SELECT resource_id, resource_type_id, reason,
                           lower(period) AS starts, upper(period) AS ends
                      FROM availability_exceptions
                     WHERE tenant_id = :tenant_id
                       AND location_id = :location_id
                       AND effect = 'closed'
                       AND (service_id IS NULL OR service_id = :service_id)
                       AND period && tstzrange(:from_ts, :to_ts, '[)')
                    """
                ),
                {
                    "tenant_id": self.tenant_id,
                    "location_id": location_id,
                    "service_id": service_id,
                    "from_ts": datetime.combine(from_date, datetime.min.time(), tzinfo=zone),
                    "to_ts": datetime.combine(
                        to_date + timedelta(days=1), datetime.min.time(), tzinfo=zone
                    ),
                },
            )
        ).mappings().all()
        out.extend(
            ClosedWindow(
                start=row["starts"],
                end=row["ends"],
                resource_id=row["resource_id"],
                resource_type_id=row["resource_type_id"],
                reason=row["reason"],
            )
            for row in exceptions
        )

        # `closed` availability rules — a recurring closure such as a half-day.
        closed_rules = (
            await self.session.execute(
                text(
                    """
                    SELECT resource_id, resource_type_id, weekdays,
                           start_time, end_time, label,
                           lower(effective_period) AS effective_from,
                           upper(effective_period) AS effective_until
                      FROM availability_rules
                     WHERE tenant_id = :tenant_id
                       AND location_id = :location_id
                       AND is_active AND effect = 'closed'
                       AND (service_id IS NULL OR service_id = :service_id)
                       AND effective_period && daterange(:from_date, :to_date, '[]')
                    """
                ),
                {
                    "tenant_id": self.tenant_id,
                    "location_id": location_id,
                    "service_id": service_id,
                    "from_date": from_date,
                    "to_date": to_date,
                },
            )
        ).mappings().all()
        for row in closed_rules:
            for window in expand_rule(
                weekdays=row["weekdays"],
                start_time=row["start_time"],
                end_time=row["end_time"],
                effective_from=row["effective_from"],
                effective_until=row["effective_until"],
                tz=zone,
                from_date=from_date,
                to_date=to_date,
                resource_id=row["resource_id"],
            ):
                out.append(
                    ClosedWindow(
                        start=window.start,
                        end=window.end,
                        resource_id=row["resource_id"],
                        resource_type_id=row["resource_type_id"],
                        reason=row["label"],
                    )
                )

        # Holidays. Both concrete dates in range and `annual` rows whose
        # month/day falls in range, the latter re-dated onto the years the window
        # spans — which is the whole point of storing them as recurring rather
        # than as one row per year (01 §5.8).
        holidays = (
            await self.session.execute(
                text(
                    """
                    SELECT name, holiday_date, start_time, end_time, recurrence
                      FROM holidays
                     WHERE tenant_id = :tenant_id
                       AND is_active
                       AND effect = 'closed'
                       AND (location_id IS NULL OR location_id = :location_id)
                       AND (
                             (recurrence = 'none'
                              AND holiday_date BETWEEN :from_date AND :to_date)
                          OR recurrence = 'annual'
                       )
                    """
                ),
                {
                    "tenant_id": self.tenant_id,
                    "location_id": location_id,
                    "from_date": from_date,
                    "to_date": to_date,
                },
            )
        ).mappings().all()

        for row in holidays:
            for occurrence in _holiday_dates(
                row["holiday_date"], row["recurrence"], from_date, to_date
            ):
                out.append(
                    expand_holiday(
                        holiday_date=occurrence,
                        start_time=row["start_time"],
                        end_time=row["end_time"],
                        tz=zone,
                        reason=row["name"],
                    )
                )

        return out

    async def busy(
        self,
        *,
        resource_ids: Sequence[UUID],
        window_start: datetime,
        window_end: datetime,
        exclude_booking_id: UUID | None = None,
        exclude_hold_id: UUID | None = None,
    ) -> list[Busy]:
        """Confirmed occupancy plus live holds, over one window.

        The hold arm carries ``expires_at > now()`` — layer 1 of the TTL
        (01 §13.3). An expired hold that the sweeper has not reached yet must not
        make a slot look taken, and the *read-time predicate is authoritative*:
        the sweeper is janitorial. Reading holds without it would make a crashed
        worker able to freeze a resource until someone noticed.
        """
        if not resource_ids:
            return []

        params = {
            "tenant_id": self.tenant_id,
            "resource_ids": list(resource_ids),
            "window_start": window_start,
            "window_end": window_end,
            "exclude_booking_id": exclude_booking_id,
            "exclude_hold_id": exclude_hold_id,
        }

        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT bi.resource_id,
                           lower(bi.occupies) AS starts,
                           upper(bi.occupies) AS ends,
                           bi.quantity,
                           'booking'::text   AS kind,
                           bi.booking_id     AS booking_id
                      FROM booking_items bi
                     WHERE bi.tenant_id = :tenant_id
                       AND bi.resource_id = ANY(:resource_ids)
                       AND bi.is_blocking
                       AND bi.occupies && tstzrange(:window_start, :window_end, '[)')
                       AND (CAST(:exclude_booking_id AS uuid) IS NULL
                            OR bi.booking_id <> CAST(:exclude_booking_id AS uuid))

                    UNION ALL

                    SELECT h.resource_id,
                           lower(h.occupies) AS starts,
                           upper(h.occupies) AS ends,
                           h.quantity,
                           'hold'::text      AS kind,
                           h.id              AS booking_id
                      FROM booking_holds h
                     WHERE h.tenant_id = :tenant_id
                       AND h.resource_id = ANY(:resource_ids)
                       AND h.status = 'active'
                       AND h.expires_at > now()
                       AND h.occupies && tstzrange(:window_start, :window_end, '[)')
                       AND (CAST(:exclude_hold_id AS uuid) IS NULL
                            OR h.id <> CAST(:exclude_hold_id AS uuid))
                    """
                ),
                params,
            )
        ).mappings().all()

        return [
            Busy(
                resource_id=row["resource_id"],
                start=row["starts"],
                end=row["ends"],
                quantity=row["quantity"],
                kind=row["kind"],
                booking_id=row["booking_id"],
            )
            for row in rows
        ]

    # ── Conflict check ────────────────────────────────────────────────────

    async def check_conflicts(
        self,
        *,
        resource_id: UUID,
        start: datetime,
        end: datetime,
        ignore_hold_id: UUID | None = None,
        ignore_booking_id: UUID | None = None,
    ) -> list[tuple[str, UUID]]:
        """Call ``booking_check_conflicts`` — 01 §13.2's advisory-locked check.

        This is the *only* correct way to ask "is this slot free" immediately
        before writing, because it is the only path that takes
        ``pg_advisory_xact_lock`` on ``(tenant, resource)`` and reads both
        ``booking_items`` and ``booking_holds`` under it. A plain SELECT would
        answer the same question and be wrong under concurrency: exclusion
        constraints are single-table, so nothing else stops a hold and a booking
        from being written for the same instant by two transactions that each saw
        the other's table as empty.

        The lock is held until the caller's transaction ends, which is what makes
        "check then write" atomic for that resource.
        """
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT conflict_kind, conflict_id
                      FROM booking_check_conflicts(
                          :tenant_id, :resource_id,
                          tstzrange(:start, :end, '[)'),
                          :ignore_hold_id, :ignore_booking_id)
                    """
                ),
                {
                    "tenant_id": self.tenant_id,
                    "resource_id": resource_id,
                    "start": start,
                    "end": end,
                    "ignore_hold_id": ignore_hold_id,
                    "ignore_booking_id": ignore_booking_id,
                },
            )
        ).all()
        return [(kind, conflict_id) for kind, conflict_id in rows]

    # ── Holds ─────────────────────────────────────────────────────────────

    async def place_hold(
        self,
        *,
        location_id: UUID,
        service_id: UUID,
        resource_id: UUID,
        start: datetime,
        end: datetime,
        ttl_seconds: int,
        quantity: int = 1,
        conversation_id: UUID | None = None,
    ) -> HoldRecord:
        """Insert one hold, after the advisory-locked conflict check.

        The check and the insert are in the same transaction *and* the check took
        the lock, so nothing can slip between them. The exclusion constraint on
        ``booking_holds`` is still the backstop, and if it fires the caller gets a
        clean conflict rather than an opaque integrity error — the lock provides
        coordination, the constraint provides correctness (01 §13.2).
        """
        conflicts = await self.check_conflicts(
            resource_id=resource_id, start=start, end=end
        )
        if conflicts:
            raise ResourceConflictError(
                "The requested time is no longer available",
                kind="overlap",
                constraint="booking_check_conflicts",
            )

        hold_id = uuid7()
        expires_at = datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)
        try:
            await self.session.execute(
                text(
                    """
                    INSERT INTO booking_holds
                        (id, tenant_id, location_id, service_id, resource_id,
                         status, occupies, quantity, expires_at, conversation_id)
                    VALUES (:id, :tenant_id, :location_id, :service_id,
                            :resource_id, 'active',
                            tstzrange(:start, :end, '[)'), :quantity,
                            :expires_at, :conversation_id)
                    """
                ),
                {
                    "id": hold_id,
                    "tenant_id": self.tenant_id,
                    "location_id": location_id,
                    "service_id": service_id,
                    "resource_id": resource_id,
                    "start": start,
                    "end": end,
                    "quantity": quantity,
                    "expires_at": expires_at,
                    "conversation_id": conversation_id,
                },
            )
        except IntegrityError as exc:
            raise _translate(exc) from exc

        return HoldRecord(
            id=hold_id,
            tenant_id=self.tenant_id,
            location_id=location_id,
            service_id=service_id,
            resource_id=resource_id,
            start_at=start,
            end_at=end,
            expires_at=expires_at,
            quantity=quantity,
        )

    async def get_hold(self, hold_id: UUID) -> HoldRecord | None:
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT id, tenant_id, location_id, service_id, resource_id,
                           status, quantity, expires_at,
                           lower(occupies) AS starts, upper(occupies) AS ends
                      FROM booking_holds
                     WHERE tenant_id = :tenant_id AND id = :id
                    """
                ),
                {"tenant_id": self.tenant_id, "id": hold_id},
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return HoldRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            location_id=row["location_id"],
            service_id=row["service_id"],
            resource_id=row["resource_id"],
            start_at=row["starts"],
            end_at=row["ends"],
            expires_at=row["expires_at"],
            quantity=row["quantity"],
            status=row["status"],
        )

    async def extend_hold(self, hold_id: UUID, *, ttl_seconds: int) -> HoldRecord:
        """Push a live hold's TTL out.

        Only an *active, unexpired* hold may be extended. Reviving an expired one
        would resurrect a claim on a slot that has been free — and possibly
        booked by someone else — since it lapsed, which the exclusion constraint
        would then reject at a confusing moment. Refusing here makes the failure
        land where the agent can re-check and re-offer.
        """
        expires_at = datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)
        result = await self.session.execute(
            text(
                """
                UPDATE booking_holds
                   SET expires_at = :expires_at, updated_at = now()
                 WHERE tenant_id = :tenant_id AND id = :id
                   AND status = 'active' AND expires_at > now()
                """
            ),
            {"tenant_id": self.tenant_id, "id": hold_id, "expires_at": expires_at},
        )
        if result.rowcount == 0:
            raise EntityNotFoundError(
                entity_type="booking_holds",
                entity_id=hold_id,
                message="That hold has expired or is no longer active.",
            )
        hold = await self.get_hold(hold_id)
        assert hold is not None  # just updated it
        return hold

    async def release_hold(self, hold_id: UUID, *, reason: str) -> bool:
        """Release a hold. Returns False when it was already gone.

        Idempotent by design: the Temporal timer, the agent's explicit release,
        and the sweeper can all race to release the same hold, and two of them
        losing must not be an error. Only ``active`` rows are touched, so a
        converted hold is never un-converted.
        """
        result = await self.session.execute(
            text(
                """
                UPDATE booking_holds
                   SET status = 'released', released_at = now(),
                       release_reason = :reason, updated_at = now()
                 WHERE tenant_id = :tenant_id AND id = :id AND status = 'active'
                """
            ),
            {"tenant_id": self.tenant_id, "id": hold_id, "reason": reason},
        )
        return result.rowcount > 0

    # ── Bookings ──────────────────────────────────────────────────────────

    async def create_booking(
        self,
        *,
        location_id: UUID,
        service: ServiceSpec,
        start: datetime,
        end: datetime,
        local_date: date,
        party_size: int = 1,
        customer_name: str | None = None,
        customer_phone_e164: str | None = None,
        customer_email: str | None = None,
        intake_data: dict[str, Any] | None = None,
        notes: str | None = None,
        conversation_id: UUID | None = None,
        channel: str | None = None,
        idempotency_key: str | None = None,
        rescheduled_from_id: UUID | None = None,
        status: str = "confirmed",
    ) -> UUID:
        """Insert the booking aggregate. Items are attached separately.

        Buffers and the cancellation policy are **copied onto the row**, not
        referenced. 01 §5.9's reasoning: a service's buffer policy can change,
        and existing bookings must keep the geometry they were created with — an
        admin editing "cleanup time" from 10 to 20 minutes would otherwise
        retroactively make historical bookings overlap and break the exclusion
        constraint on the next unrelated write. The same argument applies to the
        cancellation window and fee, which is a promise made to a specific caller
        at a specific moment.
        """
        booking_id = uuid7()
        now = datetime.now(tz=UTC)

        # Idempotent create (01 §5.9): a retrying agent must not double-book.
        # Checked before the insert so a replay returns the original id rather
        # than surfacing a unique violation the caller has to interpret.
        if idempotency_key:
            existing = (
                await self.session.execute(
                    text(
                        "SELECT id FROM bookings "
                        " WHERE tenant_id = :tenant_id "
                        "   AND idempotency_key = :key"
                    ),
                    {"tenant_id": self.tenant_id, "key": idempotency_key},
                )
            ).scalar_one_or_none()
            if existing is not None:
                log.info(
                    "booking.create.idempotent_replay",
                    tenant_id=str(self.tenant_id),
                    booking_id=str(existing),
                )
                return existing

        try:
            await self.session.execute(
                text(
                    """
                    INSERT INTO bookings
                        (id, tenant_id, location_id, service_id, reference, status,
                         service_period, buffer_before_minutes, buffer_after_minutes,
                         local_date, party_size, duration_units,
                         customer_name, customer_phone_e164, customer_email,
                         intake_data, notes, conversation_id, channel, booked_by,
                         price_total, currency,
                         cancellation_window_minutes, cancellation_fee,
                         confirmed_at, idempotency_key, rescheduled_from_id)
                    VALUES
                        (:id, :tenant_id, :location_id, :service_id, :reference,
                         CAST(:status AS booking_status),
                         tstzrange(:start, :end, '[)'),
                         :buffer_before, :buffer_after,
                         :local_date, :party_size, 1,
                         :customer_name, :customer_phone, :customer_email,
                         CAST(:intake_data AS jsonb), :notes,
                         :conversation_id,
                         CAST(:channel AS conversation_channel), 'agent',
                         :price_total, :currency,
                         :cancellation_window, :cancellation_fee,
                         :confirmed_at, :idempotency_key, :rescheduled_from_id)
                    """
                ),
                {
                    "id": booking_id,
                    "tenant_id": self.tenant_id,
                    "location_id": location_id,
                    "service_id": service.id,
                    "reference": new_reference(),
                    "status": status,
                    "start": start,
                    "end": end,
                    "buffer_before": service.buffer_before_minutes,
                    "buffer_after": service.buffer_after_minutes,
                    "local_date": local_date,
                    "party_size": party_size,
                    "customer_name": customer_name,
                    "customer_phone": customer_phone_e164,
                    "customer_email": customer_email,
                    "intake_data": _json(intake_data or {}),
                    "notes": notes,
                    "conversation_id": conversation_id,
                    "channel": channel,
                    "price_total": service.price,
                    "currency": service.currency,
                    "cancellation_window": service.cancellation_window_minutes,
                    "cancellation_fee": service.cancellation_fee,
                    "confirmed_at": now if status == "confirmed" else None,
                    "idempotency_key": idempotency_key,
                    "rescheduled_from_id": rescheduled_from_id,
                },
            )
        except IntegrityError as exc:
            raise _translate(exc) from exc

        return booking_id

    async def add_item(
        self,
        *,
        booking_id: UUID,
        resource_id: UUID,
        role: str,
        requirement_id: UUID | None,
        quantity: int,
    ) -> None:
        """Attach one resource allocation. **This is where 23P01 fires.**

        ``occupies`` is passed as an empty-but-valid placeholder and immediately
        overwritten by ``trg_booking_items_sync_occupancy``, which derives it from
        the parent booking's service or blocking period according to the
        requirement's ``respects_buffers``. The value here only satisfies ``NOT
        NULL`` — the application is deliberately not allowed to choose it, because
        it is the exclusion constraint's entire input.
        """
        try:
            await self.session.execute(
                text(
                    """
                    INSERT INTO booking_items
                        (id, tenant_id, booking_id, resource_id, role,
                         requirement_id, quantity, occupies)
                    VALUES (:id, :tenant_id, :booking_id, :resource_id, :role,
                            :requirement_id, :quantity,
                            (SELECT service_period FROM bookings
                              WHERE id = :booking_id AND tenant_id = :tenant_id))
                    """
                ),
                {
                    "id": uuid7(),
                    "tenant_id": self.tenant_id,
                    "booking_id": booking_id,
                    "resource_id": resource_id,
                    "role": role,
                    "requirement_id": requirement_id,
                    "quantity": quantity,
                },
            )
        except IntegrityError as exc:
            raise _translate(exc) from exc

    async def convert_hold(
        self,
        *,
        hold_id: UUID,
        booking_id: UUID,
        role: str = "primary",
        requirement_id: UUID | None = None,
    ) -> None:
        """``booking_convert_hold`` — release and allocate in one transaction.

        Order matters and the function gets it right: the hold is marked
        converted *first*, so it no longer blocks, and the exclusion constraint on
        ``booking_items`` then guards the insert. Either the hold becomes a
        booking or nothing changes; there is no window in which the slot is
        unprotected (01 §13.3).

        A hold whose TTL passed while the caller was talking raises
        ``lock_not_available`` rather than converting. That is deliberate and
        loud: silently converting an expired hold is how two people end up in the
        same chair.
        """
        try:
            await self.session.execute(
                text(
                    "SELECT booking_convert_hold(:tenant_id, :hold_id, "
                    ":booking_id, :role, :requirement_id)"
                ),
                {
                    "tenant_id": self.tenant_id,
                    "hold_id": hold_id,
                    "booking_id": booking_id,
                    "role": role,
                    "requirement_id": requirement_id,
                },
            )
        except (IntegrityError, DBAPIError) as exc:
            raise _translate(exc) from exc

    async def move_booking(
        self, *, booking_id: UUID, start: datetime, end: datetime, local_date: date
    ) -> None:
        """Move an existing booking in place.

        One UPDATE, and ``trg_bookings_cascade_period`` carries the new geometry
        onto every child item, where the exclusion constraint re-evaluates it.
        That is what makes a reschedule atomic: it is either legal and committed,
        or rejected and rolled back. The alternative — cancel then rebook — has a
        window in which the slot is free and can be taken by someone else, which
        is the failure this method exists to make impossible.
        """
        try:
            result = await self.session.execute(
                text(
                    """
                    UPDATE bookings
                       SET service_period = tstzrange(:start, :end, '[)'),
                           local_date = :local_date,
                           updated_at = now(),
                           version = version + 1
                     WHERE tenant_id = :tenant_id AND id = :id
                       AND status NOT IN ('cancelled','no_show','completed')
                    """
                ),
                {
                    "tenant_id": self.tenant_id,
                    "id": booking_id,
                    "start": start,
                    "end": end,
                    "local_date": local_date,
                },
            )
        except (IntegrityError, DBAPIError) as exc:
            raise _translate(exc) from exc

        if result.rowcount == 0:
            raise EntityNotFoundError(
                entity_type="bookings",
                entity_id=booking_id,
                message="That booking cannot be moved: it is cancelled or completed.",
            )

    async def cancel_booking(
        self,
        *,
        booking_id: UUID,
        reason: str | None,
        cancelled_by: str = "agent",
        fee_charged: Decimal | None = None,
    ) -> bool:
        """Mark cancelled. Returns False when it already was.

        ``status`` and ``cancelled_at`` move together — ``ck_bookings__cancelled_
        coherent`` refuses any other combination, which is what stops the classic
        "the report says cancelled but the timestamp is null" inconsistency. The
        items follow automatically: ``trg_bookings_cascade_period`` flips
        ``is_blocking`` to false, and the partial exclusion constraint then
        ignores them, so the slot is genuinely free the instant the transaction
        commits.
        """
        result = await self.session.execute(
            text(
                """
                UPDATE bookings
                   SET status = 'cancelled', cancelled_at = now(),
                       cancellation_reason = :reason, cancelled_by = :cancelled_by,
                       cancellation_fee_charged = :fee,
                       updated_at = now(), version = version + 1
                 WHERE tenant_id = :tenant_id AND id = :id
                   AND status NOT IN ('cancelled','completed','no_show')
                """
            ),
            {
                "tenant_id": self.tenant_id,
                "id": booking_id,
                "reason": reason,
                "cancelled_by": cancelled_by,
                "fee": fee_charged,
            },
        )
        return result.rowcount > 0

    async def get_booking(self, booking_id: UUID) -> BookingRecord | None:
        return await self._one_booking(
            "b.id = :key", {"key": booking_id}
        )

    async def booking_by_reference(self, reference: str) -> BookingRecord | None:
        return await self._one_booking(
            "b.reference = :key", {"key": reference.strip().upper()}
        )

    async def bookings_by_phone(
        self, phone_e164: str, *, upcoming_only: bool = True, limit: int = 10
    ) -> list[BookingRecord]:
        """"Find my appointment" from the number the caller is speaking from.

        The hottest lookup on the voice path, and the one that lets a caller who
        has lost their reference still be helped. ``upcoming_only`` defaults True
        because "cancel my appointment" almost always means the next one, and
        offering a list that starts with last year's is noise.
        """
        clause = "b.customer_phone_e164 = :key"
        if upcoming_only:
            clause += " AND b.ends_at > now() AND b.status <> 'cancelled'"
        return await self._many_bookings(clause, {"key": phone_e164}, limit=limit)

    async def bookings_for_conversation(
        self, conversation_id: UUID, *, limit: int = 10
    ) -> list[BookingRecord]:
        """What this conversation has booked so far.

        Lets the agent answer "what did we just agree?" and lets a cancel/
        reschedule in the same call resolve without the caller reciting a
        reference they were told thirty seconds ago.
        """
        return await self._many_bookings(
            "b.conversation_id = :key", {"key": conversation_id}, limit=limit
        )

    async def bookings_on_date(
        self,
        on_date: date,
        *,
        resource_id: UUID | None = None,
        include_cancelled: bool = False,
        limit: int = 500,
    ) -> list[BookingRecord]:
        """Everything in the diary for one business day.

        The single-day case of :meth:`bookings_in_range`, kept as its own name
        because "the diary for Tuesday" is what the calendar asks for and
        ``from_date=x, to_date=x`` reads like a mistake at the call site.
        """
        return await self.bookings_in_range(
            on_date,
            on_date,
            resource_id=resource_id,
            include_cancelled=include_cancelled,
            limit=limit,
        )

    async def bookings_in_range(
        self,
        from_date: date,
        to_date: date,
        *,
        resource_id: UUID | None = None,
        statuses: Sequence[str] | None = None,
        include_cancelled: bool = False,
        ending_after: datetime | None = None,
        limit: int = 500,
    ) -> list[BookingRecord]:
        """The diary between two business days, inclusive at both ends.

        Filtered on ``local_date`` rather than on a UTC instant range, and that
        is the whole point of the column existing (01 §5). "Tuesday" is a
        property of the business's own calendar: on the two days a year the
        clock shifts, a 23- or 25-hour UTC window either drops an appointment
        or borrows one from the neighbouring day, and the person it happens to
        is a real customer standing at a locked door. ``local_date`` is written
        at booking time in the location's zone, so this query cannot drift.

        ``resource_id`` filters to one allocated resource via ``booking_items``
        — a booking consumes several (a person and a room), so the predicate is
        an EXISTS over the items rather than a column on ``bookings``.

        ``ending_after`` is the "upcoming only" filter, and it deliberately
        compares against ``ends_at`` rather than ``starts_at``. An appointment
        that began ten minutes ago and runs for thirty is **happening right
        now** — it is the one most likely to be asked about at the desk, and
        filtering on the start time would hide exactly that row. Filtering
        here rather than in the client is not an optimisation: a practice with
        a year of history would otherwise ship every past appointment over the
        wire for the browser to discard.

        ``statuses`` narrows to an explicit set. ``include_cancelled`` is
        separate because it answers a different question — the default hides
        cancellations from *every* status view, while asking for
        ``statuses=['cancelled']`` is a deliberate request to see them.
        """
        clause = "b.local_date BETWEEN :from_date AND :to_date"
        params: dict[str, Any] = {"from_date": from_date, "to_date": to_date}

        if statuses:
            # Bound as an array rather than interpolated: the values arrive
            # from a query string, and `= ANY(:statuses)` keeps them
            # parameters. The cast is explicit because the column is an enum
            # and asyncpg will not infer text[] against it.
            clause += " AND b.status::text = ANY(:statuses)"
            params["statuses"] = list(statuses)
        elif not include_cancelled:
            clause += " AND b.status <> 'cancelled'"

        if ending_after is not None:
            clause += " AND b.ends_at >= :ending_after"
            params["ending_after"] = ending_after

        if resource_id is not None:
            clause += (
                " AND EXISTS (SELECT 1 FROM booking_items bi"
                "              WHERE bi.booking_id = b.id"
                "                AND bi.tenant_id = b.tenant_id"
                "                AND bi.resource_id = :resource_id)"
            )
            params["resource_id"] = resource_id

        return await self._many_bookings(clause, params, limit=limit)

    async def list_resources(
        self, *, resource_type_code: str | None = None
    ) -> list[ResourceSpec]:
        """Every bookable resource, optionally of one type.

        ``resources_for_service`` answers "who could do *this*"; this answers
        "who is there at all", which is what a diary's filter needs — it must
        list a resource whose day is empty, and a service-driven query cannot
        because it starts from a service.
        """
        # The optional filter is expressed in SQL rather than by assembling the
        # statement in Python: one static string, one bound parameter, nothing
        # interpolated. A NULL ``:type_code`` short-circuits to "every type".
        rows = (
            await self.session.execute(
                text(
                    """
                    SELECT r.id, r.tenant_id, r.location_id, r.resource_type_id,
                           r.code, r.name, r.capacity, r.overbook_allowance,
                           r.attributes, r.tags, r.timezone, r.sort_order,
                           rt.is_capacity_shared, rt.time_unit,
                           rt.slot_granularity_minutes
                      FROM resources r
                      JOIN resource_types rt
                        ON rt.id = r.resource_type_id AND rt.tenant_id = r.tenant_id
                     WHERE r.tenant_id = :tenant_id
                       AND r.is_active AND r.is_bookable AND r.deleted_at IS NULL
                       AND rt.is_active
                       AND (CAST(:type_code AS text) IS NULL
                            OR rt.code = CAST(:type_code AS text))
                     ORDER BY r.sort_order, r.code
                    """
                ),
                {"tenant_id": self.tenant_id, "type_code": resource_type_code},
            )
        ).mappings().all()

        return [
            ResourceSpec(
                id=row["id"],
                tenant_id=row["tenant_id"],
                location_id=row["location_id"],
                resource_type_id=row["resource_type_id"],
                code=row["code"],
                name=row["name"],
                capacity=row["capacity"],
                overbook_allowance=row["overbook_allowance"],
                attributes=dict(row["attributes"] or {}),
                tags=tuple(row["tags"] or ()),
                timezone=row["timezone"],
                sort_order=row["sort_order"],
                is_capacity_shared=row["is_capacity_shared"],
                time_unit=row["time_unit"],
                slot_granularity_minutes=row["slot_granularity_minutes"],
            )
            for row in rows
        ]

    _BOOKING_SELECT = """
        SELECT b.id, b.tenant_id, b.reference, b.status::text AS status,
               b.service_id, s.code AS service_code, s.name AS service_name,
               b.location_id, l.timezone,
               b.starts_at, b.ends_at, b.local_date, b.party_size,
               b.customer_name, b.customer_phone_e164, b.customer_email,
               b.price_total, b.currency,
               b.cancellation_window_minutes, b.cancellation_fee,
               b.cancellation_fee_charged, b.cancelled_at, b.cancellation_reason,
               b.rescheduled_from_id, b.intake_data,
               COALESCE(
                   (SELECT array_agg(r.name ORDER BY bi.role, r.name)
                      FROM booking_items bi
                      JOIN resources r
                        ON r.id = bi.resource_id AND r.tenant_id = bi.tenant_id
                     WHERE bi.booking_id = b.id AND bi.tenant_id = b.tenant_id),
                   '{}'
               ) AS resource_names,
               -- Same ORDER BY as the names above, deliberately: the two arrays
               -- are read positionally by callers that render a name and filter
               -- on the id beside it, and a different sort here would pair the
               -- wrong id with the wrong name in a way nothing type-checks.
               COALESCE(
                   (SELECT array_agg(r.id ORDER BY bi.role, r.name)
                      FROM booking_items bi
                      JOIN resources r
                        ON r.id = bi.resource_id AND r.tenant_id = bi.tenant_id
                     WHERE bi.booking_id = b.id AND bi.tenant_id = b.tenant_id),
                   '{}'
               ) AS resource_ids
          FROM bookings b
          JOIN services  s ON s.id = b.service_id  AND s.tenant_id = b.tenant_id
          JOIN locations l ON l.id = b.location_id AND l.tenant_id = b.tenant_id
         WHERE b.tenant_id = :tenant_id
    """

    async def _one_booking(
        self, clause: str, params: dict[str, Any]
    ) -> BookingRecord | None:
        # `clause` is a module-controlled fragment, never request data — the
        # caller-supplied value always travels as a bound parameter.
        row = (
            await self.session.execute(
                text(f"{self._BOOKING_SELECT} AND {clause} LIMIT 1"),
                {"tenant_id": self.tenant_id, **params},
            )
        ).mappings().one_or_none()
        return _to_record(row) if row else None

    async def _many_bookings(
        self, clause: str, params: dict[str, Any], *, limit: int
    ) -> list[BookingRecord]:
        rows = (
            await self.session.execute(
                text(
                    f"{self._BOOKING_SELECT} AND {clause} "
                    f"ORDER BY b.starts_at LIMIT :limit"
                ),
                {"tenant_id": self.tenant_id, "limit": limit, **params},
            )
        ).mappings().all()
        return [_to_record(row) for row in rows]


def _to_record(row: Any) -> BookingRecord:
    return BookingRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        reference=row["reference"],
        status=row["status"],
        service_id=row["service_id"],
        service_code=row["service_code"],
        service_name=row["service_name"],
        location_id=row["location_id"],
        start_at=row["starts_at"],
        end_at=row["ends_at"],
        local_date=row["local_date"],
        timezone=row["timezone"],
        party_size=row["party_size"],
        customer_name=row["customer_name"],
        customer_phone_e164=row["customer_phone_e164"],
        customer_email=row["customer_email"],
        resource_names=tuple(row["resource_names"] or ()),
        resource_ids=tuple(row["resource_ids"] or ()),
        price_total=row["price_total"],
        currency=row["currency"],
        cancellation_window_minutes=row["cancellation_window_minutes"],
        cancellation_fee=row["cancellation_fee"],
        cancellation_fee_charged=row["cancellation_fee_charged"],
        cancelled_at=row["cancelled_at"],
        cancellation_reason=row["cancellation_reason"],
        rescheduled_from_id=row["rescheduled_from_id"],
        intake_data=dict(row["intake_data"] or {}),
    )


def _holiday_dates(
    stored: date, recurrence: str, from_date: date, to_date: date
) -> list[date]:
    """The concrete dates one holiday row occupies inside a range.

    ``annual`` rows store one month/day and repeat forever, so the year is
    re-based onto every year the window spans (01 §5.8's "without inserting a row
    per year"). A 29 February holiday in a non-leap year is *skipped* rather than
    shifted: shifting it would silently close the practice on a day the calendar
    does not agree is the holiday, and no answer is better than a wrong one.
    """
    if recurrence != "annual":
        return [stored] if from_date <= stored <= to_date else []

    out: list[date] = []
    for year in range(from_date.year, to_date.year + 1):
        try:
            candidate = stored.replace(year=year)
        except ValueError:
            continue  # 29 Feb in a non-leap year
        if from_date <= candidate <= to_date:
            out.append(candidate)
    return out


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, default=str)


def _translate(exc: Exception) -> Exception:
    """Postgres SQLSTATE → domain error, matching ``TenantRepository._translate``.

    ``23P01`` (``exclusion_violation``) is the one that matters: it is how a
    double-booking attempt surfaces, and the booking service catches it
    specifically in order to offer alternatives rather than retry blindly. The
    shared-capacity trigger raises the same SQLSTATE deliberately (01 §5.10), so
    both the exclusive and the shared path arrive here identically.

    ``55P03`` (``lock_not_available``) is ``booking_convert_hold``'s "your hold
    expired while you were talking". It maps to the same conflict kind because
    the caller's next action is the same — re-check availability and re-offer —
    but it carries its own constraint name so a log can tell them apart.
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    cause = getattr(orig, "__cause__", None)
    constraint = getattr(cause, "constraint_name", None)

    if sqlstate == "23P01":
        return ResourceConflictError(
            "The requested time is no longer available",
            kind="overlap",
            constraint=constraint or "ex_booking_items__no_overlap",
        )
    if sqlstate == "55P03":
        return ResourceConflictError(
            "That time was held for you but the hold expired; it may no longer "
            "be available",
            kind="overlap",
            constraint="booking_convert_hold.ttl_expired",
        )
    if sqlstate == "23505":
        return ResourceConflictError(
            "A record with these values already exists",
            kind="duplicate",
            constraint=constraint,
        )
    if sqlstate == "23503":
        return ResourceConflictError(
            "Referenced record does not exist or belongs to another tenant",
            kind="foreign_key",
            constraint=constraint,
        )
    if sqlstate == "23514":
        return ResourceConflictError(
            "Value violates a business rule", kind="check", constraint=constraint
        )
    return exc

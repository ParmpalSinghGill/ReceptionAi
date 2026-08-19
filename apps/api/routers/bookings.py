"""The diary — one day of bookings, and the resources that can filter it.

    GET /api/v1/bookings?date=YYYY-MM-DD&resource_id=<uuid>
    GET /api/v1/resources

Both serve the admin portal's calendar. Two conventions from the contract are
load-bearing here and easy to get wrong, so they are stated where the code is:

**The tenant is never a parameter.** It comes from the bearer token's ``tid``
claim, through :func:`~apps.api.deps.tenant_context`, into ``SET LOCAL
app.tenant_id`` (contract §10). There is no ``tenant_id`` query parameter to
omit and none to forget — a caller cannot name another tenant's diary because
the vocabulary to do so does not exist on this route.

**A day is a local date, not a UTC window.** ``?date=`` is the business's own
calendar day and the query filters on ``bookings.local_date``. Deriving the
window as ``[midnight UTC, +24h)`` would be wrong for every tenant not on UTC,
and wrong by an hour for everyone on the two days a year the clock shifts —
which is the case where an appointment silently disappears from the day it
belongs to. See :meth:`BookingRepository.bookings_on_date`.

**Why the whole day rather than a cursor page.** Contract §2.2 makes cursor
pagination the rule for lists, and this is the deliberate exception: a day is
bounded by the number of resources times the length of the working day. A
calendar grid must lay out every appointment before it can render anything, so
paginating would mean fetching every page anyway — with the cursor's cost and
none of its benefit. Lists that can grow without bound (conversations, the
audit log) still page.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from apps.api.deps import CurrentPrincipal, ReadOnlyDbSession, requires
from apps.api.problems import ProblemException
from apps.api.schemas import (
    BookingListResponse,
    BookingResource,
    BookingSummary,
    Money,
    ResourceListResponse,
    rfc3339,
)
from apps.api.security.permissions import Perm
from fastapi import APIRouter, Depends, Query
from packages.booking.domain import BookingRecord
from packages.core.errors import EntityNotFoundError
from packages.persistence.repositories.booking import BookingRepository
from sqlalchemy import text

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["bookings"])

#: A day's worth of appointments, capped. Well above what a real practice books
#: in a day and low enough that a pathological tenant cannot make one request
#: hold the connection for a second.
_DAY_LIMIT = 500

#: The widest span one request may ask for. A quarter is enough for every
#: "what does the next few months look like" question the list view asks, and
#: short enough that the query stays on the ``local_date`` index.
_MAX_RANGE_DAYS = 92


def _resource_index(
    resources: list[BookingResource],
) -> dict[UUID, BookingResource]:
    return {r.id: r for r in resources}


def _to_summary(
    record: BookingRecord,
    *,
    resources: dict[UUID, BookingResource],
    unmask: bool,
) -> BookingSummary:
    """One repository record as the wire shape.

    ``unmask`` is the viewer asymmetry from 05 §A2.3. A principal without
    ``conversations:read_pii`` still sees the shape of the day — how full it
    is, which resource is busy, what service was booked — and sees ``null``
    where the customer's name and number would be. Masking here rather than in
    the frontend is the only place it means anything: a client-side blank is a
    suggestion, and the data has already crossed the wire.
    """
    # `resource_ids` and `resource_names` are ordered identically by the
    # repository, so a resource the index does not know (deleted since the
    # booking was made) still renders under the name recorded on the row
    # rather than vanishing from the appointment.
    booked: list[BookingResource] = []
    for index, resource_id in enumerate(record.resource_ids):
        known = resources.get(resource_id)
        if known is not None:
            booked.append(known)
            continue
        name = (
            record.resource_names[index]
            if index < len(record.resource_names)
            else str(resource_id)
        )
        booked.append(BookingResource(id=resource_id, code="", name=name))

    price = (
        Money.from_decimal(record.price_total, record.currency)
        if record.price_total is not None and record.currency is not None
        else None
    )

    return BookingSummary(
        id=record.id,
        reference=record.reference,
        status=record.status,
        # `rfc3339` never returns None for a non-None input; the casts keep the
        # type checker honest without a runtime branch that cannot be taken.
        starts_at=rfc3339(record.start_at) or "",
        ends_at=rfc3339(record.end_at) or "",
        local_date=record.local_date.isoformat(),
        timezone=record.timezone,
        service_id=record.service_id,
        service_code=record.service_code,
        service_name=record.service_name,
        customer_name=record.customer_name if unmask else None,
        customer_phone=record.customer_phone_e164 if unmask else None,
        resources=booked,
        price=price,
    )


@router.get(
    "/bookings",
    response_model=BookingListResponse,
    dependencies=[Depends(requires(Perm.BOOKINGS_READ))],
    summary="Bookings for one business day or a range of them",
)
async def list_bookings(
    principal: CurrentPrincipal,
    session: ReadOnlyDbSession,
    on_date: date | None = Query(
        default=None,
        alias="date",
        description=(
            "A single business day, YYYY-MM-DD. Shorthand for from=&to= the "
            "same day; the calendar's day view uses this."
        ),
    ),
    from_date: date | None = Query(
        default=None,
        alias="from",
        description="First business day of the range, inclusive.",
    ),
    to_date: date | None = Query(
        default=None,
        alias="to",
        description="Last business day of the range, inclusive.",
    ),
    resource_id: UUID | None = Query(
        default=None,
        description="Only bookings this resource is allocated to.",
    ),
    status_filter: list[str] | None = Query(
        default=None,
        alias="status",
        description=(
            "Repeatable. Only these statuses. Omit for everything except "
            "cancellations."
        ),
    ),
    include_past: bool = Query(
        default=False,
        description=(
            "Include appointments that have already finished. By default only "
            "those still running or yet to start are returned."
        ),
    ),
    include_cancelled: bool = Query(
        default=False,
        description="Include cancelled bookings, which are hidden by default.",
    ),
) -> BookingListResponse:
    """The diary, with each booking's service and resources joined.

    Two shapes through one route, because they are the same question asked over
    different spans: `?date=` for the calendar's single day, `?from=&to=` for
    the list. Neither defaults to "today" — "today" depends on a timezone, and
    the server's idea of it and the caller's differ for part of every day, so a
    diary that silently showed the wrong day at 11pm would be reported as data
    loss. The client knows the tenant's zone and says which days it means.

    **`include_past=false` is the default and filters on the appointment's
    END.** An appointment that started ten minutes ago and runs for thirty is
    happening right now; it is the row most likely to be asked about at the
    desk, and filtering on the start time would hide precisely that one.
    """
    if on_date is not None and (from_date is not None or to_date is not None):
        raise ProblemException(
            status=422,
            code="validation_failed",
            detail="Pass either `date` or `from`/`to`, not both.",
        )

    start_day = on_date if on_date is not None else from_date
    end_day = on_date if on_date is not None else to_date

    if start_day is None or end_day is None:
        raise ProblemException(
            status=422,
            code="validation_failed",
            detail="A `date`, or both `from` and `to`, are required.",
        )

    if end_day < start_day:
        raise ProblemException(
            status=422,
            code="validation_failed",
            detail="`to` falls before `from`.",
        )

    if (end_day - start_day).days > _MAX_RANGE_DAYS:
        # A bounded answer rather than a slow one. The list view pages by
        # widening its range; an unbounded span would let one request scan a
        # tenant's entire history and hold a connection while it did.
        raise ProblemException(
            status=422,
            code="validation_failed",
            detail=f"A range may span at most {_MAX_RANGE_DAYS} days.",
        )

    repo = BookingRepository(session)

    try:
        location_id = await repo.default_location_id()
        timezone = await repo.location_timezone(location_id)
    except EntityNotFoundError as exc:
        # A tenant with no location cannot have a diary. 404 with the frozen
        # code rather than an empty 200: an empty day and an unconfigured
        # workspace need different screens, and the portal cannot tell them
        # apart from `items: []`.
        raise ProblemException(
            status=404,
            code="not_found",
            detail="This workspace has no location configured yet.",
        ) from exc

    records = await repo.bookings_in_range(
        start_day,
        end_day,
        resource_id=resource_id,
        statuses=status_filter,
        include_cancelled=include_cancelled,
        # `now()` from the application clock rather than the database's, so the
        # boundary is the same instant the rest of the request reasons about.
        ending_after=None if include_past else datetime.now(UTC),
        limit=_DAY_LIMIT,
    )

    resources = _resource_index(await _list_resources(repo))
    unmask = Perm.CONVERSATIONS_READ_PII.value in principal.permissions

    return BookingListResponse(
        # `date` stays the single day when one was asked for, so the calendar's
        # existing contract is unchanged; a range reports both ends instead.
        date=on_date.isoformat() if on_date is not None else None,
        from_date=start_day.isoformat(),
        to_date=end_day.isoformat(),
        timezone=timezone,
        items=[
            _to_summary(record, resources=resources, unmask=unmask)
            for record in records
        ],
    )


@router.get(
    "/resources",
    response_model=ResourceListResponse,
    dependencies=[Depends(requires(Perm.BOOKINGS_READ))],
    summary="The bookable resources a diary can be filtered by",
)
async def list_resources(session: ReadOnlyDbSession) -> ResourceListResponse:
    """Every active, bookable resource.

    Deliberately not derived from the day's bookings: a filter built from what
    is booked cannot offer a resource whose day is empty, and "show me who has
    nothing on" is the question a receptionist actually asks.
    """
    return ResourceListResponse(items=await _list_resources(BookingRepository(session)))


async def _list_resources(repo: BookingRepository) -> list[BookingResource]:
    specs = await repo.list_resources()
    type_codes = await _resource_type_codes(repo)
    return [
        BookingResource(
            id=spec.id,
            code=spec.code,
            name=spec.name,
            resource_type_code=type_codes.get(spec.resource_type_id),
            attributes=spec.attributes,
        )
        for spec in specs
    ]


async def _resource_type_codes(repo: BookingRepository) -> dict[UUID, str]:
    """``resource_type_id`` → its code, so the portal can group the filter.

    The type's *code* rather than its name: a code is stable configuration the
    UI can group on, while the display name is a string the tenant may rename
    at any time.
    """
    rows = (
        await repo.session.execute(
            text(
                "SELECT id, code FROM resource_types "
                " WHERE tenant_id = :tenant_id AND is_active"
            ),
            {"tenant_id": repo.tenant_id},
        )
    ).mappings().all()
    return {row["id"]: row["code"] for row in rows}

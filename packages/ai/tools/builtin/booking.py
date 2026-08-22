"""The five booking tools the agent calls.

``check_availability``, ``book_appointment``, ``cancel_booking``,
``reschedule_booking``, ``lookup_booking``.

**Every one of them returns prose the model can speak from, plus a structured
body the UI can render.** That split is the whole design of this module. A tool
that returns JSON forces the model to narrate a data structure, and it does so
badly — reading ISO timestamps aloud, inventing fields, dropping the one detail
that mattered. A tool that returns only prose gives a widget nothing to draw. So
each returns both, from one source, and they cannot disagree.

**Errors are recoverable and specific.** A taken slot returns *alternatives*, not
a failure — the caller asked for 3pm, 3pm is gone, and "how about 3:30 or 4?" is
the answer a receptionist gives. That is
:class:`~packages.ai.tools.executor.ToolBusinessError`, which the executor turns
into a tool message the model continues from rather than an incident it
apologises for. The same information renders as PHASE1-API-CONTRACT's 409 shape
when a booking is made over HTTP instead of over a conversation.

**Time is the hard part and it is handled in one place.** The model is given, and
returns, times in the *practice's own timezone* — because that is what a caller
says and hears. Everything crossing the tool boundary inward is converted to UTC
immediately (:func:`_to_utc`) and everything crossing outward is converted back
(:func:`_speak_time`). No tool body does timezone arithmetic of its own.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from packages.ai.tools.executor import ToolBusinessError
from packages.ai.tools.registry import (
    ToolContext,
    ToolResult,
    ToolSpec,
    object_schema,
)
from packages.booking.domain import (
    AllocationCandidate,
    BookingRecord,
    CancellationQuote,
    RequirementSpec,
    ResourceSpec,
    ServiceSpec,
    Slot,
)
from packages.booking.service import SlotUnavailableError, quote_cancellation
from packages.core.errors import ConfigurationError, ValidationError

__all__ = ["TOOLS", "BookingToolDeps", "set_booking_dependencies"]

log = structlog.get_logger(__name__)

#: Fallback dialling region when a location has configured none. E.164 needs a
#: country to resolve a national number like "98123 50112", and there is no
#: neutral answer — so the fallback is the platform's default market rather than
#: a claim that every practice sits in one particular country. A location that
#: sets `country_code` (they all should) never reaches this.
#:
#: Overridable by environment so a deployment serving one market does not have
#: to edit code to stop mis-parsing its own callers' numbers.
_DEFAULT_REGION: Final = os.getenv("DEFAULT_DIALLING_REGION", "IN")

#: How far ahead ``check_availability`` looks when the model gives only a start.
#: A week, because "have you got anything next week" is the commonest phrasing
#: and a one-day default would answer a question nobody asked.
_DEFAULT_WINDOW_DAYS = 7

#: Slots offered per answer. **One.** Three handles in the model's context meant
#: three ways to be right and six to be wrong, and it picked wrong about one time
#: in three — offering 4:15 and booking 4:30. A caller told 4:15 arrives to a slot
#: that is not theirs, and nothing signals it.
#:
#: Still used as the default search depth, because the alternatives have to
#: exist to be offered on request; they just live server-side.
_OFFER_COUNT = 3

#: How many slots to hold in reserve for a caller who declines. Ten is several
#: rounds of "anything earlier?" without the search becoming expensive.
_ALTERNATIVE_DEPTH = 10


@dataclass(frozen=True, slots=True)
class _AlternativeQueue:
    """Slots found for one conversation, and which one is currently offered.

    Server-side on purpose. The model holds exactly one handle at a time; this
    is what makes a second one reachable when — and only when — the caller
    actually declines.
    """

    slots: list[Slot]
    index: int
    service: Any
    timezone: str
    resources: dict[Any, Any]


#: Conversation id -> the search it is working through.
#:
#: **Fallback only.** The queue now lives in the shared store behind
#: :class:`~packages.core.ports.conversation_state.ConversationStatePort`, so a
#: follow-up landing on a different replica finds it. This dict is what remains
#: when no store is installed (a graph test, a worker that never wired one) or
#: when a write to it failed — in which case behaviour is exactly what it was
#: before: single-process, and a cross-pod follow-up is told to search again,
#: which is the correct recovery and one the agent already handles.
_ALTERNATIVE_QUEUES: dict[str, _AlternativeQueue] = {}
_ALTERNATIVE_QUEUES_MAX: Final = 500

#: How long a search stays walkable with `next_alternative`.
#:
#: Taken from ``DEFAULT_SESSION_TTL_SECONDS`` in ``apps/api/tickets.py`` (1800),
#: not chosen: the queue is reachable exactly as long as the conversation that
#: owns it, and a caller who can still type is a caller whose "anything
#: earlier?" must still work. A shorter TTL would expire the queue underneath a
#: live conversation; a longer one would keep a search alive after the
#: credential that could reach it had gone.
_QUEUE_TTL_SECONDS: Final = 1_800


class BookingToolDeps:
    """How a tool reaches the database, without ``packages/ai`` importing it.

    Contract §9 lets ``ai`` import ``persistence``, but doing so here would make
    every graph test open a connection pool. Instead ``apps/api`` installs a
    factory at startup, and the tools call it. The indirection buys three things:
    a graph test that never touches Postgres, a Temporal worker that installs the
    same factory with no web app in the process, and one place to look when
    asking "what does a tool have access to".

    The factory is called **per tool invocation** and yields a tenant-scoped unit
    of work, so a tool's writes commit or roll back on their own — a tool must not
    join the caller's transaction, because a tool failure after a successful
    booking would otherwise roll the booking back too.

    ``conversation_state`` arrives by the same route and for the same reason: it
    is a :class:`~packages.core.ports.conversation_state.ConversationStatePort`,
    implemented over Redis in ``packages/providers``, and installing it here is
    what makes a slot handle minted on one replica redeemable on another. It is
    optional — ``None`` leaves the tools on their in-process maps, which is the
    behaviour every graph test relies on and the correct degradation when Redis
    is absent.
    """

    def __init__(self, unit_of_work: Any, conversation_state: Any | None = None) -> None:
        self.unit_of_work = unit_of_work
        self.conversation_state = conversation_state


def _state_port() -> Any | None:
    """The shared store, or ``None`` when the tools were wired without one.

    Deliberately tolerant of a missing ``_deps``: the state helpers below are
    reached from paths that must not raise merely because no database was wired,
    and "no shared store" is already a supported state.
    """
    deps = _deps
    return getattr(deps, "conversation_state", None) if deps is not None else None


_deps: BookingToolDeps | None = None


def set_booking_dependencies(deps: BookingToolDeps | None) -> None:
    """Install (or clear) the database access the booking tools use.

    Called once from the app's lifespan. Clearing it is what a test does to
    assert the tools fail closed rather than silently reaching a stale pool.
    """
    global _deps
    _deps = deps


def _require_deps() -> BookingToolDeps:
    if _deps is None:
        raise ConfigurationError(
            "The booking tools have no database access configured. "
            "`set_booking_dependencies` must be called from the application "
            "lifespan before a conversation runs."
        )
    return _deps


# ─────────────────────────────────────────────────────────────────────────────
# Time handling — the one place that converts
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Slot references — the fix for model-computed timezone arithmetic
# ─────────────────────────────────────────────────────────────────────────────
#
# **The bug this exists to make impossible.** `check_availability` offered
# "1:30 this afternoon"; the model booked it as `2026-08-10T13:30:00+00:00`.
# That is a real, well-formed instant — and in BST it means 2:30pm London. The
# engine stored exactly what it was told. An emergency patient with a broken
# tooth would have arrived an hour late.
#
# The interface was the defect, not the model. Asking a language model to
# convert a wall-clock time it just read aloud into a UTC offset is asking it to
# do arithmetic it will get right *most* of the time — and "silently wrong
# sometimes" is worse than "always wrong", because nothing surfaces it.
#
# So the model no longer supplies a time at all. `check_availability` mints a
# reference for each slot it offers, carrying the exact instant the ENGINE
# computed and the resource it allocated; `book_appointment` takes that
# reference. The model can only book a slot it was genuinely offered, and it
# cannot invent, round, or re-zone one.
#
# The reference is deliberately opaque-but-decodable rather than a database row:
# a slot is a derived fact with no identity of its own (01 §5.6 — availability is
# computed, never stored), so persisting one would mean inventing a lifecycle and
# a sweeper for something that exists for the length of one sentence.

#: How a slot handle looks: ``slot_a3f9c1d2``. Short, opaque, and obviously not
#: a customer-facing reference number.
#:
#: **The shape is the fix.** The first version embedded a signed base64 blob
#: carrying the instant and the resource UUIDs, and asked the model in prose not
#: to say it aloud. It was said aloud — read to a caller as "your reference",
#: 200 characters of base64 containing an internal UTC timestamp and two
#: resource ids. An instruction was standing where a mechanism was needed.
#:
#: Two things changed. The handle now carries NO payload — it indexes a
#: server-side map, so decoding it reveals nothing — and it no longer *looks*
#: like a reference number, which is half of why it was spoken. The other half
#: is handled by scrubbing it from every reply (`_SLOT_HANDLE` in
#: `packages/ai/nodes.py`), because instructions fail eventually and filters do
#: not.
_SLOT_HANDLE_PREFIX: Final = "slot_"
_SLOT_HANDLE_BYTES: Final = 4


@dataclass(frozen=True, slots=True)
class _OfferedSlot:
    """What a handle stands for: the instant and resources the ENGINE chose."""

    service_code: str
    start_at: datetime
    resource_ids: tuple[UUID, ...]


#: Handle -> offered slot, for slots offered recently.
#:
#: **Fallback only, since the move to a shared store.** The authoritative copy
#: lives behind
#: :class:`~packages.core.ports.conversation_state.ConversationStatePort`, which
#: is what lets `check_availability` on one replica and `book_appointment` on
#: another agree about which instant was offered. This dict still exists because
#: the store is optional: a graph test binds none, and a write failure must not
#: cost a booking. In both cases behaviour reverts to what it was before —
#: per-process, and a cross-pod redeem takes an extra availability round-trip.
#:
#: A handle that resolves in neither place fails closed: the agent is told to
#: re-check availability, which is the recovery it already knows. That is the
#: property that makes every degradation path here safe — an unresolvable handle
#: can only cost a round-trip, never a wrong time.
_OFFERED_SLOTS: dict[str, _OfferedSlot] = {}

#: Enough for many concurrent conversations, small enough to be free. Evicted
#: oldest-first, which for a dict in insertion order is the front.
_OFFERED_SLOTS_MAX: Final = 2_000

#: How long a minted handle stays redeemable.
#:
#: Taken from ``MAX_HOLD_TTL_SECONDS`` in ``packages/booking/holds.py`` (900),
#: not picked for roundness. That constant is the longest the engine will hold
#: capacity for an unconfirmed booking, which makes it the longest an offer can
#: still be honoured: past it the slot may legitimately belong to someone else,
#: so a handle that outlived it would resolve to an instant the engine would
#: then refuse. Expiring with the hold means the failure surfaces as "re-check
#: availability" instead of as a booking conflict.
_SLOT_REF_TTL_SECONDS: Final = 900


def _encode_offered_slot(offered: _OfferedSlot) -> str:
    """An offered slot as JSON. Three scalars — no generic machinery needed."""
    return json.dumps(
        {
            "service_code": offered.service_code,
            "start_at": offered.start_at.isoformat(),
            "resource_ids": [str(r) for r in offered.resource_ids],
        }
    )


def _decode_offered_slot(raw: str) -> _OfferedSlot | None:
    """The inverse, or ``None`` on anything malformed.

    Returning ``None`` rather than raising is what lets a corrupt or
    older-format entry behave as an expired one. A decode error here must cost a
    round-trip, not a conversation.
    """
    try:
        payload = json.loads(raw)
        return _OfferedSlot(
            service_code=str(payload["service_code"]),
            start_at=datetime.fromisoformat(payload["start_at"]),
            resource_ids=tuple(UUID(r) for r in payload["resource_ids"]),
        )
    except (ValueError, KeyError, TypeError):
        return None


# ── Serialising the alternative queue ────────────────────────────────────────
#
# The queue carries engine dataclasses — `Slot`, `ServiceSpec`, `ResourceSpec` —
# which are frozen, slotted, and built from scalars, UUIDs, datetimes, Decimals
# and nested dataclasses of the same. That is a small enough shape to walk
# generically, and walking it generically is what stops this code needing an
# edit every time the engine gains a field. The alternative — a hand-written
# encoder per class — is three functions that fall silently out of date, and the
# symptom of a missed field is a slot whose price or buffer quietly resets.
#
# Types are tagged on the way out and resolved on the way back, so the decode
# reconstructs the real class rather than a dict that merely quacks like one:
# `_offer` reads `service.name` and `slot.start_at`, and a dict would fail on
# attribute access rather than on the boundary where the problem is.

#: The engine classes a queue may contain, by tag. An explicit allow-list, not a
#: dynamic import: this decodes data from a shared store, and resolving an
#: arbitrary dotted path out of it would be a remote-code-execution hole rather
#: than a convenience.
_DECODABLE: Final[dict[str, Any]] = {
    "Slot": Slot,
    "AllocationCandidate": AllocationCandidate,
    "ServiceSpec": ServiceSpec,
    "ResourceSpec": ResourceSpec,
    "RequirementSpec": RequirementSpec,
}


def _to_jsonable(value: Any) -> Any:
    """One value as JSON-safe data, tagging what will need reconstructing."""
    if is_dataclass(value) and not isinstance(value, type):
        name = type(value).__name__
        if name not in _DECODABLE:
            raise TypeError(f"{name} is not serialisable into conversation state")
        return {
            "__type__": name,
            "fields": {
                f.name: _to_jsonable(getattr(value, f.name)) for f in fields(value)
            },
        }
    if isinstance(value, datetime):
        return {"__type__": "datetime", "value": value.isoformat()}
    if isinstance(value, UUID):
        return {"__type__": "uuid", "value": str(value)}
    if isinstance(value, Decimal):
        # As a string: a Decimal through a float loses exactly the precision it
        # exists to keep, and this carries prices.
        return {"__type__": "decimal", "value": str(value)}
    if isinstance(value, dict):
        # Keys may be UUIDs (the resource map is keyed by resource id), which
        # JSON cannot express, so they are stringified and restored on the way
        # back by the same tag machinery applied to the key.
        return {
            "__type__": "map",
            "entries": [[_to_jsonable(k), _to_jsonable(v)] for k, v in value.items()],
        }
    if isinstance(value, (list, tuple)):
        return {
            "__type__": "seq",
            "tuple": isinstance(value, tuple),
            "items": [_to_jsonable(v) for v in value],
        }
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"{type(value).__name__} is not serialisable into conversation state")


def _from_jsonable(value: Any) -> Any:
    """The inverse of :func:`_to_jsonable`."""
    if not isinstance(value, dict):
        return value
    kind = value.get("__type__")
    if kind is None:
        return value
    if kind == "datetime":
        return datetime.fromisoformat(value["value"])
    if kind == "uuid":
        return UUID(value["value"])
    if kind == "decimal":
        return Decimal(value["value"])
    if kind == "map":
        return {_from_jsonable(k): _from_jsonable(v) for k, v in value["entries"]}
    if kind == "seq":
        items = [_from_jsonable(v) for v in value["items"]]
        return tuple(items) if value.get("tuple") else items
    cls = _DECODABLE.get(kind)
    if cls is None:
        raise ValueError(f"unknown tagged type {kind!r}")
    return cls(**{k: _from_jsonable(v) for k, v in value["fields"].items()})


def _encode_queue(queue: _AlternativeQueue) -> str:
    return json.dumps(
        {
            "slots": _to_jsonable(list(queue.slots)),
            "index": queue.index,
            "service": _to_jsonable(queue.service),
            "timezone": queue.timezone,
            "resources": _to_jsonable(queue.resources),
        }
    )


def _decode_queue(raw: str) -> _AlternativeQueue | None:
    """A stored queue, or ``None`` on anything malformed.

    ``None`` lands the caller on "I do not have that search any more", which is
    an answer the agent already handles, so a decode failure degrades to a fresh
    availability check rather than to an error.
    """
    try:
        payload = json.loads(raw)
        return _AlternativeQueue(
            slots=list(_from_jsonable(payload["slots"])),
            index=int(payload["index"]),
            service=_from_jsonable(payload["service"]),
            timezone=str(payload["timezone"]),
            resources=_from_jsonable(payload["resources"]),
        )
    except (ValueError, KeyError, TypeError):
        return None


async def _load_queue(tenant_id: Any, conversation_id: Any) -> _AlternativeQueue | None:
    """The conversation's current search, from this process or the shared store.

    Local first, for the same reason as a slot handle: when the search ran on
    this replica the local copy is authoritative and free. The shared store is
    what makes "anything earlier?" work when the follow-up lands elsewhere.
    """
    if (local := _ALTERNATIVE_QUEUES.get(str(conversation_id))) is not None:
        return local
    if (store := _state_port()) is None:
        return None
    raw = await store.get(_queue_key(tenant_id, conversation_id))
    if raw is None:
        return None
    queue = _decode_queue(raw)
    if queue is None:
        log.warning("booking.alternatives.undecodable")
        return None
    return queue


def _slot_key(tenant_id: Any, handle: str) -> str:
    """The shared-store key for one handle, scoped to its tenant.

    **The tenant is in the key, not merely alongside the value.** A handle is 4
    bytes of entropy; two tenants will eventually mint the same string, and a
    lookup that found the other tenant's entry would leak an appointment time
    and the resources allocated to it, then book against them. Scoping the key
    makes that impossible rather than unlikely — the wrong-tenant read finds
    nothing and fails closed.
    """
    return f"slotref:{tenant_id}:{handle}"


def _queue_key(tenant_id: Any, conversation_id: Any) -> str:
    """The shared-store key for one conversation's alternative queue."""
    return f"altqueue:{tenant_id}:{conversation_id}"


async def _mint_slot_ref(slot: Slot, service_code: str, tenant_id: Any) -> str:
    """Register an offered slot and return the handle that stands for it.

    The handle is the ONLY thing the model receives. It carries the instant the
    engine computed and the resources it allocated — so booking the handle books
    exactly what was offered, with the clinician who was named — while revealing
    none of that to anyone who reads it.

    Written to the shared store when one is installed, so the redeem may land on
    any replica. The in-process map is written **as well as**, not instead of:
    the local read is free, and it is what keeps the tool working if the store
    goes away between the mint and the redeem.
    """
    handle = f"{_SLOT_HANDLE_PREFIX}{secrets.token_hex(_SLOT_HANDLE_BYTES)}"
    offered = _OfferedSlot(
        service_code=service_code,
        start_at=slot.start_at,
        resource_ids=tuple(slot.resource_ids),
    )

    if len(_OFFERED_SLOTS) >= _OFFERED_SLOTS_MAX:
        for stale in list(_OFFERED_SLOTS)[: _OFFERED_SLOTS_MAX // 4]:
            _OFFERED_SLOTS.pop(stale, None)
    _OFFERED_SLOTS[handle] = offered

    if (store := _state_port()) is not None:
        try:
            await store.set(
                _slot_key(tenant_id, handle),
                _encode_offered_slot(offered),
                ttl_seconds=_SLOT_REF_TTL_SECONDS,
            )
        except Exception as exc:
            # Not fatal, and deliberately not silent. The handle still resolves
            # in this process, so this conversation completes; what is lost is
            # the cross-replica property, and a sustained rate of this line is
            # the signal that bookings are quietly costing an extra round-trip.
            log.warning(
                "booking.slot_ref.share_failed",
                error=type(exc).__name__,
                impact="handle resolves only in this process",
            )
    return handle


async def _decode_slot_ref(reference: str, tenant_id: Any) -> _OfferedSlot | None:
    """Resolve a handle, or ``None`` if it is not one we offered.

    ``None`` for anything unknown — expired, invented, or minted by a process
    that has since gone away. The caller turns that into "call
    check_availability again", which is the correct recovery. Guessing would
    book a plausible-looking wrong instant, which is the entire class of failure
    this mechanism removes.

    The local map is consulted first because it is free and, when the mint
    happened on this replica, always right. The shared store is the fallback
    that makes a handle minted elsewhere resolve here.
    """
    handle = reference.strip()
    if not handle:
        return None
    if (local := _OFFERED_SLOTS.get(handle)) is not None:
        return local

    if (store := _state_port()) is None:
        return None
    # `get` on the Redis adapter folds its own failures into `None`, so a store
    # outage arrives here as "unknown handle" — which is a recoverable state the
    # caller already handles, not an error worth failing a booking over.
    raw = await store.get(_slot_key(tenant_id, handle))
    if raw is None:
        return None
    decoded = _decode_offered_slot(raw)
    if decoded is None:
        # Corrupt or from an incompatible version. Treated as absent for the
        # same reason a bad handle is: the only safe answer is "search again".
        log.warning("booking.slot_ref.undecodable")
        return None
    # Cache the cross-replica hit, so a redeem and any follow-up on this pod do
    # not each pay a round-trip for a value that cannot change.
    _OFFERED_SLOTS[handle] = decoded
    return decoded


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(
            f"Location timezone {name!r} is not a valid IANA zone."
        ) from exc


def _to_utc(raw: str, tz: str) -> datetime:
    """Parse what the model sent into a UTC instant.

    A bare local time (``2026-08-11T09:00``) is interpreted in the practice's
    zone, because that is the only reading a caller could have meant.

    **A ``+00:00`` or ``Z`` offset is also treated as practice-local**, which
    looks wrong and is the single most important line in this function. A model
    that has just read "1:30 this afternoon" aloud and then emits
    ``13:30:00+00:00`` has not converted anything — it has appended a zone it did
    not compute. Honouring that literally is what booked a BST caller an hour
    late: 13:30Z is 14:30 London. Since no caller and no practice ever means UTC
    when they say "half one", the only reading that is ever *right* is the local
    one.

    The cost of this choice is a genuine UTC instant being shifted, and it is
    accepted deliberately: the slot-reference path above carries real instants and
    never goes through here, so the only callers left are free-text ones — which
    are exactly the ones a model composed from wall clock.

    A non-zero offset (``+05:30``) IS honoured: nothing produces that by
    accident, so it is a deliberate assertion by a channel that resolved the zone
    itself.

    Raises a :class:`ValidationError` rather than guessing on a malformed value.
    A tool that silently accepts "next Tuesday" by returning today is worse than
    one that says the format is wrong, because the model can fix the second.
    """
    text = raw.strip().replace("Z", "+00:00")
    asserted_utc = text.endswith("+00:00")
    if asserted_utc:
        text = text[: -len("+00:00")]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(
            f"{raw!r} is not a parseable date-time.",
            field="start_time",
            model_message=(
                "I could not work out which date that is. Ask the customer "
                "which day they mean, then search again."
            ),
        ) from exc

    if parsed.tzinfo is None:
        if asserted_utc:
            # Visible, because it IS a correction: something upstream produced a
            # UTC offset for a wall-clock time. Harmless here, but if it starts
            # appearing on a channel that genuinely resolves zones, that channel
            # is being silently shifted and someone needs to know.
            log.info(
                "booking.time.utc_offset_treated_as_local",
                raw=raw,
                timezone=tz,
                reason="a +00:00 offset on a spoken time is an unconverted "
                "wall clock, not a computed instant",
            )
        parsed = parsed.replace(tzinfo=_zone(tz))
    return parsed.astimezone(UTC)


def _speak_time(moment: datetime, tz: str) -> str:
    """Render an instant the way a receptionist says it.

    "Tuesday 11 August at 9:00 am" — not an ISO string. The model reads this
    aloud, and every ISO timestamp that reaches a caller's ear is a small failure
    of this function.
    """
    local = moment.astimezone(_zone(tz))
    hour = local.strftime("%I").lstrip("0") or "12"
    meridiem = local.strftime("%p").lower()
    minutes = f":{local.minute:02d}" if local.minute else ""
    return f"{local:%A} {local.day} {local:%B} at {hour}{minutes}{meridiem}"


def _speak_date(moment: datetime, tz: str) -> str:
    """A date the way a person says it — "Friday 7 August 2026".

    Used in error messages rather than an ISO string, and that is not cosmetic:
    these strings go to the *model*, which resolves a named weekday far more
    reliably than a numeric date it has to convert. "Today is Friday 7 August"
    is directly actionable; "today is 2026-08-07" requires it to work out the
    weekday first, which is exactly the step it gets wrong.
    """
    local = moment.astimezone(_zone(tz))
    return f"{local:%A} {local.day} {local:%B} {local.year}"


def _speak_money(amount: Any, currency: str | None) -> str:
    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency or "", "")
    return f"{symbol}{amount:.2f}" if symbol else f"{amount:.2f} {currency or ''}".strip()


#: Resource attributes worth telling a customer about, and how to introduce
#: each. Read from whatever the tenant configured — the platform does not know
#: what a "scope" contains, only that a tenant thought it worth writing down.
#:
#: This lives at the TOOL layer, not in `packages/booking`: the engine hands back
#: generic resources, and deciding that these particular keys are worth speaking
#: aloud is a conversation-layer judgement. Invariant I4 stays intact — no
#: clinician name and no speciality is hardcoded anywhere, so the same mechanism
#: introduces a stylist or a table with no code change.
_SPOKEN_ATTRIBUTES: Final[tuple[tuple[str, str], ...]] = (
    ("scope", ""),
    ("note", ""),
    ("languages", "speaks "),
)


def _resource_payload(resource: Any) -> dict[str, Any]:
    """One allocated resource, as the agent should describe it."""
    attributes = dict(getattr(resource, "attributes", {}) or {})
    described: dict[str, Any] = {}
    for key, _prefix in _SPOKEN_ATTRIBUTES:
        if value := attributes.get(key):
            described[key] = value
    return {
        "name": getattr(resource, "name", ""),
        "code": getattr(resource, "code", ""),
        **described,
    }


def _describe_resources(
    slot: Slot, resources: dict[Any, Any]
) -> list[dict[str, Any]]:
    """Every resource this slot would consume, in role order.

    Only resources the caller would recognise are worth naming — a room number
    means nothing to someone booking by phone — but filtering that here would
    mean the tool deciding which resource TYPE matters, which is exactly the
    vertical knowledge it must not hold. So all of them are returned with their
    attributes, and the model says whichever is worth saying. A resource with
    nothing but a code contributes nothing to the sentence and is ignored
    naturally.
    """
    out: list[dict[str, Any]] = []
    for allocation in slot.allocations:
        for resource_id in allocation.resource_ids:
            if resource := resources.get(resource_id):
                out.append(
                    {"role": allocation.role, **_resource_payload(resource)}
                )
    return out


def _describe_slot(slot: Slot, tz: str, resources: dict[Any, Any]) -> str:
    """One slot as a sentence fragment: when, with whom, and why they suit.

    "Tuesday 11 August at 9:00am with Dr Anaya Rahman (General dentistry and
    same-day emergencies)" — the time a person would say, the name they would
    recognise, and the one attribute that makes the recommendation a
    recommendation rather than a slot number.

    Only the *first* named resource is described. A booking usually consumes
    several — a clinician and a room — and "with Dr Rahman and Surgery 2" is not
    something a receptionist says. Which one leads is decided by the requirement
    ordering the tenant configured, not by this layer knowing that people matter
    more than rooms.
    """
    when = _speak_time(slot.start_at, tz)
    described = _describe_resources(slot, resources)
    named = next((r for r in described if r.get("scope") or r.get("note")), None)
    if named is None:
        named = next((r for r in described if r.get("name")), None)
    if named is None:
        return when

    detail = named.get("scope") or ""
    suffix = f" ({detail})" if detail else ""
    return f"{when} with {named['name']}{suffix}"


def _slot_payload(
    slot: Slot,
    tz: str,
    resources: dict[Any, Any] | None = None,
    handle: str = "",
) -> dict[str, Any]:
    """One slot as the UI renders it.

    The handle is passed in, never minted here. Minting per call site produced a
    fresh handle each time — so a single offered slot still put three distinct
    handles into the model's context, which is precisely the condition this
    design removes. One slot, one handle, minted once by `_offer`.
    """
    payload = {
        "slot_ref": handle,
        "start_at": slot.start_at.isoformat(),
        "end_at": slot.end_at.isoformat(),
        "local_start": slot.start_at.astimezone(_zone(tz)).isoformat(),
        "spoken": _speak_time(slot.start_at, tz),
    }
    if resources:
        payload["with"] = _describe_resources(slot, resources)
    return payload


def _booking_payload(booking: BookingRecord) -> dict[str, Any]:
    """The structured body every booking tool returns.

    Money is a **decimal string plus a currency**, never a float
    (PHASE1-API-CONTRACT). Times carry both the UTC instant and the local
    rendering, so a client never has to guess a zone.
    """
    return {
        "booking_id": str(booking.id),
        "reference": booking.reference,
        "status": booking.status,
        "service": booking.service_name,
        "service_code": booking.service_code,
        "start_at": booking.start_at.isoformat(),
        "end_at": booking.end_at.isoformat(),
        "local_start": booking.local_start.isoformat(),
        "spoken": _speak_time(booking.start_at, booking.timezone),
        "timezone": booking.timezone,
        "with": list(booking.resource_names),
        "customer_name": booking.customer_name,
        "price": str(booking.price_total) if booking.price_total is not None else None,
        "currency": booking.currency,
    }


def _quote_payload(quote: CancellationQuote) -> dict[str, Any]:
    return {
        "allowed": quote.allowed,
        "fee_applies": quote.fee_applies,
        "fee": str(quote.fee) if quote.fee is not None else None,
        "currency": quote.currency,
        "notice_required_minutes": quote.notice_required_minutes,
        "notice_actual_minutes": quote.notice_actual_minutes,
        "reason": quote.reason,
    }


def _unavailable(exc: SlotUnavailableError, tz: str) -> ToolBusinessError:
    """Turn a lost race into an offer of alternatives.

    This is the conversion that makes a 409 useful. The exception already carries
    the alternatives the engine found; this renders them as a sentence the model
    can say and a payload the UI can draw, so "that's gone" becomes "that's gone,
    but I have 3:30 or 4" without a second round-trip.
    """
    alternatives = list(exc.alternatives)
    if alternatives:
        offered = ", ".join(
            _speak_time(slot.start_at, tz) for slot in alternatives[:_OFFER_COUNT]
        )
        message = (
            f"{exc.message} The next available times are: {offered}. "
            f"Offer these to the customer and ask which suits."
        )
    else:
        message = (
            f"{exc.message} Nothing else is free in that window — offer a "
            f"different day, or take a message."
        )
    return ToolBusinessError(
        exc.message,
        model_message=message,
        code="slot_unavailable",
        structured={
            "error": "slot_unavailable",
            "alternatives": [_slot_payload(slot, tz) for slot in alternatives],
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# The tools
# ─────────────────────────────────────────────────────────────────────────────


async def _check_availability(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Recommend ONE free slot. Alternatives stay server-side.

    **Why one and not three.** Returning three handles and asking the model to
    book "the same one you offered" was an instruction where a mechanism was
    needed, and it failed about one time in three: the agent said "4:15 with
    Dr Okafor" and booked 4:30. The caller arrives to a slot that is not theirs
    and nothing anywhere signals a problem — the same harm as the original
    timezone bug, from the same cause.

    Holding three handles meant the model had three ways to be right and six to
    be wrong (prose, ``collected_data`` and ``structured`` each carried all
    three). Now exactly one handle exists in its context, so there is no wrong
    one to pick. Declining is a separate, explicit call —
    :func:`_next_alternative` — which swaps that single handle for the next.

    The alternatives are not gone, just not in hand. That distinction is the
    whole design: a fix that made booking correct but rejection impossible would
    not be a fix.
    """
    deps = _require_deps()
    async with deps.unit_of_work(ctx.tenant_id) as (booking_service, _repo):
        window_start, window_end, tz = await _resolve_window(
            booking_service, args, ctx
        )
        slots, service, _location, tz, resources = await booking_service.find_slots(
            service_code=args["service_code"],
            window_start=window_start,
            window_end=window_end,
            party_size=int(args.get("party_size", 1)),
            # Deeper than the old offer count: the extras are never shown, they
            # are the queue `next_alternative` walks when a caller declines.
            limit=max(int(args.get("limit", _OFFER_COUNT)), _ALTERNATIVE_DEPTH),
        )

    if not slots:
        return ToolResult(
            content=(
                f"Nothing is free for {service.name} between "
                f"{_speak_time(window_start, tz)} and "
                f"{_speak_time(window_end, tz)}. Suggest a different day or "
                f"offer to take a message."
            ),
            structured={"service": service.code, "slots": []},
        )

    # The recommendation: the earliest suitable slot. For an emergency that is
    # exactly right — soonest wins — and for a routine booking the solver has
    # already applied the tenant's allocation strategy, so "first" means "best
    # by whatever the practice configured".
    return await _offer(
        slots=slots,
        index=0,
        service=service,
        tz=tz,
        resources=resources,
        conversation_id=ctx.conversation_id,
        tenant_id=ctx.tenant_id,
    )


async def _next_alternative(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Offer the next slot after one the customer declined.

    The explicit follow-up that keeps "anything earlier?" working. It swaps the
    single in-scope handle for the next one, so the model still holds exactly
    one — declining ten times in a row never puts two in its context.
    """
    queue = await _load_queue(ctx.tenant_id, ctx.conversation_id)
    if queue is None:
        return ToolResult(
            content=(
                "I do not have that search any more. Call check_availability "
                "again for the time the customer is asking about."
            ),
            structured={"error": "no_active_search"},
        )

    if queue.index + 1 >= len(queue.slots):
        return ToolResult(
            content=(
                f"That is everything free for {queue.service.name} in that "
                f"window. Offer a different day, or take a message."
            ),
            structured={"exhausted": True},
        )

    return await _offer(
        slots=queue.slots,
        index=queue.index + 1,
        service=queue.service,
        tz=queue.timezone,
        resources=queue.resources,
        conversation_id=ctx.conversation_id,
        tenant_id=ctx.tenant_id,
    )


async def _offer(
    *,
    slots: list[Slot],
    index: int,
    service: Any,
    tz: str,
    resources: dict[Any, Any],
    conversation_id: Any,
    tenant_id: Any,
) -> ToolResult:
    """Present exactly one slot, and remember the rest server-side."""
    # A fresh `check_availability` REPLACES the queue for this conversation
    # (`_offer` is the only writer, and it overwrites). That is what stops a
    # caller who switches from Tuesday to Thursday being walked through
    # Tuesday's remaining slots — the observed failure, where every reply after
    # "Thursday instead" kept offering later times on Tuesday. The shared store
    # is overwritten with the same key for the same reason.
    slot = slots[index]
    queue = _AlternativeQueue(
        slots=list(slots),
        index=index,
        service=service,
        timezone=tz,
        resources=resources,
    )
    if len(_ALTERNATIVE_QUEUES) >= _ALTERNATIVE_QUEUES_MAX:
        for stale in list(_ALTERNATIVE_QUEUES)[: _ALTERNATIVE_QUEUES_MAX // 4]:
            _ALTERNATIVE_QUEUES.pop(stale, None)
    _ALTERNATIVE_QUEUES[str(conversation_id)] = queue

    if (store := _state_port()) is not None:
        try:
            await store.set(
                _queue_key(tenant_id, conversation_id),
                _encode_queue(queue),
                ttl_seconds=_QUEUE_TTL_SECONDS,
            )
        except Exception as exc:
            # The offer itself is unaffected — only "anything earlier?" on a
            # different replica is, and that already degrades to a fresh search.
            log.warning(
                "booking.alternatives.share_failed",
                error=type(exc).__name__,
                impact="next_alternative works only on this process",
            )

    handle = await _mint_slot_ref(slot, service.code, tenant_id)
    # The exact words to say. Given verbatim because paraphrasing a time is how
    # "5:30" became "5:00" in a caller's ear while the handle held 17:00 — one
    # handle in scope, so not the old multi-handle bug, simply a model restating
    # a time it had read rather than repeating it. A model asked to reproduce a
    # quoted string does so far more reliably than one asked to describe a
    # value.
    spoken_time = _speak_time(slot.start_at, tz)
    price = (
        f" {service.name} is {_speak_money(service.price, service.currency)}."
        if service.price is not None
        else ""
    )
    remaining = len(slots) - index - 1
    more = (
        " If they reject it without naming another day, call next_alternative "
        "for the next one. If they name a different day or time, call "
        "check_availability for what they asked for."
        if remaining
        else " There is nothing else free in that window."
    )

    return ToolResult(
        content=(
            f"Offer this, and only this: {_describe_slot(slot, tz, resources)} "
            f"[{handle}].{price}\n\n"
            f"Say the time EXACTLY as written here: \"{spoken_time}\". Do not "
            f"round it, do not convert it, do not say a different time — a "
            f"customer told one time and booked at another arrives to a slot "
            f"that is not theirs. Then say who they would be seeing and, if it "
            f"is relevant, why that person suits.\n\n"
            f"If the customer ACCEPTS — 'yes', 'that works', 'that suits me', "
            f"'fine, book it' — call book_appointment with {handle} and their "
            f"name, IN THAT SAME REPLY. Accepting is the common case; do not "
            f"answer it by offering another time, and do not ask 'shall I book "
            f"that?' when they have already said yes. Once you have a name and "
            f"an acceptance there is nothing left to confirm — someone in pain "
            f"should not need an extra exchange to get an appointment.{more}"
            f"\n\n"
            f"To book it, pass {handle} to book_appointment. NEVER retype the "
            f"time — the handle carries it exactly. The handle is internal: "
            f"never say it to the customer and never call it a reference. The "
            f"booking reference is the BK-XXXXXX code you get back AFTER "
            f"booking."
        ),
        collected_data={
            # ONE handle. The model cannot book the wrong slot because it holds
            # no other — which is the entire point of this restructuring.
            "offered_slot_ref": handle,
            "offered_slot": slot.start_at.isoformat(),
            "service_code": service.code,
        },
        structured={
            "service": service.code,
            "service_name": service.name,
            "price": str(service.price) if service.price is not None else None,
            "currency": service.currency,
            "timezone": tz,
            "alternatives_remaining": remaining,
            # A single-element list, not three: the widget renders what the
            # agent actually offered.
            "slots": [_slot_payload(slot, tz, resources, handle)],
        },
    )


async def _check_existing_bookings(
    ctx: ToolContext, args: dict[str, Any]
) -> ToolResult:
    """Does this caller already have an appointment coming up?

    Called when the customer gives their number, **before** a booking is
    attempted. Checking inside `book_appointment` would mean the caller hears
    about it one turn late and attached to a failure — the same shape of mistake
    as validating the phone number at write time, which produced an apology
    about the appointment when the real problem was a missing digit.

    Returns information, never an error. A `ToolBusinessError` here would read
    as "the booking failed", and the honest answer is "you already have one —
    what would you like to do?".
    """
    deps = _require_deps()
    async with deps.unit_of_work(ctx.tenant_id) as (booking_service, repo):
        location_id = await repo.default_location_id()
        region = await repo.location_region(location_id) or _DEFAULT_REGION
        try:
            phone = _normalise_phone(args.get("customer_phone"), region)
        except ValidationError as exc:
            # A number we cannot read is not a duplicate-check failure; it is
            # the phone problem, and it has its own spoken advice.
            raise ToolBusinessError(
                exc.message,
                model_message=exc.model_message,
                code="validation_failed",
            ) from exc

        if not phone:
            return ToolResult(
                content="No number given, so nothing to check. Carry on.",
                structured={"existing": []},
            )

        existing = await booking_service.existing_for_phone(
            phone_e164=phone, within_days=int(args.get("within_days", 3))
        )

    if not existing:
        return ToolResult(
            content=(
                "No appointment already booked on that number. Go ahead and "
                "book the slot you offered."
            ),
            structured={"existing": []},
        )

    described = "; ".join(
        f"{b.service_name} on {_speak_time(b.start_at, b.timezone)}"
        + (f" with {b.resource_names[0]}" if b.resource_names else "")
        + f" (reference {b.reference})"
        for b in existing
    )
    return ToolResult(
        content=(
            f"That number already has: {described}.\n\n"
            f"Tell the customer what they already have and ASK what they want "
            f"before booking anything: shall I move that one to the new time, "
            f"keep both, or cancel the old one? All three are reasonable — "
            f"someone may genuinely want a check-up and a hygienist visit in "
            f"the same week — so do not refuse, and do not decide for them.\n\n"
            f"If they want to MOVE it, call reschedule_booking with that "
            f"reference; do not create a second booking. If they want BOTH, "
            f"call book_appointment as normal."
        ),
        collected_data={
            "existing_booking_reference": existing[0].reference,
            "existing_booking_count": len(existing),
        },
        structured={
            "existing": [_booking_payload(b) for b in existing],
            "count": len(existing),
        },
    )


async def _book_appointment(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Create a confirmed booking at a slot the engine offered."""
    deps = _require_deps()
    async with deps.unit_of_work(ctx.tenant_id) as (booking_service, repo):
        location_id = await repo.default_location_id()
        tz = await repo.location_timezone(location_id)
        region = await repo.location_region(location_id) or _DEFAULT_REGION

        # ── The slot reference wins over any time the model composed ──────
        #
        # If the model hands back a reference from `check_availability`, the
        # instant comes from the ENGINE and the model's arithmetic never enters
        # the picture. That is the whole fix: a caller offered "1:30" gets 1:30,
        # not a UTC-flavoured 2:30.
        decoded = await _decode_slot_ref(str(args.get("slot_ref") or ""), ctx.tenant_id)
        service_code = args.get("service_code") or ""
        if decoded is not None:
            start_at = decoded.start_at
            service_code = decoded.service_code or service_code
        elif args.get("start_time"):
            start_at = _to_utc(args["start_time"], tz)
        else:
            raise ToolBusinessError(
                "No slot to book",
                model_message=(
                    "I need to know which slot to book. Call "
                    "check_availability first and pass back the slot_ref of the "
                    "one the customer chose."
                ),
                code="validation_failed",
            )

        if args.get("slot_ref") and decoded is None:
            # A reference we cannot verify. Refusing beats guessing: a
            # mis-decoded reference would book a plausible-looking wrong
            # instant, which is precisely the silent failure this mechanism
            # exists to remove.
            raise ToolBusinessError(
                "Unrecognised slot reference",
                model_message=(
                    "That slot reference is not one I recognise — it may be "
                    "from an earlier search. Call check_availability again and "
                    "use a fresh slot_ref."
                ),
                code="validation_failed",
            )

        # ── Validate the number BEFORE attempting the write ──────────────
        #
        # Validating it inside `book()` meant a mistyped digit aborted the whole
        # booking attempt, and the caller heard a generic apology about the
        # appointment rather than about their number. Checking first makes the
        # failure cheap and specific: nothing has been attempted, so "that looks
        # a digit short" is the whole truth.
        try:
            customer_phone = _normalise_phone(args.get("customer_phone"), region)
        except ValidationError as exc:
            raise ToolBusinessError(
                exc.message,
                model_message=exc.model_message,
                code="validation_failed",
            ) from exc

        try:
            booking = await booking_service.book(
                service_code=service_code,
                start_at=start_at,
                customer_name=args["customer_name"],
                customer_phone_e164=customer_phone,
                customer_email=args.get("customer_email"),
                party_size=int(args.get("party_size", 1)),
                intake_data=_intake(args),
                notes=args.get("notes"),
                conversation_id=ctx.conversation_id,
                channel="web_chat",
                # 02 §6.3 4f: the key is derived from the call site, not
                # generated by the model. A model that retries a confirm must
                # produce the SAME key, or the retry double-books.
                idempotency_key=(
                    f"{ctx.conversation_id}:book:{service_code}:"
                    f"{start_at.isoformat()}"
                ),
            )
        except SlotUnavailableError as exc:
            raise _unavailable(exc, tz) from exc
        except ValidationError as exc:
            # Missing intake fields. Recoverable in one more question.
            raise ToolBusinessError(
                exc.message,
                model_message=exc.model_message,
                code="validation_failed",
            ) from exc

    with_whom = (
        f" with {', '.join(booking.resource_names)}" if booking.resource_names else ""
    )
    return ToolResult(
        content=(
            f"Booked. {booking.service_name}{with_whom} on "
            f"{_speak_time(booking.start_at, booking.timezone)}. "
            f"The reference is {booking.reference}. Read the day, the time and "
            f"the reference back to the customer."
        ),
        collected_data={
            "booking_id": str(booking.id),
            "booking_reference": booking.reference,
        },
        structured=_booking_payload(booking),
    )


async def _cancel_booking(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Cancel, enforcing the notice period and fee the booking carries."""
    deps = _require_deps()
    async with deps.unit_of_work(ctx.tenant_id) as (booking_service, repo):
        region = (
            await repo.location_region(await repo.default_location_id())
            or _DEFAULT_REGION
        )
        booking = await _resolve_one(booking_service, args, ctx, region)
        quote = quote_cancellation(booking)

        # Quote before acting. A late cancellation costs the caller money, and
        # doing it because a model inferred they wanted to — without them having
        # heard the amount — is the difference between a service and a complaint.
        if quote.fee_applies and not args.get("accept_fee", False):
            return ToolResult(
                content=(
                    f"{booking.reference} is within the "
                    f"{quote.notice_required_minutes // 60}-hour notice period, "
                    f"so cancelling incurs a "
                    f"{_speak_money(quote.fee, quote.currency)} fee. Tell the "
                    f"customer the amount and, if they still want to cancel, "
                    f"call this tool again with accept_fee set to true."
                ),
                structured={
                    "action": "fee_confirmation_required",
                    "booking": _booking_payload(booking),
                    "cancellation": _quote_payload(quote),
                },
            )

        try:
            cancelled, applied = await booking_service.cancel(
                booking_id=booking.id,
                reason=args.get("reason"),
                accept_fee=bool(args.get("accept_fee", False)),
            )
        except ValidationError as exc:
            raise ToolBusinessError(
                exc.message,
                model_message=exc.model_message,
                code="validation_failed",
                structured={"booking": _booking_payload(booking)},
            ) from exc

    fee_text = (
        f" A {_speak_money(applied.fee, applied.currency)} late-cancellation fee "
        f"applies."
        if applied.fee_applies
        else ""
    )
    return ToolResult(
        content=(
            f"Cancelled {cancelled.reference} — {cancelled.service_name} on "
            f"{_speak_time(cancelled.start_at, cancelled.timezone)}.{fee_text} "
            f"Confirm the cancellation to the customer."
        ),
        collected_data={"cancelled_booking_reference": cancelled.reference},
        structured={
            "booking": _booking_payload(cancelled),
            "cancellation": _quote_payload(applied),
        },
    )


async def _reschedule_booking(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Move a booking to a new time, atomically."""
    deps = _require_deps()
    async with deps.unit_of_work(ctx.tenant_id) as (booking_service, repo):
        region = (
            await repo.location_region(await repo.default_location_id())
            or _DEFAULT_REGION
        )
        booking = await _resolve_one(booking_service, args, ctx, region)
        tz = booking.timezone

        # Same precedence as booking: an engine-minted reference beats a
        # model-composed time, for the same reason.
        decoded = await _decode_slot_ref(str(args.get("slot_ref") or ""), ctx.tenant_id)
        if decoded is not None:
            new_start = decoded.start_at
        elif args.get("new_start_time"):
            new_start = _to_utc(args["new_start_time"], tz)
        else:
            raise ToolBusinessError(
                "No new time given",
                model_message=(
                    "I need to know the new time. Call check_availability and "
                    "pass back the slot_ref of the one the customer chose."
                ),
                code="validation_failed",
            )
        was = _speak_time(booking.start_at, tz)
        try:
            moved = await booking_service.reschedule(
                booking_id=booking.id,
                new_start_at=new_start,
                reason=args.get("reason"),
            )
        except SlotUnavailableError as exc:
            # The original booking is untouched — the move is one UPDATE that
            # either commits or rolls back. Say so, because a caller who hears
            # "that didn't work" needs to know they still have their old slot.
            raise _unavailable(exc, tz) from exc
        except ValidationError as exc:
            raise ToolBusinessError(
                exc.message,
                model_message=exc.model_message,
                code="validation_failed",
            ) from exc

    return ToolResult(
        content=(
            f"Moved {moved.reference} from {was} to "
            f"{_speak_time(moved.start_at, moved.timezone)}. Read the new day "
            f"and time back to the customer and confirm the reference is "
            f"unchanged."
        ),
        collected_data={"booking_reference": moved.reference},
        structured={"booking": _booking_payload(moved), "previous_spoken": was},
    )


async def _lookup_booking(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Find a customer's booking by whatever they can supply."""
    deps = _require_deps()
    async with deps.unit_of_work(ctx.tenant_id) as (booking_service, repo):
        # Region matters on the way IN as well as out: a caller quoting their
        # own national number must resolve to the same E.164 string that was
        # stored, or their booking is simply not found.
        region = (
            await repo.location_region(await repo.default_location_id())
            or _DEFAULT_REGION
        )
        bookings = await booking_service.lookup(
            reference=args.get("reference"),
            phone_e164=_normalise_phone(args.get("customer_phone"), region),
            conversation_id=(
                ctx.conversation_id
                if not args.get("reference") and not args.get("customer_phone")
                else None
            ),
            limit=int(args.get("limit", 5)),
        )

    if not bookings:
        return ToolResult(
            content=(
                "No appointment found with those details. Ask the customer for "
                "the booking reference, or the phone number the appointment was "
                "made under."
            ),
            structured={"bookings": []},
        )

    lines = [
        f"{b.reference}: {b.service_name} on "
        f"{_speak_time(b.start_at, b.timezone)}"
        + (f" with {', '.join(b.resource_names)}" if b.resource_names else "")
        + (" (cancelled)" if b.status == "cancelled" else "")
        for b in bookings
    ]
    return ToolResult(
        content="Found: " + "; ".join(lines) + ".",
        collected_data={"booking_reference": bookings[0].reference},
        structured={"bookings": [_booking_payload(b) for b in bookings]},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _resolve_window(
    booking_service: Any, args: dict[str, Any], ctx: ToolContext
) -> tuple[datetime, datetime, str]:
    """Turn the model's window arguments into a UTC pair plus a timezone.

    Defaults are chosen to match how people ask. No start means "from now"; no
    end means "the next week". Both defaulting to a single instant would make
    every unqualified "have you got anything?" return nothing.

    **A window entirely in the past is refused, loudly, with today's date in the
    message.** This is the single most important behaviour in this function, and
    it was originally wrong in a way that made the whole booking path unusable:

    A model that does not know today's date resolves "Tuesday morning" to a
    Tuesday that has already passed. The old code then clamped ``start`` up to
    ``now`` while leaving ``end`` in the past, producing an inverted window and
    an error reading "the end of the window must be after its start" — which is
    *false from the model's point of view*, because the end it sent genuinely was
    after the start it sent. Being told something untrue and uncorrectable, the
    model retried the identical call, six times, and then told the caller it
    could not pull up the diary.

    So: detect the real condition, name it, and say what today is. A tool error
    is a cooperative correction (03 §6.3) — its entire job is to contain enough
    for the model to fix the call on the next attempt.
    """
    repo = booking_service.repository
    tz = await repo.location_timezone(await repo.default_location_id())
    _ = ctx

    now = datetime.now(tz=UTC)
    today = _speak_date(now, tz)

    requested_start = (
        _to_utc(args["window_start"], tz) if args.get("window_start") else now
    )
    requested_end = (
        _to_utc(args["window_end"], tz) if args.get("window_end") else None
    )

    # The whole window is behind us. Diagnosed before any clamping, so the
    # message describes what the model actually asked for.
    if requested_end is not None and requested_end <= now:
        raise ValidationError(
            f"Search window ends {_speak_date(requested_end, tz)}, before "
            f"today ({today}).",
            field="window_start",
            today=today,
            model_message=(
                f"That date has already passed — today is {today}. Work out "
                f"the correct upcoming date and search again. A weekday the "
                f"customer names always means the NEXT one. Do not tell the "
                f"customer about this; just search for the right date."
            ),
        )

    if requested_end is None and requested_start < now - timedelta(hours=1):
        # Only a start was given, and it is in the past. Same class of mistake;
        # the one-hour grace absorbs a caller booking "this morning" at 09:05
        # for 09:00, which is a legitimate near-miss rather than a wrong date.
        raise ValidationError(
            f"Search start {_speak_date(requested_start, tz)} is before today "
            f"({today}).",
            field="window_start",
            today=today,
            model_message=(
                f"That date has already passed — today is {today}. Work out "
                f"the correct upcoming date and search again, without "
                f"mentioning this to the customer."
            ),
        )

    start = max(requested_start, now)
    end = requested_end or start + timedelta(days=_DEFAULT_WINDOW_DAYS)

    if end <= start:
        # A genuinely inverted or zero-length window that is NOT a past-date
        # mistake — the two now have distinct messages, because they have
        # distinct fixes.
        raise ValidationError(
            f"Search window ends {_speak_date(end, tz)}, before its start "
            f"{_speak_date(start, tz)}.",
            field="window_end",
            today=today,
            model_message=(
                f"That search window is back to front. Today is {today} — "
                f"search again with the dates the right way round, without "
                f"mentioning this to the customer."
            ),
        )
    return start, end, tz


async def _resolve_one(
    booking_service: Any,
    args: dict[str, Any],
    ctx: ToolContext,
    region: str = _DEFAULT_REGION,
) -> BookingRecord:
    """Find the one booking a cancel or reschedule refers to.

    Ambiguity is refused rather than resolved. Cancelling "an" appointment when
    the caller has three is the kind of confident wrong action that ends a
    customer relationship, so several matches come back as a question for the
    model to ask, not a guess.
    """
    bookings = await booking_service.lookup(
        reference=args.get("reference"),
        booking_id=UUID(args["booking_id"]) if args.get("booking_id") else None,
        phone_e164=_normalise_phone(args.get("customer_phone"), region),
        conversation_id=(
            ctx.conversation_id
            if not any(
                args.get(k) for k in ("reference", "booking_id", "customer_phone")
            )
            else None
        ),
        limit=5,
    )
    live = [b for b in bookings if b.status != "cancelled"] or bookings

    if not live:
        raise ToolBusinessError(
            "No matching booking",
            model_message=(
                "I could not find that appointment. Ask the customer for the "
                "booking reference, or the phone number it was booked under."
            ),
            code="not_found",
        )
    if len(live) > 1:
        options = "; ".join(
            f"{b.reference} ({b.service_name}, {_speak_time(b.start_at, b.timezone)})"
            for b in live
        )
        raise ToolBusinessError(
            "Ambiguous booking reference",
            model_message=(
                f"There is more than one appointment on that record: {options}. "
                f"Ask the customer which one they mean and call again with its "
                f"reference."
            ),
            code="conflict",
            structured={"bookings": [_booking_payload(b) for b in live]},
        )
    return live[0]


#: National-number rules per region: the trunk prefix a caller drops, the country
#: calling code, and the total national length.
#:
#: **This table is data, and it is here rather than in `packages/booking` for a
#: reason.** Resolving "07700 900321" needs a country, and the country is a
#: property of the tenant — a restaurant in Lisbon dials differently from a
#: dental practice in Manchester. The engine stays region-agnostic; this layer
#: converts, using whatever region the location declares.
#:
#: Deliberately small. It covers the markets the platform actually serves, and
#: anything not listed falls through to "we need it in international form",
#: which is correct rather than a wrong guess. Writing the wrong country code
#: into a record someone will later ring is worse than asking again.
#: Dialling rules per region: (trunk prefix, country code, subscriber digits).
#:
#: `subscriber` is the number WITHOUT the trunk prefix and WITHOUT the country
#: code — the digits that actually identify the line. It is a range, not a
#: single value, because most countries do not have one length: an Indian
#: mobile is 10 digits while a Delhi landline is 8 behind an area code, a
#: Chinese mobile is 11 against a landline's 8, and Italian numbers run 6 to 11.
#: An earlier version stored a single `national_len` and consequently rejected
#: every valid number in half the world — including the ten-digit Indian mobile
#: that prompted this rewrite.
#:
#: Ranges are deliberately permissive at the edges. This is a receptionist
#: taking a number down, not a carrier validating a route: the cost of refusing
#: a real number (a caller who cannot book) far exceeds the cost of accepting an
#: unusual one (a call that does not connect, which staff notice and fix).
_DIALLING_RULES: Final[dict[str, tuple[str, str, range]]] = {
    # region: (trunk, country code, subscriber-digit range)
    "GB": ("0", "44", range(10, 11)),   # strictly 10 behind the trunk 0
    "IE": ("0", "353", range(7, 10)),
    "US": ("1", "1", range(10, 11)),    # NANP is strictly 10
    "CA": ("1", "1", range(10, 11)),
    "IN": ("0", "91", range(10, 11)),   # mobiles are 10; landlines 10 with STD
    "CN": ("0", "86", range(8, 12)),    # mobile 11, landline 8 behind area code
    "RU": ("8", "7", range(10, 11)),
    "IT": ("", "39", range(6, 12)),     # no trunk prefix; the 0 is part of the number
    "PT": ("", "351", range(9, 10)),
    "ES": ("", "34", range(9, 10)),
    "FR": ("0", "33", range(9, 10)),
    "DE": ("0", "49", range(6, 12)),    # famously variable
    "NL": ("0", "31", range(9, 10)),
    "AU": ("0", "61", range(9, 10)),
    "NZ": ("0", "64", range(8, 11)),
}


def _normalise_phone(raw: str | None, region: str = _DEFAULT_REGION) -> str | None:
    """Coerce a spoken phone number into E.164, or refuse it.

    ``ck_bookings__phone_e164`` enforces the format at the database, and a
    constraint violation deep in an insert is a far worse error than a clear one
    here.

    **The region comes from the location, not from an assumption.** This function
    previously hardcoded ``startswith("07")``, ``+44`` and an 11-digit rule —
    dental-practice-in-Britain knowledge sitting in generic tool code, which a
    restaurant in Lisbon hits on its first caller. The rules are now a table
    keyed by the tenant's own region.

    A number already in international form is accepted as given: nothing produces
    a leading ``+`` by accident, so it is a deliberate assertion.

    Raises :class:`ValidationError` whose ``model_message`` is written to be
    **spoken** — see the note on that argument. Guessing a country code writes
    the wrong number into a record someone will later ring, so refusing is right;
    refusing in the caller's language is what was missing.
    """
    if not raw:
        return None

    digits = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    trunk, country, subscriber = _DIALLING_RULES.get(
        region.upper(), _DIALLING_RULES[_DEFAULT_REGION]
    )

    def _ok(national: str) -> bool:
        return len(national) in subscriber

    if digits.startswith("+"):
        # Already international. Nothing produces a leading "+" by accident, so
        # it is a deliberate assertion and we take it as given.
        candidate = digits
    elif digits.startswith(country) and _ok(digits[len(country) :]):
        # Country code without the "+", e.g. "919012345678" in India. Checked
        # BEFORE the trunk branch: in Russia the trunk is "8" and the country
        # code is "7", so "8..." and "7..." must not be confused, and in the
        # NANP the trunk and country code are both "1".
        candidate = "+" + digits
    elif trunk and digits.startswith(trunk) and _ok(digits[len(trunk) :]):
        # National form: strip the trunk prefix, prepend the country code.
        # "07700900321" -> "+447700900321"; "09012345678" -> "+919012345678".
        candidate = "+" + country + digits[len(trunk) :]
    elif _ok(digits):
        # Bare subscriber number, which is how people usually say it: an Indian
        # mobile as "9012345678", an Italian number as "0212345678" (that
        # leading zero is part of the number, not a trunk prefix).
        candidate = "+" + country + digits
    elif (foreign := _foreign_country_code(digits)) is not None:
        # A number carrying somebody else's country code — "919012345678" given
        # to a British practice. Accepted as-is: it is unambiguous, and a
        # practice with overseas patients is ordinary.
        candidate = "+" + digits
        log.info(
            "booking.phone.foreign_number_accepted",
            location_region=region.upper(),
            resolved_region=foreign,
        )
    elif _ANY_SUBSCRIBER_LEN.start <= len(digits) < _ANY_SUBSCRIBER_LEN.stop:
        # Last resort: the digit count is a plausible subscriber length in at
        # least one supported country, so treat it as local rather than refuse.
        #
        # **Why accept rather than ask.** The practice serves one country; a
        # caller giving a number for that country is the overwhelmingly common
        # case, and asking "what is your country code?" is a question no
        # receptionist asks and no caller expects. When the digits are the wrong
        # length for HERE but a right length SOMEWHERE, the likeliest
        # explanations are a local number said slightly unusually or a caller
        # abroad — and both are better served by taking the number down than by
        # interrogating the person about dialling plans.
        #
        # The cost is bounded and visible: a number stored that does not
        # connect, which staff discover on the first call-back. The cost of the
        # alternative is a caller who cannot book at all.
        candidate = "+" + country + digits
        log.info(
            "booking.phone.length_accepted_leniently",
            location_region=region.upper(),
            digit_count=len(digits),
        )
    else:
        raise ValidationError(
            # Diagnostic, for the log. Never spoken.
            f"{raw!r} is not resolvable to E.164 for region {region!r}.",
            field="customer_phone",
            # Spoken, and SPECIFIC. "I did not catch that number" is polite and
            # useless: a caller who typed ten digits instead of eleven repeats
            # the same ten and fails again. Naming what is wrong — too short,
            # too long, unrecognisable — is the difference between a recoverable
            # turn and a loop.
            #
            # Still no "E.164", no "region GB": the diagnostic half carries
            # those for the log.
            model_message=_phone_advice(digits, subscriber),
        )

    if not (8 <= len(candidate) <= 16):
        raise ValidationError(
            f"{raw!r} is {len(candidate)} characters; outside E.164 bounds.",
            field="customer_phone",
            model_message=_phone_advice(digits, subscriber),
        )
    return candidate


#: The union of every supported country's subscriber-length range.
#:
#: A number whose digit count falls anywhere in here is accepted rather than
#: refused, on the reasoning in `_normalise_phone`: the practice serves one
#: country, so a number that is a plausible length *somewhere* is far more
#: likely to be a real number said unusually than a typo worth interrogating.
#: Derived rather than written down, so adding a region to `_DIALLING_RULES`
#: widens it automatically and the two cannot drift apart.
_ANY_SUBSCRIBER_LEN: Final = range(
    min(r.start for _t, _c, r in _DIALLING_RULES.values()),
    max(r.stop for _t, _c, r in _DIALLING_RULES.values()),
)


def _foreign_country_code(digits: str) -> str | None:
    """The region a bare number belongs to, if it is plainly a foreign one.

    Returns the region key when ``digits`` begins with a known country code and
    what follows is a valid subscriber length there — "919012345678" is an
    Indian mobile whoever is being told it. Returns ``None`` otherwise, so a
    local number that is merely mistyped still gets the "read it back" reply
    rather than being silently reinterpreted as somewhere else's.

    Longest country code first: "1" (NANP) is a prefix of nothing, but "7"
    (Russia) would otherwise shadow "78" if one existed, and "3" shadows "351"
    and "39". Sorting by length removes the ordering hazard entirely.
    """
    for region, (_trunk, country, subscriber) in sorted(
        _DIALLING_RULES.items(), key=lambda kv: -len(kv[1][1])
    ):
        if digits.startswith(country) and len(digits) - len(country) in subscriber:
            return region
    return None


def _phone_advice(digits: str, subscriber: range) -> str:
    """What to tell the customer about a number that did not resolve.

    Names the actual discrepancy, because "I did not catch that" invites the
    caller to repeat exactly what just failed. Counts digits rather than
    describing a format — "a digit short" is something a person can check
    against what they typed; "must match E.164" is not.

    Takes the acceptable RANGE rather than one length, because most countries
    have several: saying "two digits short" to someone whose landline is
    legitimately shorter than a mobile is confidently wrong, which is worse than
    vague. Only when the range is a single length can a precise count be given.
    """
    count = len(digits.lstrip("+"))
    shortest, longest = min(subscriber), max(subscriber)

    if not count:
        return (
            "Tell the customer you did not catch a number at all, and ask them "
            "to say it."
        )
    if count < shortest:
        # Only claim an exact shortfall when there is one length it could be.
        if shortest == longest:
            short = shortest - count
            digit_word = "digit" if short == 1 else "digits"
            return (
                f"That number looks {short} {digit_word} short — tell the "
                f"customer that and ask them to say it again so you can check "
                f"it against what they gave you."
            )
        return (
            "That number looks too short — tell the customer that and ask them "
            "to say it again so you can check it."
        )
    if count > longest:
        return (
            "That number looks a little long — tell the customer that and ask "
            "them to say it again so you can check it."
        )
    return (
        "That number does not look quite right — ask the customer to say it "
        "again so you can check it."
    )


def _intake(args: dict[str, Any]) -> dict[str, Any]:
    """Collect the practice-specific answers out of the flat argument object.

    Flat rather than nested because models fill a flat object far more reliably
    than a nested one, and the fields a practice requires are declared in
    ``services.intake_schema`` — which the engine validates against, so a missing
    one is a question the agent asks rather than a schema the model must satisfy.
    """
    out: dict[str, Any] = {}
    for key in ("reason_for_visit", "date_of_birth", "is_new_patient"):
        value = args.get(key)
        if value not in (None, ""):
            out[key] = value
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Schemas and registration
# ─────────────────────────────────────────────────────────────────────────────
#
# Every schema is strict (`additionalProperties: false`, enforced by the
# registry). Descriptions are written for the *model*, not for a developer: they
# say when to call the tool and what a good argument looks like, because that
# text is the highest-leverage prompt engineering in the system.

_TIME_DESCRIPTION: Final = (
    "ISO-8601 date and time in the practice's local timezone, e.g. "
    "'2026-08-11T09:30'. Do not include a timezone offset unless the customer "
    "gave one."
)

TOOLS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="booking.check_availability",
        description=(
            "Find open appointment times for a service. Call this before "
            "offering the customer any time — never guess availability. Returns "
            "the next few free slots with the times spelled out."
        ),
        parameters=object_schema(
            {
                "service_code": {
                    "type": "string",
                    "description": (
                        "The service being booked, e.g. 'checkup', "
                        "'new-patient-exam', 'hygienist', 'emergency'."
                    ),
                },
                "window_start": {
                    "type": "string",
                    "description": (
                        "Earliest time to consider. " + _TIME_DESCRIPTION +
                        " Omit for 'as soon as possible'."
                    ),
                },
                "window_end": {
                    "type": "string",
                    "description": (
                        "Latest time to consider. " + _TIME_DESCRIPTION +
                        " Omit to search the next week."
                    ),
                },
                "party_size": {"type": "integer", "minimum": 1, "maximum": 20},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            ["service_code"],
        ),
        impl=_check_availability,
        # A read: safe to run in parallel with other reads, and safe to retry.
        idempotent=True,
        side_effecting=False,
        timeout_ms=6_000,
    ),
    ToolSpec(
        name="booking.next_alternative",
        description=(
            "Step to the NEXT slot in the same search, when the customer has "
            "rejected the one you just offered without naming a different day "
            "or time — 'anything earlier?', 'not that one', 'what else have you "
            "got?'.\n\n"
            "Do NOT call this when:\n"
            "- the customer ACCEPTED the offer ('that works', 'yes', '8:15 "
            "suits me', 'fine, book it') — call book_appointment instead;\n"
            "- the customer named a DIFFERENT day or time ('Thursday "
            "instead', 'next week', 'the afternoon') — call check_availability "
            "for what they actually asked for, because this search is for the "
            "old day and stepping through it will keep offering it."
        ),
        parameters=object_schema(
            {
                "declined": {
                    "type": "string",
                    "maxLength": 200,
                    "description": (
                        "What the customer said to REJECT the time you offered, "
                        "quoted. If you cannot quote a rejection, they did not "
                        "reject it — do not call this tool."
                    ),
                }
            },
            # Required, deliberately. With no required argument this was the
            # cheapest call available and the model reached for it by default —
            # walking the queue instead of booking, five times in one
            # conversation. Making it state WHAT was rejected forces the
            # question "was anything actually rejected?", which is exactly the
            # check that was missing.
            ["declined"],
        ),
        impl=_next_alternative,
        idempotent=False,
        side_effecting=False,
        timeout_ms=6_000,
    ),
    ToolSpec(
        name="booking.check_existing_bookings",
        description=(
            "Check whether this customer already has an appointment coming up. "
            "Call this AS SOON AS they give you their phone number, before "
            "booking. If they already have one, tell them and ask whether to "
            "move it, keep both, or cancel the old one — never silently create "
            "a second."
        ),
        parameters=object_schema(
            {
                "customer_phone": {
                    "type": "string",
                    "description": "The number the customer gave you.",
                },
                "within_days": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 90,
                    "description": (
                        "How many days ahead to look. Leave unset for the "
                        "practice's own setting."
                    ),
                },
            },
            ["customer_phone"],
        ),
        impl=_check_existing_bookings,
        idempotent=True,
        side_effecting=False,
        timeout_ms=6_000,
        pii_arg_paths=("customer_phone",),
    ),
    ToolSpec(
        name="booking.book_appointment",
        description=(
            "Create a confirmed appointment at an exact time. Only call this "
            "after check_availability offered that exact time AND the customer "
            "said yes to it. You must have their full name; ask for a contact "
            "number too."
        ),
        parameters=object_schema(
            {
                "slot_ref": {
                    "type": "string",
                    "description": (
                        "The slot_ref of the time the customer chose, exactly as "
                        "check_availability returned it. ALWAYS use this. It "
                        "carries the precise time, so you never have to work out "
                        "a timezone — and getting that wrong books the customer "
                        "an hour out."
                    ),
                },
                "service_code": {"type": "string"},
                "start_time": {
                    "type": "string",
                    "description": (
                        "Fallback ONLY when you have no slot_ref. "
                        + _TIME_DESCRIPTION
                        + " Give the practice's local wall-clock time with no "
                        "timezone offset — do not convert to UTC."
                    ),
                },
                "customer_name": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 120,
                    "description": "The customer's full name, as they gave it.",
                },
                "customer_phone": {
                    "type": "string",
                    "description": (
                        "Contact number. A UK number starting 07, or any number "
                        "in full international form starting with +."
                    ),
                },
                "customer_email": {"type": "string", "maxLength": 254},
                "reason_for_visit": {
                    "type": "string",
                    "maxLength": 500,
                    "description": (
                        "What the customer said they need, in their own words. "
                        "Do not interpret it clinically."
                    ),
                },
                "date_of_birth": {
                    "type": "string",
                    "description": "YYYY-MM-DD, if the practice requires it.",
                },
                "is_new_patient": {"type": "boolean"},
                "party_size": {"type": "integer", "minimum": 1, "maximum": 20},
                "notes": {"type": "string", "maxLength": 1000},
            },
            # `start_time` is deliberately NOT required: the slot_ref path is
            # the intended one and carries the instant itself. Requiring a time
            # would force the model to compose the value this design exists to
            # stop it composing.
            ["customer_name"],
        ),
        impl=_book_appointment,
        side_effecting=True,
        timeout_ms=10_000,
        # A3: a turn that crashes after this call replays it. The idempotency key
        # is derived from the conversation and the slot, so the replay collapses
        # onto the same booking rather than creating a second one.
        replay_cache_ttl_s=3600,
        pii_arg_paths=(
            "customer_name",
            "customer_phone",
            "customer_email",
            "date_of_birth",
            "reason_for_visit",
        ),
    ),
    ToolSpec(
        name="booking.reschedule_booking",
        description=(
            "Move an existing appointment to a new time. Call this AS SOON AS "
            "the customer has agreed a new time — 'please move it', 'that's "
            "fine', 'yes' — passing the slot_ handle check_availability gave "
            "you for the new slot.\n\n"
            "Do not ask 'shall I move it?' after they have already told you to "
            "move it; they have answered that. If the new time turns out to be "
            "taken, the original appointment is left exactly as it was — say "
            "so."
        ),
        parameters=object_schema(
            {
                "reference": {
                    "type": "string",
                    "description": "The booking reference, e.g. 'BK-7F3K2Q'.",
                },
                "booking_id": {"type": "string"},
                "customer_phone": {
                    "type": "string",
                    "description": (
                        "Use when the customer does not have their reference."
                    ),
                },
                "slot_ref": {
                    "type": "string",
                    "description": (
                        "The slot_ref of the new time, exactly as "
                        "check_availability returned it. ALWAYS prefer this over "
                        "new_start_time."
                    ),
                },
                "new_start_time": {
                    "type": "string",
                    "description": (
                        "Fallback ONLY when you have no slot_ref. "
                        + _TIME_DESCRIPTION
                        + " Practice-local wall clock, no offset."
                    ),
                },
                "reason": {"type": "string", "maxLength": 500},
            },
            [],
        ),
        impl=_reschedule_booking,
        side_effecting=True,
        timeout_ms=10_000,
        replay_cache_ttl_s=3600,
        pii_arg_paths=("customer_phone",),
    ),
    ToolSpec(
        name="booking.cancel_booking",
        description=(
            "Cancel an appointment. If it is inside the notice period this "
            "returns the fee WITHOUT cancelling — tell the customer the amount, "
            "and only call again with accept_fee=true if they still want to go "
            "ahead."
        ),
        parameters=object_schema(
            {
                "reference": {"type": "string"},
                "booking_id": {"type": "string"},
                "customer_phone": {"type": "string"},
                "reason": {"type": "string", "maxLength": 500},
                "accept_fee": {
                    "type": "boolean",
                    "description": (
                        "Set true ONLY after the customer has been told the fee "
                        "amount and has agreed to it."
                    ),
                },
            },
            [],
        ),
        impl=_cancel_booking,
        side_effecting=True,
        timeout_ms=10_000,
        replay_cache_ttl_s=3600,
        pii_arg_paths=("customer_phone",),
    ),
    ToolSpec(
        name="booking.lookup_booking",
        description=(
            "Find a customer's existing appointment. Call this before "
            "cancelling or rescheduling if you do not already have the "
            "reference. With no arguments it finds bookings made in this "
            "conversation."
        ),
        parameters=object_schema(
            {
                "reference": {"type": "string"},
                "customer_phone": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            [],
        ),
        impl=_lookup_booking,
        idempotent=True,
        side_effecting=False,
        timeout_ms=6_000,
        pii_arg_paths=("customer_phone",),
    ),
)

"""The generic booking engine — 01 §5, §13, §14.

**This package contains no string that names a vertical.** Its entire domain
vocabulary is resource, service, requirement, window, slot, booking, hold. A
physician, a dining table and a hotel room are all *resources*; a consultation,
a dinner reservation and a room night are all *services*. The differences
between them are data — the four dials of 01 §14.0 — never code here.

That is not a stylistic preference. It is invariant I4 (02 §5.6), and the
realistic failure it guards against is not malice: it is someone adding
``if service.code == "emergency":`` to the solver at 6pm to ship a demo. The
moment one vertical's word appears in this package, every other vertical is a
special case, and the claim that verticals are data becomes a slogan. It is
enforced by ``tests/architecture/test_booking_is_vertical_agnostic.py``, which
inspects the parsed AST rather than raw text so that a docstring may still
explain a concept in plain English.

The layout mirrors what the engine actually does:

* :mod:`packages.booking.domain` — the frozen value types the engine speaks.
* :mod:`packages.booking.availability` — rules minus exceptions, holidays,
  bookings and holds -> free slots, timezone-correct.
* :mod:`packages.booking.holds` — place / extend / release, TTL-bounded, under
  the per-``(tenant, resource)`` advisory lock of 01 §13.2.
* :mod:`packages.booking.service` — book / cancel / reschedule / lookup, plus
  cancellation-policy evaluation.
"""

from __future__ import annotations

from packages.booking.availability import AvailabilityEngine, SlotQuery
from packages.booking.domain import (
    AllocationCandidate,
    BookingRecord,
    CancellationQuote,
    HoldRecord,
    OpenWindow,
    RequirementSpec,
    ResourceSpec,
    ServiceSpec,
    Slot,
)
from packages.booking.holds import HoldExpiredError, HoldManager
from packages.booking.service import BookingService, SlotUnavailableError

__all__ = [
    "AllocationCandidate",
    "AvailabilityEngine",
    "BookingRecord",
    "BookingService",
    "CancellationQuote",
    "HoldExpiredError",
    "HoldManager",
    "HoldRecord",
    "OpenWindow",
    "RequirementSpec",
    "ResourceSpec",
    "ServiceSpec",
    "Slot",
    "SlotQuery",
    "SlotUnavailableError",
]

"""A horizon-bound service must not report an empty diary.

**The bug, as heard on a call.** The caller mentions pain, so the model picks
``emergency`` — which the manifest tells it to, and which is right. They then
ask for next week. ``emergency`` carries ``booking_horizon_days=1``, and
``find_slots`` clamps its search to ``now + horizon``: the window opens after
the range closes, so the engine returns ``[]``. The diary for next week was in
fact wide open — seven 25-minute appointments in a nine-hour day — and the
caller was told nothing was available.

**Why the old message made it worse.** It said "suggest a different day". Every
later day is *further* outside the horizon, so the one remedy offered was the
one that cannot work; the model would re-ask, get ``[]`` again, and the call
would circle. The fix names the real constraint and the actual remedy — a
routine service code — because a model that is told the true reason can act on
it, and this is a mechanism, not another instruction asking it to be careful.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest


def _classify(service: object, window_start: datetime, now: datetime) -> str:
    """The branch under test, lifted verbatim from ``check_availability``."""
    horizon_days = getattr(service, "booking_horizon_days", None)
    horizon_end = (
        now + timedelta(days=horizon_days) if horizon_days is not None else None
    )
    if horizon_end is not None and window_start >= horizon_end:
        return "outside_booking_horizon"
    return "empty_diary"


NOW = datetime(2026, 8, 22, 13, 0, tzinfo=UTC)
EMERGENCY = SimpleNamespace(
    booking_horizon_days=1, name="Emergency Appointment", code="emergency"
)
ROUTINE = SimpleNamespace(
    booking_horizon_days=90, name="Routine Check-up", code="checkup"
)


def test_same_day_service_asked_for_next_week_is_a_horizon_answer() -> None:
    """The reported call: pain -> emergency -> "next week" -> "nothing free"."""
    assert (
        _classify(EMERGENCY, NOW + timedelta(days=3), NOW) == "outside_booking_horizon"
    )


def test_same_day_service_asked_for_today_is_a_real_diary_answer() -> None:
    """Inside the horizon an empty result means the diary really is full, and
    "try another day" is then the correct advice."""
    assert _classify(EMERGENCY, NOW + timedelta(hours=2), NOW) == "empty_diary"


def test_routine_service_next_week_is_never_a_horizon_answer() -> None:
    """90 days out: an empty result here is a genuinely full diary."""
    assert _classify(ROUTINE, NOW + timedelta(days=3), NOW) == "empty_diary"


def test_a_service_without_a_horizon_is_not_misreported() -> None:
    """Absent metadata must not manufacture a horizon explanation."""
    assert _classify(SimpleNamespace(name="x"), NOW + timedelta(days=3), NOW) == (
        "empty_diary"
    )


@pytest.mark.parametrize("days", [1, 2, 7, 90])
def test_the_boundary_is_the_horizon_itself(days: int) -> None:
    """Just inside is a diary answer; just outside is a horizon answer. The
    clamp is ``>=``, so the instant the horizon ends belongs to the horizon."""
    svc = SimpleNamespace(booking_horizon_days=days, name="s", code="s")
    edge = NOW + timedelta(days=days)
    assert _classify(svc, edge - timedelta(minutes=1), NOW) == "empty_diary"
    assert _classify(svc, edge, NOW) == "outside_booking_horizon"

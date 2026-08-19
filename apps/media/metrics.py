"""Voice tier metrics — Core Contract A7, consumed by 05's HPA.

``receptionai_voice_active_sessions`` and ``receptionai_voice_session_capacity``
are a **cross-document dependency**, not implementation details: 05
§E19.4 scales the voice deployment on them. Renaming either silently breaks
autoscaling in a way no test here would catch, which is why the names are
asserted in ``tests/unit/test_voice_metrics.py``.

CPU is not usable as the scaling signal: a media pod sits at 15-25% CPU while
completely full, because it spends the call waiting on sockets. A CPU-based HPA
would never scale out, and would scale *in* while calls were live.
"""

from __future__ import annotations

import time
from types import TracebackType

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "VOICE_ACTIVE_SESSIONS",
    "VOICE_SESSION_CAPACITY",
    "VOICE_SESSION_DURATION",
    "VOICE_TURNS",
    "SessionMetrics",
]

VOICE_ACTIVE_SESSIONS = Gauge(
    "receptionai_voice_active_sessions",
    "Live media sessions on this pod. The HPA scaling signal for the voice tier.",
    labelnames=("tenant_id", "mode"),
    multiprocess_mode="livesum",
)

VOICE_SESSION_CAPACITY = Gauge(
    "receptionai_voice_session_capacity",
    "Max concurrent sessions this pod accepts. Lets the HPA compute utilization "
    "as a ratio rather than hardcoding pod capacity in the autoscaler.",
)

VOICE_SESSION_DURATION = Histogram(
    "receptionai_voice_session_duration_seconds",
    "Call duration. Drives drain-time estimates for maxUnavailable:0 rollouts.",
    buckets=(5, 15, 30, 60, 120, 300, 600, 1800),
)

VOICE_TURNS = Counter(
    "receptionai_voice_turns_total",
    "Agent turns completed on the voice path, by outcome.",
    labelnames=("outcome",),
)


class SessionMetrics:
    """Context manager owning one session's gauge movements.

    A context manager rather than paired inc/dec calls because the decrement
    must survive every exit path. A gauge that leaks on an exception drifts
    upward forever, and an HPA reading a drifting gauge scales out a tier that
    is actually idle — which looks like a capacity problem and is a bug.
    """

    __slots__ = ("_mode", "_started", "_tenant_id")

    def __init__(self, *, tenant_id: str, mode: str) -> None:
        self._tenant_id = tenant_id
        self._mode = mode
        self._started = 0.0

    def __enter__(self) -> SessionMetrics:
        VOICE_ACTIVE_SESSIONS.labels(self._tenant_id, self._mode).inc()
        self._started = time.monotonic()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        VOICE_ACTIVE_SESSIONS.labels(self._tenant_id, self._mode).dec()
        VOICE_SESSION_DURATION.observe(time.monotonic() - self._started)

"""The voice scaling metrics are a contract, not an implementation detail.

Core Contract A7 and 05 §E19.4: the voice tier's HPA scales on
``receptionai_voice_active_sessions`` against
``receptionai_voice_session_capacity``. Renaming either breaks autoscaling
silently — the deployment keeps running, the metric simply never appears, and
the tier never scales out. Nothing else in the repo would catch that, so the
names are asserted here as literal strings.
"""

from __future__ import annotations

from apps.media.metrics import (
    VOICE_ACTIVE_SESSIONS,
    VOICE_SESSION_CAPACITY,
    SessionMetrics,
)
from prometheus_client import REGISTRY


def _sample(name: str, labels: dict[str, str] | None = None) -> float | None:
    return REGISTRY.get_sample_value(name, labels or {})


def test_contract_metric_names_exist() -> None:
    """The two names 05's HPA reads, spelled exactly as the contract spells them."""
    VOICE_SESSION_CAPACITY.set(25)

    assert _sample("receptionai_voice_session_capacity") == 25
    # The gauge is labelled, so it only materialises once a label set is used.
    VOICE_ACTIVE_SESSIONS.labels("tenant-a", "composed").inc()
    assert (
        _sample(
            "receptionai_voice_active_sessions",
            {"tenant_id": "tenant-a", "mode": "composed"},
        )
        == 1
    )
    VOICE_ACTIVE_SESSIONS.labels("tenant-a", "composed").dec()


def test_active_sessions_is_labelled_by_tenant_and_mode() -> None:
    """05 aggregates per tenant and per mode; unlabelled would collapse both."""
    assert VOICE_ACTIVE_SESSIONS._labelnames == ("tenant_id", "mode")


def test_session_gauge_returns_to_zero_on_success() -> None:
    labels = {"tenant_id": "tenant-b", "mode": "composed"}
    before = _sample("receptionai_voice_active_sessions", labels) or 0.0

    with SessionMetrics(tenant_id="tenant-b", mode="composed"):
        during = _sample("receptionai_voice_active_sessions", labels)
        assert during == before + 1

    assert _sample("receptionai_voice_active_sessions", labels) == before


def test_session_gauge_returns_to_zero_on_exception() -> None:
    """The path that actually leaks in production.

    A gauge that drifts upward on the error path makes the HPA scale out a tier
    that is completely idle, which reads as a capacity problem and is a bug.
    """
    labels = {"tenant_id": "tenant-c", "mode": "composed"}
    before = _sample("receptionai_voice_active_sessions", labels) or 0.0

    class PipelineCrashedError(Exception):
        pass

    try:
        with SessionMetrics(tenant_id="tenant-c", mode="composed"):
            raise PipelineCrashedError
    except PipelineCrashedError:
        pass

    assert _sample("receptionai_voice_active_sessions", labels) == before

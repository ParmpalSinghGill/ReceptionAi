"""The Principal's invariants and the frozen error vocabulary.

No database. These are the checks that the *type system cannot express*, and
each one corresponds to a real failure mode rather than defensive noise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from packages.core.auth import Principal
from packages.core.errors import (
    FROZEN_ERROR_CODES,
    PROBLEM_TITLES,
    AuthorizationError,
    ConcurrencyConflictError,
    DomainError,
    EntityNotFoundError,
    ResourceConflictError,
    StepUpRequiredError,
    assert_titles_complete,
    title_for,
)
from packages.core.ids import uuid7


def _principal(**overrides: object) -> Principal:
    base: dict[str, object] = {
        "kind": "user",
        "tenant_id": uuid7(),
        "tenant_plan": "pro",
        "permissions": frozenset({"booking:create"}),
        "user_id": uuid7(),
    }
    base.update(overrides)
    return Principal(**base)  # type: ignore[arg-type]


# ── Principal invariants (05 §A3.2) ─────────────────────────────────────────


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown principal kind"):
        _principal(kind="robot")


def test_user_principal_requires_a_user_id() -> None:
    with pytest.raises(ValueError, match="user principal requires user_id"):
        _principal(user_id=None)


def test_api_key_principal_requires_an_api_key_id() -> None:
    with pytest.raises(ValueError, match="requires api_key_id"):
        _principal(kind="api_key", user_id=None, api_key_id=None)


def test_api_key_cannot_hold_aal_2() -> None:
    """05 §A1.7: a machine credential must never satisfy step-up.

    A key cannot present a second factor, so ``aal >= 2`` on one can only come
    from a bug or a forged claim — and it would unlock precisely the routes
    (rotate key, change billing, export data, delete tenant) that step-up exists
    to protect.
    """
    with pytest.raises(ValueError, match="cannot hold aal >= 2"):
        _principal(kind="api_key", user_id=None, api_key_id=uuid7(), aal=2)

    # aal=1 is fine.
    key = _principal(kind="api_key", user_id=None, api_key_id=uuid7(), aal=1)
    assert key.aal == 1


def test_impersonation_is_only_valid_for_user_principals() -> None:
    with pytest.raises(ValueError, match="only valid for user principals"):
        _principal(
            kind="api_key", user_id=None, api_key_id=uuid7(), acting_as=uuid7()
        )


def test_principal_is_frozen_and_slotted() -> None:
    """A typo'd keyword is a TypeError, not a silently-ignored attribute.

    The failure this prevents: a Principal with a misspelled ``plan=`` that
    quietly defaulted would fail *open* in the rate limiter and in plan-gated
    adapter resolution.
    """
    p = _principal()
    with pytest.raises(AttributeError):
        p.tenant_plan = "enterprise"  # type: ignore[misc]
    with pytest.raises(TypeError):
        _principal(plan="enterprise")


def test_impersonator_and_subject_are_reconciled() -> None:
    """01 names the actor; 05 names the subject. One field, no drift.

    ``user_id`` is the staff member holding the session; ``acting_as`` is the
    customer being acted upon. ``impersonator_user_id`` returns the actor (what
    01 §15 wants) and returns None when no impersonation is in play — so an
    ordinary session records no impersonator rather than recording the user as
    impersonating themselves.
    """
    staff = uuid7()
    customer = uuid7()

    impersonating = _principal(user_id=staff, acting_as=customer)
    assert impersonating.impersonator_user_id == staff
    assert impersonating.subject_user_id == customer
    assert impersonating.is_impersonated
    assert impersonating.audit_actor == f"staff:{staff}#acting:{customer}"

    ordinary = _principal(user_id=staff)
    assert ordinary.impersonator_user_id is None
    assert ordinary.subject_user_id == staff
    assert not ordinary.is_impersonated
    assert ordinary.audit_actor == f"user:{staff}"


def test_auth_age_is_infinite_when_unknown() -> None:
    """Fail-closed: unknown auth time reads as arbitrarily stale."""
    assert _principal(auth_time=None).auth_age_seconds == float("inf")

    recent = _principal(auth_time=datetime.now(tz=UTC) - timedelta(seconds=30))
    assert 29 <= recent.auth_age_seconds <= 31


def test_audit_actor_covers_every_kind() -> None:
    key_id = uuid7()
    assert (
        _principal(kind="api_key", user_id=None, api_key_id=key_id).audit_actor
        == f"apikey:{key_id}"
    )
    assert _principal(kind="service", user_id=None).audit_actor == "system"


def test_missing_permissions_returns_the_whole_set() -> None:
    """Returned, not raised, so the caller can put them all in one extension.

    Telling a client about one missing permission at a time turns a single fix
    into a round-trip loop.
    """
    p = _principal(permissions=frozenset({"booking:create"}))
    required = frozenset({"booking:create", "booking:cancel", "billing:manage"})
    assert p.missing_permissions(required) == frozenset(
        {"booking:cancel", "billing:manage"}
    )
    assert p.has_permission("booking:create")
    assert not p.has_permission("billing:manage")


# ── The frozen error vocabulary (A9) ────────────────────────────────────────


def test_every_frozen_code_has_a_title() -> None:
    """A9's required assertion.

    A missing title raises ``KeyError`` *inside the error-rendering path* —
    discoverable only by failing to render an error, which is to say during an
    incident.
    """
    assert_titles_complete()
    assert set(PROBLEM_TITLES) == FROZEN_ERROR_CODES


def test_title_for_refuses_an_unknown_code() -> None:
    """Not defaulted, deliberately.

    A ``code.replace("_", " ").capitalize()`` fallback would let a typo ship as
    a plausible-looking title and never be noticed.
    """
    with pytest.raises(KeyError):
        title_for("not_a_real_code")


def test_domain_errors_carry_frozen_codes() -> None:
    with pytest.raises(ValueError, match="not in the frozen vocabulary"):
        DomainError("boom", code="invented_code")


def test_error_status_mapping() -> None:
    """Each status is a domain fact, not a transport detail."""
    not_found = EntityNotFoundError(entity_type="bookings", entity_id=uuid7())
    # 404, not 403: under RLS another tenant's row is indistinguishable from a
    # nonexistent one, and confirming otherwise makes ids enumerable.
    assert not_found.http_status == 404
    assert not_found.code == "not_found"

    conflict = ConcurrencyConflictError(
        entity_type="bookings", entity_id=uuid7(), expected_version=3
    )
    # 412, not 409: the caller sent a precondition and it failed, so re-read and
    # retry is the correct client response.
    assert conflict.http_status == 412
    assert conflict.code == "version_conflict"
    assert conflict.extensions["expected_version"] == 3

    overlap = ResourceConflictError("taken", kind="overlap", constraint="ex_x")
    # Overlap gets its own code because it is the one conflict every booking
    # client is told to special-case.
    assert overlap.code == "slot_unavailable"
    assert overlap.http_status == 409
    assert overlap.extensions["conflict_kind"] == "overlap"

    duplicate = ResourceConflictError("exists", kind="duplicate")
    assert duplicate.code == "conflict"

    step_up = StepUpRequiredError()
    assert step_up.code == "step_up_required"
    assert step_up.extensions["required_aal"] == 2
    assert isinstance(step_up, AuthorizationError)

    forbidden = AuthorizationError(missing_permissions=("billing:manage",))
    assert forbidden.extensions["missing_permissions"] == ["billing:manage"]


def test_every_error_has_a_renderable_title() -> None:
    """No error can be constructed that the error path cannot render."""
    errors: list[DomainError] = [
        EntityNotFoundError(entity_type="x", entity_id=uuid7()),
        ConcurrencyConflictError(entity_type="x", entity_id=uuid7(), expected_version=1),
        ResourceConflictError("x", kind="overlap"),
        ResourceConflictError("x", kind="duplicate"),
        AuthorizationError(),
        StepUpRequiredError(),
    ]
    for error in errors:
        assert error.title in PROBLEM_TITLES.values()

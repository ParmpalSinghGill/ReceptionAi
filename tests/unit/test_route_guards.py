"""Default-deny and the error vocabulary — the startup-time guarantees.

These run without a stack. They build the app object (no lifespan, so no
connection pool) and inspect its route table, which is exactly what
:func:`~apps.api.security.route_audit.assert_all_routes_guarded` does at
startup.

The most important test here is
:func:`test_the_route_scan_actually_finds_routes`. A traversal bug makes the
default-deny gate pass while checking nothing — it is a security control that
fails *silently open*, and only an explicit count catches it.
"""

from __future__ import annotations

import pytest
from apps.api.security.permissions import ROLE_GRANTS, STEP_UP_REQUIRED, Perm
from apps.api.security.route_audit import (
    PUBLIC_ROUTES,
    assert_all_routes_guarded,
    is_authz_check,
    iter_api_routes,
    unregistered_public_routes,
)
from apps.api.settings import Settings
from packages.core.errors import FROZEN_ERROR_CODES, assert_titles_complete


@pytest.fixture(scope="module")
def app():
    """The app object only — no lifespan, so no pool and no Redis."""
    from apps.api.main import create_app

    return create_app(Settings(env="test", llm_provider="stub"))


def test_the_route_scan_actually_finds_routes(app) -> None:
    """The gate must examine something, or "all routes guarded" means nothing.

    FastAPI changed how ``include_router`` stores routes: some versions flatten
    them onto ``app.routes``, others leave a wrapper object holding them. A
    scan that only walks the top level finds the wrapper, skips it because it
    is not an ``APIRoute``, and reports zero unguarded routes — passing loudly
    while checking nothing. This asserts the count.
    """
    routes = list(iter_api_routes(app))
    paths = {route.path for route in routes}

    assert len(routes) >= 10, f"the traversal found only {len(routes)} routes"
    # Every path the frozen contract names must be present and visible to it.
    for expected in (
        "/api/v1/public/demo/sessions",
        # The widget's other two calls share these with the admin client, per
        # PHASE1-API-CONTRACT — one path, two credentials.
        "/api/v1/conversations/{conversation_id}",
        "/api/v1/conversations/{conversation_id}/messages",
        "/api/v1/conversations/{conversation_id}/stream",
        "/api/v1/me",
        "/api/v1/auth/login",
        "/healthz",
        "/readyz",
    ):
        assert expected in paths, f"{expected} is missing from the route table"


def test_every_route_carries_an_authorization_decision(app) -> None:
    """05 §A2.4. The same call the app factory makes at startup."""
    assert_all_routes_guarded(app)


def test_public_routes_are_the_only_unguarded_ones(app) -> None:
    """Enumerate them, so widening the allow-list shows up as a test change."""
    unguarded = {
        f"{method} {route.path}"
        for route in iter_api_routes(app)
        for method in (route.methods or set()) - {"HEAD", "OPTIONS"}
        if not any(
            is_authz_check(dep.call)
            for dep in route.dependant.dependencies
            if dep.call is not None
        )
        and not _has_nested_authz(route)
    }

    assert unguarded <= set(PUBLIC_ROUTES), (
        f"routes are unguarded and not in PUBLIC_ROUTES: {unguarded - set(PUBLIC_ROUTES)}"
    )


def _has_nested_authz(route) -> bool:
    from apps.api.security.route_audit import _flatten

    return any(is_authz_check(call) for call in _flatten(route.dependant))


def test_the_public_allowlist_has_no_stale_entries(app) -> None:
    """A route deleted or renamed must not leave its exemption behind.

    The next route to land on that path would inherit it silently. The four
    OpenAPI paths are expected: FastAPI mounts them as plain Starlette routes,
    outside the authz dependency system entirely.
    """
    stale = unregistered_public_routes(app) - {
        "GET /docs",
        "GET /docs/oauth2-redirect",
        "GET /openapi.json",
        "GET /redoc",
    }
    assert not stale, f"PUBLIC_ROUTES exempts routes that do not exist: {stale}"


def test_a_route_without_a_guard_fails_startup() -> None:
    """The gate actually fires — a negative test for the control itself.

    Without this, a broken ``assert_all_routes_guarded`` passes every other
    test in this file by never raising.
    """
    from fastapi import FastAPI

    naked = FastAPI()

    @naked.get("/api/v1/oops")
    async def oops() -> dict[str, str]:
        return {}

    with pytest.raises(RuntimeError, match="without an authorization decision"):
        assert_all_routes_guarded(naked)


# ── The permission model ─────────────────────────────────────────────────────


def test_step_up_permissions_are_a_subset_of_the_vocabulary() -> None:
    assert STEP_UP_REQUIRED <= set(Perm)


def test_the_role_grid_grants_only_real_permissions() -> None:
    """A grid naming a permission that does not exist would seed a role with a
    capability nothing enforces."""
    for role, grants in ROLE_GRANTS.items():
        unknown = grants - set(Perm)
        assert not unknown, f"role {role!r} grants unknown permissions: {unknown}"


def test_a_viewer_cannot_read_pii() -> None:
    """05 §A2.3. The asymmetry that makes the two permissions separate.

    A viewer sees *that* a conversation went badly without reading what a
    patient said about their symptoms.
    """
    viewer = ROLE_GRANTS["viewer"]
    assert Perm.CONVERSATIONS_READ in viewer
    assert Perm.CONVERSATIONS_READ_PII not in viewer


def test_only_the_owner_can_delete_the_tenant() -> None:
    for role, grants in ROLE_GRANTS.items():
        if role != "owner":
            assert Perm.TENANT_DELETE not in grants, f"{role} can delete the tenant"


# ── The error envelope ───────────────────────────────────────────────────────


def test_every_frozen_code_has_a_title() -> None:
    """Amendment A9. A missing title raises ``KeyError`` *inside* the
    error-rendering path — discoverable only by failing to render an error, at
    the moment you most need one."""
    assert_titles_complete()


def test_the_problem_builder_only_emits_frozen_codes() -> None:
    """``ProblemException`` resolves its title through ``title_for``, which
    raises on an unknown code — so a typo cannot ship as a plausible title."""
    from apps.api.problems import ProblemException

    for code in FROZEN_ERROR_CODES:
        problem = ProblemException(status=400, code=code)
        assert problem.title

    with pytest.raises(KeyError):
        ProblemException(status=400, code="not_a_real_code")

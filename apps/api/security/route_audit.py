"""Default-deny, enforced at startup — 05 §A2.4.

*"The most common real-world authz bug is not a wrong check, it is a missing
check on a newly added endpoint. A code review can miss that; a startup
assertion cannot."*

A route with no ``requires(...)`` dependency and no entry in
:data:`PUBLIC_ROUTES` crashes the app at startup. The failure is loud, it is
local (you added the route; you get the error on the next run), and it cannot
be skipped by a reviewer with sixty files open.

**Why an allow-list of public routes rather than a decorator marking them
public.** A decorator is something you *add* to make a route public — so the
default for a forgotten route becomes "public", and forgetting is the exact
failure being defended against. An allow-list inverts it: the default is
"guarded", and making something public is an edit to a file that shows up in a
diff as a security change.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from fastapi import FastAPI
from fastapi.routing import APIRoute

__all__ = [
    "PUBLIC_ROUTES",
    "assert_all_routes_guarded",
    "is_authz_check",
    "iter_api_routes",
]

#: Routes reachable without an authorization decision, each with its reason.
#: Anything not here needs `Depends(requires(...))`.
PUBLIC_ROUTES: Final[frozenset[str]] = frozenset(
    {
        # Liveness/readiness. A probe cannot hold a credential, and a health
        # check behind auth fails during exactly the incident it exists for.
        "GET /healthz",
        "GET /readyz",
        # The auth handshake itself. These cannot require a token; they mint
        # one. Rate-limited per IP instead (05 §B8.2).
        "POST /api/v1/auth/login",
        "POST /api/v1/auth/refresh",
        "POST /api/v1/auth/logout",
        # The public demo widget's entry point. Deliberately anonymous — that
        # is the product — and defended by three other controls instead of a
        # principal: a per-IP rate limit, a single-use 20-second stream ticket
        # scoped to one conversation, and a hard binding to the seeded demo
        # tenant so the route cannot name another tenant's data.
        #
        # Note what is NOT here: the widget's other two calls go to
        # /api/v1/conversations/{id}/messages and .../stream, which are fully
        # guarded by `requires()` for both callers. A ticket resolves to a
        # narrow principal rather than to an exemption, so those routes need no
        # entry on this list (PHASE1-API-CONTRACT).
        "POST /api/v1/public/demo/sessions",
        # OpenAPI. Served only outside production; the app factory omits the
        # URLs entirely there rather than guarding them.
        "GET /openapi.json",
        "GET /docs",
        "GET /docs/oauth2-redirect",
        "GET /redoc",
    }
)


def is_authz_check(dependency: Any) -> bool:
    """Whether a dependency callable is an authorization decision.

    Set by ``requires()`` as ``__is_authz_check__`` on the closure it returns.
    An attribute rather than an isinstance check because the dependency is a
    function, and FastAPI stores the function itself.
    """
    return bool(getattr(dependency, "__is_authz_check__", False))


def _flatten(dependant: Any) -> Iterable[Any]:
    """Every callable in a route's dependency tree, including nested ones.

    Recursion matters: a router-level ``dependencies=[Depends(requires(...))]``
    appears as a sub-dependency of each route, and a shallow scan would report
    every route on that router as unguarded.
    """
    for sub in dependant.dependencies:
        if sub.call is not None:
            yield sub.call
        yield from _flatten(sub)


def iter_api_routes(app_or_router: Any) -> Iterable[APIRoute]:
    """Every :class:`APIRoute` reachable from an app, recursively.

    **This must recurse, and getting it wrong is silent.** Depending on the
    FastAPI version, ``include_router`` either flattens its routes onto
    ``app.routes`` or leaves a wrapper object (``_IncludedRouter``, ``Mount``)
    holding them. A scan that only walks the top level finds the wrapper,
    skips it because it is not an ``APIRoute``, and reports zero unguarded
    routes — passing loudly while checking nothing.

    That is the exact failure this module exists to prevent, so the traversal
    is version-agnostic: it follows both the ``routes`` list and FastAPI's
    ``_IncludedRouter.original_router`` wrapper, and yields only leaf
    ``APIRoute`` objects. ``assert_all_routes_guarded`` additionally refuses to
    pass when this yields nothing, so a future wrapper class this does not know
    about fails the build instead of disabling the gate.
    """
    seen: set[int] = set()

    def walk(node: Any) -> Iterable[APIRoute]:
        # Cycle guard: a router included in two places is one object reached
        # twice, and without this the traversal would report its routes twice.
        if id(node) in seen:
            return
        seen.add(id(node))

        for route in getattr(node, "routes", ()):
            if isinstance(route, APIRoute):
                yield route
                continue
            # FastAPI >= 0.116 wraps an included router rather than flattening
            # it; the real router hangs off `original_router`.
            nested = getattr(route, "original_router", None) or route
            if hasattr(nested, "routes"):
                yield from walk(nested)

    yield from walk(app_or_router)


def assert_all_routes_guarded(app: FastAPI) -> None:
    """Fail startup if any route lacks an explicit authorization decision.

    Raises:
        RuntimeError: naming every offending route and both remedies. The
            message matters — this fires on someone's first run after adding an
            endpoint, and "add ``Depends(requires(...))`` or register it in
            PUBLIC_ROUTES" is the whole fix.
    """
    unguarded: list[str] = []
    scanned = 0

    for route in iter_api_routes(app):
        scanned += 1
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            signature = f"{method} {route.path}"
            if signature in PUBLIC_ROUTES:
                continue
            if any(is_authz_check(call) for call in _flatten(route.dependant)):
                continue
            unguarded.append(signature)

    if scanned == 0:
        # A check that examined nothing is a check that passed for the wrong
        # reason. An app with no routes is never legitimate here — the factory
        # mounts at minimum the health routes before calling this — so zero
        # means the traversal above failed to find them, which would make every
        # subsequent "all routes guarded" result meaningless.
        raise RuntimeError(
            "assert_all_routes_guarded found no routes to check. The route "
            "traversal is broken (see iter_api_routes), so the default-deny "
            "gate is not actually running."
        )

    if unguarded:
        raise RuntimeError(
            "Routes without an authorization decision:\n  "
            + "\n  ".join(sorted(unguarded))
            + "\n\nAdd Depends(requires(...)) to the route, or — if it is "
            "genuinely public — add it to PUBLIC_ROUTES in "
            "apps/api/security/route_audit.py with a comment saying why. "
            "Default-deny is enforced here rather than in review because a "
            "missing check is invisible in a diff (05 §A2.4)."
        )


def unregistered_public_routes(app: FastAPI) -> set[str]:
    """Entries in :data:`PUBLIC_ROUTES` that match no route on the app.

    A stale allow-list entry is not a live vulnerability, but it is a route
    someone deleted or renamed while leaving its exemption behind — and the
    next route to land on that path inherits the exemption silently. Reported
    by a test rather than failing startup, since a partially-mounted app during
    development would otherwise trip it constantly.

    The four OpenAPI paths always appear here and are expected: FastAPI mounts
    ``/docs``, ``/redoc`` and ``/openapi.json`` as plain Starlette ``Route``
    objects, not ``APIRoute``, so :func:`iter_api_routes` does not see them —
    they are also never dispatched through the authz dependency system, which
    is why they are listed in :data:`PUBLIC_ROUTES` as documentation rather
    than as a live exemption. Callers filter them out.
    """
    live = {
        f"{method} {route.path}"
        for route in iter_api_routes(app)
        for method in (route.methods or set()) - {"HEAD", "OPTIONS"}
    }
    return set(PUBLIC_ROUTES) - live

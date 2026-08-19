"""``GET /api/v1/me`` — the current principal, permissions, tenant summary.

Drives RBAC-conditional UI (06 §8.4). The admin shell calls this once on load
and hides every control the principal cannot use.

**The permissions list here is advisory, not authoritative**, and that
distinction matters enough to state in the route that serves it. It exists so
the UI does not render a button that will 403; it is *not* the enforcement.
Enforcement is :func:`~apps.api.deps.requires` on each route plus RLS in the
database (05 §A0's three layers). A client that patched this response to list
every permission would gain exactly nothing.
"""

from __future__ import annotations

from apps.api.deps import CurrentPrincipal, ReadOnlyDbSession, requires
from apps.api.schemas import MeResponse, TenantSummary
from apps.api.security.permissions import Perm
from fastapi import APIRouter, Depends
from packages.persistence.repositories.tenants import TenantRepo
from packages.persistence.repositories.users import UserRepository

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["identity"])


@router.get(
    "/me",
    response_model=MeResponse,
    dependencies=[Depends(requires(Perm.TENANT_READ))],
    summary="The current principal and their tenant",
)
async def read_me(
    principal: CurrentPrincipal, session: ReadOnlyDbSession
) -> MeResponse:
    """Who am I, what may I do, and which workspace am I in.

    Guarded by ``tenant:read``, which every role in the standard grid holds
    (05 §A2.3). That is deliberate rather than leaving it unguarded: a route
    with no authorization decision would fail
    :func:`~apps.api.security.route_audit.assert_all_routes_guarded` at
    startup, and adding it to the public allow-list to avoid that would make a
    genuinely authenticated route look public in the one file people read to
    find out what is public.
    """
    tenant = await TenantRepo(session).current_or_raise()

    email: str | None = None
    display_name: str | None = None
    if principal.subject_user_id is not None:
        user = await UserRepository(session).get(principal.subject_user_id)
        if user is not None:
            email = user.email
            display_name = user.display_name or user.full_name

    return MeResponse(
        # The SUBJECT, not the actor: under impersonation the UI must show the
        # customer being acted as, with `is_impersonated` telling it to render
        # the banner that says so.
        user_id=principal.subject_user_id,
        email=email,
        display_name=display_name,
        kind=principal.kind,
        roles=list(principal.roles),
        # Sorted so the JSON is byte-stable across requests; an unstable
        # ordering defeats caching and makes every poll look like a change.
        permissions=sorted(principal.permissions),
        aal=principal.aal,
        is_impersonated=principal.is_impersonated,
        tenant=TenantSummary(
            id=tenant.id,
            slug=tenant.slug,
            display_name=tenant.display_name,
            status=str(tenant.status),
            plan=str(tenant.compliance_profile),
            timezone=tenant.default_timezone,
            currency=tenant.default_currency,
        ),
    )

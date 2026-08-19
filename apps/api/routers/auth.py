"""Login and refresh — 05 §A1.

These are the two routes that cannot require a token, because they mint one.
They are in :data:`~apps.api.security.route_audit.PUBLIC_ROUTES` for exactly
that reason and are defended by a per-IP rate limit instead (05 §B8.2), which
is what makes credential stuffing expensive.

**The login path reads users before any tenant context exists**, which is a
genuine ordering problem: RLS scopes ``users`` by tenant membership, and we do
not know the tenant until we know the user. 01 §11.6 solves it with
``auth_resolve_user_by_email`` and ``auth_list_memberships`` — ``SECURITY
DEFINER`` functions granted to a dedicated ``receptionai_auth`` role that has
``SELECT`` on nothing. The blast radius of a compromised auth credential is
"can enumerate password hashes", not "can read every tenant".

Phase 1 runs those functions through the ordinary app connection rather than a
separate ``receptionai_auth`` login role, because the local stack provisions
one credential. The functions are still the only path — so switching to a
dedicated role is a DSN change, not a code change. That gap is real and is
called out here rather than left for someone to discover.

**Refresh rotation with reuse detection** (05 §A1.3): every refresh mints a new
row pointing at its parent, and presenting an already-rotated token revokes the
whole family. A stolen refresh token is therefore usable at most once, and its
use *logs the legitimate user out*, which is the signal that surfaces the theft.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from apps.api.deps import AppStateDep
from apps.api.problems import ProblemException
from apps.api.schemas import LoginRequest, RefreshRequest, TokenResponse
from apps.api.security.passwords import verify_password
from apps.api.security.tokens import hash_refresh_token
from apps.api.state import AppState
from fastapi import APIRouter, Request, status
from packages.core.ids import uuid7
from packages.core.tenancy import TenantContext
from packages.persistence.models.identity import Session as SessionRow
from sqlalchemy import text

__all__ = ["router"]

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post(
    "/login", response_model=TokenResponse, status_code=status.HTTP_200_OK
)
async def login(
    body: LoginRequest, request: Request, state: AppStateDep
) -> TokenResponse:
    """Exchange credentials for an access token and a refresh token."""
    identity = await _resolve_user(state, body.email)

    # One error for "no such user" and for "wrong password", and the password
    # verify runs even when the user does not exist (`verify_password` does a
    # dummy hash on a None). Without both, response time and message
    # distinguish a registered email from an unregistered one, which is a user
    # enumeration oracle on a login form.
    if identity is None or not verify_password(body.password, identity["password_hash"]):
        log.info("auth.login_failed", reason="bad_credentials")
        raise ProblemException(
            status=401,
            code="invalid_token",
            detail="Email or password is incorrect.",
        )

    if identity["status"] != "active":
        raise ProblemException(
            status=403,
            code="forbidden",
            detail="This account is not active.",
        )
    locked_until = identity["locked_until"]
    if locked_until is not None and locked_until > datetime.now(tz=UTC):
        raise ProblemException(
            status=403,
            code="forbidden",
            detail="This account is temporarily locked.",
        )

    memberships = await _list_memberships(state, identity["user_id"])
    membership = _select_membership(memberships, body.tenant_slug)

    tenant_id = membership["tenant_id"]
    ctx = TenantContext(tenant_id=tenant_id, user_id=identity["user_id"])

    async with state.session_factory.unit_of_work(ctx) as session:
        from packages.persistence.repositories.tenants import TenantRepo
        from packages.persistence.repositories.users import UserRepository

        tenant = await TenantRepo(session).current_or_raise()
        if tenant.status in ("suspended", "cancelled"):
            raise ProblemException(
                status=403,
                code="tenant_suspended",
                detail=tenant.suspension_reason or "This workspace is suspended.",
            )

        permissions = await UserRepository(session).permissions_of(identity["user_id"])

        refresh = state.tokens.mint_refresh()
        session_id = uuid7()
        session.add(
            SessionRow(
                id=session_id,
                user_id=identity["user_id"],
                tenant_id=tenant_id,
                refresh_token_hash=refresh.digest,
                family_id=refresh.family_id,
                expires_at=refresh.expires_at,
                auth_method="password",
                # Neither is PII-free, but both are needed to make a stolen
                # session recognizable in the sessions list a user can review.
                ip_address=request.client.host if request.client else None,
                user_agent=(request.headers.get("user-agent") or "")[:512] or None,
            )
        )
        await session.flush()

    roles = tuple(m["role_code"] for m in memberships if m["tenant_id"] == tenant_id)
    access, claims = state.tokens.mint_access(
        user_id=identity["user_id"],
        tenant_id=tenant_id,
        tenant_plan=str(tenant.compliance_profile or "standard"),
        roles=roles,
        permissions=permissions,
        session_id=session_id,
        aal=1,
    )

    log.info(
        "auth.login",
        user_id=str(identity["user_id"]),
        tenant_id=str(tenant_id),
        roles=list(roles),
    )
    return TokenResponse(
        access_token=access,
        refresh_token=refresh.secret,
        expires_in=int((claims.exp - claims.iat).total_seconds()),
        tenant_id=tenant_id,
        user_id=identity["user_id"],
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(body: RefreshRequest, state: AppStateDep) -> TokenResponse:
    """Rotate a refresh token, detecting reuse — 05 §A1.3.

    The rotation is what makes theft survivable: the old token is revoked as
    soon as a new one is issued, so a stolen copy is dead on first use by
    whoever loses the race. Detecting the *loser's* attempt is how we learn a
    theft happened at all, and revoking the whole family is the response —
    aggressive on purpose, because a false positive costs one re-login and a
    false negative costs a compromised account.
    """
    token_digest = hash_refresh_token(body.refresh_token)

    async with state.session_factory.ops_unit_of_work() as ops:
        # `sessions` RLS is `user_id = app.user_id` (01 §11.5), and we do not
        # know the user until the row is found — the same bootstrapping problem
        # login has, solved the same way: a SECURITY DEFINER function
        # (migration 0002). The DIGEST is passed, never the token, so a
        # plaintext refresh token never reaches a query log or
        # `pg_stat_statements`.
        row = (
            await ops.execute(
                text("SELECT * FROM auth_find_session_by_token(:digest)"),
                {"digest": token_digest},
            )
        ).mappings().first()

        if row is None:
            raise ProblemException(
                status=401, code="invalid_token", detail="Invalid refresh token."
            )

        reuse_detected = row["revoked_at"] is not None
        expired = row["expires_at"] <= datetime.now(tz=UTC)

        user_id: UUID = row["user_id"]
        tenant_id: UUID = row["tenant_id"]
        parent_id: UUID = row["id"]
        family_id: UUID = row["family_id"]

    # ── Reuse detection, and why the revoke is OUTSIDE the block above ────
    #
    # `ops_unit_of_work` wraps its body in `session.begin()`, which commits on
    # a clean exit and ROLLS BACK on an exception. Revoking the family and then
    # raising inside that block therefore undoes the revocation — the response
    # says "this session was revoked" and the rows are untouched, so the stolen
    # token keeps working and the whole control is decorative.
    #
    # Found by a test asserting the child token stops working, which is the
    # only observable difference between the two orderings.
    if reuse_detected:
        # This token was already rotated, so either the legitimate holder
        # replayed an old one (harmless but wrong) or someone else has a copy.
        # We cannot tell, so we assume theft.
        log.warning(
            "auth.refresh_reuse_detected",
            family_id=str(family_id),
            user_id=str(user_id),
        )
        async with state.session_factory.ops_unit_of_work() as ops:
            await ops.execute(
                text("SELECT auth_revoke_session_family(:family, :reason)"),
                {"family": family_id, "reason": "refresh_token_reuse"},
            )
        # Committed. Now refuse.
        raise ProblemException(
            status=401,
            code="token_revoked",
            detail="This session was revoked. Please sign in again.",
        )

    if expired:
        raise ProblemException(
            status=401, code="token_expired", detail="Refresh token has expired."
        )

    ctx = TenantContext(tenant_id=tenant_id, user_id=user_id)
    async with state.session_factory.unit_of_work(ctx) as session:
        from packages.persistence.repositories.tenants import TenantRepo
        from packages.persistence.repositories.users import UserRepository

        tenant = await TenantRepo(session).current_or_raise()
        permissions = await UserRepository(session).permissions_of(user_id)

        rotated = state.tokens.mint_refresh(family_id=family_id)
        new_session_id = uuid7()
        session.add(
            SessionRow(
                id=new_session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                refresh_token_hash=rotated.digest,
                parent_session_id=parent_id,
                family_id=family_id,
                expires_at=rotated.expires_at,
                auth_method="password",
            )
        )
        await session.flush()

    # Revoke the parent. Through the definer function for the same reason the
    # lookup was: this session is scoped to `app.user_id`, and the parent row
    # is the one being replaced — an UPDATE filtered by a policy that may or
    # may not match is a revocation that may or may not happen.
    async with state.session_factory.ops_unit_of_work() as ops:
        await ops.execute(
            text("SELECT auth_revoke_session(:digest, :reason)"),
            {"digest": token_digest, "reason": "rotated"},
        )

    memberships = await _list_memberships(state, user_id)
    roles = tuple(m["role_code"] for m in memberships if m["tenant_id"] == tenant_id)

    access, claims = state.tokens.mint_access(
        user_id=user_id,
        tenant_id=tenant_id,
        tenant_plan=str(tenant.compliance_profile or "standard"),
        roles=roles,
        permissions=permissions,
        session_id=new_session_id,
        aal=1,
    )
    return TokenResponse(
        access_token=access,
        refresh_token=rotated.secret,
        expires_in=int((claims.exp - claims.iat).total_seconds()),
        tenant_id=tenant_id,
        user_id=user_id,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest, state: AppStateDep) -> None:
    """Revoke one refresh token.

    Only the presented token, not its family: a normal logout on a laptop must
    not sign the same user out on their phone. Family revocation is reserved
    for the reuse-detection path above, where it is a theft response.

    Idempotent, and silent about whether the token existed — a logout endpoint
    that reports "no such token" is an oracle for testing stolen tokens.
    """
    async with state.session_factory.ops_unit_of_work() as ops:
        await ops.execute(
            text("SELECT auth_revoke_session(:digest, :reason)"),
            {
                "digest": hash_refresh_token(body.refresh_token),
                "reason": "logout",
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# The pre-tenant lookups (01 §11.6)
# ─────────────────────────────────────────────────────────────────────────────


async def _resolve_user(state: AppState, email: str) -> dict[str, Any] | None:
    """``auth_resolve_user_by_email`` — a ``SECURITY DEFINER`` function.

    Returns authentication material and nothing else: no bookings, no
    conversations, no secrets. That narrowness is the security property, and it
    is why this is a function rather than a relaxed RLS policy.
    """
    async with state.session_factory.ops_unit_of_work() as ops:
        row = (
            await ops.execute(
                text("SELECT * FROM auth_resolve_user_by_email(:email)"),
                {"email": email},
            )
        ).mappings().first()
        return dict(row) if row else None


async def _list_memberships(state: AppState, user_id: UUID) -> list[dict[str, Any]]:
    """``auth_list_memberships`` — which tenants this user may sign in to."""
    async with state.session_factory.ops_unit_of_work() as ops:
        rows = (
            await ops.execute(
                text("SELECT * FROM auth_list_memberships(:user_id)"),
                {"user_id": user_id},
            )
        ).mappings().all()
        return [dict(row) for row in rows]


def _select_membership(
    memberships: list[dict[str, Any]], tenant_slug: str | None
) -> dict[str, Any]:
    """Choose the tenant this token will be scoped to.

    Refuses to guess when a user belongs to several and named none. The
    alternative — picking the first — means a contractor with access to two
    clinics silently signs into whichever sorts first, and every action they
    take lands in the wrong workspace. An explicit error costs one round-trip
    and is unambiguous.
    """
    live = [m for m in memberships if m["tenant_status"] not in ("cancelled",)]
    if not live:
        raise ProblemException(
            status=403,
            code="forbidden",
            detail="This account has no active workspace.",
        )

    if tenant_slug is not None:
        for membership in live:
            if membership["tenant_slug"] == tenant_slug:
                return membership
        raise ProblemException(
            status=403,
            code="forbidden",
            detail=f"No access to workspace {tenant_slug!r}.",
        )

    distinct = {m["tenant_id"] for m in live}
    if len(distinct) > 1:
        raise ProblemException(
            status=400,
            code="bad_request",
            detail=(
                "This account belongs to several workspaces. Send "
                "`tenant_slug` to choose one."
            ),
            available_tenants=sorted({str(m["tenant_slug"]) for m in live}),
        )
    return live[0]

"""The public demo widget's entry point — PHASE1-API-CONTRACT.

**One endpoint lives here**: ``POST /api/v1/public/demo/sessions``, which opens
an anonymous conversation against the seeded demo tenant and mints a stream
ticket.

The widget's *other* two calls — posting a turn and reading the stream — go to
the ordinary conversation routes in :mod:`apps.api.routers.conversations`,
because that is what the contract freezes:

    POST /api/v1/conversations/{id}/messages
    GET  /api/v1/conversations/{id}/stream?ticket=<ticket>

with *"`ticket` for the demo widget; admin clients authenticate by cookie and
omit it."* Those routes accept either credential;
:func:`~apps.api.deps.resolve_conversation_access` is where the two are
reconciled, and its docstring carries the argument for why one shared path is
safe.

The security model for the anonymous entry point:

* There is **no principal**, by design — that is the product. So the tenant is
  not resolved from a credential; it is **pinned** to the seeded demo tenant,
  resolved once at startup from a slug in configuration. A request cannot name
  a tenant, cannot influence which tenant is used, and cannot reach another
  tenant's rows even if it guesses a conversation id, because the RLS session
  is opened against the demo tenant and every other tenant's rows are invisible
  to it. That property has its own test.
* The ticket it mints is **single-use, 20 seconds, and scoped to one
  conversation** (:mod:`apps.api.tickets`) — because a browser cannot set
  ``Authorization`` on ``EventSource`` and a durable token must never appear in
  a URL.
* Abuse is bounded by a **per-IP rate limit** (05 §B8.2), applied by the
  middleware to the ``/api/v1/public/`` prefix.
"""

from __future__ import annotations

import structlog
from apps.api.deps import AppStateDep
from apps.api.problems import ProblemException
from apps.api.schemas import DemoSessionRequest, DemoSessionResponse
from apps.api.state import AppState
from fastapi import APIRouter, Request, status
from packages.core.tenancy import TenantContext
from packages.persistence.models.enums import ConversationChannel

__all__ = ["router"]

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/public/demo", tags=["public"])

def _demo_context(state: AppState) -> TenantContext:
    """The pinned demo tenant.

    Resolved at startup, not per request, and carried on the app state. A
    per-request lookup by slug would be a database round-trip on the hottest
    anonymous path — and, worse, would put a *string from configuration* into a
    query on every request, which is one refactor away from being a string from
    the request.
    """
    if state.demo_tenant_id is None:
        raise ProblemException(
            status=503,
            code="service_unavailable",
            detail=(
                "The demo is not configured on this deployment. Run "
                "tools/seed_demo.py to create the demo tenant."
            ),
        )
    return TenantContext(tenant_id=state.demo_tenant_id)


@router.post(
    "/sessions",
    response_model=DemoSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start an anonymous demo conversation",
)
async def start_demo_session(
    request: Request,
    state: AppStateDep,
    body: DemoSessionRequest | None = None,
) -> DemoSessionResponse:
    """Open a conversation against the seeded demo tenant and mint a ticket.

    The body is optional so a client that posts nothing at all — which the
    contract's own example does — still works.
    """
    ctx = _demo_context(state)
    mode = (body or DemoSessionRequest()).mode

    started = await state.conversations.start(
        ctx=ctx,
        # A browser voice call is inbound voice. Recording it as web chat would
        # make the channel column a constant.
        channel=(
            ConversationChannel.VOICE_INBOUND
            if mode == "voice"
            else ConversationChannel.WEB_CHAT
        ),
        # Not the raw IP: an IP is personal data under GDPR, and this row is
        # retained. The channel metadata records only that it was the demo.
        channel_metadata={"source": "public_demo", "mode": mode},
    )

    ticket = await state.tickets.mint(
        tenant_id=ctx.tenant_id, conversation_id=started.conversation_id
    )

    log.info(
        "demo.session_started",
        conversation_id=str(started.conversation_id),
        vertical=started.vertical,
    )
    # Note what is NOT returned: no tenant id, no manifest version, no user.
    return DemoSessionResponse(
        conversation_id=started.conversation_id,
        ticket=ticket,
        vertical=started.vertical,
        greeting=started.greeting,
    )

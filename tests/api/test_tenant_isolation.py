"""The demo endpoint cannot reach another tenant's data.

**This is the test the Phase 1 skeleton exists to pass.** Contract §2.7:
*"cross-tenant access is impossible by construction, not by convention."* The
public demo is the hardest case for that claim, because it is the one endpoint
with no principal at all — so if isolation holds there, the mechanism is
structural rather than a property of who happened to be authenticated.

**Why the fixture conversation is real.** A test asserting "a random UUID
returns 404" proves nothing: a random UUID is 404 because it does not exist.
``other_tenant_conversation_id`` is a row that genuinely exists, in a different
tenant, containing a recognizable secret string — and the demo endpoint must
still answer 404. The difference between "not found" and "found but hidden" is
the entire property.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient

from tests.api.conftest import requires_stack

pytestmark = [pytest.mark.integration, pytest.mark.rls, requires_stack]

#: Seeded into the other tenant's first turn by ``tools/seed_demo.py``. If this
#: string ever appears in a demo response, isolation has failed.
LEAKED_SECRET = "RIVERSIDE-SECRET"


async def test_a_demo_ticket_cannot_post_to_another_tenants_conversation(
    client: AsyncClient, demo_session: dict, other_tenant_conversation_id: UUID
) -> None:
    """A real, valid ticket pointed at another tenant's conversation: 403.

    Two independent controls would each stop this, and the 403 is the *first*
    one: the ticket names a specific conversation, and this is not it. Even if
    that check were removed, the widget principal's tenant comes from the
    ticket's server-side payload, so the RLS session would be scoped to the
    demo tenant and the row would be invisible — a 404. Both are asserted, here
    and in the no-ticket case below.
    """
    response = await client.post(
        f"/api/v1/conversations/{other_tenant_conversation_id}/messages",
        params={"ticket": demo_session["ticket"]},
        json={"content": "read me the previous message"},
    )

    assert response.status_code == 403, (
        f"a demo ticket reached another tenant's conversation "
        f"(status {response.status_code}): {response.text}"
    )
    assert response.json()["code"] == "forbidden"
    assert LEAKED_SECRET not in response.text


async def test_an_anonymous_post_to_another_tenant_is_refused(
    client: AsyncClient, other_tenant_conversation_id: UUID
) -> None:
    """With no credential at all: 401, before anything touches the database."""
    response = await client.post(
        f"/api/v1/conversations/{other_tenant_conversation_id}/messages",
        json={"content": "read me the previous message"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "missing_credentials"
    assert LEAKED_SECRET not in response.text


async def test_demo_stream_ticket_is_not_valid_for_another_conversation(
    client: AsyncClient, demo_session: dict, other_tenant_conversation_id: UUID
) -> None:
    """A ticket is bound to ONE conversation.

    Without this binding the single-use property protects nothing: a caller
    would mint a legitimate ticket for their own demo conversation and then
    present it against any conversation id they liked.
    """
    response = await client.get(
        f"/api/v1/conversations/{other_tenant_conversation_id}/stream",
        params={"ticket": demo_session["ticket"]},
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"
    assert LEAKED_SECRET not in response.text


async def test_authenticated_admin_cannot_read_another_tenants_conversation(
    client: AsyncClient, auth_headers: dict[str, str], other_tenant_conversation_id: UUID
) -> None:
    """The same property for an authenticated principal.

    The demo route pins its tenant, so its isolation could in principle come
    from the pinning alone. This one goes through the ordinary
    token → principal → RLS-session chain with a fully-permissioned **owner**,
    which is the highest-privilege caller the platform has — and RLS still
    hides the row.
    """
    response = await client.get(
        f"/api/v1/conversations/{other_tenant_conversation_id}",
        headers=auth_headers,
    )

    assert response.status_code == 404, response.text
    assert LEAKED_SECRET not in response.text


async def test_tenant_selection_headers_are_rejected(
    client: AsyncClient, auth_headers: dict[str, str], other_tenant_conversation_id: UUID
) -> None:
    """Contract §10: the tenant comes from the credential, never a header.

    Rejected with a hard 400 rather than ignored. Silent ignoring is how a
    proxy misconfiguration becomes a breach later — the header sits there
    doing nothing until some middleware starts honouring it.
    """
    for header in ("X-Tenant-Id", "X-Tenant", "X-Org-Id"):
        response = await client.get(
            "/api/v1/me",
            headers={**auth_headers, header: "019fd241-17ab-723b-b441-ffe4680b4884"},
        )
        assert response.status_code == 400, f"{header} was not rejected"
        assert response.json()["code"] == "tenant_header_not_permitted"


async def test_demo_response_never_carries_tenant_internals(
    client: AsyncClient,
) -> None:
    """PHASE1-API-CONTRACT: *"Never returns tenant internals."*

    An anonymous member of the public gets what the widget needs to render and
    connect, and nothing that identifies the tenant in our system.
    """
    response = await client.post("/api/v1/public/demo/sessions")
    assert response.status_code == 201

    body = response.json()
    assert set(body) == {"conversation_id", "ticket", "vertical", "greeting"}

    for forbidden in ("tenant_id", "tenant", "manifest_version", "slug", "user_id"):
        assert forbidden not in body, f"{forbidden} leaked into the demo response"

"""Stream tickets: single-use, short-lived, conversation-scoped.

The three properties PHASE1-API-CONTRACT specifies, each tested at both levels
— against the service directly, where the atomicity is visible, and through the
endpoint, where the status codes a frontend switches on are.

The concurrency test is the one that matters. ``GETDEL`` versus ``GET`` then
``DELETE`` is invisible in a sequential test: both pass. The difference only
appears when two redemptions race, which is exactly the condition a reconnect
storm produces.
"""

from __future__ import annotations

import asyncio

import pytest
from apps.api.tickets import TICKET_PREFIX, StreamTicketService
from httpx import AsyncClient
from packages.core.ids import uuid7

from tests.api.conftest import requires_stack

pytestmark = [pytest.mark.integration, requires_stack]


@pytest.fixture
def tickets(app) -> StreamTicketService:
    return app.state.app_state.tickets


async def test_a_ticket_redeems_exactly_once(tickets: StreamTicketService) -> None:
    tenant_id, conversation_id = uuid7(), uuid7()
    token = await tickets.mint(tenant_id=tenant_id, conversation_id=conversation_id)

    assert token.startswith(TICKET_PREFIX)

    first = await tickets.redeem(token)
    assert first is not None
    assert first.tenant_id == tenant_id
    assert first.conversation_id == conversation_id

    # And never again.
    assert await tickets.redeem(token) is None
    assert await tickets.redeem(token) is None


async def test_concurrent_redemption_yields_exactly_one_winner(
    tickets: StreamTicketService,
) -> None:
    """The property ``GETDEL`` buys, and the reason it is not GET-then-DELETE.

    Twenty coroutines race for one ticket. With a non-atomic read-then-delete,
    several read the value before any deletes it and several "win" — and the
    single-use guarantee evaporates under precisely the load (a reconnect
    storm) that makes it matter.
    """
    token = await tickets.mint(tenant_id=uuid7(), conversation_id=uuid7())

    results = await asyncio.gather(*(tickets.redeem(token) for _ in range(20)))
    winners = [claim for claim in results if claim is not None]

    assert len(winners) == 1, f"{len(winners)} redemptions succeeded; must be exactly 1"


async def test_an_unknown_or_malformed_ticket_is_refused(
    tickets: StreamTicketService,
) -> None:
    """Every failure mode answers identically — no oracle for probing."""
    assert await tickets.redeem("") is None
    assert await tickets.redeem("not-a-ticket") is None
    assert await tickets.redeem(f"{TICKET_PREFIX}completely-made-up") is None


async def test_a_revoked_ticket_cannot_be_redeemed(
    tickets: StreamTicketService,
) -> None:
    token = await tickets.mint(tenant_id=uuid7(), conversation_id=uuid7())
    await tickets.revoke(token)
    assert await tickets.redeem(token) is None


async def test_reconnect_with_a_spent_ticket_returns_401_meaning_remint(
    client: AsyncClient, demo_session: dict
) -> None:
    """*"A 401 on reconnect means re-mint the ticket, not auth failed"* (04 §9.1).

    ``EventSource`` reconnects by replaying the original URL, ticket included,
    so the second connection always presents a spent ticket. That is the
    expected path, not an error condition — and the frontends distinguish it by
    the ``WWW-Authenticate`` error parameter, which is why this asserts on the
    header and not only the status.
    """
    conversation_id = demo_session["conversation_id"]
    ticket = demo_session["ticket"]

    # First use consumes it. No pending message, so the stream reports idle and
    # closes — which is enough to prove the ticket was accepted and spent.
    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/stream",
        params={"ticket": ticket},
        timeout=10.0,
    ) as first:
        assert first.status_code == 200
        async for _ in first.aiter_lines():
            break

    # Isolate the ticket: the stream exchanged it for a session and httpx
    # stored that cookie, exactly as a browser would. Without clearing it the
    # reconnect below would authenticate on the SESSION and return 200, which
    # is correct behaviour but is not what this test is about.
    client.cookies.clear()

    second = await client.get(
        f"/api/v1/conversations/{conversation_id}/stream",
        params={"ticket": ticket},
    )

    assert second.status_code == 401
    assert second.headers["content-type"].startswith("application/problem+json")
    assert 'error="invalid_token"' in second.headers.get("www-authenticate", "")

    body = second.json()
    assert body["code"] == "invalid_token"
    # The remedy has to be in the message: a frontend developer reading a bare
    # 401 concludes the session is dead and sends the user back to the start.
    assert "single-use" in body["detail"].lower()


async def test_a_stream_requires_some_credential(
    client: AsyncClient, demo_session: dict
) -> None:
    """No token and no ticket is a 401, never an open stream.

    The ``ticket`` parameter is optional in the signature — an admin client
    legitimately omits it and authenticates by header — so the refusal comes
    from `resolve_conversation_access` failing closed rather than from request
    validation. That distinction is why this asserts 401 and not 422.
    """
    response = await client.get(
        f"/api/v1/conversations/{demo_session['conversation_id']}/stream"
    )
    assert response.status_code == 401
    assert response.json()["code"] == "missing_credentials"


# ─────────────────────────────────────────────────────────────────────────────
# What a ticket may and may not reach
# ─────────────────────────────────────────────────────────────────────────────


async def test_posting_a_message_does_not_consume_the_ticket(
    client: AsyncClient, demo_session: dict
) -> None:
    """``/messages`` peeks; only ``/stream`` redeems.

    The sequencing the widget depends on: it posts a turn and then opens the
    stream, both with the same ticket. If the POST consumed it, the widget
    could never read the answer to its own question and every turn would need
    two tickets.
    """
    conversation_id = demo_session["conversation_id"]
    ticket = demo_session["ticket"]

    posted = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        params={"ticket": ticket},
        json={"content": "hello"},
    )
    assert posted.status_code == 202

    # Still valid, because the POST only peeked.
    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/stream",
        params={"ticket": ticket},
        timeout=30.0,
    ) as stream:
        assert stream.status_code == 200, (
            "the ticket was consumed by POST /messages, so the widget cannot "
            "read the reply to its own turn"
        )
        async for _ in stream.aiter_lines():
            break


async def test_a_ticket_cannot_reach_me(
    client: AsyncClient, demo_session: dict
) -> None:
    """A widget principal is not a session.

    `/me` takes no ticket parameter and the middleware finds no credential, so
    it is a 401 — the ticket buys access to one conversation, never an identity.
    """
    response = await client.get(
        "/api/v1/me", params={"ticket": demo_session["ticket"]}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "missing_credentials"


async def test_a_ticket_cannot_read_a_conversation_it_does_not_name(
    client: AsyncClient, demo_session: dict
) -> None:
    """Even within the same tenant, and even for a GET.

    Two demo sessions belong to the *same* seeded tenant, so RLS alone would
    let either see the other. The conversation binding on the ticket is what
    stops it — which is why this is tested separately from the cross-tenant
    case.
    """
    other = await client.post("/api/v1/public/demo/sessions")
    other_id = other.json()["conversation_id"]
    assert other_id != demo_session["conversation_id"]

    response = await client.get(
        f"/api/v1/conversations/{other_id}/stream",
        params={"ticket": demo_session["ticket"]},
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


async def test_a_ticket_cannot_post_into_another_conversation_in_its_tenant(
    client: AsyncClient, demo_session: dict
) -> None:
    """The same binding on the write path."""
    other = await client.post("/api/v1/public/demo/sessions")
    other_id = other.json()["conversation_id"]

    response = await client.post(
        f"/api/v1/conversations/{other_id}/messages",
        params={"ticket": demo_session["ticket"]},
        json={"content": "not my conversation"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


async def test_an_admin_token_beats_a_stale_ticket_parameter(
    client: AsyncClient, auth_headers: dict[str, str], demo_session: dict
) -> None:
    """A request carrying both credentials resolves as the admin.

    Honouring the weaker one would silently downgrade an admin session — and
    would do it on a request that looks entirely normal.
    """
    response = await client.get(
        f"/api/v1/conversations/{demo_session['conversation_id']}",
        params={"ticket": "dt_obviously-not-a-real-ticket"},
        headers=auth_headers,
    )
    # The bogus ticket is ignored; the token authorizes the read.
    assert response.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# The widget's real ordering: stream FIRST, then post
# ─────────────────────────────────────────────────────────────────────────────
#
# Every test above this block posts a message and then opens the stream. That
# is the natural order for curl and for an integration test, and it is the
# OPPOSITE of what a browser does — `EventSource` is opened when the widget
# mounts, and the user types afterwards.
#
# The whole suite passed while the demo was completely broken in a browser,
# because a ticket consumed by opening the stream left nothing for the message.
# These tests exercise the browser's order.


async def _drain_until(response, *, event_name: str, limit: int = 200):
    """Read SSE frames until ``event_name`` arrives. Returns its payload."""
    import json

    current: str | None = None
    seen = 0
    async for line in response.aiter_lines():
        seen += 1
        if seen > limit:
            break
        if line.startswith("event: "):
            current = line[7:]
        elif line.startswith("data: ") and current == event_name:
            return json.loads(line[6:])
    return None


async def test_stream_first_then_post_is_the_supported_ordering(
    client: AsyncClient, demo_session: dict
) -> None:
    """The browser's order: open the stream, wait, then send.

    This is the regression test for the bug that shipped. Opening the stream
    redeems the ticket, so the POST that follows must authenticate with the
    exchanged session — and if it cannot, the demo cannot hold a conversation
    at all.
    """
    conversation_id = demo_session["conversation_id"]

    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/stream",
        params={"ticket": demo_session["ticket"]},
        timeout=30.0,
    ) as stream:
        assert stream.status_code == 200

        session = await _drain_until(stream, event_name="session")
        assert session is not None, (
            "the stream did not emit a `session` event, so a cross-origin "
            "widget has no credential for its next request"
        )
        assert session["session"].startswith("ds_")
        assert session["conversation_id"] == conversation_id
        assert session["expires_in"] > 60

        # The user types. The ticket is now spent; the session is what works.
        posted = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"X-Conversation-Session": session["session"]},
            json={"content": "hello there"},
        )
        assert posted.status_code == 202, (
            f"the widget could not send its first message: {posted.text}"
        )


async def test_the_spent_ticket_no_longer_works_after_the_exchange(
    client: AsyncClient, demo_session: dict
) -> None:
    """Exchanging does not weaken single-use.

    The ticket is consumed by the exchange exactly as it was before; what
    changed is that it now buys a credential rather than only a socket.
    """
    conversation_id = demo_session["conversation_id"]
    ticket = demo_session["ticket"]

    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/stream",
        params={"ticket": ticket},
        timeout=30.0,
    ) as stream:
        assert stream.status_code == 200
        await _drain_until(stream, event_name="session")

    # Clear the exchanged cookie so the TICKET is what is under test. httpx
    # stores `Set-Cookie` like a browser does, so leaving it would make this
    # assert the session's validity rather than the ticket's — and it would
    # pass for the wrong reason.
    client.cookies.clear()

    reused = await client.get(
        f"/api/v1/conversations/{conversation_id}/stream",
        params={"ticket": ticket},
    )
    assert reused.status_code == 401
    assert reused.json()["code"] == "invalid_token"


async def test_a_session_is_reusable_across_several_messages(
    client: AsyncClient, demo_session: dict
) -> None:
    """A conversation is many turns; the credential must survive all of them.

    A single-use credential here would mean one message per session, which is
    a conversation of length one.
    """
    conversation_id = demo_session["conversation_id"]

    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/stream",
        params={"ticket": demo_session["ticket"]},
        timeout=30.0,
    ) as stream:
        session = await _drain_until(stream, event_name="session")

    assert session is not None
    headers = {"X-Conversation-Session": session["session"]}

    for message in ("first", "second", "third"):
        posted = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": message},
        )
        assert posted.status_code == 202, f"message {message!r}: {posted.text}"


async def test_a_session_is_scoped_to_its_conversation(
    client: AsyncClient, demo_session: dict
) -> None:
    """403 on another conversation — including within the same tenant.

    The exchanged credential inherits the ticket's binding. Without this a
    widget would obtain a legitimate session for its own chat and then use it
    against any conversation id it guessed, and RLS alone would not stop it
    because both belong to the seeded demo tenant.
    """
    conversation_id = demo_session["conversation_id"]

    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/stream",
        params={"ticket": demo_session["ticket"]},
        timeout=30.0,
    ) as stream:
        session = await _drain_until(stream, event_name="session")
    assert session is not None

    other = await client.post("/api/v1/public/demo/sessions")
    other_id = other.json()["conversation_id"]
    assert other_id != conversation_id

    response = await client.post(
        f"/api/v1/conversations/{other_id}/messages",
        headers={"X-Conversation-Session": session["session"]},
        json={"content": "not my conversation"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


async def test_a_session_cannot_reach_me(
    client: AsyncClient, demo_session: dict
) -> None:
    """The exchanged credential is not a login.

    `/me` has no ``{conversation_id}``, so there is nothing a
    conversation-scoped credential could authorize, and resolution fails closed
    before the credential is even examined.
    """
    conversation_id = demo_session["conversation_id"]

    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/stream",
        params={"ticket": demo_session["ticket"]},
        timeout=30.0,
    ) as stream:
        session = await _drain_until(stream, event_name="session")
    assert session is not None

    response = await client.get(
        "/api/v1/me", headers={"X-Conversation-Session": session["session"]}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "missing_credentials"


async def test_a_session_cannot_read_another_tenants_conversation(
    client: AsyncClient, demo_session: dict, other_tenant_conversation_id
) -> None:
    """The cross-tenant case, for the exchanged credential specifically."""
    conversation_id = demo_session["conversation_id"]

    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/stream",
        params={"ticket": demo_session["ticket"]},
        timeout=30.0,
    ) as stream:
        session = await _drain_until(stream, event_name="session")
    assert session is not None

    response = await client.post(
        f"/api/v1/conversations/{other_tenant_conversation_id}/messages",
        headers={"X-Conversation-Session": session["session"]},
        json={"content": "leak it"},
    )
    assert response.status_code == 403
    assert "RIVERSIDE-SECRET" not in response.text


async def test_reconnecting_with_the_session_needs_no_new_ticket(
    client: AsyncClient, demo_session: dict
) -> None:
    """A dropped stream reconnects on the credential it already holds.

    ``EventSource`` replays the original URL with its spent ticket, which 401s
    — that is the contract's "re-mint" path and is tested above. A widget that
    kept its session can instead reconnect with that and skip the round-trip
    entirely, which is the path a well-behaved client should take.
    """
    conversation_id = demo_session["conversation_id"]

    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/stream",
        params={"ticket": demo_session["ticket"]},
        timeout=30.0,
    ) as stream:
        session = await _drain_until(stream, event_name="session")
    assert session is not None

    # Reconnect: no ticket at all, just the session.
    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/stream",
        headers={"X-Conversation-Session": session["session"]},
        timeout=30.0,
    ) as reconnected:
        assert reconnected.status_code == 200, (
            "a session-bearing reconnect was refused, so a widget that dropped "
            "its connection cannot resume without re-minting"
        )


async def test_the_session_cookie_is_httponly_and_scoped(
    client: AsyncClient, demo_session: dict
) -> None:
    """The cookie half of the delivery.

    ``HttpOnly`` so an XSS on the embedding page cannot read it, and
    path-scoped so it is not sent to unrelated endpoints.
    """
    conversation_id = demo_session["conversation_id"]

    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/stream",
        params={"ticket": demo_session["ticket"]},
        timeout=30.0,
    ) as stream:
        assert stream.status_code == 200
        set_cookie = stream.headers.get("set-cookie", "")

    assert "rai_demo_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Path=/api/v1/conversations" in set_cookie
    assert "SameSite=lax" in set_cookie.replace("Samesite", "SameSite")

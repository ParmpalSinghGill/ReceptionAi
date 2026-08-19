"""Endpoint behaviour against the frozen Phase 1 contract.

Two frontends are being built against ``PHASE1-API-CONTRACT.md`` in parallel, so
these assert the *shape* of each response — status codes, field names, error
envelope — not just that a request succeeds. A response that works but renames
a field breaks both frontends, and only this catches it.
"""

from __future__ import annotations

import re

import pytest
from httpx import AsyncClient

from tests.api.conftest import requires_stack

pytestmark = [pytest.mark.integration, requires_stack]

#: RFC 3339 UTC with a literal `Z`, per the contract's Conventions section.
RFC3339_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def _installed_manifest_version() -> str:
    """The version of the manifest the demo tenant actually has installed.

    Read from the file the seed installs rather than hardcoded, so publishing a
    new manifest version is a one-file change instead of a hunt through the
    suite for literals that all mean "whatever is current".
    """
    from packages.verticals import load_manifest_file
    from packages.verticals.manifests import manifest_path

    return load_manifest_file(manifest_path("medical")).version
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# ── Health ───────────────────────────────────────────────────────────────────


async def test_healthz_is_liveness_only(client: AsyncClient) -> None:
    """Liveness must not check dependencies.

    A liveness probe that fails when Postgres is down gets every pod killed
    during a database blip, turning a recoverable incident into an outage plus
    a cold start.
    """
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_checks_dependencies(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "ok"


# ── The demo session ─────────────────────────────────────────────────────────


async def test_demo_session_matches_the_contract(client: AsyncClient) -> None:
    response = await client.post("/api/v1/public/demo/sessions")

    assert response.status_code == 201
    body = response.json()

    assert UUID_RE.match(body["conversation_id"])
    assert body["ticket"].startswith("dt_")
    assert body["vertical"] == "medical"
    # From the manifest, verbatim — not generated.
    assert body["greeting"] == "Thanks for calling Northgate Dental — how can I help?"


async def test_posting_a_message_returns_202_with_a_turn_id(
    client: AsyncClient, demo_session: dict
) -> None:
    response = await client.post(
        f"/api/v1/conversations/{demo_session['conversation_id']}/messages",
        params={"ticket": demo_session["ticket"]},
        json={"content": "hello", "idempotency_key": "test-key-1"},
    )

    assert response.status_code == 202
    body = response.json()
    assert UUID_RE.match(body["turn_id"])
    assert RFC3339_Z.match(body["accepted_at"]), (
        f"timestamps must be RFC 3339 UTC with a literal Z, got "
        f"{body['accepted_at']!r}"
    )


async def test_message_content_is_validated(
    client: AsyncClient, demo_session: dict
) -> None:
    conversation_id = demo_session["conversation_id"]

    ticket = demo_session["ticket"]
    empty = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        params={"ticket": ticket},
        json={"content": ""},
    )
    assert empty.status_code == 422
    assert empty.json()["code"] == "validation_failed"

    unknown_field = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        params={"ticket": ticket},
        json={"content": "hi", "surprise": "value"},
    )
    # `extra="forbid"` on the request models: an ignored field is a client that
    # believes it sent something it did not.
    assert unknown_field.status_code == 422


# ── Errors ───────────────────────────────────────────────────────────────────


async def test_every_error_is_rfc9457(client: AsyncClient) -> None:
    """Amendment A9: one envelope, handler-level and middleware-level alike."""
    response = await client.get("/api/v1/me")  # no credentials

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")

    body = response.json()
    for field in ("type", "title", "status", "code", "instance"):
        assert field in body, f"problem body is missing {field!r}"
    assert body["status"] == 401
    assert body["type"].startswith("https://receptionai.dev/problems/")
    assert body["code"] == "missing_credentials"
    # Every response carries it, and it is what a user quotes to support.
    assert body["request_id"] == response.headers["x-request-id"]


async def test_an_unknown_guarded_path_is_401_before_it_is_404(
    client: AsyncClient,
) -> None:
    """Authentication runs before routing, and that ordering is deliberate.

    An unauthenticated request to an unknown path under ``/api/v1/`` gets 401,
    not 404. Middleware sits above the router, so the credential check happens
    before Starlette decides whether a path matches — which means an
    unauthenticated caller cannot use 404-versus-401 to map the API surface.

    It also means a route *added* without a matching middleware exemption fails
    closed rather than open, which is the direction we want to be wrong in.
    """
    response = await client.get("/api/v1/does-not-exist")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "missing_credentials"


async def test_an_unknown_path_404s_once_authenticated(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """With a credential, routing runs and the 404 is a problem document.

    Starlette raises its own ``HTTPException`` for an unmatched path, before
    any handler — so it must be translated into the one envelope, or clients
    would meet a second error shape from paths they mistyped.
    """
    response = await client.get("/api/v1/does-not-exist", headers=auth_headers)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "not_found"


async def test_request_id_is_echoed_and_generated(client: AsyncClient) -> None:
    generated = await client.get("/healthz")
    assert UUID_RE.match(generated.headers["x-request-id"])

    supplied = await client.get("/healthz", headers={"X-Request-Id": "trace-abc-123"})
    assert supplied.headers["x-request-id"] == "trace-abc-123"

    # A malformed value is replaced, not echoed: it lands in a response header
    # and in structured logs, where a CRLF is header splitting and a huge value
    # is log flooding.
    hostile = await client.get("/healthz", headers={"X-Request-Id": "a" * 500})
    assert hostile.headers["x-request-id"] != "a" * 500
    assert UUID_RE.match(hostile.headers["x-request-id"])


# ── Auth ─────────────────────────────────────────────────────────────────────


async def test_login_returns_a_usable_token_pair(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "demo@northgate.dental",
            "password": "demo1234",
            "tenant_slug": "northgate-dental",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] > 0
    assert UUID_RE.match(body["tenant_id"])
    assert UUID_RE.match(body["user_id"])
    assert body["access_token"].count(".") == 2  # a JWS
    assert body["refresh_token"] and "." not in body["refresh_token"]  # opaque


async def test_a_wrong_password_is_indistinguishable_from_a_missing_user(
    client: AsyncClient,
) -> None:
    """No user-enumeration oracle on the login form."""
    wrong_password = await client.post(
        "/api/v1/auth/login",
        json={"email": "demo@northgate.dental", "password": "not-the-password"},
    )
    no_such_user = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "not-the-password"},
    )

    assert wrong_password.status_code == no_such_user.status_code == 401
    assert wrong_password.json()["code"] == no_such_user.json()["code"]
    assert wrong_password.json()["detail"] == no_such_user.json()["detail"]


async def test_refresh_rotates_and_detects_reuse(client: AsyncClient) -> None:
    """05 §A1.3. Reuse of a rotated token revokes the whole family.

    Aggressive on purpose: a false positive costs one re-login, a false
    negative costs a compromised account.
    """
    login = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "demo@northgate.dental",
            "password": "demo1234",
            "tenant_slug": "northgate-dental",
        },
    )
    original = login.json()["refresh_token"]

    rotated = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": original}
    )
    assert rotated.status_code == 200
    new_token = rotated.json()["refresh_token"]
    assert new_token != original

    # Replaying the original is the theft signal.
    reused = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": original}
    )
    assert reused.status_code == 401
    assert reused.json()["code"] == "token_revoked"

    # And the family is gone, including the token that legitimately replaced it.
    after = await client.post("/api/v1/auth/refresh", json={"refresh_token": new_token})
    assert after.status_code == 401


async def test_me_reports_permissions_for_rbac_driven_ui(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/api/v1/me", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "user"
    assert "owner" in body["roles"]
    assert body["is_impersonated"] is False
    assert body["tenant"]["slug"] == "northgate-dental"
    # Sorted, so the JSON is byte-stable across polls.
    assert body["permissions"] == sorted(body["permissions"])
    assert "conversations:read" in body["permissions"]


async def test_an_invalid_token_is_refused(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/me", headers={"Authorization": "Bearer not.a.jwt"}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


# ── Conversations ────────────────────────────────────────────────────────────


async def test_a_full_turn_is_persisted_and_readable(
    client: AsyncClient, auth_headers: dict[str, str], demo_session: dict
) -> None:
    """The walking skeleton's whole claim, end to end.

    Contract §7's shape, written by the manifest-compiled graph, read back
    through the authenticated path with the same tenant.
    """
    conversation_id = demo_session["conversation_id"]

    await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        params={"ticket": demo_session["ticket"]},
        json={"content": "what are your opening hours?"},
    )
    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/stream",
        params={"ticket": demo_session["ticket"]},
        timeout=30.0,
    ) as stream:
        async for line in stream.aiter_lines():
            if "turn_complete" in line:
                break

    detail = await client.get(
        f"/api/v1/conversations/{conversation_id}", headers=auth_headers
    )
    assert detail.status_code == 200
    body = detail.json()

    assert body["channel"] == "web_chat"
    assert body["agent_code"] == "medical"
    # Read from the installed manifest rather than hardcoded: the point of this
    # assertion is that the conversation records WHICH manifest version answered
    # it — a call that started on one version must finish on it, even if a new
    # one ships mid-call (03 §4.4). Pinning the literal would make a routine
    # manifest publish look like a regression.
    assert body["agent_version"] == _installed_manifest_version()
    assert RFC3339_Z.match(body["started_at"])

    roles = [turn["role"] for turn in body["turns"]]
    assert "user" in roles and "assistant" in roles

    assistant = next(t for t in body["turns"] if t["role"] == "assistant")
    assert assistant["content"], "the assistant turn has no content"
    # Which manifest agent produced it — routed, not hardcoded.
    assert assistant["agent_node"] in {
        "receptionist",
        "scheduler",
        "triage",
        "practice_faq",
        "human_transfer",
        "wrap_up",
    }

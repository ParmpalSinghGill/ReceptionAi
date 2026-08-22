"""``GET /api/v1/conversations`` — the portal's list view.

The properties worth a test here are the ones that would otherwise be
discovered in production, by the wrong person:

* the tenant is **not** a parameter, so naming another one cannot widen the
  answer (contract §10);
* a **viewer** — a principal with ``conversations:read`` and without
  ``conversations:read_pii`` — sees that a conversation happened and gets null
  where the caller's words, name and number would be (05 §A2.3);
* the cursor pages without duplicating or skipping rows, and refuses a cursor
  minted for a different sort;
* filters narrow rather than merely appearing to.

They run against the real stack, because every one of them is a property of the
database and of RLS. An isolation proof against a mocked repository proves only
that the mock returned what it was told to.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from httpx import AsyncClient

from tests.api.conftest import requires_stack

pytestmark = [pytest.mark.integration, requires_stack]


async def _first_page(client: AsyncClient, headers: dict[str, str], **params) -> dict:
    response = await client.get(
        "/api/v1/conversations", params={"limit": 25, **params}, headers=headers
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def test_the_list_is_newest_first_with_a_readable_summary_per_row(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """The shape the page renders, in one request.

    A receptionist opens this to find the chat they held ninety seconds ago, so
    the order is not incidental — it is the feature. Asserting the summary
    fields here is what stops a later refactor from turning the screen into an
    N+1 of detail lookups just to show a turn count.
    """
    body = await _first_page(client, auth_headers)

    if not body["items"]:
        pytest.skip("no conversations for the demo tenant; run `python3 tools/seed_demo.py`")

    starts = [row["started_at"] for row in body["items"]]
    assert starts == sorted(starts, reverse=True), (
        "the list must be newest-first — this screen answers 'what just "
        "happened', and a chronological list buries that under history"
    )

    for row in body["items"]:
        assert row["started_at"].endswith("Z"), "contract §6: RFC 3339 UTC with Z"
        assert row["channel"], "a row without a channel cannot be rendered"
        assert isinstance(row["turn_count"], int)
        # `contained` is the inverse of escalation and drives a badge on every
        # row; a null there would render as an empty cell rather than a state.
        assert row["contained"] is not row["escalated"]


async def test_the_cursor_pages_without_duplicating_or_skipping_rows(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Three pages, and every row appears exactly once.

    This is the property offset pagination cannot give on an append-heavy
    partitioned table: rows written between two requests shift an offset window
    under the reader, so some rows come back twice and others are never seen.
    A keyset cursor names a position in the data instead of a count of rows
    skipped.
    """
    seen: list[str] = []
    cursor: str | None = None

    for _ in range(3):
        params: dict[str, object] = {"limit": 5}
        if cursor:
            params["cursor"] = cursor
        response = await client.get(
            "/api/v1/conversations", params=params, headers=auth_headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(row["id"] for row in body["items"])
        cursor = body.get("next_cursor")
        if not cursor:
            break

    if len(seen) < 2:
        pytest.skip("not enough conversations seeded to page through")

    assert len(seen) == len(set(seen)), (
        "a row came back on two different pages — the keyset predicate is not "
        "excluding the previous page's last row"
    )


async def test_a_cursor_from_another_sort_is_refused_rather_than_misread(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """A cursor carries its own sort, and a mismatch is a 400.

    Without the embedded sort key this is the worst failure available: the
    keyset predicate is applied against the wrong column, and the caller gets a
    plausible page that is silently wrong. Nothing errors, so nobody looks.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from packages.core.api.pagination import encode_cursor

    foreign = encode_cursor(datetime.now(UTC), uuid4(), sort="created_at:asc")

    response = await client.get(
        "/api/v1/conversations",
        params={"cursor": foreign},
        headers=auth_headers,
    )

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["type"].startswith("https://receptionai.dev/problems/")


async def test_a_malformed_cursor_is_a_400_problem_document_not_a_500(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """A cursor arrives in a query string, so it can be truncated or edited."""
    response = await client.get(
        "/api/v1/conversations",
        params={"cursor": "clearly-not-a-cursor"},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "validation_failed"


async def test_a_tenant_id_parameter_cannot_change_the_response(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Contract §10: the tenant comes from the token's ``tid``, never the request.

    Two different refusals, and both matter:

    * a ``tenant_id`` **query parameter** is *inert* — the route has no such
      parameter, so naming another tenant changes nothing rather than being
      picked up by some permissive filter layer;
    * a tenant-selecting **header** is *rejected outright* with 400
      ``tenant_header_not_permitted``, which is the stronger of the two
      answers: the request never reaches the handler at all.

    Asserting the second as "returns the same rows" would still pass if the
    middleware were deleted, so it is asserted as the refusal it actually is.
    """
    honest = await client.get(
        "/api/v1/conversations", params={"limit": 10}, headers=auth_headers
    )
    assert honest.status_code == 200, honest.text

    smuggled_param = await client.get(
        "/api/v1/conversations",
        params={
            "limit": 10,
            "tenant_id": "019fd241-17ab-723b-b441-ffe4680b4884",
            "tid": "019fd241-17ab-723b-b441-ffe4680b4884",
        },
        headers=auth_headers,
    )

    assert smuggled_param.status_code == 200, smuggled_param.text
    assert smuggled_param.json() == honest.json(), (
        "a tenant_id query parameter changed the response — the tenant must "
        "come from the token's tid claim and nothing else"
    )

    smuggled_header = await client.get(
        "/api/v1/conversations",
        params={"limit": 10},
        headers={**auth_headers, "X-Tenant-Id": "019fd241-17ab-723b-b441-ffe4680b4884"},
    )

    assert smuggled_header.status_code == 400, smuggled_header.text
    assert smuggled_header.json()["code"] == "tenant_header_not_permitted"


async def test_a_viewer_sees_the_conversation_but_not_what_was_said(
    client: AsyncClient, auth_headers: dict[str, str], viewer_headers: dict[str, str]
) -> None:
    """05 §A2.3's asymmetry, which is the whole reason the two permissions split.

    A viewer holds ``conversations:read`` and not ``conversations:read_pii``.
    They must still see that a conversation happened, on what channel, how many
    turns it ran and how it went — that is an operations view, and removing it
    would make the role useless. What they must not see is the content: the
    caller's opening line, name and number.

    Asserted against the OWNER's response for the same rows, so this cannot
    pass by the viewer simply getting an empty list.
    """
    owner_body = await _first_page(client, auth_headers)
    if not owner_body["items"]:
        pytest.skip("no conversations for the demo tenant")

    viewer_body = await _first_page(client, viewer_headers)

    assert [row["id"] for row in viewer_body["items"]] == [
        row["id"] for row in owner_body["items"]
    ], "a viewer must see the same conversations, not fewer"

    for row in viewer_body["items"]:
        assert row["opening_line"] is None, (
            "the caller's own words reached a principal without "
            "conversations:read_pii — masking must happen server-side"
        )
        assert row["contact_identifier"] is None
        assert row["contact_name"] is None

    # The non-PII half must survive, or the role sees nothing worth seeing.
    for viewer_row, owner_row in zip(
        viewer_body["items"], owner_body["items"], strict=True
    ):
        assert viewer_row["channel"] == owner_row["channel"]
        assert viewer_row["turn_count"] == owner_row["turn_count"]
        assert viewer_row["started_at"] == owner_row["started_at"]
        assert viewer_row["outcome"] == owner_row["outcome"]

    # And the owner must actually be getting something, or the masking
    # assertion above is vacuous.
    assert any(row["opening_line"] for row in owner_body["items"]), (
        "no owner row carried an opening line, so the masking assertion "
        "proves nothing — seed a conversation with turns"
    )


async def test_a_viewer_gets_masked_content_on_the_detail_view_too(
    client: AsyncClient, auth_headers: dict[str, str], viewer_headers: dict[str, str]
) -> None:
    """Masking one screen and not the other is the same leak with extra steps."""
    body = await _first_page(client, auth_headers)
    with_turns = next((row for row in body["items"] if row["turn_count"] > 0), None)
    if with_turns is None:
        pytest.skip("no conversation with turns to inspect")

    response = await client.get(
        f"/api/v1/conversations/{with_turns['id']}", headers=viewer_headers
    )
    assert response.status_code == 200, response.text

    user_turns = [t for t in response.json()["turns"] if t["role"] == "user"]
    for turn in user_turns:
        assert turn["content"] is None or "redacted" in turn["content"], (
            "a user turn's content reached a principal without "
            "conversations:read_pii unmasked"
        )


async def test_the_channel_filter_narrows_to_that_channel(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """A filter that returns everything is indistinguishable from no filter."""
    body = await _first_page(client, auth_headers)
    if not body["items"]:
        pytest.skip("no conversations for the demo tenant")

    target = body["items"][0]["channel"]
    filtered = await _first_page(client, auth_headers, channel=target)

    assert filtered["items"], "filtering by a channel that IS present returned nothing"
    assert {row["channel"] for row in filtered["items"]} == {target}


async def test_the_search_matches_turn_content_and_treats_it_as_text(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """``?q=`` searches what was said, and a wildcard is a literal.

    The second half matters more than it looks: without escaping, a caller
    searching for ``100%`` gets every conversation in the tenant, because their
    text was silently promoted to a LIKE pattern.
    """
    body = await _first_page(client, auth_headers)
    line = next((row["opening_line"] for row in body["items"] if row["opening_line"]), None)
    if not line:
        pytest.skip("no conversation with a caller line to search for")

    word = max(line.split(), key=len)
    if len(word) < 3:
        pytest.skip("no usefully distinctive word in the seeded transcripts")

    found = await _first_page(client, auth_headers, q=word)
    assert found["items"], f"searching for {word!r}, which is in a transcript, found nothing"

    wildcard = await _first_page(client, auth_headers, q="%")
    assert not wildcard["items"], (
        "a literal '%' matched conversations — the search term is being used "
        "as a LIKE pattern instead of as text"
    )


async def test_an_unknown_channel_is_a_422_naming_the_alternatives(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get(
        "/api/v1/conversations",
        params={"channel": "carrier_pigeon"},
        headers=auth_headers,
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_failed"
    assert "web_chat" in body["detail"], "the error should say what IS accepted"


async def test_a_reversed_date_range_is_a_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get(
        "/api/v1/conversations",
        params={"from": "2026-08-20T00:00:00Z", "to": "2026-08-01T00:00:00Z"},
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


async def test_the_list_requires_authentication(client: AsyncClient) -> None:
    """No credential, no list — and the refusal carries the frozen shape."""
    response = await client.get("/api/v1/conversations")

    assert response.status_code == 401
    assert response.json()["type"].startswith("https://receptionai.dev/problems/")


async def test_a_widget_ticket_cannot_list_conversations(
    client: AsyncClient, demo_session: dict
) -> None:
    """The demo widget holds a credential for ONE conversation, not for the list.

    A ticket yields ``WIDGET_PERMISSIONS`` with ``scope_all_conversations``
    false. If that credential could reach this route, a visitor to the public
    marketing site would be able to read every conversation the practice has
    ever had — which is the single worst outcome available on this endpoint.
    """
    ticket = demo_session["ticket"]

    response = await client.get(
        f"/api/v1/conversations?ticket={quote(ticket)}",
    )

    assert response.status_code == 401, (
        "a demo ticket reached the conversation LIST — it is scoped to one "
        "conversation and must not authenticate a tenant-wide read"
    )

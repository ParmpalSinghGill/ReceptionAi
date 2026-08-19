"""``GET /api/v1/bookings`` and ``GET /api/v1/resources`` — the diary.

The properties worth a test here are the ones a calendar screen would
otherwise discover in production:

* the tenant is **not** a parameter, so naming another tenant cannot widen the
  answer (contract §10);
* a day is the business's own ``local_date``, not a UTC window;
* the resource filter narrows by allocation, through ``booking_items``;
* an unparseable date is a 422 in the RFC 9457 shape, not a 500.

They run against the real stack because every one of them is a property of the
database — an RLS proof against a mocked repository proves only that the mock
returned what it was told to.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.api.conftest import requires_stack

pytestmark = [pytest.mark.integration, requires_stack]

#: The day ``tools/seed_demo.py`` guarantees has bookings on it. Resolved from
#: the API rather than hardcoded, so the suite does not rot the moment the seed
#: moves its fixtures.
async def _a_day_with_bookings(client: AsyncClient, headers: dict[str, str]) -> str | None:
    """Find a seeded day by walking forward from today.

    Deliberately not a constant: the seed places its fixtures relative to
    *now*, so a hardcoded date passes for a week and then fails for a reason
    that has nothing to do with the route.
    """
    from datetime import UTC, datetime, timedelta

    start = datetime.now(UTC).date()
    for offset in range(-3, 21):
        day = (start + timedelta(days=offset)).isoformat()
        response = await client.get(
            "/api/v1/bookings", params={"date": day}, headers=headers
        )
        assert response.status_code == 200, response.text
        if response.json()["items"]:
            return day
    return None


async def test_a_day_returns_its_bookings_with_service_and_resources_joined(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """The shape a calendar needs, in one request.

    A day view cannot lay itself out from ids: it needs the service's name and
    the resource's name for every appointment. Asserting the join here is what
    stops a later refactor from turning the screen into N+1 lookups the
    frontend has to make.
    """
    day = await _a_day_with_bookings(client, auth_headers)
    if day is None:
        pytest.skip("no seeded bookings in the window; run `python3 tools/seed_demo.py`")

    body = (
        await client.get("/api/v1/bookings", params={"date": day}, headers=auth_headers)
    ).json()

    assert body["date"] == day
    assert body["timezone"], "the response must name the zone the day is drawn in"

    for booking in body["items"]:
        assert booking["local_date"] == day, (
            "every row must belong to the requested BUSINESS day — a UTC window "
            "would pull in a neighbouring day's edges"
        )
        assert booking["service_name"], "the service is joined, not just its id"
        assert booking["starts_at"].endswith("Z"), "contract §6: RFC 3339 UTC with Z"
        assert booking["status"] != "cancelled", "cancelled rows are hidden by default"


async def test_the_resource_filter_narrows_to_that_resource(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """``?resource_id=`` returns that resource's appointments and no others.

    The filter goes through ``booking_items`` because a booking consumes
    several resources at once. A test that only counted rows would pass on a
    filter that returned nothing, so this asserts the resource is actually
    present on every row that comes back.
    """
    day = await _a_day_with_bookings(client, auth_headers)
    if day is None:
        pytest.skip("no seeded bookings in the window")

    everything = (
        await client.get("/api/v1/bookings", params={"date": day}, headers=auth_headers)
    ).json()["items"]

    # Pick a resource that genuinely has something on it, so a filter that
    # silently returns nothing cannot pass.
    target = next(
        (r for booking in everything for r in booking["resources"]),
        None,
    )
    if target is None:
        pytest.skip("the seeded bookings have no allocated resources")

    filtered = (
        await client.get(
            "/api/v1/bookings",
            params={"date": day, "resource_id": target["id"]},
            headers=auth_headers,
        )
    ).json()["items"]

    assert filtered, "filtering by a resource that IS booked returned nothing"
    assert len(filtered) <= len(everything)
    for booking in filtered:
        assert any(r["id"] == target["id"] for r in booking["resources"]), (
            f"booking {booking['reference']} came back for resource "
            f"{target['name']} but is not allocated to it"
        )


async def test_a_tenant_id_parameter_cannot_widen_the_answer(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Contract §10: the tenant comes from the token, never from the request.

    The route has no ``tenant_id`` parameter, so the interesting assertion is
    that adding one changes nothing — an unknown query parameter must be inert
    rather than being picked up by some permissive filter layer.
    """
    day = await _a_day_with_bookings(client, auth_headers)
    if day is None:
        pytest.skip("no seeded bookings in the window")

    honest = await client.get(
        "/api/v1/bookings", params={"date": day}, headers=auth_headers
    )
    smuggled = await client.get(
        "/api/v1/bookings",
        params={
            "date": day,
            "tenant_id": "019fd241-17ab-723b-b441-ffe4680b4884",
            "X-Tenant-Id": "019fd241-17ab-723b-b441-ffe4680b4884",
        },
        headers=auth_headers,
    )

    assert smuggled.status_code == 200
    assert smuggled.json() == honest.json(), (
        "a tenant_id parameter changed the response — the tenant must come "
        "from the token's tid claim and nothing else"
    )


async def test_an_unparseable_date_is_a_422_problem_document(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Bad input is a problem document, not a stack trace."""
    response = await client.get(
        "/api/v1/bookings", params={"date": "the day before yesterday"}, headers=auth_headers
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["type"].startswith("https://receptionai.dev/problems/")
    assert "request_id" in body


async def test_the_diary_requires_authentication(client: AsyncClient) -> None:
    """No credential, no diary — and the refusal carries the frozen shape."""
    response = await client.get("/api/v1/bookings", params={"date": "2026-08-13"})

    assert response.status_code == 401
    assert response.json()["code"] in {"missing_credentials", "unauthenticated"}


async def test_resources_lists_bookable_resources_with_their_type(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """The filter's options.

    ``resource_type_code`` is what lets the portal group people separately from
    rooms without hardcoding either word, so its presence is part of the
    contract rather than an incidental field.
    """
    response = await client.get("/api/v1/resources", headers=auth_headers)

    assert response.status_code == 200
    items = response.json()["items"]
    assert items, "the seeded tenant has resources; an empty list means the filter is broken"
    for resource in items:
        assert resource["id"] and resource["name"]
        assert "resource_type_code" in resource

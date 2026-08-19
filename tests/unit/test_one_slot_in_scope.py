"""The model holds exactly one slot handle, so it cannot book the wrong one.

**The bug.** ``check_availability`` returned three slots, each with a handle, in
three places at once — the prose, ``collected_data`` and ``structured``. The
prompt asked the model to "book the SAME one you offered". About one time in
three it did not:

    run 1  offered 3:45  ->  booked 3:45   ok
    run 2  offered 2:45  ->  booked 2:45   ok
    run 3  offered 4:15  ->  booked 4:30   WRONG

A caller told 4:15 who is booked at 4:30 arrives to a slot that is not theirs,
and nothing anywhere signals a problem. Same harm as the original timezone bug,
same cause: an instruction standing where a mechanism belongs.

**The fix is subtractive.** One slot is offered, one handle exists in the model's
context, and the alternatives live server-side behind an explicit
``next_alternative`` call. There is no wrong handle to pick because there is no
other handle.

These assertions are against the **tool record**, not a transcript. The failure
was invisible in two runs out of three, which is exactly why it survived.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from packages.ai.tools import ToolExecutionContext, ToolExecutor, default_registry
from packages.ai.tools.builtin import booking as booking_tools
from packages.core.ids import uuid7
from packages.core.ports.llm import ToolCall

#: Any slot handle, wherever it appears.
HANDLE = re.compile(r"\bslot_[0-9a-f]{4,32}\b")


def handles_in(result: Any) -> set[str]:
    """Every handle reachable from a tool result — prose, state and payload.

    All three, because the model reads all three. Counting only the prose is how
    "one handle" became "one handle plus two more in `collected_data`".
    """
    import json

    blob = " ".join(
        [
            result.content or "",
            json.dumps(result.collected_data, default=str),
            json.dumps(result.structured, default=str),
        ]
    )
    return set(HANDLE.findall(blob))


class _StubService:
    """Enough of ``BookingService`` to answer one availability query."""

    def __init__(self, slots: list[Any], service: Any, resources: dict[Any, Any]):
        self._slots = slots
        self._service = service
        self._resources = resources
        self.repository = _StubRepo()

    async def find_slots(self, **kwargs: Any):
        limit = int(kwargs.get("limit", 3))
        return (
            self._slots[:limit],
            self._service,
            "loc",
            "Europe/London",
            self._resources,
        )


class _StubRepo:
    async def default_location_id(self) -> str:
        return "loc"

    async def location_timezone(self, _location: str) -> str:
        return "Europe/London"


@pytest.fixture
def offering(monkeypatch: pytest.MonkeyPatch):
    """A practice with several free slots, wired into the booking tools."""
    from contextlib import asynccontextmanager
    from dataclasses import dataclass
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from packages.booking.domain import AllocationCandidate, ResourceSpec, Slot

    @dataclass(frozen=True)
    class _Service:
        id: Any = "svc"
        code: str = "emergency"
        name: str = "Emergency Appointment"
        price: Decimal | None = Decimal("75.0000")
        currency: str | None = "GBP"

    base = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)
    people = [
        ResourceSpec(
            id=uuid7(),
            tenant_id=uuid7(),
            location_id=uuid7(),
            resource_type_id=uuid7(),
            code=f"clin-{n}",
            name=f"Clinician {n}",
            attributes={"scope": "Same-day emergencies"},
        )
        for n in range(4)
    ]
    resources = {p.id: p for p in people}

    slots = [
        Slot(
            start_at=base + timedelta(minutes=15 * n),
            end_at=base + timedelta(minutes=15 * n + 20),
            allocations=(
                AllocationCandidate(
                    role="primary",
                    requirement_id=uuid7(),
                    resource_ids=(people[n % len(people)].id,),
                    quantity=1,
                    respects_buffers=False,
                ),
            ),
        )
        for n in range(6)
    ]

    service = _StubService(slots, _Service(), resources)

    @asynccontextmanager
    async def unit_of_work(_tenant_id: Any):
        yield (service, service.repository)

    booking_tools.set_booking_dependencies(
        booking_tools.BookingToolDeps(unit_of_work)
    )
    booking_tools._ALTERNATIVE_QUEUES.clear()
    yield slots
    booking_tools.set_booking_dependencies(None)


def _context(conversation_id: Any) -> ToolExecutionContext:
    import time as _time

    return ToolExecutionContext(
        tenant_id=uuid7(),
        conversation_id=conversation_id,
        agent_key="scheduler",
        correlation_id="test",
        deadline_ms=int(_time.monotonic() * 1000) + 60_000,
        channel="web_chat",
    )


async def test_exactly_one_handle_is_ever_in_scope(offering) -> None:
    """**The regression test.** One handle means no wrong handle.

    Checked across prose, ``collected_data`` and ``structured`` together,
    because the model reads all three and the old version put three handles in
    each.
    """
    ctx = _context(uuid7())
    result = await booking_tools._check_availability(
        ctx, {"service_code": "emergency"}
    )

    found = handles_in(result)
    assert len(found) == 1, (
        f"{len(found)} handles reachable from one availability result: {found}. "
        f"Every extra handle is a way for the model to book a slot the customer "
        f"was never offered."
    )


async def test_the_offered_handle_is_the_earliest_slot(offering) -> None:
    """The recommendation is the soonest — right for an emergency, and for a
    routine booking the solver has already applied the tenant's ranking."""
    ctx = _context(uuid7())
    result = await booking_tools._check_availability(
        ctx, {"service_code": "emergency"}
    )

    handle = result.collected_data["offered_slot_ref"]
    resolved = booking_tools._decode_slot_ref(handle)
    assert resolved is not None
    assert resolved.start_at == offering[0].start_at


async def test_declining_offers_a_different_time(offering) -> None:
    """A caller must still be able to say "anything else?".

    A fix that made booking correct but rejection impossible would not be a fix.
    """
    conversation = uuid7()
    ctx = _context(conversation)

    first = await booking_tools._check_availability(
        ctx, {"service_code": "emergency"}
    )
    second = await booking_tools._next_alternative(ctx, {"reason": "too early"})

    first_handle = first.collected_data["offered_slot_ref"]
    second_handle = second.collected_data["offered_slot_ref"]

    assert second_handle != first_handle
    assert booking_tools._decode_slot_ref(
        second_handle
    ).start_at > booking_tools._decode_slot_ref(first_handle).start_at, (
        "Declining must move forward through the queue, not re-offer the same "
        "slot or jump backwards."
    )
    # And still exactly one handle in scope after declining.
    assert len(handles_in(second)) == 1


async def test_declining_repeatedly_never_widens_the_scope(offering) -> None:
    """Ten "not that one"s must not leave ten handles in context."""
    conversation = uuid7()
    ctx = _context(conversation)

    result = await booking_tools._check_availability(
        ctx, {"service_code": "emergency"}
    )
    seen = [result.collected_data["offered_slot_ref"]]
    for _ in range(4):
        result = await booking_tools._next_alternative(ctx, {})
        if "offered_slot_ref" not in result.collected_data:
            break
        seen.append(result.collected_data["offered_slot_ref"])
        assert len(handles_in(result)) == 1

    assert len(set(seen)) == len(seen), "the same slot was offered twice"


async def test_running_out_of_alternatives_is_reported_honestly(offering) -> None:
    """"That is everything free" beats silently re-offering the last slot."""
    conversation = uuid7()
    ctx = _context(conversation)
    await booking_tools._check_availability(ctx, {"service_code": "emergency"})

    last = None
    for _ in range(12):
        last = await booking_tools._next_alternative(ctx, {})
        if last.structured.get("exhausted"):
            break

    assert last is not None
    assert last.structured.get("exhausted") is True
    assert handles_in(last) == set(), (
        "An exhausted search must not leave a handle in scope."
    )


async def test_an_alternative_without_a_search_asks_for_one(offering) -> None:
    """A follow-up with no active search recovers rather than failing."""
    result = await booking_tools._next_alternative(_context(uuid7()), {})
    assert result.structured.get("error") == "no_active_search"
    assert "check_availability" in result.content


async def test_the_booked_handle_is_the_offered_handle(offering) -> None:
    """**The property, end to end through the executor.**

    Drive availability, take the ONE handle it returned, and assert that is the
    handle `book_appointment` receives — with no other handle having been in
    scope to confuse it.
    """
    from packages.verticals import load_manifest_file
    from packages.verticals.manifests import manifest_path

    manifest = load_manifest_file(manifest_path("medical"))
    executor = ToolExecutor(default_registry())
    agent = manifest.graph.agent("scheduler")
    conversation = uuid7()
    ctx = _context(conversation)

    availability = await executor.execute_many(
        calls=[
            ToolCall(
                id="c1",
                name="booking.check_availability",
                arguments={"service_code": "emergency"},
            )
        ],
        agent=agent,
        context=ctx,
    )
    offered = availability[0].collected_data["offered_slot_ref"]
    assert len(HANDLE.findall(availability[0].message.content or "")) >= 1

    # Everything the model could possibly pass, from everything it was given.
    reachable = handles_in(
        booking_tools.ToolResult(
            content=availability[0].message.content or "",
            collected_data=availability[0].collected_data,
            structured=availability[0].structured,
        )
    )
    assert reachable == {offered}, (
        f"The model had {reachable} to choose from; only {offered!r} was "
        f"offered. Any other handle is a slot the customer was never told about."
    )


# ── Accepting must be easier than declining ──────────────────────────────────
#
# The one-slot mechanism fixed the drift but made `next_alternative` the
# cheapest call in the toolbox: it had NO required arguments while
# `book_appointment` required a name. An ambiguous reply therefore walked the
# queue — `next_alternative` x5 and `book_appointment` x0 in one routine
# booking, with the caller's "8:15 suits me, I'm Verify Harness" answered by an
# offer of 8:30.


def test_declining_requires_stating_what_was_declined() -> None:
    """A tool with no required argument is the path of least resistance.

    Requiring the rejection to be quoted forces the question "was anything
    actually rejected?" — which is exactly the check that was missing when an
    acceptance was answered with the next slot.
    """
    registry = default_registry()
    spec = registry.get("booking.next_alternative")
    assert spec is not None
    assert spec.parameters["required"] == ["declined"], (
        "next_alternative must require the caller's rejection. With no required "
        "argument it is cheaper to call than book_appointment, and the model "
        "reaches for it by default."
    )


def test_the_decline_tool_warns_against_using_it_for_a_new_day() -> None:
    """A change of intent is a fresh search, not a step through a stale queue.

    The caller said "Thursday instead" and was walked through Tuesday's
    remaining slots, because the description invited exactly that
    ("do you have anything Thursday instead?").
    """
    spec = default_registry().get("booking.next_alternative")
    assert spec is not None
    description = spec.description.lower()
    assert "check_availability" in description
    assert "different day" in description
    assert "accepted" in description


async def test_a_fresh_search_replaces_the_queue(offering) -> None:
    """A change of intent discards the old search.

    Otherwise `next_alternative` after "Thursday instead" keeps walking
    Tuesday — which is what the caller saw: 8:15, then 8:30, then 8:45, all on
    the day they had just moved away from.
    """
    conversation = uuid7()
    ctx = _context(conversation)

    await booking_tools._check_availability(ctx, {"service_code": "emergency"})
    await booking_tools._next_alternative(ctx, {"declined": "too early"})
    walked = booking_tools._ALTERNATIVE_QUEUES[str(conversation)].index
    assert walked == 1, "the queue should have advanced once"

    # A new search for a different day.
    await booking_tools._check_availability(ctx, {"service_code": "emergency"})
    reset = booking_tools._ALTERNATIVE_QUEUES[str(conversation)].index
    assert reset == 0, (
        "A fresh check_availability must replace the queue. Leaving the old "
        "index in place walks the customer through the day they just rejected."
    )

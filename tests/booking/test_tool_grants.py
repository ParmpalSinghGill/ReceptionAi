"""Grant enforcement — contract §8, 03 §6.2, 02 §6.5.

*"Tool invocation requires an explicit grant in the vertical manifest; no tool is
reachable purely because a model asked for it."*

The model naming a tool is a **request**, never an authorization. These tests
attack that from both directions:

* an agent without the grant cannot call the tool, even though the tool exists
  and its implementation is registered;
* an agent without the grant is never even **told** the tool exists, because a
  model cannot be talked into requesting a capability it has never heard of.

And the specific bug 02 §6.5 records from review: a QA agent holding a write
tool. QA agents consume untrusted retrieved content, which makes them the
prompt-injection escalation path — so they get read tools and the escalation
allow-list, and nothing else.
"""

from __future__ import annotations

import pytest
from packages.ai.tools import ToolExecutionContext, ToolExecutor, default_registry
from packages.core.ids import uuid7
from packages.core.ports.llm import ToolCall
from packages.verticals import load_manifest_file
from packages.verticals.manifests import manifest_path

#: Pure grant logic — no database, no network, no stack. These run everywhere.
pytestmark = []


@pytest.fixture(scope="module")
def manifest():
    return load_manifest_file(manifest_path("medical"))


@pytest.fixture(scope="module")
def executor() -> ToolExecutor:
    return ToolExecutor(default_registry())


def _context(agent_key: str) -> ToolExecutionContext:
    import time as _time

    return ToolExecutionContext(
        tenant_id=uuid7(),
        conversation_id=uuid7(),
        agent_key=agent_key,
        correlation_id="test",
        # Well into the future, so a timeout never masks a grant result.
        deadline_ms=int(_time.monotonic() * 1000) + 60_000,
    )


async def test_an_agent_without_the_grant_cannot_call_a_write_tool(
    manifest, executor: ToolExecutor
) -> None:
    """**The core assertion.**

    ``practice_faq`` is the information agent. ``booking.cancel_booking`` exists,
    is registered, and has a working implementation — and the call still must not
    execute, because *this agent* was not granted it.

    The denial comes back as a tool message rather than an exception, because the
    provider protocols require every ``tool_call`` to be answered by a
    ``tool_result`` with a matching id; raising would leave an unanswered call in
    the message list and the next request would 400.
    """
    agent = manifest.graph.agent("practice_faq")
    assert not agent.tools, "the fixture agent must genuinely hold no grants"

    outcomes = await executor.execute_many(
        calls=[
            ToolCall(
                id="call_1",
                name="booking.cancel_booking",
                arguments={"reference": "BK-ABC123"},
            )
        ],
        agent=agent,
        context=_context("practice_faq"),
    )

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.record["status"] == "denied", (
        "An ungranted tool must be denied. If this executes, the manifest grant "
        "is decorative and any injected instruction can reach any tool."
    )
    assert outcome.message.role == "tool"
    assert outcome.message.tool_call_id == "call_1"
    # Bland text, deliberately. A grant denial is adversarial-adjacent: if
    # injected content is probing for a tool, detailed feedback is an oracle.
    assert "not available" in (outcome.message.content or "")
    assert "cancel" not in (outcome.message.content or "").lower()


async def test_an_ungranted_tool_is_not_even_described_to_the_model(
    manifest, executor: ToolExecutor
) -> None:
    """02 §6.4: invisible beats refused.

    Not describing a tool is strictly better than refusing it — the model cannot
    be talked into requesting something it has never heard of, and every refusal
    costs a turn the caller is waiting through.
    """
    faq_schemas = executor.schemas_for(manifest.graph.agent("practice_faq"))
    assert faq_schemas == [], (
        "The information agent must be shown no tools at all."
    )

    scheduler_schemas = {
        s.name for s in executor.schemas_for(manifest.graph.agent("scheduler"))
    }
    assert "booking.book_appointment" in scheduler_schemas
    assert "comms.transfer_to_human" not in scheduler_schemas, (
        "The scheduler was not granted the transfer tool, so it must not see it."
    )


def test_no_information_agent_holds_a_state_changing_tool(manifest) -> None:
    """02 §6.5's regression test — a real bug caught in review.

    A QA/FAQ agent reads untrusted retrieved content, so it is the
    prompt-injection escalation path. It may hold read tools and the escalation
    allow-list (take a message, transfer to a human) and nothing else. An agent
    that genuinely needs to book should be a *booking* agent.

    The information agent here is identified by holding knowledge collections and
    no handoff-only role — matching on a `role` field would be better, but the
    Phase 1 manifest schema has `kind` rather than `role`, so the check is
    written against what exists rather than against what a later schema may add.
    """
    registry = default_registry()
    escalation_allow_list = {"comms.transfer_to_human"}

    information_agents = [
        agent
        for agent in manifest.graph.agents
        if agent.knowledge_collections and agent.key != "scheduler"
    ]
    assert information_agents, "the fixture must contain an information agent"

    for agent in information_agents:
        for grant in agent.tools:
            spec = registry.get(grant.tool)
            assert spec is not None
            if not spec.side_effecting:
                continue
            assert grant.tool in escalation_allow_list, (
                f"Information agent {agent.key!r} holds state-changing tool "
                f"{grant.tool!r}. QA agents consume untrusted content and are "
                f"the injection escalation path; they get read tools plus the "
                f"escalation allow-list and nothing else (02 §6.5)."
            )


async def test_arguments_are_validated_before_execution(
    manifest, executor: ToolExecutor
) -> None:
    """A granted tool called with bad arguments fails validation, not the impl.

    And the message is **specific**, unlike a denial: a validation error is a
    cooperative correction, and telling the model which field is wrong is how it
    fixes the call rather than giving up.
    """
    agent = manifest.graph.agent("scheduler")
    outcomes = await executor.execute_many(
        calls=[
            ToolCall(
                id="call_2",
                name="booking.book_appointment",
                # Missing `start_time` and `customer_name`, both required.
                arguments={"service_code": "checkup"},
            )
        ],
        agent=agent,
        context=_context("scheduler"),
    )

    outcome = outcomes[0]
    assert outcome.record["status"] == "invalid_args"
    content = outcome.message.content or ""
    assert "Invalid arguments" in content
    assert "call the tool again" in content, (
        "A validation error must tell the model to retry, or it treats a "
        "fixable mistake as a dead end."
    )


async def test_an_unregistered_tool_is_refused_even_if_somehow_granted(
    manifest, executor: ToolExecutor
) -> None:
    """Defence in depth for a registry that changed under a cached graph.

    Distinct text from a grant denial — "temporarily unavailable" rather than
    "not available in this conversation" — because this one is *our* fault and
    the right client behaviour is to retry, not to give up.
    """
    agent = manifest.graph.agent("scheduler")
    outcomes = await executor.execute_many(
        calls=[ToolCall(id="call_3", name="booking.nonexistent", arguments={})],
        agent=agent,
        context=_context("scheduler"),
    )
    assert outcomes[0].record["status"] == "denied"


def test_every_registered_tool_has_a_strict_schema() -> None:
    """``additionalProperties: false``, enforced at registration.

    A permissive schema lets a model attach a field the schema never described,
    and it arrives in the implementation's ``arguments`` dict unvalidated — the
    same class of hole as an unparameterised query. The registry refuses one, so
    this asserts the catalogue actually went through it.
    """
    registry = default_registry()
    for name in sorted(registry.names()):
        spec = registry.get(name)
        assert spec is not None
        assert spec.parameters.get("additionalProperties") is False, (
            f"{name} has a permissive argument schema."
        )
        assert spec.description, f"{name} has no description for the model"


def test_pii_arguments_are_redacted_in_the_audit_record() -> None:
    """Contract §8: PII masked in logs and traces.

    Partial rather than total masking, so an engineer can still correlate two
    records as the same person while the log carries almost no identity — "we
    mask everything" becomes "we log nothing" becomes "we log raw".
    """
    from packages.ai.tools.executor import _redact

    masked = _redact(
        {"customer_name": "Robert Kim", "service_code": "checkup"},
        ("customer_name",),
    )
    assert masked["service_code"] == "checkup", "non-PII must pass through"
    assert masked["customer_name"] != "Robert Kim"
    assert "Robert" not in masked["customer_name"]


def test_the_manifest_grants_write_tools_only_to_the_booking_agent(
    manifest,
) -> None:
    """The shape of the medical manifest, asserted rather than assumed.

    A regression guard on the *manifest* rather than on the enforcer: the
    enforcement code can be perfect and the manifest can still hand a booking
    tool to the wrong agent, which is precisely the bug 02 §6.5 records.
    """
    registry = default_registry()
    writers = {
        agent.key
        for agent in manifest.graph.agents
        for grant in agent.tools
        if (spec := registry.get(grant.tool)) is not None and spec.side_effecting
    }
    assert writers == {"scheduler", "human_transfer"}, (
        f"Only the scheduling and escalation agents may hold write tools; "
        f"found {sorted(writers)}."
    )

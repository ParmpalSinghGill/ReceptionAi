"""The supervisor routes on the conversation, not on the last utterance.

**The bug this file exists to prevent.** Routing originally passed only the most
recent user message. That works perfectly on turn one and degrades to nonsense by
turn three, because the answers people give to follow-up questions carry no topic
on their own:

    Caller: chipped tooth              -> triage   (right)
    Desk:   how long ago, and is it getting worse?
    Caller: from yesterday night       -> triage   (right, by luck)
    Desk:   is it getting any worse?
    Caller: yes                        -> wrap_up  (WRONG)

The supervisor saw the single word "yes" and reasonably concluded the caller was
finished. The triage agent, mid-assessment, never ran again, and a caller
describing a worsening symptom was dropped into a closing message.

The signature is what makes it dangerous: it is invisible on the first turn, so
every quick manual check passes. These tests drive the multi-turn case directly.
"""

from __future__ import annotations

from typing import Any

import pytest
from packages.ai.nodes import NodeFactory, _recent_exchange
from packages.core.ids import uuid7
from packages.core.ports.llm import (
    ChatResult,
    Message,
    ProviderCapabilities,
    StreamDelta,
    Usage,
)
from packages.verticals import load_manifest_file
from packages.verticals.manifests import manifest_path


class RecordingLLM:
    """Captures the routing prompt and answers with a scripted key."""

    provider = "recording"
    model = "recording-1"

    def __init__(self, answer: str = "triage") -> None:
        self.answer = answer
        self.routing_prompts: list[str] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_tools=True,
            supports_parallel_tools=True,
            supports_streaming_tools=True,
            supports_json_schema=True,
            supports_prompt_caching=False,
            context_window=32_768,
            max_output_tokens=4_096,
        )

    def count_tokens(self, messages: Any) -> int:
        return 1

    async def chat(self, *, messages: Any, tools: Any = None, **kwargs: Any):
        text = messages[-1].content or ""
        if "Specialists you may route to:" in text:
            self.routing_prompts.append(text)
        return ChatResult(
            message=Message(role="assistant", content=self.answer),
            tool_calls=[],
            usage=Usage(prompt_tokens=1, completion_tokens=1),
            model=self.model,
            provider=self.provider,
            finish_reason="stop",
        )

    async def stream(self, *, messages: Any, tools: Any = None, **kwargs: Any):
        yield StreamDelta(kind="text", text="ok")
        yield StreamDelta(kind="done")

    async def aclose(self) -> None:
        return None


@pytest.fixture(scope="module")
def manifest():
    return load_manifest_file(manifest_path("medical"))


def _state(messages: list[Message], current: str | None = None) -> dict[str, Any]:
    """Graph state as the supervisor really sees it.

    `agent_history` carries who spoke, and `current_agent` is the SUPERVISOR —
    it sets that field to itself on every hop. An earlier version of these
    fixtures set `current_agent` to the specialist, which is a state the graph
    never produces; the tests passed while the override they were guarding
    silently never fired in production.
    """
    return {
        "messages": messages,
        # A NEW turn: `agent_history` is empty (new_state resets it) and
        # `current_agent` is unset. Who spoke last turn survives only in
        # `collected_data`, which the conversation service persists — this is
        # the state the supervisor genuinely sees on the first hop of a turn,
        # and getting it wrong is why the override silently never fired.
        "agent_history": [],
        "current_agent": None,
        "collected_data": {"_last_speaker": current} if current else {},
        "tenant_id": uuid7(),
        "conversation_id": uuid7(),
        "timezone": "Europe/London",
        "channel": "web_chat",
        "locale": "en-GB",
    }


#: The exact exchange that exposed the bug.
TRIAGE_EXCHANGE = [
    Message(role="user", content="chipped tooth."),
    Message(
        role="assistant",
        content="Could you tell me how long ago it happened, and whether it's "
        "getting worse?",
    ),
    Message(role="user", content="from yesterday night"),
    Message(role="assistant", content="And is it getting any worse since then?"),
    Message(role="user", content="yes"),
]


async def test_the_routing_prompt_carries_the_conversation_not_one_line(
    manifest,
) -> None:
    """**The regression guard.**

    The word "yes" is meaningless alone. If the earlier turns are missing from
    the routing prompt, the supervisor cannot know this is a clinical assessment
    in progress — and it will route a worsening symptom to the closing agent.
    """
    llm = RecordingLLM(answer="triage")
    factory = NodeFactory(llm=llm, manifest=manifest)
    node = factory.supervisor(manifest.graph.agent("receptionist"), manifest.graph)

    await node(_state(TRIAGE_EXCHANGE, current="triage"))  # type: ignore[arg-type]

    assert llm.routing_prompts, "the supervisor did not consult the model"
    prompt = llm.routing_prompts[0]

    assert "chipped tooth" in prompt, (
        "The routing prompt lost the topic. Routing on the last utterance alone "
        "means a follow-up answer like 'yes' carries no context, and the "
        "supervisor re-decides from nothing every turn."
    )
    assert "getting worse" in prompt
    assert "yes" in prompt


async def test_the_routing_prompt_names_the_current_specialist(manifest) -> None:
    """Continuity: an unfinished assessment keeps its specialist.

    Without this the supervisor re-decides from scratch on every turn, so a
    specialist can never ask a second question — the moment the caller answers,
    routing moves on.
    """
    llm = RecordingLLM()
    factory = NodeFactory(llm=llm, manifest=manifest)
    node = factory.supervisor(manifest.graph.agent("receptionist"), manifest.graph)

    await node(_state(TRIAGE_EXCHANGE, current="triage"))  # type: ignore[arg-type]

    prompt = llm.routing_prompts[0]
    assert "triage" in prompt
    # The property, not the wording: the supervisor is told who is handling it
    # and that a mid-task specialist keeps the turn.
    assert "Keep routing to them while they are mid-task" in prompt


async def test_a_first_turn_still_routes_without_history(manifest) -> None:
    """The empty-history case must not break.

    This is the case that always worked, and it has to keep working — the fix
    adds context, it does not require it.
    """
    llm = RecordingLLM(answer="triage")
    factory = NodeFactory(llm=llm, manifest=manifest)
    node = factory.supervisor(manifest.graph.agent("receptionist"), manifest.graph)

    result = await node(  # type: ignore[arg-type]
        _state([Message(role="user", content="chipped tooth.")])
    )
    assert result["next_agent"] == "triage"


def test_the_exchange_renderer_labels_speakers() -> None:
    """"yes" is only meaningful next to the question it answers."""
    rendered = _recent_exchange(TRIAGE_EXCHANGE)
    assert "Caller: chipped tooth." in rendered
    assert "Desk: Could you tell me" in rendered
    assert rendered.strip().endswith("Caller: yes")


def test_the_exchange_renderer_excludes_tool_messages() -> None:
    """Tool traffic is internal plumbing, not the caller's problem.

    Including it would teach the router about the platform rather than about
    what the caller wants, and it is exactly the sort of content the platform
    frame forbids surfacing.
    """
    rendered = _recent_exchange(
        [
            Message(role="user", content="chipped tooth"),
            Message(role="tool", tool_call_id="c1", content="{'slots': [...]}"),
            Message(role="assistant", content="I have 3:40 today."),
        ]
    )
    assert "slots" not in rendered
    assert "chipped tooth" in rendered
    assert "3:40" in rendered


def test_the_exchange_renderer_is_bounded() -> None:
    """A long conversation must not produce an unbounded routing prompt.

    The router is deliberately a cheap, fast model; feeding it a full transcript
    every turn defeats the reason it exists.
    """
    long_history = [
        Message(role="user", content="x" * 5_000) for _ in range(50)
    ]
    rendered = _recent_exchange(long_history)
    assert len(rendered) < 2_000


async def test_an_announced_handoff_releases_the_current_specialist(
    manifest,
) -> None:
    """Continuity must have a release condition, or a specialist gets stuck.

    "Keep routing to them" alone is its own bug: triage assessed the symptom,
    said "I'll pass you over to the scheduler", and the supervisor sent the next
    four turns straight back to triage — which repeated the same handoff
    sentence while the scheduler's booking tools stayed unreachable. The DB
    showed no booking through the whole conversation.

    The specialist's own words are the release signal, which is what lets this
    work for any manifest without naming an agent in the engine.
    """
    llm = RecordingLLM(answer="scheduler")
    factory = NodeFactory(llm=llm, manifest=manifest)
    node = factory.supervisor(manifest.graph.agent("receptionist"), manifest.graph)

    handed_over = [
        Message(role="user", content="chipped tooth"),
        Message(role="assistant", content="How long ago, and is it worsening?"),
        Message(role="user", content="yes, worse since last night"),
        Message(
            role="assistant",
            content="That needs one of today's emergency slots. I'll pass you "
            "over to the scheduler now.",
        ),
        Message(role="user", content="yes please"),
    ]
    await node(_state(handed_over, current="triage"))  # type: ignore[arg-type]

    prompt = llm.routing_prompts[0]
    assert "handing over" in prompt or "handing" in prompt, (
        "The routing prompt must describe the release condition, or a "
        "specialist that has announced a handoff keeps receiving turns."
    )
    assert "route to THAT specialist now" in prompt


HANDED_OVER = [
    Message(role="user", content="chipped tooth"),
    Message(role="assistant", content="How long ago, and is it worsening?"),
    Message(role="user", content="yes, worse since last night"),
    Message(
        role="assistant",
        content="That needs one of today's emergency slots. I'll pass you over "
        "to my colleague now to get that booked.",
    ),
    Message(role="user", content="yes please"),
]


async def test_a_specialist_that_announced_a_handoff_is_not_re_selected(
    manifest,
) -> None:
    """**Structural**, not prompted — because prompted was unreliable.

    The routing instruction alone held on some runs and not others. When it
    failed, triage repeated "I'll pass you to my colleague" for four consecutive
    turns: the scheduler's booking tools were never reachable, no row was ever
    written, and a caller in pain got nothing. "Unreliable in prose" is the
    signal to move a rule into structure.

    Here the router is *told* to pick triage again and must not be obeyed. The
    override target comes from the manifest's own `handoffs`, so nothing in the
    engine knows what triage or a scheduler is.
    """
    llm = RecordingLLM(answer="triage")  # the router insists on the stuck agent
    factory = NodeFactory(llm=llm, manifest=manifest)
    node = factory.supervisor(manifest.graph.agent("receptionist"), manifest.graph)

    result = await node(_state(HANDED_OVER, current="triage"))  # type: ignore[arg-type]

    assert result["next_agent"] != "triage", (
        "The supervisor re-selected a specialist that had just announced a "
        "handoff. That is the loop that made booking unreachable."
    )
    assert result["next_agent"] in manifest.graph.agent("triage").handoffs, (
        "The override target must come from the manifest's declared handoffs, "
        "not from anything the engine knows about this vertical."
    )


async def test_a_specialist_mid_task_is_left_alone(manifest) -> None:
    """The override must fire ONLY on an announced handoff.

    A specialist asking its second question has not finished, and forcing it
    onward mid-assessment would be the original bug in a new costume.
    """
    llm = RecordingLLM(answer="triage")
    factory = NodeFactory(llm=llm, manifest=manifest)
    node = factory.supervisor(manifest.graph.agent("receptionist"), manifest.graph)

    result = await node(_state(TRIAGE_EXCHANGE, current="triage"))  # type: ignore[arg-type]
    assert result["next_agent"] == "triage"


@pytest.mark.parametrize(
    ("reply", "announced"),
    [
        ("I'll pass you over to my colleague now.", True),
        ("I've passed your details over to my colleague.", True),
        ("My colleague will confirm the time with you.", True),
        ("How long ago did it happen?", False),
        ("I have Tuesday at 9:00 or 9:15. Which suits?", False),
        ("You're booked for Tuesday at 9:00.", False),
    ],
)
def test_handoff_announcement_detection(reply: str, announced: bool) -> None:
    """Only a genuine "someone else takes it from here" releases the agent."""
    from packages.ai.nodes import _announced_handoff

    state = {"messages": [Message(role="assistant", content=reply)]}
    assert _announced_handoff(state) is announced  # type: ignore[arg-type]


async def test_the_speaking_agent_survives_into_the_next_turn(manifest) -> None:
    """The cross-turn mechanism the override depends on.

    Neither `current_agent` nor `agent_history` can answer "who was handling
    this when the caller last replied": the first is the supervisor by then, the
    second is reset by `new_state` at the top of every turn. Both look plausible,
    and both made the override a no-op in production while the unit tests passed
    against hand-built state that the graph never produces.
    """
    from packages.ai.tools import ToolExecutor, default_registry

    llm = RecordingLLM(answer="triage")
    registry = default_registry()
    factory = NodeFactory(
        llm=llm,
        manifest=manifest,
        executor=ToolExecutor(registry),
        tool_schemas=registry.schemas(),
    )
    node = factory.llm_agent(manifest.graph.agent("triage"), manifest.graph)

    import time

    result = await node(  # type: ignore[arg-type]
        {
            "messages": [Message(role="user", content="chipped tooth")],
            "collected_data": {},
            "tenant_id": uuid7(),
            "conversation_id": uuid7(),
            "timezone": "Europe/London",
            "max_tool_calls": 16,
            "tool_call_count": 0,
            "deadline_ms": int(time.monotonic() * 1000) + 60_000,
        }
    )

    assert result["collected_data"]["_last_speaker"] == "triage", (
        "The speaking agent must be recorded somewhere that survives the turn "
        "boundary, or the supervisor cannot tell who was handling the "
        "conversation when the caller replies."
    )


# ── A named destination is authoritative ─────────────────────────────────────
#
# The emergency path failed roughly one time in four: triage said "I'll pass you
# to the scheduler now to get that booked", the router chose `human_transfer`
# instead, and the caller was told "I've passed your details to the team, they'll
# be in touch" — with no booking, and nobody actually told.
#
# It is not reproducible by argument. A probe of the exact failing transcript
# picked `scheduler` six times out of six in isolation; the failure only appears
# over volume. That is precisely when a rule belongs in structure rather than in
# a prompt.


NAMED_DESTINATION = [
    Message(role="user", content="chipped tooth."),
    Message(role="assistant", content="How long ago, and is it getting worse?"),
    Message(role="user", content="yes, worse since last night"),
    Message(
        role="assistant",
        content="Thanks — that sounds like it needs one of today's emergency "
        "slots. I'll pass you to the scheduler now to get that booked.",
    ),
    Message(role="user", content="yes please. Case India, 07700900321"),
]


async def test_a_named_destination_overrides_the_router(manifest) -> None:
    """**The regression test.** The router is told the wrong answer and ignored.

    The specialist named where it was sending the caller, and it took that
    decision holding the whole conversation. Re-deciding it from a one-line
    catalogue is how an emergency became an escalation.
    """
    llm = RecordingLLM(answer="human_transfer")  # the router picks wrongly
    factory = NodeFactory(llm=llm, manifest=manifest)
    node = factory.supervisor(manifest.graph.agent("receptionist"), manifest.graph)

    result = await node(_state(NAMED_DESTINATION, current="triage"))  # type: ignore[arg-type]

    assert result["next_agent"] == "scheduler", (
        "The router sent the caller to the escalation agent after triage had "
        "said it was passing them to the scheduler. That caller is told "
        "'someone will be in touch' and nobody is."
    )


async def test_an_unnamed_handoff_still_uses_the_router(manifest) -> None:
    """Only an explicit destination overrides. "Let me hand you over" does not.

    Without this the override would fire on any handoff-shaped sentence and
    pick whichever agent name happened to appear nearby — which is guessing with
    extra steps.
    """
    vague = [
        *NAMED_DESTINATION[:3],
        Message(role="assistant", content="Let me hand you over now."),
        Message(role="user", content="okay"),
    ]
    llm = RecordingLLM(answer="practice_faq")
    factory = NodeFactory(llm=llm, manifest=manifest)
    node = factory.supervisor(manifest.graph.agent("receptionist"), manifest.graph)

    result = await node(_state(vague, current="triage"))  # type: ignore[arg-type]
    assert result["next_agent"] == "practice_faq"


async def test_a_passing_mention_is_not_a_destination(manifest) -> None:
    """Naming an agent mid-sentence must not be read as routing.

    "The scheduler has the diary, but first — how long has it hurt?" is triage
    continuing to work, not handing over.
    """
    mention = [
        Message(role="user", content="chipped tooth"),
        Message(
            role="assistant",
            content="The scheduler has the diary, but first — how long has it "
            "been hurting?",
        ),
        Message(role="user", content="since last night"),
    ]
    llm = RecordingLLM(answer="triage")
    factory = NodeFactory(llm=llm, manifest=manifest)
    node = factory.supervisor(manifest.graph.agent("receptionist"), manifest.graph)

    result = await node(_state(mention, current="triage"))  # type: ignore[arg-type]
    assert result["next_agent"] == "triage"


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("I'll pass you to the scheduler now to get that booked.", "scheduler"),
        ("I'll hand you over to triage to assess that.", "triage"),
        ("I\u2019ll pass you to the scheduler now.", "scheduler"),
        ("I'll pass you to my colleague to book it in.", None),
        ("I am putting you through to a colleague.", None),
    ],
)
def test_named_destination_extraction(reply: str, expected: str | None) -> None:
    """Matched against the manifest's own agent keys — no vertical knowledge.

    A restaurant's `reservations` agent is recognised by exactly this code.
    """
    from packages.ai.nodes import _named_destination

    members = ["scheduler", "triage", "practice_faq", "human_transfer", "wrap_up"]
    state = {"messages": [Message(role="assistant", content=reply)]}
    assert _named_destination(state, members) == expected  # type: ignore[arg-type]

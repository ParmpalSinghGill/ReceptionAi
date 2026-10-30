"""Speculative end-of-turn: writes wait, hits reuse the run, misses undo it.

Speculation starts the agent before the caller is known to have finished. Three
properties make that safe, and each is asserted here against the real code path
rather than a description of it:

1. A side-effecting tool (a booking) does not run until the turn is confirmed —
   through the real :class:`ToolExecutor`, not a mock of it.
2. A confirmed turn whose words match reuses the speculative run: one agent run,
   not two.
3. A turn the caller talked on past is cancelled and its rows withdrawn *before*
   the confirmed turn starts, so the confirmed turn's history is clean.
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest
from packages.ai.tools import ToolExecutor, ToolRegistry, ToolResult, ToolSpec
from packages.ai.tools.gate import side_effects_held
from packages.ai.tools.registry import ToolExecutionContext
from packages.core.ports.llm import ToolCall
from packages.verticals.manifest import AgentSpec, NodeKind, ToolGrant

pytest.importorskip("pipecat", reason="voice extra not installed (pip install -e '.[voice]')")

from packages.providers.voice.agent_bridge import VoiceAgentProcessor
from packages.providers.voice.turn_signals import (
    UserTurnText,
    normalize_utterance,
)
from pipecat.processors.frame_processor import FrameDirection

_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def _executor(log: list[str]) -> tuple[ToolExecutor, AgentSpec]:
    async def write(_ctx: object, _args: dict[str, object]) -> ToolResult:
        log.append("write")
        return ToolResult(content="written")

    async def read(_ctx: object, _args: dict[str, object]) -> ToolResult:
        log.append("read")
        return ToolResult(content="read")

    registry = ToolRegistry()
    registry.register(ToolSpec(name="t.write", description="w", parameters=_SCHEMA, impl=write))
    registry.register(
        ToolSpec(
            name="t.read",
            description="r",
            parameters=_SCHEMA,
            impl=read,
            idempotent=True,
            side_effecting=False,
        )
    )
    agent = AgentSpec(
        key="scheduler",
        kind=NodeKind.LLM_AGENT,
        display_name="Scheduler",
        role_prompt="x",
        tools=[ToolGrant(tool="t.write"), ToolGrant(tool="t.read")],
    )
    return ToolExecutor(registry), agent


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        agent_key="scheduler",
        correlation_id="t",
        deadline_ms=int(time.monotonic() * 1000) + 30_000,
    )


class TestSideEffectGate:
    async def test_a_write_waits_for_confirmation(self) -> None:
        log: list[str] = []
        executor, agent = _executor(log)
        gate = asyncio.Event()

        async def speculative_run() -> None:
            with side_effects_held(gate):
                await executor.execute_many(
                    calls=[ToolCall(id="1", name="t.write", arguments={})],
                    agent=agent,
                    context=_context(),
                )

        task = asyncio.create_task(speculative_run())
        await asyncio.sleep(0.05)
        assert log == [], "the booking ran before the caller had finished"
        gate.set()
        await task
        assert log == ["write"]

    async def test_a_read_does_not_wait(self) -> None:
        log: list[str] = []
        executor, agent = _executor(log)
        with side_effects_held(asyncio.Event()):
            await executor.execute_many(
                calls=[ToolCall(id="1", name="t.read", arguments={})],
                agent=agent,
                context=_context(),
            )
        assert log == ["read"]

    async def test_a_cancelled_speculation_never_writes(self) -> None:
        log: list[str] = []
        executor, agent = _executor(log)

        async def speculative_run() -> None:
            with side_effects_held(asyncio.Event()):
                await executor.execute_many(
                    calls=[ToolCall(id="1", name="t.write", arguments={})],
                    agent=agent,
                    context=_context(),
                )

        task = asyncio.create_task(speculative_run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert log == []

    async def test_outside_speculation_writes_run_at_once(self) -> None:
        log: list[str] = []
        executor, agent = _executor(log)
        await executor.execute_many(
            calls=[ToolCall(id="1", name="t.write", arguments={})],
            agent=agent,
            context=_context(),
        )
        assert log == ["write"]


class TestNormalize:
    def test_interim_and_final_of_the_same_words_match(self) -> None:
        assert normalize_utterance("Thursday at ten.") == normalize_utterance("thursday at ten")

    def test_more_words_do_not_match(self) -> None:
        assert normalize_utterance("Thursday at ten") != normalize_utterance(
            "Thursday at ten, actually eleven"
        )


def _processor(
    replies: dict[str, str],
) -> tuple[VoiceAgentProcessor, list[str], list[str]]:
    """A bridge with speculation on, a canned graph and recorded side effects."""
    calls: list[str] = []
    spoken: list[str] = []

    async def run_turn(text: str, **_: object) -> str:
        calls.append(f"run:{text}")
        await asyncio.sleep(0.02)
        return replies.get(text, "?")

    async def withdraw(text: str) -> None:
        calls.append(f"withdraw:{text}")

    proc = VoiceAgentProcessor(
        run_turn=run_turn,
        speculation="vad",
        turn_text=UserTurnText(),
        withdraw_speculative=withdraw,
    )

    def create_task(coro, name=None):  # type: ignore[no-untyped-def]
        return asyncio.get_running_loop().create_task(coro, name=name)

    async def push(reply: str, _direction: FrameDirection) -> None:
        spoken.append(reply)

    proc.create_task = create_task  # type: ignore[method-assign]
    proc._push_spoken_reply = push  # type: ignore[method-assign]
    return proc, calls, spoken


class TestSpeculativeTurns:
    async def test_a_hit_reuses_the_speculative_run(self) -> None:
        proc, calls, spoken = _processor({"book thursday": "Thursday is free."})
        proc._speculate("book thursday")
        spec = proc._speculation
        assert spec is not None

        proc._speculation = None
        await proc._drain_turns("Book Thursday.", FrameDirection.DOWNSTREAM, spec)

        assert calls == ["run:book thursday"], "the confirmed turn ran the agent twice"
        assert spoken == ["Thursday is free."]
        assert spec.gate.is_set(), "writes stayed held after confirmation"

    async def test_a_miss_is_withdrawn_before_the_confirmed_turn_runs(self) -> None:
        proc, calls, spoken = _processor(
            {
                "book thursday": "Thursday is free.",
                "book thursday at eleven": "Eleven on Thursday is free.",
            }
        )
        proc._speculate("book thursday")
        spec = proc._speculation
        assert spec is not None
        proc._abandon(spec, reason="text_mismatch")

        await proc._drain_turns("book thursday at eleven", FrameDirection.DOWNSTREAM)

        assert "withdraw:book thursday" in calls
        assert calls.index("withdraw:book thursday") < calls.index(
            "run:book thursday at eleven"
        ), "the confirmed turn loaded history before the guess was withdrawn"
        assert spoken == ["Eleven on Thursday is free."]

    async def test_resumed_speech_cancels_the_guess(self) -> None:
        proc, calls, _ = _processor({"book thursday": "Thursday is free."})
        proc._speculate("book thursday")
        spec = proc._speculation
        assert spec is not None

        await proc.on_turn_resumed()
        await proc._settle_abandoned()

        assert proc._speculation is None
        assert spec.task is not None and spec.task.done()
        assert "withdraw:book thursday" in calls

    async def test_no_speculation_while_the_agent_is_busy(self) -> None:
        proc, calls, _ = _processor({})
        proc._busy = True
        proc._speculate("anything")
        assert proc._speculation is None
        assert calls == []

    async def test_no_speculation_while_a_phone_number_is_being_read(self) -> None:
        """The digit-group pauses belong to the phone reassembly."""
        proc, _, _ = _processor({})
        proc._state["last_bot_text"] = "Could I take a phone number for you?"
        proc._speculate("zero seven seven")
        assert proc._speculation is None

    async def test_the_same_words_do_not_restart_a_guess(self) -> None:
        proc, _, _ = _processor({"book thursday": "x"})
        proc._speculate("book thursday")
        first = proc._speculation
        proc._speculate("Book Thursday.")
        assert proc._speculation is first
        assert first is not None and first.task is not None
        await first.task

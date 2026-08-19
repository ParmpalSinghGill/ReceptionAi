"""``StubLLMAdapter`` — the same port, offline and deterministic.

This is **not** a test double that lives in ``tests/``. It is a first-class
adapter selected by configuration, for two reasons that are really one reason:

1. **A developer with no API key must get a running system**, not a
   crash-loop. The alternative — requiring a key to start — means the first
   experience of the platform is a 500, and it means CI needs a live secret to
   run an endpoint test.
2. **The test suite must never touch the network.** A test that calls DeepSeek
   is slow, flaky, costs money, and fails when a laptop is offline. Every
   endpoint and SSE-framing test in this repository runs against this adapter,
   so the assertions are about *our* framing rather than about a provider's
   mood.

Determinism is the load-bearing property. The reply is a pure function of the
last user message, so an SSE test can assert an exact token sequence. Streaming
is chunked at word boundaries with a small configurable delay, so a test can
observe genuinely-interleaved frames — a stub that emitted one giant token
would let a broken flush path pass.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any, Final, Literal

from packages.core.ports.llm import (
    ChatResult,
    FinishReason,
    Message,
    ProviderCapabilities,
    StreamDelta,
    ToolCall,
    ToolSchema,
    Usage,
)

__all__ = ["STUB_CAPABILITIES", "StubLLMAdapter"]

STUB_CAPABILITIES: Final = ProviderCapabilities(
    supports_tools=True,
    supports_parallel_tools=True,
    supports_streaming_tools=True,
    supports_json_schema=True,
    supports_prompt_caching=False,
    context_window=32_768,
    max_output_tokens=4_096,
)

#: Keyword → canned reply. Matched against the last user message, longest
#: keyword first, so "reschedule" wins over "schedule". Deliberately shaped
#: like a dental receptionist so the demo reads correctly with the seeded
#: medical manifest and the marketing site's Northgate Dental framing.
_CANNED: Final[tuple[tuple[str, str], ...]] = (
    (
        "emergency",
        "That sounds urgent. I can get you seen today — we hold two emergency "
        "slots each afternoon. Can you tell me your name and a number we can "
        "reach you on?",
    ),
    (
        "reschedule",
        "Of course, I can move that appointment for you. What day works "
        "better? We have openings Tuesday morning and Thursday afternoon.",
    ),
    (
        "cancel",
        "No problem at all, I can cancel that for you. Could I take the name "
        "the appointment is under?",
    ),
    (
        "tooth",
        "I'm sorry you're in pain — that's never fun. I can get you booked in "
        "with one of our dentists. Has the pain been constant, or does it come "
        "and go?",
    ),
    (
        "pain",
        "I'm sorry to hear that. Let's get you seen quickly. Is the pain "
        "keeping you awake, or is it manageable at the moment?",
    ),
    (
        "clean",
        "A hygiene appointment, absolutely. Those run about forty minutes. "
        "Would a morning or an afternoon suit you better?",
    ),
    (
        "price",
        "Happy to help with that. A check-up is £45 and a hygiene visit is "
        "£85. Would you like me to book one in?",
    ),
    (
        "hours",
        "We're open Monday to Friday, eight until six, and Saturday mornings "
        "until one. Would you like to come in this week?",
    ),
    (
        "book",
        "I'd be glad to book that in. What sort of appointment are you after — "
        "a check-up, a hygiene visit, or something that's bothering you?",
    ),
    (
        "appointment",
        "Certainly. Let me find you a time. Is this a routine visit, or is "
        "something troubling you?",
    ),
)

_DEFAULT_REPLY: Final = (
    "Thanks for getting in touch with Northgate Dental. I can help you book, "
    "move, or cancel an appointment, or answer questions about our services. "
    "What can I do for you?"
)


class StubLLMAdapter:
    """A deterministic, offline ``LLMPort``.

    Satisfies :class:`~packages.core.ports.llm.LLMPort` structurally, exactly
    as the real adapters do — the caller cannot tell which it holds, which is
    the property that makes an endpoint test meaningful.
    """

    provider = "stub"

    def __init__(
        self,
        *,
        model: str = "stub-1",
        token_delay_ms: int = 0,
        scripted_tool_call: ToolCall | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        """
        Args:
            token_delay_ms: Sleep between streamed chunks. Zero in unit tests
                (fast); a few milliseconds when running the app locally, so the
                UI exercises the same incremental-render path it will use
                against a real provider rather than receiving one instant blob.
            scripted_tool_call: When set, the stream emits this tool call
                instead of text. Lets a tool-path test run with no provider.
            fail_with: When set, every call raises it. Lets an error-path test
                assert the SSE ``error`` frame without a network fault.
        """
        self._model = model
        self._token_delay_s = token_delay_ms / 1000.0
        self._scripted_tool_call = scripted_tool_call
        self._fail_with = fail_with

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> ProviderCapabilities:
        return STUB_CAPABILITIES

    def count_tokens(self, messages: Sequence[Message]) -> int:
        return sum(len(m.content or "") for m in messages) // 4 + 1

    async def chat(
        self,
        *,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None = None,
        tool_choice: dict[str, Any] | Literal["auto", "none", "required"] = "auto",
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout_ms: int = 15_000,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult:
        if self._fail_with is not None:
            raise self._fail_with

        if self._scripted_tool_call is not None:
            return ChatResult(
                message=Message(
                    role="assistant",
                    content=None,
                    tool_calls=(self._scripted_tool_call,),
                ),
                tool_calls=[self._scripted_tool_call],
                usage=self._usage(messages, ""),
                model=self._model,
                provider=self.provider,
                finish_reason="tool_calls",
            )

        reply = self.reply_for(messages)
        return ChatResult(
            message=Message(role="assistant", content=reply),
            tool_calls=[],
            usage=self._usage(messages, reply),
            model=self._model,
            provider=self.provider,
            finish_reason="stop",
            ttft_ms=1,
            total_ms=2,
        )

    async def stream(
        self,
        *,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None = None,
        tool_choice: dict[str, Any] | Literal["auto", "none", "required"] = "auto",
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout_ms: int = 15_000,
    ) -> AsyncIterator[StreamDelta]:
        if self._fail_with is not None:
            raise self._fail_with

        if self._scripted_tool_call is not None:
            yield StreamDelta(kind="tool_call_start", tool_call=self._scripted_tool_call)
            yield StreamDelta(
                kind="tool_call_delta",
                tool_call=self._scripted_tool_call,
                partial_args="{}",
            )
            yield StreamDelta(kind="tool_call_end", tool_call=self._scripted_tool_call)
            yield StreamDelta(kind="usage", usage=self._usage(messages, ""))
            yield StreamDelta(kind="done")
            return

        reply = self.reply_for(messages)
        finish: FinishReason = "stop"

        for chunk in _chunk(reply):
            if self._token_delay_s:
                # Cancellable by construction: a sleep is a suspension point,
                # so a client disconnect propagates CancelledError here and the
                # generator unwinds instead of running to completion into a
                # socket nobody is reading.
                await asyncio.sleep(self._token_delay_s)
            yield StreamDelta(kind="text", text=chunk)

        yield StreamDelta(kind="usage", usage=self._usage(messages, reply))
        yield StreamDelta(kind="done")
        _ = finish

    async def aclose(self) -> None:
        """Symmetry with the HTTP adapters, so lifespan shutdown is uniform."""
        return None

    # ── Determinism ───────────────────────────────────────────────────────

    @staticmethod
    def reply_for(messages: Sequence[Message]) -> str:
        """The canned reply for a message list. Pure, and therefore assertable.

        Exposed as a static method so a test can compute the expected token
        stream without reaching into adapter internals.
        """
        last_user = next(
            (m.content for m in reversed(list(messages)) if m.role == "user" and m.content),
            "",
        )

        if (routed := _route_if_asked(last_user)) is not None:
            return routed

        haystack = last_user.lower()
        for keyword, reply in _CANNED:
            if keyword in haystack:
                return reply
        return _DEFAULT_REPLY

    def _usage(self, messages: Sequence[Message], reply: str) -> Usage:
        """Plausible, deterministic token counts.

        Derived from content length rather than randomized: a usage assertion
        in a test must be stable, and a cost roll-up computed from these
        numbers should look sane in the local dashboard rather than being zero.
        """
        return Usage(
            prompt_tokens=self.count_tokens(messages),
            completion_tokens=max(1, len(reply) // 4),
        )


#: Recognizes the supervisor's routing instruction (`packages.ai.nodes`
#: `_route`). The stub must answer it with a bare agent key rather than prose,
#: or every routed turn falls back to the first declared member and a
#: multi-agent manifest is untestable without a live provider.
_ROUTING_PROMPT: Final = "Specialists you may route to:"
_MEMBER_LINE: Final = re.compile(r"^- ([a-z][a-z0-9_]*):", re.MULTILINE)
#: The recent-exchange block the supervisor now sends. It replaced a single
#: "The caller just said:" line — routing on one utterance meant a follow-up
#: answer like "yes" carried no topic, so the router lost the thread by turn
#: three. The stub reads the same block the real prompt carries.
_CONVERSATION: Final = re.compile(
    r"The recent conversation:\n(.*?)\n\nReply with", re.DOTALL
)

#: Keyword → the *kind* of specialist to prefer. Matched against the member
#: keys actually offered, so this stays manifest-agnostic: it expresses "this
#: sounds clinical" and lets the manifest decide what its clinical agent is
#: called. A manifest with no matching member falls through to the first.
_ROUTE_HINTS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("emergency", ("triage", "urgent", "human", "transfer")),
    ("swelling", ("triage", "urgent", "human", "transfer")),
    ("bleeding", ("triage", "urgent", "human", "transfer")),
    ("pain", ("triage", "urgent")),
    ("tooth", ("triage", "urgent")),
    ("hurt", ("triage", "urgent")),
    ("ache", ("triage", "urgent")),
    ("hours", ("faq", "info", "practice")),
    ("open", ("faq", "info", "practice")),
    ("price", ("faq", "info", "practice")),
    ("cost", ("faq", "info", "practice")),
    ("park", ("faq", "info", "practice")),
    ("where", ("faq", "info", "practice")),
    ("person", ("human", "transfer")),
    ("speak to", ("human", "transfer")),
    ("complain", ("human", "transfer")),
    ("book", ("schedul", "booking", "appointment")),
    ("appointment", ("schedul", "booking", "appointment")),
    ("reschedul", ("schedul", "booking", "appointment")),
    ("cancel", ("schedul", "booking", "appointment")),
    ("thanks", ("wrap", "close", "done")),
    ("bye", ("wrap", "close", "done")),
    ("that's all", ("wrap", "close", "done")),
)


def _route_if_asked(prompt: str) -> str | None:
    """Answer a supervisor routing instruction with one agent key.

    Returns ``None`` when the prompt is not a routing instruction, so the
    ordinary canned-reply path runs.

    The member keys are read out of the prompt rather than hardcoded, which is
    what keeps the stub manifest-agnostic: a restaurant manifest routing to
    ``reservations`` and ``kitchen_faq`` works with no change here.
    """
    if _ROUTING_PROMPT not in prompt:
        return None

    members = _MEMBER_LINE.findall(prompt)
    if not members:
        return None

    said = _CONVERSATION.search(prompt)
    # Weight the LAST caller line: the routing hints below express "this sounds
    # clinical", and the most recent thing said is the strongest signal of what
    # the caller wants now. Earlier lines still count, so a follow-up "yes"
    # inherits the topic rather than matching nothing.
    haystack = (said.group(1) if said else prompt).lower()

    for keyword, preferences in _ROUTE_HINTS:
        if keyword not in haystack:
            continue
        for preference in preferences:
            for member in members:
                if preference in member:
                    return member
    return members[0]


def _chunk(text: str) -> list[str]:
    """Split into word-plus-trailing-space fragments.

    Word-level rather than character-level because that is roughly what a real
    tokenizer emits, and a client that reassembles by concatenation is exercised
    identically. Whitespace stays attached to the preceding word so
    ``"".join(chunks) == text`` exactly — a stub whose chunks did not
    reassemble to the original would make every equality assertion downstream
    subtly wrong.
    """
    return re.findall(r"\S+\s*", text) or ([text] if text else [])


def deterministic_seed(text: str) -> int:
    """A stable integer from a string, for any stub needing pseudo-variety.

    ``hashlib`` rather than ``hash()``: Python's string hash is randomized per
    process by ``PYTHONHASHSEED``, so a test asserting on it would pass locally
    and fail in CI roughly half the time.
    """
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")

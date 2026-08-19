"""``LLMPort`` — contract §5, verbatim from 03 §9.1.

No subsystem may import a vendor SDK outside ``packages/providers``,
``packages/connectors``, or ``packages/channels`` (contract §9, enforced by
``tools/check_layering.py``). This module is the seam: everything above it
speaks these dataclasses, and only an adapter knows what an OpenAI or Anthropic
request body looks like.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

__all__ = [
    "ChatResult",
    "FinishReason",
    "LLMPort",
    "Message",
    "MessageRole",
    "ProviderCapabilities",
    "StreamDelta",
    "ToolCall",
    "ToolSchema",
    "Usage",
]

MessageRole = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "tool_calls", "length", "content_filter"]


@dataclass(frozen=True, slots=True)
class Message:
    """One message in a conversation, provider-agnostic.

    03 refers to LangChain's ``AnyMessage``/``AIMessage``. Core cannot import
    ``langchain_core`` — it is a first-party-external dependency of
    ``packages/ai``, not of the domain — so the port speaks this shape and the
    ``packages/ai`` layer converts. Same fields, no import edge.
    """

    role: MessageRole
    content: str | None = None
    #: Structured/multimodal parts, when a plain string is insufficient.
    content_parts: tuple[dict[str, Any], ...] = ()
    #: Populated on an assistant message that requested tools.
    tool_calls: tuple[ToolCall, ...] = ()
    #: Populated on a ``role="tool"`` message: which call this answers.
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class ToolSchema:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    cached_prompt_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ChatResult:
    message: Message
    tool_calls: list[ToolCall]
    usage: Usage
    model: str
    provider: str
    finish_reason: FinishReason
    ttft_ms: int | None = None
    total_ms: int = 0


@dataclass(frozen=True, slots=True)
class StreamDelta:
    """One streaming event.

    ``kind`` discriminates so the voice pipeline can start TTS on the first
    ``text`` delta without waiting for tool-call args.
    """

    kind: Literal[
        "text", "tool_call_start", "tool_call_delta", "tool_call_end", "usage", "done"
    ]
    text: str | None = None
    tool_call: ToolCall | None = None
    partial_args: str | None = None
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What an adapter can actually do.

    Exists because the fallback chain must not silently degrade correctness
    (03 §9.3). If the primary supports parallel tool calls and the fallback does
    not, the router must serialize rather than send a request the fallback will
    reject.
    """

    supports_tools: bool
    supports_parallel_tools: bool
    supports_streaming_tools: bool
    supports_json_schema: bool
    supports_prompt_caching: bool
    context_window: int
    max_output_tokens: int


@runtime_checkable
class LLMPort(Protocol):
    """chat/completion, streaming, tool-calling, token accounting.

    Universal adapter requirements (contract §5) apply to every implementation:
    async, cancellable, timeout-bounded, retry-with-jitter, circuit-broken,
    emits OTel spans, emits cost/usage events, never logs secrets or raw PII.
    """

    provider: str

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
    ) -> ChatResult: ...

    def stream(
        self,
        *,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None = None,
        tool_choice: dict[str, Any] | Literal["auto", "none", "required"] = "auto",
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout_ms: int = 15_000,
    ) -> AsyncIterator[StreamDelta]: ...

    def count_tokens(self, messages: Sequence[Message]) -> int: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...

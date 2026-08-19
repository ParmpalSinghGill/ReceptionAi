"""``LLMPort`` adapters — the stub, the OpenAI-compatible wire translation,
and the selection policy.

Not one of these touches the network. The DeepSeek adapter is exercised against
a scripted ``httpx.MockTransport`` speaking the real wire format, which is what
lets the streaming accumulator, the retry policy and the error mapping be
tested at all — a live provider is slow, flaky, and cannot be made to return a
429 on demand.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from packages.core.errors import ConfigurationError, PortUnavailableError
from packages.core.ports.llm import LLMPort, Message, ToolCall, ToolSchema
from packages.providers.llm import (
    DeepSeekLLMAdapter,
    LLMProviderSettings,
    StubLLMAdapter,
    build_llm_adapter,
)

# ─────────────────────────────────────────────────────────────────────────────
# The stub
# ─────────────────────────────────────────────────────────────────────────────


def test_the_stub_satisfies_the_port() -> None:
    """Structurally, so a caller cannot tell which adapter it holds.

    That is the property that makes an endpoint test meaningful: the code path
    under test is the production one.
    """
    assert isinstance(StubLLMAdapter(), LLMPort)


async def test_the_stub_is_deterministic() -> None:
    """Same input, same output — so an SSE test can assert exact tokens."""
    stub = StubLLMAdapter()
    messages = [Message(role="user", content="my tooth hurts")]

    first = await stub.chat(messages=messages)
    second = await stub.chat(messages=messages)

    assert first.message.content == second.message.content
    assert "sorry" in (first.message.content or "").lower()


async def test_the_stub_streams_multiple_chunks_that_reassemble() -> None:
    """Word-level chunks that concatenate back to the exact reply.

    A stub emitting one giant token would let a broken flush path pass every
    test; chunks that did not reassemble would make every downstream equality
    assertion subtly wrong.
    """
    stub = StubLLMAdapter()
    messages = [Message(role="user", content="what are your hours?")]

    deltas = [
        delta async for delta in stub.stream(messages=messages) if delta.kind == "text"
    ]

    assert len(deltas) > 3
    assembled = "".join(d.text or "" for d in deltas)
    assert assembled == StubLLMAdapter.reply_for(messages)


async def test_the_stream_ends_with_usage_then_done() -> None:
    stub = StubLLMAdapter()
    kinds = [
        delta.kind
        async for delta in stub.stream(messages=[Message(role="user", content="hi")])
    ]

    assert kinds[-2:] == ["usage", "done"]


async def test_the_stub_can_be_scripted_to_fail() -> None:
    """So an error-path test needs no network fault."""
    boom = PortUnavailableError("provider down", port="LLMPort")
    stub = StubLLMAdapter(fail_with=boom)

    with pytest.raises(PortUnavailableError):
        await stub.chat(messages=[Message(role="user", content="hi")])


async def test_the_stub_can_be_scripted_to_call_a_tool() -> None:
    call = ToolCall(id="call_1", name="booking.hold_slot", arguments={})
    stub = StubLLMAdapter(scripted_tool_call=call)

    kinds = [
        delta.kind
        async for delta in stub.stream(messages=[Message(role="user", content="book")])
    ]

    assert "tool_call_start" in kinds
    assert "tool_call_end" in kinds


async def test_the_stub_is_cancellable_mid_stream() -> None:
    """Contract §5: every adapter is cancellable.

    A non-zero delay makes each chunk a suspension point, so a cancellation
    propagates into the generator instead of it running to completion into a
    socket nobody is reading.
    """
    stub = StubLLMAdapter(token_delay_ms=50)

    async def consume() -> int:
        count = 0
        async for _ in stub.stream(messages=[Message(role="user", content="hello")]):
            count += 1
        return count

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.06)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# ─────────────────────────────────────────────────────────────────────────────
# The OpenAI-compatible wire format
# ─────────────────────────────────────────────────────────────────────────────


def _sse(chunks: list[dict]) -> str:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return body + "data: [DONE]\n\n"


def _adapter(handler) -> DeepSeekLLMAdapter:
    return DeepSeekLLMAdapter(
        api_key="sk-test-not-a-real-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_streaming_text_deltas_are_translated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {"choices": [{"delta": {"content": "Sorry "}}]},
                    {"choices": [{"delta": {"content": "you're "}}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                    {"usage": {"prompt_tokens": 12, "completion_tokens": 3}},
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    adapter = _adapter(handler)
    deltas = [
        d async for d in adapter.stream(messages=[Message(role="user", content="hi")])
    ]
    await adapter.aclose()

    # Concatenated verbatim, whitespace included: the adapter must not trim,
    # or rejoined tokens lose the spaces between words.
    text = "".join(d.text or "" for d in deltas if d.kind == "text")
    assert text == "Sorry you're "

    usage = next(d.usage for d in deltas if d.kind == "usage")
    assert usage is not None
    assert usage.prompt_tokens == 12
    assert usage.completion_tokens == 3


async def test_tool_call_arguments_are_reassembled_across_chunks() -> None:
    """The wire delivers arguments as string fragments, split mid-token.

    A single chunk is routinely ``'{"da'``. Accumulating by *index* rather than
    by id is required: providers omit the id on continuation chunks, so index
    is the only field present on every fragment. Getting this wrong produces a
    tool call with truncated JSON, which fails argument validation with a
    message that points at the model rather than at us.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_abc",
                                            "function": {
                                                "name": "booking.find_availability",
                                                "arguments": '{"serv',
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    # No id on the continuation — index is all we get.
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "function": {"arguments": 'ice_key": "clean'},
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {"index": 0, "function": {"arguments": 'ing"}'}}
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    },
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    adapter = _adapter(handler)
    deltas = [
        d async for d in adapter.stream(messages=[Message(role="user", content="book")])
    ]
    await adapter.aclose()

    finished = [d.tool_call for d in deltas if d.kind == "tool_call_end"]
    assert len(finished) == 1
    call = finished[0]
    assert call is not None
    assert call.id == "call_abc"
    assert call.name == "booking.find_availability"
    assert call.arguments == {"service_key": "cleaning"}


async def test_malformed_tool_arguments_degrade_rather_than_raise() -> None:
    """An unparseable fragment yields ``{}``, not a failed turn.

    The tool executor validates against the tool's JSON Schema, where an empty
    object fails with a message naming the missing required fields. That
    message is actionable; a ``JSONDecodeError`` from inside the adapter is not.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_x",
                                            "function": {
                                                "name": "booking.hold_slot",
                                                "arguments": "{not json at all",
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    adapter = _adapter(handler)
    calls = [
        d.tool_call
        async for d in adapter.stream(messages=[Message(role="user", content="x")])
        if d.kind == "tool_call_end"
    ]
    await adapter.aclose()

    assert calls[0] is not None
    assert calls[0].arguments == {}


async def test_a_retryable_status_is_retried_then_surfaces_as_503() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="upstream unavailable")

    adapter = DeepSeekLLMAdapter(
        api_key="sk-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_attempts=3,
    )

    with pytest.raises(PortUnavailableError) as raised:
        await adapter.chat(messages=[Message(role="user", content="hi")])
    await adapter.aclose()

    assert attempts == 3
    # 503, never 500: a 500 tells the client "your request was wrong", a 503
    # tells it "we are broken, retry" — which drives backoff correctly.
    assert raised.value.http_status == 503


async def test_a_client_error_is_not_retried() -> None:
    """Retrying a 400 only multiplies the latency of a doomed request."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, text="bad request")

    adapter = DeepSeekLLMAdapter(
        api_key="sk-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_attempts=3,
    )

    with pytest.raises(PortUnavailableError):
        await adapter.chat(messages=[Message(role="user", content="hi")])
    await adapter.aclose()

    assert attempts == 1


async def test_the_provider_message_is_never_forwarded_to_the_caller() -> None:
    """A provider error can echo prompt content, which is customer PII (§8)."""
    secret = "PATIENT-NAME-Jane-Doe"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=f"invalid prompt: {secret}")

    adapter = DeepSeekLLMAdapter(
        api_key="sk-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_attempts=1,
    )

    with pytest.raises(PortUnavailableError) as raised:
        await adapter.chat(messages=[Message(role="user", content="hi")])
    await adapter.aclose()

    assert secret not in str(raised.value)


async def test_sending_tools_to_a_model_without_tool_support_is_refused() -> None:
    """03 §9.3: the router must serialize or refuse, never send a doomed body.

    ``deepseek-reasoner`` does not support tool calling, and its capability
    record says so — so the request is rejected before the round-trip rather
    than by the provider afterwards.
    """
    adapter = DeepSeekLLMAdapter(
        api_key="sk-test",
        model="deepseek-reasoner",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )

    with pytest.raises(ConfigurationError, match="does not support tool calling"):
        await adapter.chat(
            messages=[Message(role="user", content="hi")],
            tools=[ToolSchema(name="x.y", description="d", parameters={})],
        )
    await adapter.aclose()


async def test_deepseek_cache_hits_are_read_into_usage() -> None:
    """DeepSeek reports its context-cache hit under its own key.

    Read, so cached tokens are billed at the cached rate rather than silently
    at the full one.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 5,
                    "prompt_cache_hit_tokens": 80,
                },
            },
        )

    adapter = _adapter(handler)
    result = await adapter.chat(messages=[Message(role="user", content="hi")])
    await adapter.aclose()

    assert result.usage.cached_prompt_tokens == 80


def test_an_unknown_model_is_refused_rather_than_guessed() -> None:
    """An invented capability record would make the fallback router route on a
    guess."""
    with pytest.raises(ConfigurationError, match="Unknown DeepSeek model"):
        DeepSeekLLMAdapter(api_key="sk-test", model="deepseek-imaginary")


# ─────────────────────────────────────────────────────────────────────────────
# Selection
# ─────────────────────────────────────────────────────────────────────────────


def test_a_missing_key_falls_back_to_the_stub_locally() -> None:
    """Never a crash: a developer with no credentials gets a running system."""
    adapter = build_llm_adapter(
        LLMProviderSettings(provider="deepseek", api_key=""),
        allow_stub_fallback=True,
    )
    assert adapter.provider == "stub"


def test_a_missing_key_is_fatal_when_fallback_is_disallowed() -> None:
    """In a deployed environment the pod must crash-loop visibly rather than
    serve canned replies that every dashboard reports as healthy."""
    with pytest.raises(ConfigurationError, match="stub fallback is disabled"):
        build_llm_adapter(
            LLMProviderSettings(provider="deepseek", api_key=""),
            allow_stub_fallback=False,
        )


def test_an_unknown_provider_is_always_fatal() -> None:
    """Even with fallback allowed.

    Falling back here would mask a typo in a ConfigMap (``deepsek``) as "the
    stub is fine", and the typo would survive into production.
    """
    with pytest.raises(ConfigurationError, match="Unknown LLM provider"):
        build_llm_adapter(
            LLMProviderSettings(provider="deepsek", api_key="sk-x"),
            allow_stub_fallback=True,
        )


def test_a_configured_key_selects_the_real_adapter() -> None:
    adapter = build_llm_adapter(
        LLMProviderSettings(provider="deepseek", api_key="sk-test"),
    )
    assert adapter.provider == "deepseek"


def test_the_api_key_is_never_reprable() -> None:
    """``SecretValue`` masks itself in f-strings, logs and reprs."""
    from packages.core.ports.secrets import SecretValue

    secret = SecretValue("sk-do-not-log-me")

    assert "do-not-log-me" not in repr(secret)
    assert "do-not-log-me" not in str(secret)
    assert "do-not-log-me" not in f"{secret}"
    assert secret.reveal() == "sk-do-not-log-me"

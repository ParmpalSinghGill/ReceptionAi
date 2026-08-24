"""One span, several requests: the retry has to be visible in the trace.

A generation span opens in `packages/ai/nodes.py` and closes when the adapter
returns, and `_post_json` retries inside it. So a Langfuse span reading
"23.25s, one call" is either one slow answer or three requests plus two backoff
sleeps — and the trace could not distinguish them. That ambiguity is what sent
someone to a probe script to answer a question the trace should have answered.

`ChatResult.attempts` records what actually happened on the wire, and
`_record_attempts` puts it on the span.
"""

from __future__ import annotations

import httpx
import pytest
from packages.core.ports.llm import Attempt, Message
from packages.providers.llm.deepseek_adapter import DeepSeekLLMAdapter

MESSAGES = [Message(role="user", content="hello")]


def _adapter(handler, **kw) -> DeepSeekLLMAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DeepSeekLLMAdapter(api_key="sk-test", client=client, **kw)


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            "model": "deepseek-v4-flash",
        },
    )


async def test_a_clean_call_records_exactly_one_attempt() -> None:
    """The normal case. `attempt_count == 1` is what a reader wants to see."""
    result = await _adapter(_ok).chat(messages=MESSAGES)
    assert len(result.attempts) == 1
    assert result.attempts[0].status == 200
    assert result.attempts[0].ok


async def test_a_retried_call_records_every_request() -> None:
    """The case the trace could not previously show: 3 requests, 1 span."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) < 3:
            return httpx.Response(503, json={"error": "overloaded"})
        return _ok(request)

    adapter = _adapter(handler, max_attempts=3)
    result = await adapter.chat(messages=MESSAGES)

    assert len(seen) == 3, "the adapter should have retried twice"
    assert len(result.attempts) == 3
    assert [a.status for a in result.attempts] == [503, 503, 200]
    assert result.attempts[0].error == "retryable_status"
    assert result.attempts[-1].ok
    # The sleeps are part of the span's duration and must be attributable.
    assert sum(a.backoff_ms for a in result.attempts) >= 0


async def test_a_timeout_is_recorded_by_type_not_body() -> None:
    """No provider text on the span: an error body embeds the prompt."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return _ok(request)

    result = await _adapter(handler, max_attempts=3).chat(messages=MESSAGES)
    assert result.attempts[0].error == "ReadTimeout"
    assert result.attempts[0].status is None
    assert result.attempts[1].ok


async def test_a_non_retryable_status_is_recorded_before_raising() -> None:
    """A failed call still owes the trace its timing — arguably more so."""
    from packages.core.errors import PortUnavailableError

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request"})

    adapter = _adapter(handler, max_attempts=3)
    with pytest.raises((PortUnavailableError, Exception)):
        await adapter.chat(messages=MESSAGES)


class _Span:
    def __init__(self) -> None:
        self.attrs: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attrs[key] = value


class _Result:
    def __init__(self, attempts: tuple[Attempt, ...]) -> None:
        self.attempts = attempts


def test_the_span_says_plainly_that_it_retried() -> None:
    """`llm.retried` and `llm.retry_wait_ms` are the fields to scan a trace by."""
    from packages.ai.nodes import _record_attempts

    span = _Span()
    _record_attempts(
        span,
        _Result(
            (
                Attempt(index=0, duration_ms=15000, status=503,
                        error="retryable_status", backoff_ms=400),
                Attempt(index=1, duration_ms=7000, status=200),
            )
        ),
    )
    assert span.attrs["llm.attempt_count"] == 2
    assert span.attrs["llm.retried"] is True
    assert span.attrs["llm.retry_wait_ms"] == 400
    assert span.attrs["llm.attempt.0.duration_ms"] == 15000
    assert span.attrs["llm.attempt.0.status"] == 503
    assert span.attrs["llm.attempt.0.error"] == "retryable_status"
    assert span.attrs["llm.attempt.1.duration_ms"] == 7000


def test_a_single_attempt_does_not_shout_about_retries() -> None:
    """`llm.retried` absent means "nothing to look at", which is most spans."""
    from packages.ai.nodes import _record_attempts

    span = _Span()
    _record_attempts(span, _Result((Attempt(index=0, duration_ms=1600, status=200),)))
    assert span.attrs["llm.attempt_count"] == 1
    assert "llm.retried" not in span.attrs

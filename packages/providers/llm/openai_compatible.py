"""The OpenAI-wire-format base adapter — contract §5, 03 §9.

DeepSeek, Together, Groq, Fireworks, OpenRouter and OpenAI itself all speak the
same ``POST /chat/completions`` request and the same ``data: {...}\\n\\n`` SSE
response. Writing one translation layer and specializing ``base_url`` + model
names is not a shortcut: a second hand-written copy of the tool-call delta
accumulator is a second place for the "arguments arrive as string fragments"
bug to live, and that bug is silent — it produces a tool call with truncated
JSON, which fails argument validation with a message that points at the model
rather than at us.

**Why the raw HTTP client and not the ``openai`` SDK.** The SDK is permitted
here (contract §9 lets ``providers`` import vendor SDKs) and is a reasonable
choice. ``httpx`` is chosen because this adapter needs three things the SDK
makes awkward: precise control of the cancellation path (an ``asyncio``
cancellation must close the socket immediately so a barge-in stops paying for
tokens), a single retry policy shared with every other adapter, and one fewer
dependency whose major version bumps break a build. The wire format is a
published, stable contract; the SDK is an abstraction over it that we would be
fighting.

Universal adapter requirements (contract §5), each implemented below:

* **async** — every call path is a coroutine or an async generator.
* **cancellable** — :meth:`OpenAICompatibleLLMAdapter.stream` closes the
  response on ``CancelledError`` and re-raises; nothing is swallowed.
* **timeout-bounded** — a per-call ``timeout_ms``, applied as a read timeout so
  a stream that stalls mid-token fails rather than hanging a voice channel.
* **retry-with-jitter** — full-jitter exponential backoff on retryable statuses
  only, and *never* on a stream that has already emitted a token (§ below).
* **emits usage** — a terminal ``usage`` delta and :class:`ChatResult.usage`.
* **never logs secrets or PII** — the key is a
  :class:`~packages.core.ports.secrets.SecretValue`, and no log statement here
  interpolates a message body.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Final, Literal

import httpx
import structlog
from packages.core.errors import ConfigurationError, PortUnavailableError
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
from packages.core.ports.secrets import SecretValue

__all__ = ["OpenAICompatibleLLMAdapter"]

log = structlog.get_logger(__name__)

#: Statuses worth retrying. 429 and 5xx are transient by definition; 408 is a
#: server-side timeout. 400/401/403/404/422 are our fault and retrying them
#: only multiplies the latency of a request that will never succeed.
_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: OpenAI-format finish reasons → the port's closed vocabulary. An unknown
#: value maps to "stop" rather than raising: a provider adding a new reason
#: must not break an otherwise-complete response, and the text is already in
#: hand.
_FINISH_REASONS: Mapping[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
}


def _finish_reason(raw: str | None) -> FinishReason:
    return _FINISH_REASONS.get(raw or "stop", "stop")


#: The separator the wire format cannot carry. OpenAI-compatible providers
#: validate function names against ``^[a-zA-Z0-9_-]+$`` and reject the whole
#: request — a 400, not a warning — if one contains a dot.
_WIRE_SEPARATOR = "__"


def _wire_tool_name(name: str) -> str:
    """``booking.check_availability`` -> ``booking__check_availability``.

    03 §6.1 names tools ``namespace.verb``, and that namespacing is worth
    keeping: it is what stops a connector-supplied ``cancel`` from colliding with
    a first-party one. But the OpenAI wire format forbids a dot in a function
    name and rejects the entire request with a 400 that names ``tools[0]`` — so
    the translation happens **here, in the adapter**, which is exactly the seam
    that exists for "the provider's format is not our model".

    The mapping is total and reversible (``__`` cannot appear in a tool key,
    whose pattern is ``[a-z][a-z0-9_]*\\.[a-z][a-z0-9_]*`` — a single underscore
    between lowercase runs, never a double), so :func:`_domain_tool_name` restores
    the original on the way back and nothing above this module ever sees the wire
    spelling.
    """
    return name.replace(".", _WIRE_SEPARATOR)


#: The fullwidth vertical line, U+FF5C. Written as an escape rather than
#: literally so this source file stays pure ASCII — the character is visually
#: indistinguishable from a normal pipe in most editors, which is exactly how a
#: later edit would silently break the pattern.
_FULLWIDTH_PIPE: Final = "\uff5c"

#: Providers sometimes emit a tool call as **inline text in the content stream**
#: instead of in the structured ``tool_calls`` field — the model imitating its
#: own training format rather than using the protocol. DeepSeek does this with a
#: fullwidth-pipe delimiter, producing content of the shape::
#:
#:     <||DSML||tool_calls>                       (with fullwidth pipes)
#:     <||DSML||invoke name="booking__check_availability">
#:     <||DSML||parameter name="service_code" string="true">checkup</...>
#:
#: Forwarded verbatim, that markup is rendered **to the caller** as speech. A
#: caller seeing XML is worse than a caller seeing an apology, and it leaks the
#: internal tool names the platform frame explicitly forbids revealing.
#:
#: Matching on the *delimiter shape* rather than on "DSML" specifically: the
#: token name is a provider implementation detail that will change, but a pipe
#: fencing a pseudo-tag is what makes it recognisable and is what must never
#: reach a person. ASCII ``<|...|>`` is covered too, since several open-weight
#: models use that spelling of the same idea.
_PIPES: Final = f"[{_FULLWIDTH_PIPE}|]"

#: Once markup starts, everything after it in that message is part of the
#: pseudo-call — parameter values, closing tags, the lot. Truncating from the
#: first delimiter is therefore right, and is what stops a half-stripped
#: `checkup</` fragment being spoken.
_MARKUP_START: Final = re.compile(rf"<\s*{_PIPES}")

#: The longest prefix of a delimiter we might be holding mid-stream. A chunk can
#: split anywhere, so a naive per-chunk filter passes `<` through and strips only
#: the following chunk — emitting the one character that starts the leak.
_MAX_PARTIAL_DELIMITER = 4


def _is_delimiter_prefix(fragment: str) -> bool:
    """Could this trailing fragment be the start of a delimiter, and nothing else?

    A bare ``"<"``, or ``"<"`` followed only by pipes — yes. ``"<b"`` — no, that
    is markup a caller might legitimately have typed back at us, and holding it
    would delay real text forever waiting for a delimiter that never comes.
    """
    return (
        bool(fragment)
        and fragment[0] == "<"
        and all(c in f"<|{_FULLWIDTH_PIPE}" for c in fragment)
    )


def _strip_inline_tool_markup(text: str) -> str:
    """Truncate a complete message at the first pseudo-tool-call delimiter.

    **Truncate, not excise.** Removing each tag individually looks tidier and is
    wrong: the text *between* the tags is argument values, so stripping only the
    tags leaves ``checkup</`` in the caller's ear. Once a model starts emitting a
    pseudo-call, everything after that point belongs to it — the prose ended at
    the delimiter.
    """
    if match := _MARKUP_START.search(text):
        return text[: match.start()].rstrip()
    return text


class _ContentSanitizer:
    """Strips inline tool-call markup from a *streamed* content channel.

    Streaming is what makes this non-trivial. Deltas split at arbitrary byte
    boundaries, so a delimiter routinely arrives as ``"<"``, then ``"||DSM"``,
    then ``"L||tool_calls>"``. A stateless per-chunk regex passes the lone
    ``"<"`` straight to the caller and strips the rest — leaking exactly the
    character that starts the tag and leaving the user looking at ``<``.

    So this holds back a short tail whenever the buffer *could* be the start of a
    delimiter, and releases it once the next chunk proves it is not. The held
    tail is at most a few characters, so it costs no perceptible latency.

    Once a delimiter is seen, the sanitizer latches into a suppressing state for
    the remainder of the message: everything after it belongs to the pseudo-call.
    """

    __slots__ = ("_buffer", "_suppressing", "suppressed")

    def __init__(self) -> None:
        self._buffer = ""
        self._suppressing = False
        #: True once any markup was seen, so the caller can log it. A provider
        #: doing this is a real event worth knowing about, not a silent fixup.
        self.suppressed = False

    def feed(self, chunk: str) -> str:
        """Absorb one delta; return the text that is safe to emit now."""
        if self._suppressing:
            self.suppressed = True
            return ""

        cleaned = self._buffer + chunk

        if match := _MARKUP_START.search(cleaned):
            # The prose ended here. Latch: everything after a delimiter belongs
            # to the pseudo-call, including the values between its tags — which
            # is why this truncates rather than excising each tag and leaving
            # `checkup</` behind.
            self._suppressing = True
            self.suppressed = True
            self._buffer = ""
            return cleaned[: match.start()]

        # Hold back a trailing fragment that could still become a delimiter.
        # Only a run of `<` and pipes qualifies — the delimiter's own prefix —
        # so ordinary text is never delayed and a `<` that turns out to be plain
        # punctuation is released by the next chunk or by `flush`.
        hold = 0
        for size in range(min(_MAX_PARTIAL_DELIMITER, len(cleaned)), 0, -1):
            if _is_delimiter_prefix(cleaned[-size:]):
                hold = size
                break
        if hold:
            self._buffer = cleaned[-hold:]
            return cleaned[:-hold]

        self._buffer = ""
        return cleaned

    def flush(self) -> str:
        """Release whatever is still held, at end of stream.

        A trailing ``"<"`` that never became a delimiter is real content and must
        be emitted; one that did is already suppressed by ``feed``.
        """
        if self._suppressing:
            return ""
        remaining = _strip_inline_tool_markup(self._buffer)
        self._buffer = ""
        return remaining


def _domain_tool_name(name: str) -> str:
    """The inverse of :func:`_wire_tool_name`, applied to every returned call.

    Without it the executor would look up ``booking__book_appointment`` in a
    registry keyed by ``booking.book_appointment``, find nothing, and deny the
    call as ungranted — which would look exactly like a grant bug and send
    someone hunting through the manifest.
    """
    return name.replace(_WIRE_SEPARATOR, ".")


class _ToolCallAccumulator:
    """Reassembles streamed tool calls.

    The wire format delivers a tool call across many chunks: the first carries
    ``id`` and ``function.name``, and every subsequent one carries a *fragment*
    of ``function.arguments`` as a string. Fragments split mid-token — a single
    chunk is routinely ``'{"da'`` — so the JSON is only parseable once the
    stream says the call is done. Accumulating by index rather than by id is
    required: providers omit the id on continuation chunks, and parallel tool
    calls interleave, so index is the only field present on every fragment.
    """

    __slots__ = ("_by_index",)

    def __init__(self) -> None:
        self._by_index: dict[int, dict[str, Any]] = {}

    def ingest(self, raw_calls: Sequence[Mapping[str, Any]]) -> list[int]:
        """Absorb one chunk's ``tool_calls`` array. Returns newly-opened indices."""
        opened: list[int] = []
        for raw in raw_calls:
            index = int(raw.get("index", 0))
            slot = self._by_index.get(index)
            if slot is None:
                slot = {"id": None, "name": None, "arguments": ""}
                self._by_index[index] = slot
                opened.append(index)
            if raw.get("id"):
                slot["id"] = raw["id"]
            function = raw.get("function") or {}
            if function.get("name"):
                slot["name"] = function["name"]
            if function.get("arguments"):
                slot["arguments"] += function["arguments"]
        return opened

    def peek(self, index: int) -> ToolCall:
        """The call so far, arguments not yet parsed. For ``tool_call_start``."""
        slot = self._by_index[index]
        return ToolCall(
            id=slot["id"] or f"call_{index}",
            # Translated back on the way out, so nothing above the adapter ever
            # sees the wire spelling — see `_wire_tool_name`.
            name=_domain_tool_name(slot["name"] or ""),
            arguments={},
        )

    def fragment(self, index: int) -> str:
        return str(self._by_index[index]["arguments"])

    def finish(self) -> list[ToolCall]:
        """Every accumulated call, arguments parsed.

        A fragment that does not parse yields ``{}`` rather than raising. The
        alternative — propagating a ``JSONDecodeError`` — turns a recoverable
        "the model emitted malformed arguments" into a failed turn, and the
        tool executor already validates arguments against the tool's JSON
        Schema, where an empty object fails with a message naming the missing
        required fields. That message is actionable; a decode error is not.
        """
        calls: list[ToolCall] = []
        for index in sorted(self._by_index):
            slot = self._by_index[index]
            raw_args = slot["arguments"] or "{}"
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError:
                log.warning(
                    "llm.tool_call.unparseable_arguments",
                    tool=slot["name"],
                    length=len(raw_args),
                )
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=_domain_tool_name(slot["name"] or ""),
                    arguments=parsed,
                )
            )
        return calls


class OpenAICompatibleLLMAdapter:
    """``LLMPort`` over the OpenAI Chat Completions wire format.

    Satisfies :class:`~packages.core.ports.llm.LLMPort` structurally; callers
    type against the protocol and never against this class.
    """

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: SecretValue | str,
        model: str,
        capabilities: ProviderCapabilities,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 3,
        base_backoff_ms: int = 200,
        max_backoff_ms: int = 4_000,
        connect_timeout_s: float = 5.0,
    ) -> None:
        if not str(api_key):
            raise ConfigurationError(
                f"{provider} adapter constructed without an API key. "
                f"Selection is `build_llm_adapter`, which falls back to the "
                f"stub rather than building an unusable adapter."
            )
        self.provider = provider
        self._base_url = base_url.rstrip("/")
        self._api_key = (
            api_key if isinstance(api_key, SecretValue) else SecretValue(api_key)
        )
        self._model = model
        self._capabilities = capabilities
        self._max_attempts = max_attempts
        self._base_backoff_ms = base_backoff_ms
        self._max_backoff_ms = max_backoff_ms
        self._connect_timeout_s = connect_timeout_s
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            # No global timeout here: it is set per request from `timeout_ms`,
            # because a routing hop and a long-form answer have very different
            # budgets and one client-level value would have to satisfy both.
            timeout=httpx.Timeout(None),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )

    # ── Port surface ──────────────────────────────────────────────────────

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def count_tokens(self, messages: Sequence[Message]) -> int:
        """An approximation, and deliberately labelled as one.

        A real tokenizer means shipping a vocabulary file per provider and
        keeping it in step with model releases; DeepSeek does not publish one.
        The budget manager (03 §7.3) needs a *conservative* estimate to decide
        what to drop, so ~3.6 chars/token under-counts nothing important: it
        errs toward "this is bigger than you think", which drops one extra
        knowledge chunk rather than overflowing a context window mid-call.

        The authoritative counts come back on the response as
        :class:`~packages.core.ports.llm.Usage` and are what billing records.
        """
        chars = 0
        for message in messages:
            chars += len(message.content or "")
            for call in message.tool_calls:
                chars += len(call.name) + len(json.dumps(call.arguments))
            chars += 4  # per-message role/framing overhead
        return int(chars / 3.6) + 1

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
        body = self._build_body(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        if response_format is not None:
            body["response_format"] = response_format

        started = time.perf_counter()
        payload = await self._post_json(body, timeout_ms=timeout_ms)
        total_ms = int((time.perf_counter() - started) * 1000)

        choices = payload.get("choices") or []
        if not choices:
            raise PortUnavailableError(
                f"{self.provider} returned no choices",
                port="LLMPort",
                adapter=self.provider,
            )
        choice = choices[0]
        raw_message = choice.get("message") or {}

        accumulator = _ToolCallAccumulator()
        if raw_message.get("tool_calls"):
            accumulator.ingest(
                [
                    {"index": i, **call}
                    for i, call in enumerate(raw_message["tool_calls"])
                ]
            )
        tool_calls = accumulator.finish()

        # Same treatment on the non-streaming path: the tool loop in
        # `packages/ai/nodes.py` calls `chat`, and markup left here would be
        # appended to the message history and then re-emitted as speech on the
        # streamed pass.
        content = raw_message.get("content")
        if content:
            stripped = _strip_inline_tool_markup(content)
            if stripped != content:
                log.warning(
                    "llm.chat.inline_tool_markup_suppressed",
                    provider=self.provider,
                    model=self._model,
                )
            content = stripped

        return ChatResult(
            message=Message(
                role="assistant",
                content=content,
                tool_calls=tuple(tool_calls),
            ),
            tool_calls=tool_calls,
            usage=self._usage(payload.get("usage")),
            model=payload.get("model", self._model),
            provider=self.provider,
            finish_reason=_finish_reason(choice.get("finish_reason")),
            total_ms=total_ms,
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
        """Token-by-token generation.

        **Retry applies only before the first byte.** Once a token has been
        yielded, a retry would replay the prefix the caller already rendered —
        the user sees "Sorry youSorry you're in pain". So the retry loop wraps
        connection establishment, and a mid-stream failure raises. The caller
        (the SSE layer) emits a terminal ``error`` event; the client re-asks
        rather than the platform silently double-billing a duplicated turn.

        **Cancellation closes the socket.** ``httpx``'s ``stream`` context
        manager is exited on ``CancelledError``, which severs the connection
        and stops the provider generating. This is what makes voice barge-in
        actually stop costing money rather than merely stop being displayed.
        """
        body = self._build_body(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        # Ask for usage on the terminal chunk. Providers that do not understand
        # this option ignore it, and `_usage(None)` then yields zeros — so cost
        # accounting degrades to "unknown", never to "wrong".
        body["stream_options"] = {"include_usage": True}

        accumulator = _ToolCallAccumulator()
        open_indices: set[int] = set()
        usage: Usage | None = None
        finish: FinishReason = "stop"
        emitted_text = False
        # Every text delta goes through here before it can become a `token`
        # event. See `_ContentSanitizer` for why a stateless per-chunk filter
        # leaks the one character that starts the tag.
        sanitizer = _ContentSanitizer()

        response = await self._open_stream(body, timeout_ms=timeout_ms)
        try:
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    # A malformed frame is a provider bug. Dropping one frame
                    # loses at most one token; aborting the turn loses all of
                    # them, and the stream usually recovers on the next frame.
                    log.warning("llm.stream.malformed_frame", provider=self.provider)
                    continue

                if chunk.get("usage"):
                    usage = self._usage(chunk["usage"])

                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}

                    if raw_text := delta.get("content"):
                        if safe := sanitizer.feed(raw_text):
                            emitted_text = True
                            yield StreamDelta(kind="text", text=safe)

                    if raw_calls := delta.get("tool_calls"):
                        for index in accumulator.ingest(raw_calls):
                            open_indices.add(index)
                            yield StreamDelta(
                                kind="tool_call_start",
                                tool_call=accumulator.peek(index),
                            )
                        for raw in raw_calls:
                            index = int(raw.get("index", 0))
                            fragment = ((raw.get("function") or {}).get("arguments")) or ""
                            if fragment:
                                yield StreamDelta(
                                    kind="tool_call_delta",
                                    tool_call=accumulator.peek(index),
                                    partial_args=fragment,
                                )

                    if choice.get("finish_reason"):
                        finish = _finish_reason(choice["finish_reason"])

            if tail := sanitizer.flush():
                emitted_text = True
                yield StreamDelta(kind="text", text=tail)
            if sanitizer.suppressed:
                # A provider emitting tool calls as prose is a real event: it
                # means the model is not using the protocol, so a call we would
                # have executed was silently dropped instead. Worth an alert
                # rather than a silent fixup.
                log.warning(
                    "llm.stream.inline_tool_markup_suppressed",
                    provider=self.provider,
                    model=self._model,
                )

            for call in accumulator.finish():
                yield StreamDelta(kind="tool_call_end", tool_call=call)

            if usage is not None:
                yield StreamDelta(kind="usage", usage=usage)
            yield StreamDelta(kind="done", text=None)

        except asyncio.CancelledError:
            # Deliberate: close the socket so the provider stops generating,
            # then re-raise. Swallowing a cancellation here would leave the
            # caller's task believing it was cancelled while this generator
            # kept billing.
            log.info(
                "llm.stream.cancelled",
                provider=self.provider,
                emitted_text=emitted_text,
            )
            raise
        except httpx.HTTPError as exc:
            raise PortUnavailableError(
                f"{self.provider} stream failed mid-response: {type(exc).__name__}",
                port="LLMPort",
                adapter=self.provider,
                # A stream that already emitted text is NOT safely retryable by
                # the caller, and the flag says so rather than leaving it to be
                # inferred.
                retryable_by_caller=not emitted_text,
            ) from exc
        finally:
            await response.aclose()

        _ = finish  # finish_reason rides on `done` via the graph layer's state

    async def aclose(self) -> None:
        """Release the connection pool. Called from the app's lifespan."""
        if self._owns_client:
            await self._client.aclose()

    # ── Wire translation ──────────────────────────────────────────────────

    def _build_body(
        self,
        *,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] | None,
        tool_choice: dict[str, Any] | str,
        temperature: float,
        max_tokens: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [_encode_message(m) for m in messages],
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if tools:
            if not self._capabilities.supports_tools:
                raise ConfigurationError(
                    f"{self.provider} does not support tool calling, but "
                    f"{len(tools)} tools were supplied. The fallback router "
                    f"must serialize or refuse rather than send a request the "
                    f"provider will reject (03 §9.3)."
                )
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": _wire_tool_name(tool.name),
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
            body["tool_choice"] = tool_choice
        return body

    @staticmethod
    def _usage(raw: Mapping[str, Any] | None) -> Usage:
        if not raw:
            return Usage(prompt_tokens=0, completion_tokens=0)
        details = raw.get("prompt_tokens_details") or {}
        completion_details = raw.get("completion_tokens_details") or {}
        return Usage(
            prompt_tokens=int(raw.get("prompt_tokens", 0)),
            completion_tokens=int(raw.get("completion_tokens", 0)),
            # DeepSeek reports its context-cache hit under its own key; OpenAI
            # nests it under prompt_tokens_details. Both are read so cached
            # tokens are billed at the cached rate on either provider.
            cached_prompt_tokens=int(
                raw.get("prompt_cache_hit_tokens")
                or details.get("cached_tokens", 0)
                or 0
            ),
            reasoning_tokens=int(completion_details.get("reasoning_tokens", 0) or 0),
        )

    # ── Transport, retry, and errors ──────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key.reveal()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _timeout(self, timeout_ms: int) -> httpx.Timeout:
        seconds = timeout_ms / 1000.0
        return httpx.Timeout(
            connect=self._connect_timeout_s,
            # `read` is per-chunk, not per-response: a stream legitimately runs
            # longer than the budget while producing tokens the whole time. It
            # is the *silence* between tokens that indicates a stall, and that
            # is exactly what a read timeout measures.
            read=seconds,
            write=seconds,
            pool=seconds,
        )

    def _backoff_seconds(self, attempt: int) -> float:
        """Full-jitter exponential backoff.

        Full jitter (``random() * capped``) rather than "delay ± jitter":
        without it, N clients that failed against the same overloaded provider
        retry in lockstep and re-create the spike that caused the failure. The
        randomization spreads them, which is the entire purpose of the jitter.
        """
        capped = min(self._max_backoff_ms, self._base_backoff_ms * (2**attempt))
        return random.random() * capped / 1000.0  # noqa: S311 - jitter, not crypto

    async def _post_json(
        self, body: dict[str, Any], *, timeout_ms: int
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                response = await self._client.post(
                    f"{self._base_url}/chat/completions",
                    json=body,
                    headers=self._headers(),
                    timeout=self._timeout(timeout_ms),
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
            else:
                if response.status_code < 400:
                    return dict(response.json())
                last_error = self._status_error(response)
                if response.status_code not in _RETRYABLE_STATUSES:
                    raise last_error
            if attempt + 1 < self._max_attempts:
                await asyncio.sleep(self._backoff_seconds(attempt))

        raise self._exhausted(last_error)

    async def _open_stream(
        self, body: dict[str, Any], *, timeout_ms: int
    ) -> httpx.Response:
        """Establish the stream, retrying only until the first byte arrives.

        Returns an *open* response the caller must close. Built with
        ``send(stream=True)`` rather than the ``stream()`` context manager
        because the retry loop and the consumption loop live in different
        scopes, and a context manager spanning both would have to be entered
        inside the retry and exited outside it.
        """
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            request = self._client.build_request(
                "POST",
                f"{self._base_url}/chat/completions",
                json=body,
                headers={**self._headers(), "Accept": "text/event-stream"},
                timeout=self._timeout(timeout_ms),
            )
            try:
                response = await self._client.send(request, stream=True)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
            else:
                if response.status_code < 400:
                    return response
                await response.aread()
                last_error = self._status_error(response)
                await response.aclose()
                if response.status_code not in _RETRYABLE_STATUSES:
                    raise last_error
            if attempt + 1 < self._max_attempts:
                await asyncio.sleep(self._backoff_seconds(attempt))

        raise self._exhausted(last_error)

    def _status_error(self, response: httpx.Response) -> PortUnavailableError:
        """Map an HTTP status onto the domain error.

        The provider's own message is *not* forwarded to the client: it can
        echo prompt content, and prompt content is customer PII (contract §8).
        It is logged, where the masking sweep applies, and the caller gets the
        status.
        """
        detail = response.text[:400] if response.text else ""
        log.warning(
            "llm.request.failed",
            provider=self.provider,
            status=response.status_code,
            detail=detail,
        )
        retry_after = response.headers.get("retry-after")
        try:
            retry_after_s = int(retry_after) if retry_after else 2
        except ValueError:
            retry_after_s = 2
        return PortUnavailableError(
            f"{self.provider} returned HTTP {response.status_code}",
            port="LLMPort",
            adapter=self.provider,
            retry_after_s=retry_after_s,
            upstream_status=response.status_code,
        )

    def _exhausted(self, last_error: Exception | None) -> PortUnavailableError:
        if isinstance(last_error, PortUnavailableError):
            return last_error
        return PortUnavailableError(
            f"{self.provider} unreachable after {self._max_attempts} attempts: "
            f"{type(last_error).__name__ if last_error else 'unknown'}",
            port="LLMPort",
            adapter=self.provider,
        )


def _encode_message(message: Message) -> dict[str, Any]:
    """One port :class:`Message` → one wire message object."""
    encoded: dict[str, Any] = {"role": message.role}

    if message.content_parts:
        encoded["content"] = list(message.content_parts)
    else:
        # An assistant message that only requested tools has no text, and the
        # wire format wants an explicit null there rather than a missing key.
        encoded["content"] = message.content

    if message.name:
        # A `tool` message carries the tool's name, and it goes through the same
        # validator as the definition did — so it needs the same encoding. An
        # `assistant` message's `name` is an agent key, which is already legal;
        # the replace is a no-op there rather than a special case.
        encoded["name"] = _wire_tool_name(message.name)
    if message.tool_call_id:
        encoded["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        encoded["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    # Encoded for the same reason the tool *definitions* are:
                    # a replayed assistant message carrying a dotted name is
                    # rejected by the same validator.
                    "name": _wire_tool_name(call.name),
                    "arguments": json.dumps(call.arguments, separators=(",", ":")),
                },
            }
            for call in message.tool_calls
        ]
    return encoded

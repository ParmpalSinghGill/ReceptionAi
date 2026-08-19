"""No provider markup ever reaches a caller — the adapter's content filter.

A model sometimes emits a tool call as **inline text in the content stream**
instead of using the structured ``tool_calls`` field. Forwarded verbatim, that
markup is rendered to the caller as speech:

    DESK: Let me check the diary for Tuesday morning.

    <||DSML||tool_calls>
    <||DSML||invoke name="booking__check_availability">
    ...

A caller seeing XML is worse than a caller seeing an apology, and it leaks the
internal tool names the platform frame explicitly forbids revealing.

**The streaming cases are the ones that matter.** A whole-message filter is easy
and a stateless per-chunk filter is subtly broken: deltas split at arbitrary
boundaries, so a lone ``"<"`` arrives in one chunk and the rest in the next, and
the naive filter passes the one character that starts the tag. Every test below
that says "split" exists because of that.
"""

from __future__ import annotations

import pytest
from packages.providers.llm.openai_compatible import (
    _ContentSanitizer,
    _strip_inline_tool_markup,
)

#: The fullwidth vertical line DeepSeek fences its pseudo-tags with, U+FF5C.
#: Written as an escape so this file stays pure ASCII — the character is
#: indistinguishable from a normal pipe in most editors, which is exactly how a
#: later edit would silently stop testing the real thing.
PIPE = "\uff5c"

#: Verbatim from a real conversation, reconstructed with the fullwidth pipes.
REAL_LEAK = (
    "Let me check the diary for Tuesday morning.\n\n"
    f"<{PIPE}{PIPE}DSML{PIPE}{PIPE}tool_calls>\n"
    f"<{PIPE}{PIPE}DSML{PIPE}{PIPE}invoke name=\"booking__check_availability\">\n"
    f"<{PIPE}{PIPE}DSML{PIPE}{PIPE}parameter name=\"service_code\" "
    f"string=\"true\">checkup</{PIPE}{PIPE}DSML{PIPE}{PIPE}parameter>"
)

#: Every marker that must never survive. Asserted as a set on every output in
#: this file rather than per-test, so a new test cannot forget the check.
FORBIDDEN = (f"<{PIPE}", PIPE, "DSML", "<|", "booking__")


def assert_clean(text: str) -> None:
    for marker in FORBIDDEN:
        assert marker not in text, (
            f"{marker!r} reached the caller in {text!r}. No provider markup may "
            f"ever be emitted as speech."
        )


def stream(chunks: list[str]) -> tuple[str, bool]:
    """Feed chunks through the sanitizer as the adapter does."""
    sanitizer = _ContentSanitizer()
    out = "".join(sanitizer.feed(chunk) for chunk in chunks)
    return out + sanitizer.flush(), sanitizer.suppressed


def test_a_whole_message_is_truncated_at_the_first_delimiter() -> None:
    """The non-streaming path — ``chat()``, which the tool loop uses.

    Truncated, not excised: the text *between* the tags is argument values, so
    removing only the tags leaves ``checkup</`` in the caller's ear.
    """
    cleaned = _strip_inline_tool_markup(REAL_LEAK)
    assert cleaned == "Let me check the diary for Tuesday morning."
    assert_clean(cleaned)
    assert "checkup" not in cleaned, (
        "An argument value survived. Stripping tags individually is not enough — "
        "the whole tail after a delimiter belongs to the pseudo-call."
    )


def test_the_leak_is_suppressed_when_it_arrives_in_one_chunk() -> None:
    out, suppressed = stream([REAL_LEAK])
    assert out.strip() == "Let me check the diary for Tuesday morning."
    assert suppressed
    assert_clean(out)


def test_the_leak_is_suppressed_when_split_across_chunks() -> None:
    """The realistic case. The delimiter straddles a chunk boundary.

    A stateless per-chunk filter emits the lone ``"<"`` here and strips the
    rest — leaving the caller looking at a stray angle bracket, which is how this
    class of bug survives a whole-message test.
    """
    out, suppressed = stream(
        ["Let me check. ", "<", f"{PIPE}{PIPE}DSM", f"L{PIPE}{PIPE}tool_calls>"]
    )
    assert out == "Let me check. "
    assert suppressed
    assert_clean(out)


def test_the_leak_is_suppressed_character_by_character() -> None:
    """The worst possible chunking — one character per delta.

    Not realistic, but it is the strongest statement of the property: there is no
    split point at which any part of the markup escapes.
    """
    out, suppressed = stream(list(REAL_LEAK))
    assert out.strip() == "Let me check the diary for Tuesday morning."
    assert suppressed
    assert_clean(out)


def test_the_ascii_spelling_is_suppressed_too() -> None:
    """Several open-weight models use ``<|...|>`` for the same idea.

    Matching the delimiter *shape* rather than the literal token name is what
    makes this hold for a provider we have not met yet.
    """
    out, suppressed = stream(["Checking. ", "<|tool_call|>", '{"name": "x"}'])
    assert out == "Checking. "
    assert suppressed
    assert_clean(out)


@pytest.mark.parametrize(
    "text",
    [
        "We're open 9<->5 on Saturday.",
        "A check-up is < £50.",
        "Use <b>bold</b> if you like.",
        "The cost is <£40 for children.",
        "3 < 5 and 5 > 3.",
    ],
)
def test_ordinary_text_containing_angle_brackets_passes_through(text: str) -> None:
    """The filter must not eat legitimate content.

    A sanitizer that swallows every ``<`` is a different bug with the same
    symptom — a caller missing part of a sentence. These are all things a
    practice might genuinely say.
    """
    out, suppressed = stream(list(text))
    assert out == text
    assert not suppressed


def test_a_trailing_angle_bracket_is_still_emitted() -> None:
    """A held-back fragment that never becomes a delimiter must be released.

    Otherwise the last character of a reply is silently dropped whenever it
    happens to be ``<``, which would be a very confusing intermittent truncation.
    """
    out, suppressed = stream(["The price is <"])
    assert out == "The price is <"
    assert not suppressed


def test_nothing_is_held_back_indefinitely() -> None:
    """Text after a bare ``<`` is emitted, not buffered forever.

    The hold-back is bounded to a delimiter's own prefix; a ``<`` followed by an
    ordinary letter is resolved on the very next chunk.
    """
    out, _ = stream(["a<", "b", "c"])
    assert out == "a<bc"


async def test_no_markup_reaches_a_token_delta_from_the_real_adapter() -> None:
    """End to end through ``OpenAICompatibleLLMAdapter.stream``.

    The unit tests above exercise the sanitizer; this exercises the **wiring**.
    A perfect filter that the stream path forgets to call is the failure mode
    this catches, and it is the one that actually shipped — the sanitizer is
    only useful if every ``text`` delta goes through it.

    The provider is faked at the HTTP layer rather than mocked at the adapter
    boundary, so the real SSE parsing, the real accumulator and the real content
    path all run.
    """
    import httpx
    from packages.core.ports.llm import ProviderCapabilities
    from packages.providers.llm.openai_compatible import OpenAICompatibleLLMAdapter

    def sse(payload: str) -> bytes:
        return f"data: {payload}\n\n".encode()

    body = b"".join(
        [
            sse('{"choices":[{"delta":{"content":"Let me check. "}}]}'),
            sse('{"choices":[{"delta":{"content":"<"}}]}'),
            sse(f'{{"choices":[{{"delta":{{"content":"{PIPE}{PIPE}DSM"}}}}]}}'),
            sse(f'{{"choices":[{{"delta":{{"content":"L{PIPE}{PIPE}tool_calls>"}}}}]}}'),
            sse('{"choices":[{"delta":{"content":"checkup"}}]}'),
            b"data: [DONE]\n\n",
        ]
    )

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )
    )
    adapter = OpenAICompatibleLLMAdapter(
        provider="fake",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        model="fake-1",
        capabilities=ProviderCapabilities(
            supports_tools=True,
            supports_parallel_tools=True,
            supports_streaming_tools=True,
            supports_json_schema=True,
            supports_prompt_caching=False,
            context_window=8192,
            max_output_tokens=1024,
        ),
        client=httpx.AsyncClient(transport=transport),
    )

    tokens = [
        delta.text or ""
        async for delta in adapter.stream(messages=[])
        if delta.kind == "text"
    ]
    await adapter.aclose()

    spoken = "".join(tokens)
    assert spoken == "Let me check. "
    assert_clean(spoken)
    for token in tokens:
        assert_clean(token)


def test_suppression_is_reported_so_it_can_be_alerted_on() -> None:
    """A provider bypassing the tool protocol is a real event, not a tidy-up.

    A call we would have executed was silently dropped instead — the caller asked
    for availability and no availability was ever checked. The flag is what makes
    that visible rather than merely invisible-and-fixed.
    """
    _, suppressed = stream([REAL_LEAK])
    assert suppressed

    _, clean = stream(["Nothing unusual here."])
    assert not clean

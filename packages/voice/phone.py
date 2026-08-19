"""Reassembling a phone number that STT delivered in pieces.

**The failure this fixes, observed on a real call.** Asked for a number, the
caller says "zero seven seven zero zero nine zero zero five five two" with the
natural pauses people put between digit groups. Silero closes a turn on each
pause, so the agent receives four separate utterances — "zero seven seven",
"zero zero nine", "zero zero", "five five two" — and answers the first one
before the rest arrive. The caller hears "could you give me the rest of your
number?" after they already said it, twice, and eventually gives up.

The fix is to hold digit-only fragments briefly instead of sending each one to
the agent, and flush when the number looks complete or the caller stops adding
to it. It operates purely on transcript text: no VAD change, no mute change, so
it cannot affect barge-in or turn-taking.

This module is deliberately vendor-free and pure — reassembly is a text problem,
and keeping it here makes it testable without a pipeline, an audio device, or a
provider account.
"""

from __future__ import annotations

import re

__all__ = [
    "FRAGMENT_FLUSH_SECONDS",
    "digits_in",
    "is_complete_number",
    "looks_like_phone_fragment",
    "wants_phone_number",
]

#: How long to wait for more digits before giving the agent what we have.
#:
#: **Measured, not guessed.** On a real call the groups of a spoken number
#: reached this process 2.4s apart — a ~0.9s pause between groups, plus the
#: turn-stop timeout and Deepgram's finalization latency on top. A 1.15s window
#: (which is what the pipeline this was ported from used, under a different VAD
#: configuration) expired between every group, so each one was sent to the agent
#: alone and the caller was asked for "the rest of your number" three times.
#:
#: 3s covers the observed gap with margin. The cost of being too generous is
#: bounded and small: a caller who has genuinely finished waits up to 3s, and
#: only when the number is *incomplete* — a complete one flushes immediately.
FRAGMENT_FLUSH_SECONDS = 3.0

#: Spoken digits, including the two ways people say zero.
_SPOKEN_DIGIT: dict[str, str] = {
    "zero": "0", "oh": "0", "o": "0", "nought": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
}  # fmt: skip

#: Words that routinely appear inside a spoken number without being part of it.
_FILLER = frozenset(
    {
        "a", "and", "double", "triple", "full", "is", "it", "it's", "its",
        "my", "number", "phone", "mobile", "please", "so", "that's", "thats",
        "the", "uh", "um", "yes", "sure", "okay", "ok",
    }
)  # fmt: skip

#: A UK mobile/landline is 10-11 digits. Below this a number cannot be whole,
#: so the buffer keeps waiting; at or above it, send immediately rather than
#: making a caller who spoke fluently wait out the timer.
_COMPLETE_MIN_DIGITS = 10
_COMPLETE_MAX_DIGITS = 15

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def digits_in(text: str) -> str:
    """Extract digits from spoken or written text.

    Handles "double seven" and "triple four", which callers use constantly and
    which a naive digit filter silently collapses to one digit.
    """
    tokens = _TOKEN_RE.findall(str(text or "").lower())
    out: list[str] = []
    repeat = 1
    for word in tokens:
        if word in ("double", "triple"):
            repeat = 2 if word == "double" else 3
            continue
        if word.isdigit():
            out.append(word if repeat == 1 else word * repeat)
            repeat = 1
            continue
        mapped = _SPOKEN_DIGIT.get(word)
        if mapped is not None:
            out.append(mapped * repeat)
            repeat = 1
            continue
        repeat = 1
    return "".join(out)


def is_complete_number(digits: str) -> bool:
    """Whether this many digits can stand as a whole phone number."""
    return _COMPLETE_MIN_DIGITS <= len(digits) <= _COMPLETE_MAX_DIGITS


def wants_phone_number(bot_text: str) -> bool:
    """Whether the agent's last reply was asking for a number.

    Buffering is applied *only* while a number was requested. Without that
    guard, "I'd like an appointment at two" would be held back as a fragment,
    which would make every call feel laggy to fix a problem that only happens
    on one question.
    """
    lowered = str(bot_text or "").lower()
    return any(
        word in lowered
        for word in ("phone", "mobile", "number", "digits", "contact")
    )


def looks_like_phone_fragment(text: str) -> bool:
    """Whether an utterance is an incomplete run of digits and nothing else.

    A complete number returns ``False`` so it goes straight to the agent. Any
    real words beyond the known fillers also return ``False``: "yes that's my
    number, thanks" is a sentence, and holding it back would stall the call.
    """
    raw = str(text or "").strip()
    if not raw:
        return False

    digits = digits_in(raw)
    # One stray digit ("I need it at 3") is not someone reciting a number.
    if len(digits) < 2:
        return False
    if is_complete_number(digits):
        return False

    tokens = _TOKEN_RE.findall(raw.lower())
    index = 0
    while index < len(tokens):
        word = tokens[index]
        if word in ("double", "triple"):
            following = tokens[index + 1] if index + 1 < len(tokens) else ""
            if following in _SPOKEN_DIGIT or (
                following.isdigit() and len(following) == 1
            ):
                index += 2
                continue
            return False
        if word in _SPOKEN_DIGIT or word.isdigit() or word in _FILLER:
            index += 1
            continue
        # A word that is neither a digit nor filler: this is speech, not a
        # number being read out.
        return False
    return True

"""Content masking for traces — 05 §B6.2, §D14.5.

This is the module that decides what a prompt looks like once a tenant has
*not* opted into full capture. It is deliberately separate from the adapter:
the masking rules are platform policy and must be identical whether the backend
is Langfuse, an OTLP collector, or a log line, and a rule that lives inside one
vendor's adapter is a rule the next vendor's adapter reimplements slightly
differently.

**Two layers, because one is not enough.**

1. :func:`mask_content` replaces content wholesale with a structural summary
   when the policy says so. This is the control.
2. :func:`scrub` runs regex redaction over whatever is about to be recorded
   *even under full capture*. This is the backstop, and it is the reason a
   tenant who opts in still does not get a credit-card number in their
   dashboard. Opting into prompt capture is consent to see the conversation,
   not consent to store a payment instrument.

The regexes are the same family as 05 §D14.5's collector ``redaction/values``
block, kept in sync deliberately: the collector catches what the SDK misses
(third-party instrumentation), and the SDK catches what never reaches the
collector (Langfuse ingests over its own HTTP API, not through the collector).
"""

from __future__ import annotations

import re
from typing import Any, Final

from packages.core.ports.tracing import Capture, TracingPolicy

__all__ = ["REDACTED", "mask_content", "scrub", "summarize"]

REDACTED: Final = "[redacted]"

#: Ordered most-specific-first: an email contains digits and a naive phone
#: pattern would otherwise eat half of one and leave a fragment, which is worse
#: than either redacting or keeping it whole.
_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("jwt", re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}")),
    ("card", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    # Indian mobile numbers are 10 digits and frequently written with a +91
    # prefix, a space, or a hyphen. The demo tenant is in Pune, so this is the
    # shape that actually shows up in the transcripts this masks.
    ("phone", re.compile(r"\+?\d[\d\s().-]{7,}\d")),
    # Long digit runs that are not obviously any of the above: policy numbers,
    # patient ids, anything a caller reads out.
    ("digits", re.compile(r"\b\d{6,}\b")),
)


def scrub(text: str) -> str:
    """Redact identifier-shaped substrings from free text.

    Runs unconditionally on anything recorded, including under
    ``Capture.FULL``. See the module docstring for why full capture is not a
    licence to store a card number.
    """
    if not text:
        return text
    out = text
    for label, pattern in _PATTERNS:
        out = pattern.sub(f"[{label}]", out)
    return out


def summarize(value: Any) -> dict[str, Any]:
    """A structural description of content, carrying no content.

    What survives masking: the *shape* of the turn. How many messages, what
    roles, how long each was. That is enough to read a latency waterfall and
    see "the prompt was 8,000 characters and grew by 3,000 this turn", which is
    most of what an engineer debugging a slow turn actually needs, while
    carrying nothing a person said.
    """
    if value is None:
        return {"redacted": True}

    if isinstance(value, str):
        return {"redacted": True, "chars": len(value)}

    if isinstance(value, list):
        roles: dict[str, int] = {}
        chars = 0
        for item in value:
            role = "unknown"
            if isinstance(item, dict):
                role = str(item.get("role", "unknown"))
                chars += len(str(item.get("content") or ""))
            else:
                role = str(getattr(item, "role", "unknown"))
                chars += len(str(getattr(item, "content", "") or ""))
            roles[role] = roles.get(role, 0) + 1
        return {
            "redacted": True,
            "messages": len(value),
            "roles": roles,
            "chars": chars,
        }

    if isinstance(value, dict):
        return {"redacted": True, "keys": sorted(str(k) for k in value)}

    return {"redacted": True, "type": type(value).__name__}


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [truncated {len(text) - limit} chars]"


def _capture_one(value: Any, policy: TracingPolicy) -> Any:
    """Apply full-capture rules to a single value: scrub, then truncate."""
    if isinstance(value, str):
        return _truncate(scrub(value), policy.max_chars)

    if isinstance(value, list):
        return [_capture_one(item, policy) for item in value]

    if isinstance(value, dict):
        return {k: _capture_one(v, policy) for k, v in value.items()}

    # A Message dataclass or similar. Render the two fields that matter and
    # nothing else — an attribute sweep would pick up whatever a future field
    # holds, which is exactly the open-ended capture this module exists to
    # prevent.
    role = getattr(value, "role", None)
    content = getattr(value, "content", None)
    if role is not None or content is not None:
        out: dict[str, Any] = {"role": str(role) if role else "unknown"}
        if content:
            out["content"] = _truncate(scrub(str(content)), policy.max_chars)
        tool_calls = getattr(value, "tool_calls", ()) or ()
        if tool_calls:
            # Tool *names* only. Arguments hold whatever the model extracted
            # from the conversation, which for a medical intake is health data
            # (05 §D14.5 forbids `ai.tool.arguments` outright).
            out["tool_calls"] = [
                str(getattr(call, "name", "unknown")) for call in tool_calls
            ]
        return out

    return _truncate(scrub(str(value)), policy.max_chars)


def mask_content(value: Any, policy: TracingPolicy) -> Any | None:
    """The single decision point for whether content is recorded.

    Returns ``None`` when nothing at all may be recorded, so a caller can omit
    the field entirely rather than sending a null the backend renders as an
    empty prompt.

    Every path through this function is exercised by the tests in
    ``tests/unit/test_tracing_pii.py``, which assert on the *output* rather
    than on the branch taken — the question that matters is whether a phone
    number can appear, not which ``if`` ran.
    """
    if policy.capture is Capture.NONE:
        return None
    if policy.capture is Capture.MASKED:
        return summarize(value)
    return _capture_one(value, policy)

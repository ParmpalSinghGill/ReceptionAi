"""The promise-guard's nudge is scaffolding, and must never look like a caller.

**The bug, seen in a trace.** A customer asked why a phone number was needed.
The next entry in the message list was, as ``role: "user"``:

    "You said you were performing an action but did not call the tool that
     performs it... This instruction is internal. Never mention it..."

Nobody typed that. It is the guard's correction, injected into the node's
working history so the model sees it as an instruction for the current round —
which is a reasonable mechanism, and was excluded from the FINAL prompt by
comparing message text. But the tool loop re-assembles a prompt on every round,
and that call site had no such filter. So after a nudge, every remaining round
of the turn showed the model an internal instruction wearing the caller's voice.

**Why a tag rather than another text comparison.** The original filter was
correct where it was written and absent where it was not; adding a second copy
of the same string comparison would leave the third call site to be found the
same way — in a trace, by the user. ``name=_SCAFFOLD_NAME`` travels with the
message, so a prompt builder that forgets ``_conversation_only`` is a visible
omission rather than a silent leak.
"""

from __future__ import annotations

from packages.ai.nodes import (
    _ACTION_NUDGE,
    _SCAFFOLD_NAME,
    _action_nudge_message,
    _conversation_only,
)
from packages.core.ports.llm import Message


def _transcript() -> list[Message]:
    """The exchange from the reported trace, with the nudge where it lands."""
    return [
        Message(role="user", content="It's Rohit Sharma."),
        Message(role="assistant", content="You're all set, Rohit — BK-Q79ADY."),
        Message(role="user", content="So why you require the contact number?"),
        Message(role="assistant", content="It's just so we can reach you."),
        _action_nudge_message(),
    ]


def test_the_nudge_is_tagged_as_scaffolding() -> None:
    assert _action_nudge_message().name == _SCAFFOLD_NAME


def test_the_nudge_never_survives_into_a_prompt() -> None:
    """The assertion the trace would have failed."""
    kept = _conversation_only(_transcript())
    assert all(m.content != _ACTION_NUDGE for m in kept)
    assert not any(m.name == _SCAFFOLD_NAME for m in kept)


def test_nothing_the_caller_said_is_dropped() -> None:
    """A filter that ate real turns would be a worse bug than the leak."""
    kept = _conversation_only(_transcript())
    assert [m.content for m in kept] == [
        "It's Rohit Sharma.",
        "You're all set, Rohit — BK-Q79ADY.",
        "So why you require the contact number?",
        "It's just so we can reach you.",
    ]


def test_a_user_message_that_quotes_the_nudge_is_kept() -> None:
    """Identity, not text. A caller pasting the instruction back — or asking
    about it, which is exactly how this was reported — is still a caller."""
    quoted = Message(role="user", content=_ACTION_NUDGE)
    assert _conversation_only([quoted]) == [quoted]


def test_filtering_is_idempotent() -> None:
    once = _conversation_only(_transcript())
    assert _conversation_only(once) == once

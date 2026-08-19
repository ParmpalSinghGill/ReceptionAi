"""``VoiceEngine`` — the seam between the media shell and the AI (04 §2.2).

The naive abstraction is one fat interface that both a composed STT→LLM→TTS
pipeline and a realtime speech-to-speech provider implement. That fails, because
the two modes disagree about *what the unit of work is*: composed's unit is a
text turn, realtime's is a continuous audio session. Forcing realtime to expose
partial transcripts as if it were an STT provider produces a leaky, lying API.

So the seam sits one level up. Both modes consume inbound audio frames and
produce outbound audio frames plus a stream of observability/control events.
The session shell — ingress, egress pacing, recording, transfer, metrics — is
identical for both; only the engine differs.

**Nothing in this module imports a vendor SDK, and nothing may.** Pipecat is an
implementation detail of one engine, not a dependency of the platform: the
composed engine that happens to be built on it lives in ``packages/providers/``
behind this interface. That is what keeps ``tools/check_layering.py`` honest and
what makes the tradeoff table in 04 §2.3 a real choice rather than marketing.

.. warning::

   **Status: declared, not yet implemented.** No class in the tree subclasses
   :class:`VoiceEngine` today. The shipped composed path
   (``packages.providers.voice.pipecat_composed``) runs *Pipecat's own*
   pipeline loop, which already owns frame transport, pacing and barge-in —
   wrapping it in ``push_audio``/``audio_out`` right now would mean draining
   its output into our queues and feeding it back, which adds a copy and a
   failure mode while removing nothing.

   What the interface buys today is the **boundary**: the app tier and the
   platform depend on this module, never on Pipecat, which is enforced by
   ``tests/architecture/test_voice_engine_seam.py``. The interface earns its
   keep when the second engine lands (realtime speech-to-speech), because that
   is the point at which the shell needs one shape to talk to. Implementing it
   before then would be a wrapper with exactly one implementation, written
   against a hypothetical second — which is how abstractions end up fitting
   neither.

   Recorded plainly rather than left to be discovered: an ABC nobody
   implements reads as either dead code or a lie about the architecture, and
   the honest third option is a dated note saying which it is.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

__all__ = ["AudioFrame", "EngineEvent", "EngineEventKind", "VoiceEngine"]


@dataclass(frozen=True, slots=True)
class AudioFrame:
    """One frame of mono PCM16 or μ-law, plus its wall-clock position.

    ``seq`` and ``monotonic_ms`` are carried rather than derived because the
    shell needs to detect gaps and reorder: a jitter buffer that cannot tell a
    late frame from a dropped one will smooth over silence it should have
    concealed (04 §6.2).
    """

    payload: bytes
    sample_rate: int
    encoding: Literal["pcm16", "mulaw"]
    seq: int
    monotonic_ms: int


#: The event vocabulary. Frozen as a Literal rather than an enum so an engine
#: cannot invent a kind the shell has no branch for — a typo becomes a type
#: error instead of a metric that silently never fires.
EngineEventKind = Literal[
    "user_speech_started",  # barge-in trigger
    "user_speech_stopped",
    "user_transcript_partial",
    "user_transcript_final",
    "assistant_text_delta",
    "assistant_audio_started",
    "assistant_turn_complete",
    "assistant_interrupted",
    "tool_call_started",
    "tool_call_finished",
    "error",
]


@dataclass(frozen=True, slots=True)
class EngineEvent:
    """Everything the shell needs to know without owning the pipeline."""

    kind: EngineEventKind
    text: str | None = None
    turn_id: str | None = None
    latency_ms: int | None = None
    detail: dict[str, object] | None = None


class VoiceEngine(abc.ABC):
    """The seam between the media shell and the AI.

    Implementations: the composed STT+LLM+TTS engine, and a realtime
    speech-to-speech engine wrapping a single provider session.
    """

    @abc.abstractmethod
    async def start(self) -> None:
        """Open provider connections. Must be idempotent."""

    @abc.abstractmethod
    async def push_audio(self, frame: AudioFrame) -> None:
        """Feed one inbound frame. Must never block on network I/O.

        A blocking implementation here stalls the ingress read loop, which is
        the one loop that cannot afford to stall: frames arrive whether or not
        we are ready for them, and the backlog is unbounded.
        """

    @abc.abstractmethod
    def audio_out(self) -> AsyncIterator[AudioFrame]:
        """Outbound audio. Yields until the engine is closed."""

    @abc.abstractmethod
    def events(self) -> AsyncIterator[EngineEvent]:
        """Control/observability stream. Drives metrics, transcripts, UI."""

    @abc.abstractmethod
    async def interrupt(self) -> None:
        """Barge-in: stop speaking now, discard pending output, keep context."""

    @abc.abstractmethod
    async def inject_text(self, text: str, *, speak: bool = True) -> None:
        """Say something the model did not produce.

        Greetings, hold messages and transfer announcements are the shell's
        words, not the agent's, and must not enter the model's context as if
        the model had said them.
        """

    @abc.abstractmethod
    async def aclose(self) -> None:
        """Release all provider resources. Must be safe to call twice."""

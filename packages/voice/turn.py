"""Turn-taking policy — vendor-free, so the lessons survive an engine swap.

The numbers here are not defaults picked from a docs page. Each one is the
residue of a specific failure in a working deployment, and the comments say
which, because a bare constant invites someone to "tidy" it back to the value
that broke.

The *mechanism* that applies these lives in the engine adapter (Pipecat
strategies, in the composed engine). The *policy* lives here so that replacing
Pipecat does not also silently replace the debugging behind these values.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["TurnPolicy", "clamp_stop_secs"]

#: Floor and ceiling for end-of-turn silence. Below 0.1s ordinary inter-word
#: pauses end the turn and the caller is cut off mid-sentence; above 3s the
#: agent feels broken and callers start repeating themselves.
_STOP_SECS_MIN = 0.1
_STOP_SECS_MAX = 3.0

#: Matches stock VAD. Long enough to survive a breath, short enough to answer.
_STOP_SECS_DEFAULT = 0.2


def clamp_stop_secs(stop_secs: float | None) -> float:
    """Coerce configured end-of-turn silence into the workable range.

    Tolerates ``None`` and junk rather than raising: this value can arrive from
    tenant configuration, and a bad row must degrade to the default instead of
    failing a call at connect time.
    """
    try:
        stop = float(stop_secs) if stop_secs is not None else _STOP_SECS_DEFAULT
    except (TypeError, ValueError):
        stop = _STOP_SECS_DEFAULT
    return max(_STOP_SECS_MIN, min(_STOP_SECS_MAX, round(stop, 2)))


@dataclass(frozen=True, slots=True)
class TurnPolicy:
    """When a caller has finished speaking, and what may interrupt whom."""

    #: VAD speech probability. 0.7 is stock; lowering it makes keyboard noise
    #: and breathing open turns.
    confidence: float = 0.7

    #: Speech must persist this long before a turn opens — suppresses coughs
    #: and door clicks.
    start_secs: float = 0.2

    #: Silence before the turn closes. See :func:`clamp_stop_secs`.
    stop_secs: float = _STOP_SECS_DEFAULT

    #: Minimum RMS for speech. Guards against a hot mic treating room tone as
    #: speech and never closing a turn.
    min_volume: float = 0.6

    #: Start a turn from a finalized STT transcript when VAD produced no
    #: ``user_started_speaking`` at all.
    #:
    #: Real failure: calls showed a final transcript in the UI with **zero**
    #: turn-start events — Silero never opened a turn for soft speech, so the
    #: stop strategy never ran and the agent never fired. The caller spoke and
    #: nothing whatsoever happened.
    soft_speech_start: bool = True

    #: Mute the mic while the bot speaks, and unmute on interruption.
    #:
    #: Two failures in one flag. Without muting, speaker echo barges in and
    #: cuts the second half of a multi-sentence reply. With naive muting that
    #: only clears on "bot stopped speaking", an interrupted reply never sends
    #: that frame and the mic stays muted for the rest of the call — pin-drop
    #: silence.
    mute_while_bot_speaks: bool = True

    #: Block a late transcript from opening a turn while the bot is speaking or
    #: the agent is mid-turn. Delayed STT of *earlier* speech was starting a new
    #: turn mid-TTS and cutting both the audio and the on-screen reply.
    block_turn_start_while_busy: bool = True

    @classmethod
    def from_stop_secs(cls, stop_secs: float | None) -> TurnPolicy:
        """The common case: everything stock except end-of-turn silence."""
        return cls(stop_secs=clamp_stop_secs(stop_secs))

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

from dataclasses import dataclass, replace
from enum import StrEnum

__all__ = [
    "STOP_SECS_DEFAULT",
    "TurnDetectionConfig",
    "TurnDetector",
    "TurnPolicy",
    "clamp_stop_secs",
]

#: Floor and ceiling for end-of-turn silence. Below 0.1s ordinary inter-word
#: pauses end the turn and the caller is cut off mid-sentence; above 3s the
#: agent feels broken and callers start repeating themselves.
_STOP_SECS_MIN = 0.1
_STOP_SECS_MAX = 3.0

#: End-of-turn silence when nothing configures it.
#:
#: Was 0.2s — stock Silero — and that cut callers off mid-thought. A real call:
#: "No, it can be a general check-up." *(pause under a second)* "No emergency."
#: The 0.2s stop closed the turn inside that pause, so the first half went to
#: the agent on its own and the second half arrived while the reply was being
#: generated — two user turns on screen, then two near-identical replies read
#: out back to back. 0.5s covers an ordinary mid-sentence pause while keeping
#: the reply snappy; it costs ~0.3s of reply latency against 3–6s of inference.
#: A pause longer than this still splits the utterance — and that case is
#: handled downstream: the voice bridge discards the unheard reply, retracts
#: it from history and answers the caller's complete thought as one message.
#:
#: Public rather than private because three layers (policy, voice config, the
#: media worker's request model) must agree on it, and three literal copies of
#: one number are how they stop agreeing.
STOP_SECS_DEFAULT = 0.5
_STOP_SECS_DEFAULT = STOP_SECS_DEFAULT


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


# ── End-of-turn detection: which method decides the caller has finished ──────


class TurnDetector(StrEnum):
    """How the end of a caller's turn is decided. Selectable per call.

    Every detector keeps Silero VAD running, even the ones that do not use it
    to decide: VAD is what the latency metrics measure "the caller's last word"
    against, and measuring all four methods against the same clock is what
    makes their numbers comparable.
    """

    #: Fixed silence after VAD stop. The production setting before detectors
    #: were selectable, kept unchanged as the baseline.
    SILENCE = "silence"

    #: Pipecat Smart Turn v3 — an audio model that reads intonation and
    #: rhythm at each pause and answers "finished" or "not yet".
    SMART_TURN = "smart_turn"

    #: Voice Activity Projection (Ekstedt & Skantze 2022) — predicts who
    #: speaks next over the coming two seconds; a predicted shift to the agent
    #: ends the caller's turn. Needs ``tools/setup_vap.py`` and PyTorch.
    VAP = "vap"

    #: Deepgram Flux — speech-to-text with its own end-of-turn model, so
    #: transcription and turn detection come from one service.
    FLUX = "flux"


#: Silero stop for the model-based detectors. Short on purpose: it only marks
#: the pause at which the model is asked, and the model — not this number —
#: decides. 0.2s is the value Smart Turn was trained against.
MODEL_VAD_STOP_SECS = 0.2

#: Longest pause a model-based detector may call "not finished" before the
#: turn is ended anyway. Covers the ~0.9s gaps between spoken digit groups
#: with room, without leaving a caller who has really finished waiting long.
MAX_SILENCE_SECS_DEFAULT = 2.0


@dataclass(frozen=True, slots=True)
class TurnDetectionConfig:
    """Which detector runs, and the few numbers each one needs.

    Resolved once per call. The defaults reproduce the pre-existing behaviour
    exactly (``silence``, no speculation), so a deployment that sets nothing
    sees no change.
    """

    detector: TurnDetector = TurnDetector.SILENCE

    #: Start the agent on an *early* end-of-turn signal, before the detector
    #: has confirmed it. A confirmed turn whose text matches reuses the work;
    #: the caller resuming cancels it. Booking writes wait for confirmation.
    speculative: bool = False

    #: Ceiling on a model detector's "not finished yet" (smart_turn, vap).
    max_silence_secs: float = MAX_SILENCE_SECS_DEFAULT

    #: VAP: probability that the agent speaks next above which the caller's
    #: turn is over.
    vap_threshold: float = 0.5

    #: Flux model and its two confidence thresholds. ``eager`` is the early
    #: signal speculation starts on; it is only sent when speculation is on.
    #: Where ``tools/setup_vap.py`` put the VAP code and weights.
    vap_model_dir: str = "~/.receptionai-models/vap"

    flux_model: str = "flux-general-en"
    flux_eot_threshold: float = 0.7
    flux_eager_eot_threshold: float = 0.5

    @classmethod
    def parse(
        cls,
        detector: str | TurnDetector | None,
        *,
        speculative: bool | None = None,
        base: TurnDetectionConfig | None = None,
    ) -> TurnDetectionConfig:
        """Overlay a detector name and speculation flag on a base config.

        Raises ``ValueError`` on an unknown detector name, so a typo in an
        environment variable fails at startup rather than silently running the
        baseline and labelling its rows with the wrong detector.
        """
        cfg = base or cls()
        if detector is not None and str(detector).strip():
            cfg = replace(cfg, detector=TurnDetector(str(detector).strip().lower()))
        if speculative is not None:
            cfg = replace(cfg, speculative=bool(speculative))
        return replace(
            cfg,
            max_silence_secs=max(0.3, min(5.0, float(cfg.max_silence_secs))),
            vap_threshold=max(0.05, min(0.95, float(cfg.vap_threshold))),
        )

    @property
    def uses_model_vad_stop(self) -> bool:
        """Whether VAD's stop is a short "ask the model" pause."""
        return self.detector in (TurnDetector.SMART_TURN, TurnDetector.VAP)

    @property
    def label(self) -> str:
        """What a metrics row calls this configuration."""
        return f"{self.detector.value}{'+spec' if self.speculative else ''}"

"""The composed voice engine, built on Pipecat + Deepgram.

``Deepgram STT (nova-3) → platform LangGraph agent → Deepgram TTS (aura-2)``.

This module is the *only* place in the platform that knows Pipecat exists, which
is what 04 §2.2 means by "Pipecat is an implementation detail of the composed-mode
engine". Everything above it speaks :class:`~packages.voice.engine.base.VoiceEngine`.

The wiring order in :func:`build_composed_pipeline` and the strategy selection in
:func:`user_turn_strategies` are the load-bearing parts; both carry the reasoning
that produced them, because both look arbitrary and neither is.

**End-of-turn detection is selectable** (:class:`~packages.voice.turn.TurnDetector`):
the original fixed-silence baseline, Smart Turn v3, VAP, or Deepgram Flux.
:func:`detector_turn_setup` is the single place that maps a detector to its VAD
window, its stop strategy and its STT service. Silero VAD runs under every one
of them, because it is the common clock the latency metrics are measured by.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from packages.providers.voice.agent_bridge import VoiceAgentProcessor
from packages.providers.voice.strategies import (
    SafeBotSpeakingMuteStrategy,
    SoftSpeechUserTurnStartStrategy,
)
from packages.providers.voice.turn_signals import TranscriptTap, UserTurnText
from packages.voice.call_metrics import CallMetrics
from packages.voice.engine.config import TenantVoiceConfig
from packages.voice.turn import (
    MODEL_VAD_STOP_SECS,
    TurnDetectionConfig,
    TurnDetector,
    TurnPolicy,
    clamp_stop_secs,
)
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.transports.base_transport import BaseTransport
from pipecat.turns.user_start import (
    ExternalUserTurnStartStrategy,
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_stop import (
    ExternalUserTurnStopStrategy,
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

__all__ = [
    "DEFAULT_KEYTERMS",
    "DetectorSetup",
    "build_composed_pipeline",
    "detector_turn_setup",
    "detector_unavailable_reason",
    "user_turn_strategies",
    "vad_params",
]

log = structlog.get_logger(__name__)

#: Boosted STT vocabulary. Months and booking words are exactly what a
#: receptionist call is made of, and they are what nova-3 gets wrong without a
#: hint — a misheard month books the wrong appointment and nobody notices until
#: the caller does not arrive.
DEFAULT_KEYTERMS: tuple[str, ...] = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "appointment", "booking", "cancel", "reschedule", "availability",
    "phone", "number", "morning", "afternoon", "evening",
    "today", "tomorrow", "next week",
)  # fmt: skip


def vad_params(policy: TurnPolicy, *, stop_secs: float | None = None) -> VADParams:
    """Translate our vendor-free policy into Pipecat's VAD knobs.

    ``stop_secs`` overrides the policy's silence window — the model detectors
    use a short one, because for them VAD's stop only says *when to ask*.
    """
    return VADParams(
        confidence=policy.confidence,
        start_secs=policy.start_secs,
        stop_secs=clamp_stop_secs(policy.stop_secs if stop_secs is None else stop_secs),
        min_volume=policy.min_volume,
    )


def user_turn_strategies(
    policy: TurnPolicy, *, shared_state: dict[str, Any]
) -> UserTurnStrategies:
    """VAD + soft-speech start; explicit speech-timeout stop.

    Three deliberate departures from Pipecat's defaults, each from a real bug:

    * **Explicit ``SpeechTimeoutUserTurnStopStrategy``.** The default stop is
      ``LocalSmartTurnAnalyzerV3``, which *ignores* ``stop_secs`` — so the
      configured end-of-turn silence silently does nothing.
    * **Soft-speech start added.** VAD alone left quiet speech with a final
      transcript and no turn at all, so the agent never ran (see the strategy's
      docstring).
    * **Stock transcription-start omitted.** It opens turns on interim
      transcripts and mid-TTS, so delayed STT of earlier speech cut the reply
      in half.
    """
    stop = clamp_stop_secs(policy.stop_secs)

    def _blocked() -> bool:
        # Never let a late transcript open a turn while the bot is speaking or
        # the agent is mid-turn.
        if not policy.block_turn_start_while_busy:
            return False
        return bool(shared_state.get("bot_speaking") or shared_state.get("busy"))

    start: list[Any] = [VADUserTurnStartStrategy()]
    if policy.soft_speech_start:
        start.append(SoftSpeechUserTurnStartStrategy(is_blocked=_blocked))

    return UserTurnStrategies(
        start=start,
        stop=[
            SpeechTimeoutUserTurnStopStrategy(
                user_speech_timeout=stop, wait_for_transcript=True
            )
        ],
    )


def build_stt(cfg: TenantVoiceConfig, *, api_key: str | None = None) -> DeepgramSTTService:
    """Streaming STT. Interim results feed barge-in; finals commit the turn.

    ``smart_format`` and ``punctuate`` are on because the agent reads this text:
    "july fourteenth at two thirty" parses far worse than "July 14th at 2:30".
    """
    return DeepgramSTTService(
        api_key=api_key or os.environ["DEEPGRAM_API_KEY"],
        settings=DeepgramSTTService.Settings(
            model=cfg.stt_model,
            language=cfg.stt_language,
            interim_results=True,
            smart_format=True,
            punctuate=True,
            keyterm=list(cfg.keyterms or DEFAULT_KEYTERMS),
        ),
    )


def build_flux_stt(cfg: TenantVoiceConfig, *, api_key: str | None = None) -> Any:
    """Deepgram Flux: transcription and end-of-turn detection in one service.

    The early end-of-turn event (``EagerEndOfTurn``) is only requested when
    speculation is on — without a listener for it, it would just cost Flux
    extra work. ``eot_timeout_ms`` is the same maximum-silence ceiling the
    other model detectors get, so all three give up on a pause at one time.
    """
    from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService

    td = cfg.turn_detection
    return DeepgramFluxSTTService(
        api_key=api_key or os.environ["DEEPGRAM_API_KEY"],
        settings=DeepgramFluxSTTService.Settings(
            model=td.flux_model,
            eot_threshold=td.flux_eot_threshold,
            eager_eot_threshold=td.flux_eager_eot_threshold if td.speculative else None,
            eot_timeout_ms=int(td.max_silence_secs * 1000),
            keyterm=list(cfg.keyterms or DEFAULT_KEYTERMS),
        ),
    )


def detector_unavailable_reason(td: TurnDetectionConfig) -> str | None:
    """Why the configured detector cannot run on this host, or ``None``."""
    if td.detector is TurnDetector.VAP:
        from packages.providers.voice.vap_turn import vap_unavailable_reason

        return vap_unavailable_reason(td.vap_model_dir)
    return None


@dataclass(frozen=True, slots=True)
class DetectorSetup:
    """Everything that differs between end-of-turn detectors."""

    vad: VADParams
    strategies: UserTurnStrategies
    #: ``"vad"`` speculates at VAD's pause; ``"external"`` on the STT's own
    #: early signal; ``None`` when speculation is off.
    speculation: str | None
    uses_flux: bool


class FluxTurnStopStrategy(ExternalUserTurnStopStrategy):  # type: ignore[no-untyped-call]
    """Pipecat's external stop strategy, without its 0.5s detour on Flux.

    Flux ends a turn by pushing the final transcript and then broadcasting
    ``UserStoppedSpeakingFrame``. The stop frame is a system frame, so it
    overtakes the transcript on the way to the aggregator: the strategy sees
    "stopped" with no text, and only commits when its 0.5s settle timer fires.
    Measured end to end, that added ~500ms to every Flux turn. Flux only sends
    a final transcript at end of turn, so committing when it lands is exact.
    """

    async def _handle_transcription(self, frame: TranscriptionFrame) -> None:
        await super()._handle_transcription(frame)
        await self._maybe_trigger_user_turn_stopped()  # type: ignore[no-untyped-call]


def detector_turn_setup(
    cfg: TenantVoiceConfig, policy: TurnPolicy, *, shared_state: dict[str, Any]
) -> DetectorSetup:
    """Map the configured detector to its VAD window, strategies and signals.

    * ``silence`` — exactly the pre-existing setup: VAD stop at the configured
      silence, then the speech-timeout stop. Unchanged so it stays the baseline.
    * ``smart_turn`` / ``vap`` — VAD stops after a short pause and the model is
      asked whether the caller has finished; "not yet" holds the turn open up to
      ``max_silence_secs``. Same start strategies as the baseline.
    * ``flux`` — Flux owns both turn start and turn end; the aggregator is told
      the turn is external. VAD still runs, for measurement only.
    """
    td = cfg.turn_detection
    if td.detector is TurnDetector.FLUX:
        return DetectorSetup(
            vad=vad_params(policy, stop_secs=MODEL_VAD_STOP_SECS),
            strategies=UserTurnStrategies(
                start=[ExternalUserTurnStartStrategy()],  # type: ignore[no-untyped-call]
                stop=[FluxTurnStopStrategy()],
            ),
            speculation="external" if td.speculative else None,
            uses_flux=True,
        )

    if td.detector is TurnDetector.SILENCE:
        return DetectorSetup(
            vad=vad_params(policy),
            strategies=user_turn_strategies(policy, shared_state=shared_state),
            speculation="vad" if td.speculative else None,
            uses_flux=False,
        )

    params = SmartTurnParams(stop_secs=td.max_silence_secs)
    if td.detector is TurnDetector.VAP:
        from packages.providers.voice.vap_turn import VapTurnAnalyzer

        analyzer: Any = VapTurnAnalyzer(
            model_dir=td.vap_model_dir, threshold=td.vap_threshold, params=params
        )
    else:
        analyzer = LocalSmartTurnAnalyzerV3(params=params)

    base = user_turn_strategies(policy, shared_state=shared_state)
    return DetectorSetup(
        vad=vad_params(policy, stop_secs=MODEL_VAD_STOP_SECS),
        strategies=UserTurnStrategies(
            start=list(base.start or []),
            stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=analyzer)],
        ),
        speculation="vad" if td.speculative else None,
        uses_flux=False,
    )


def build_tts(cfg: TenantVoiceConfig, *, api_key: str | None = None) -> DeepgramTTSService:
    """Streaming TTS, first-byte optimised."""
    return DeepgramTTSService(
        api_key=api_key or os.environ["DEEPGRAM_API_KEY"],
        settings=DeepgramTTSService.Settings(voice=cfg.tts_voice),
    )


def build_composed_pipeline(
    transport: BaseTransport,
    *,
    cfg: TenantVoiceConfig,
    run_turn: Callable[..., Awaitable[str]],
    retract_last_reply: Callable[[], Awaitable[None]] | None = None,
    on_transcript: Callable[[str, str], Awaitable[None]] | None = None,
    shared_state: dict[str, Any] | None = None,
    api_key: str | None = None,
    metrics: CallMetrics | None = None,
    withdraw_speculative: Callable[[str], Awaitable[None]] | None = None,
) -> Pipeline:
    """Assemble ``STT → agent → TTS`` with the turn policy applied.

    Args:
        run_turn: One caller utterance in, the agent's reply out. This is the
            platform's ``GraphRunner`` — the same one web chat uses.
        metrics: This call's metrics collector; timings are stamped into it.
        withdraw_speculative: Undoes a speculative turn the caller talked on
            past. Required for speculation to leave history clean.
    """
    state = shared_state if shared_state is not None else {}
    policy = TurnPolicy.from_stop_secs(cfg.stop_secs)
    setup = detector_turn_setup(cfg, policy, shared_state=state)

    stt = (
        build_flux_stt(cfg, api_key=api_key)
        if setup.uses_flux
        else build_stt(cfg, api_key=api_key)
    )
    tts = build_tts(cfg, api_key=api_key)
    turn_text = UserTurnText()
    agent = VoiceAgentProcessor(
        run_turn=run_turn,
        retract_last_reply=retract_last_reply,
        on_transcript=on_transcript,
        shared_state=state,
        metrics=metrics,
        speculation=setup.speculation,
        turn_text=turn_text,
        withdraw_speculative=withdraw_speculative,
    )
    tap = TranscriptTap(
        turn_text,
        on_update=agent.on_transcript_update if setup.speculation == "vad" else None,
    )

    if setup.uses_flux:
        # Flux's own early end-of-turn guess and its retraction. Wired even
        # without speculation so the metrics log shows how often Flux guessed.
        @stt.event_handler("on_eager_end_of_turn")  # type: ignore[untyped-decorator]
        async def _on_eager(_service: Any, transcript: str) -> None:
            await agent.on_early_end_of_turn(transcript)

        @stt.event_handler("on_turn_resumed")  # type: ignore[untyped-decorator]
        async def _on_resumed(_service: Any) -> None:
            await agent.on_turn_resumed()

    log.info(
        "voice.pipeline.built",
        stt_model=cfg.turn_detection.flux_model if setup.uses_flux else cfg.stt_model,
        tts_voice=cfg.tts_voice,
        detector=cfg.turn_detection.label,
        vad_stop_secs=setup.vad.stop_secs,
        stop_secs=cfg.stop_secs,
        soft_speech_start=policy.soft_speech_start,
    )

    mute_strategies = (
        [SafeBotSpeakingMuteStrategy()] if policy.mute_while_bot_speaks else []
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        LLMContext(),
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=setup.vad),
            user_mute_strategies=mute_strategies,
            user_turn_strategies=setup.strategies,
        ),
    )

    # Order matters. The agent sits *after* the user aggregator so it receives a
    # committed utterance rather than a token stream, and *before* TTS so its
    # single TTSSpeakFrame is what gets synthesized. The assistant aggregator
    # trails the transport so context is updated with what was actually spoken.
    # The transcript tap sits before the aggregator because the aggregator
    # consumes transcription frames, and speculation needs them earlier.
    return Pipeline(
        [
            transport.input(),
            stt,
            tap,
            user_aggregator,
            agent,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

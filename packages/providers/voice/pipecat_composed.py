"""The composed voice engine, built on Pipecat + Deepgram.

``Deepgram STT (nova-3) → platform LangGraph agent → Deepgram TTS (aura-2)``.

This module is the *only* place in the platform that knows Pipecat exists, which
is what 04 §2.2 means by "Pipecat is an implementation detail of the composed-mode
engine". Everything above it speaks :class:`~packages.voice.engine.base.VoiceEngine`.

The wiring order in :func:`build_composed_pipeline` and the strategy selection in
:func:`user_turn_strategies` are the load-bearing parts; both carry the reasoning
that produced them, because both look arbitrary and neither is.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from packages.providers.voice.agent_bridge import VoiceAgentProcessor
from packages.providers.voice.strategies import (
    SafeBotSpeakingMuteStrategy,
    SoftSpeechUserTurnStartStrategy,
)
from packages.voice.engine.config import TenantVoiceConfig
from packages.voice.turn import TurnPolicy, clamp_stop_secs
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.transports.base_transport import BaseTransport
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

__all__ = [
    "DEFAULT_KEYTERMS",
    "build_composed_pipeline",
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


def vad_params(policy: TurnPolicy) -> VADParams:
    """Translate our vendor-free policy into Pipecat's VAD knobs."""
    return VADParams(
        confidence=policy.confidence,
        start_secs=policy.start_secs,
        stop_secs=clamp_stop_secs(policy.stop_secs),
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
) -> Pipeline:
    """Assemble ``STT → agent → TTS`` with the turn policy applied.

    Args:
        run_turn: One caller utterance in, the agent's reply out. This is the
            platform's ``GraphRunner`` — the same one web chat uses.
    """
    state = shared_state if shared_state is not None else {}
    policy = TurnPolicy.from_stop_secs(cfg.stop_secs)

    stt = build_stt(cfg, api_key=api_key)
    tts = build_tts(cfg, api_key=api_key)
    agent = VoiceAgentProcessor(
        run_turn=run_turn,
        retract_last_reply=retract_last_reply,
        on_transcript=on_transcript,
        shared_state=state,
    )

    params = vad_params(policy)
    log.info(
        "voice.pipeline.built",
        stt_model=cfg.stt_model,
        tts_voice=cfg.tts_voice,
        stop_secs=params.stop_secs,
        soft_speech_start=policy.soft_speech_start,
    )

    mute_strategies = (
        [SafeBotSpeakingMuteStrategy()] if policy.mute_while_bot_speaks else []
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        LLMContext(),
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=params),
            user_mute_strategies=mute_strategies,
            user_turn_strategies=user_turn_strategies(policy, shared_state=state),
        ),
    )

    # Order matters. The agent sits *after* the user aggregator so it receives a
    # committed utterance rather than a token stream, and *before* TTS so its
    # single TTSSpeakFrame is what gets synthesized. The assistant aggregator
    # trails the transport so context is updated with what was actually spoken.
    return Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            agent,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

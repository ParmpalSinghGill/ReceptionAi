"""The composed pipeline assembles, in the right order, with the right strategies.

Voice bugs are found by talking to it, not by unit tests — but *construction*
failures are worth catching here, because they are the ones that turn into "the
call connects and then nothing happens". This builds the real Pipecat pipeline
against a real transport, with no network I/O: nothing here reaches Deepgram.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "pipecat", reason="voice extra not installed (pip install -e '.[voice]')"
)

from packages.providers.voice.pipecat_composed import (
    build_composed_pipeline,
    user_turn_strategies,
    vad_params,
)
from packages.voice.engine.config import TenantVoiceConfig
from packages.voice.turn import TurnPolicy

DUMMY_KEY = "dg_test_key_not_used_offline"


@pytest.fixture
def transport() -> object:
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

    return SmallWebRTCTransport(
        webrtc_connection=SmallWebRTCConnection(),
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )


async def _reply(_text: str) -> str:
    return "Booked."


def test_pipeline_stage_order(transport: object) -> None:
    """STT → agent → TTS, with the aggregators on the outside.

    The agent must sit after the user aggregator (so it receives a committed
    utterance, not a token stream) and before TTS (so its single speak frame is
    what gets synthesized).
    """
    pipeline = build_composed_pipeline(
        transport,  # type: ignore[arg-type]
        cfg=TenantVoiceConfig(),
        run_turn=_reply,
        api_key=DUMMY_KEY,
    )
    names = [type(p).__name__ for p in pipeline._processors]

    assert names == [
        "PipelineSource",
        "SmallWebRTCInputTransport",
        "DeepgramSTTService",
        "LLMUserAggregator",
        "VoiceAgentProcessor",
        "DeepgramTTSService",
        "SmallWebRTCOutputTransport",
        "LLMAssistantAggregator",
        "PipelineSink",
    ]


def test_turn_strategies_are_the_debugged_set() -> None:
    """VAD + soft-speech start, explicit speech-timeout stop.

    The stop strategy in particular must NOT be left as Pipecat's default
    smart-turn analyzer, which ignores ``stop_secs`` entirely — so the
    configured end-of-turn silence would silently do nothing.
    """
    strategies = user_turn_strategies(TurnPolicy(), shared_state={})

    assert [type(s).__name__ for s in strategies.start] == [
        "VADUserTurnStartStrategy",
        "SoftSpeechUserTurnStartStrategy",
    ]
    assert [type(s).__name__ for s in strategies.stop] == [
        "SpeechTimeoutUserTurnStopStrategy"
    ]


def test_soft_speech_start_can_be_disabled() -> None:
    strategies = user_turn_strategies(
        TurnPolicy(soft_speech_start=False), shared_state={}
    )
    assert [type(s).__name__ for s in strategies.start] == ["VADUserTurnStartStrategy"]


def test_vad_params_carry_the_policy() -> None:
    params = vad_params(TurnPolicy(stop_secs=0.45))
    assert params.stop_secs == 0.45
    assert params.confidence == 0.7
    assert params.start_secs == 0.2


def test_stop_secs_is_clamped_through_the_pipeline() -> None:
    """A tenant cannot configure a 30-second end-of-turn wait."""
    assert vad_params(TurnPolicy.from_stop_secs(30)).stop_secs == 3.0

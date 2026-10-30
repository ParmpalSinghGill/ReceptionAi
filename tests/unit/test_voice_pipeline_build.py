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
    detector_turn_setup,
    detector_unavailable_reason,
    user_turn_strategies,
    vad_params,
)
from packages.voice.engine.config import TenantVoiceConfig
from packages.voice.turn import (
    MODEL_VAD_STOP_SECS,
    TurnDetectionConfig,
    TurnDetector,
    TurnPolicy,
)

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
        "TranscriptTap",
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


# ── selectable end-of-turn detectors ──────────────────────────────────────────


def _cfg(detector: str, *, speculative: bool = False, **kw: object) -> TenantVoiceConfig:
    return TenantVoiceConfig(
        turn_detection=TurnDetectionConfig.parse(
            detector, speculative=speculative, base=TurnDetectionConfig(**kw)  # type: ignore[arg-type]
        )
    )


def test_silence_detector_is_the_unchanged_baseline() -> None:
    """The default must be exactly the setup that existed before detectors
    were selectable — otherwise "baseline" rows measure something new."""
    cfg = TenantVoiceConfig()
    setup = detector_turn_setup(cfg, TurnPolicy.from_stop_secs(cfg.stop_secs), shared_state={})
    assert cfg.turn_detection.detector is TurnDetector.SILENCE
    assert setup.vad.stop_secs == cfg.stop_secs
    assert [type(s).__name__ for s in setup.strategies.stop] == [
        "SpeechTimeoutUserTurnStopStrategy"
    ]
    assert setup.speculation is None
    assert setup.uses_flux is False


def test_smart_turn_asks_the_model_at_a_short_pause() -> None:
    cfg = _cfg("smart_turn", max_silence_secs=1.5)
    setup = detector_turn_setup(cfg, TurnPolicy(), shared_state={})
    assert setup.vad.stop_secs == MODEL_VAD_STOP_SECS
    (stop,) = setup.strategies.stop
    assert type(stop).__name__ == "TurnAnalyzerUserTurnStopStrategy"
    analyzer = stop._turn_analyzer
    assert type(analyzer).__name__ == "LocalSmartTurnAnalyzerV3"
    assert analyzer.params.stop_secs == 1.5
    # Same start strategies as the baseline: only the end of turn differs.
    assert [type(s).__name__ for s in setup.strategies.start] == [
        "VADUserTurnStartStrategy",
        "SoftSpeechUserTurnStartStrategy",
    ]


def test_flux_owns_the_turn_and_keeps_vad_for_measurement() -> None:
    setup = detector_turn_setup(_cfg("flux", speculative=True), TurnPolicy(), shared_state={})
    assert [type(s).__name__ for s in setup.strategies.stop or []] == ["FluxTurnStopStrategy"]
    assert setup.uses_flux is True
    assert setup.speculation == "external"
    assert setup.vad.stop_secs == MODEL_VAD_STOP_SECS


def test_speculation_source_follows_the_detector() -> None:
    for name in ("silence", "smart_turn"):
        setup = detector_turn_setup(_cfg(name, speculative=True), TurnPolicy(), shared_state={})
        assert setup.speculation == "vad"


def test_flux_pipeline_uses_the_flux_stt(transport: object) -> None:
    pipeline = build_composed_pipeline(
        transport,  # type: ignore[arg-type]
        cfg=_cfg("flux"),
        run_turn=_reply,
        api_key=DUMMY_KEY,
    )
    names = [type(p).__name__ for p in pipeline._processors]
    assert names[2] == "DeepgramFluxSTTService"


def test_vap_reports_what_is_missing(tmp_path: object) -> None:
    """An uninstalled VAP is refused at connect time with the fix in the
    message, not discovered as a silent call."""
    reason = detector_unavailable_reason(
        TurnDetectionConfig.parse("vap", base=TurnDetectionConfig(vap_model_dir=str(tmp_path)))
    )
    assert reason is not None
    assert "setup_vap.py" in reason or "pip install" in reason


def test_unknown_detector_name_is_rejected() -> None:
    with pytest.raises(ValueError):
        TurnDetectionConfig.parse("smartturn")


async def test_flux_commits_when_the_transcript_trails_the_stop_frame() -> None:
    """Flux's stop frame overtakes its transcript; the turn must not wait 0.5s.

    Measured end to end before this fix: every Flux turn sat on Pipecat's
    settle timer for ~500ms after Flux had already ended it.
    """
    from packages.providers.voice.pipecat_composed import FluxTurnStopStrategy
    from pipecat.frames.frames import (
        TranscriptionFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )

    fired: list[str] = []
    strategy = FluxTurnStopStrategy()

    async def trigger() -> None:
        fired.append("stop")

    strategy.trigger_user_turn_stopped = trigger  # type: ignore[method-assign]
    await strategy.process_frame(UserStartedSpeakingFrame())
    await strategy.process_frame(UserStoppedSpeakingFrame())
    assert fired == [], "committed before any words arrived"
    await strategy.process_frame(TranscriptionFrame("book thursday", "u", "t", finalized=True))
    assert fired == ["stop"]

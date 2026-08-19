"""Turn policy and mode config — the rules that make a call feel like a call.

The constants under test are not style choices; each encodes a failure that was
found by holding a real conversation. The tests exist so that "tidying" one back
to a library default is a red build rather than a call that cuts people off.
"""

from __future__ import annotations

import pytest
from packages.voice.engine.config import (
    TenantVoiceConfig,
    TranscriptFidelity,
    VoiceMode,
)
from packages.voice.turn import TurnPolicy, clamp_stop_secs


class TestClampStopSecs:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            (0.2, 0.2),
            (1.5, 1.5),
            (0.0, 0.1),  # below the floor: would cut callers off mid-sentence
            (-3, 0.1),
            (99, 3.0),  # above the ceiling: the agent reads as broken
            (None, 0.2),
        ],
    )
    def test_clamps_into_the_workable_range(
        self, given: float | None, expected: float
    ) -> None:
        assert clamp_stop_secs(given) == expected

    def test_junk_degrades_to_the_default(self) -> None:
        """This value can come from tenant config; a bad row must not kill a call."""
        assert clamp_stop_secs("not a number") == 0.2  # type: ignore[arg-type]


class TestTurnPolicy:
    def test_defaults_carry_the_hard_won_settings(self) -> None:
        policy = TurnPolicy()
        # Soft speech with no VAD event left turns dead: the agent never ran.
        assert policy.soft_speech_start is True
        # Without muting, speaker echo barges in and cuts the reply in half.
        assert policy.mute_while_bot_speaks is True
        # A late transcript arriving mid-TTS was starting a new turn and
        # cutting both the audio and the on-screen reply.
        assert policy.block_turn_start_while_busy is True

    def test_from_stop_secs_clamps(self) -> None:
        assert TurnPolicy.from_stop_secs(50).stop_secs == 3.0


class TestTenantVoiceConfig:
    def test_defaults_to_the_composed_pipeline(self) -> None:
        assert TenantVoiceConfig().resolved_mode is VoiceMode.COMPOSED

    def test_compliance_lock_beats_configuration(self) -> None:
        """A BAA is not a preference the admin UI may override (04 §2.3)."""
        cfg = TenantVoiceConfig(
            mode=VoiceMode.REALTIME, compliance_locked_mode=VoiceMode.COMPOSED
        )
        assert cfg.resolved_mode is VoiceMode.COMPOSED

    def test_transcript_fidelity_tracks_the_mode_honestly(self) -> None:
        """Realtime transcripts are a side-channel and must say so."""
        assert (
            TenantVoiceConfig(mode=VoiceMode.COMPOSED).transcript_fidelity
            is TranscriptFidelity.EXACT
        )
        assert (
            TenantVoiceConfig(mode=VoiceMode.REALTIME).transcript_fidelity
            is TranscriptFidelity.APPROXIMATE
        )

    def test_fidelity_follows_the_compliance_lock(self) -> None:
        """A locked mode changes what the transcript may claim, too."""
        cfg = TenantVoiceConfig(
            mode=VoiceMode.REALTIME, compliance_locked_mode=VoiceMode.COMPOSED
        )
        assert cfg.transcript_fidelity is TranscriptFidelity.EXACT

    def test_deepgram_defaults_are_the_verified_pair(self) -> None:
        cfg = TenantVoiceConfig()
        assert cfg.stt_model == "nova-3"
        assert cfg.tts_voice == "aura-2-thalia-en"

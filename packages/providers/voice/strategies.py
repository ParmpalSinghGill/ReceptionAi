"""Pipecat turn strategies that fix two specific, reproducible call failures.

Both classes exist because a stock Pipecat strategy has a failure mode that is
silent, intermittent, and reads to the caller as "the robot broke". Neither is a
preference. Read :mod:`packages.voice.turn` for the policy these enforce.
"""

from __future__ import annotations

from collections.abc import Callable

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    TranscriptionFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_mute.base_user_mute_strategy import BaseUserMuteStrategy
from pipecat.turns.user_start.base_user_turn_start_strategy import (
    BaseUserTurnStartStrategy,
)

__all__ = ["SafeBotSpeakingMuteStrategy", "SoftSpeechUserTurnStartStrategy"]


class SafeBotSpeakingMuteStrategy(BaseUserMuteStrategy):
    """Mute while the bot speaks, and always unmute if speech is interrupted.

    Muting during TTS is necessary: without it, speaker echo barges in and cuts
    the second half of a multi-sentence reply, so the transcript shows a full
    answer while the audio stops halfway.

    Pipecat's always-on mute strategy clears only on ``BotStoppedSpeakingFrame``.
    If TTS is interrupted before that frame ever arrives, the user stays muted
    **forever** — the rest of the call is pin-drop silence and nothing in the
    logs says why. Clearing on ``InterruptionFrame`` too is the whole fix.
    """

    def __init__(self) -> None:
        super().__init__()
        self._bot_speaking = False

    async def process_frame(self, frame: Frame) -> bool:
        await super().process_frame(frame)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame | InterruptionFrame):
            self._bot_speaking = False

        return self._bot_speaking


class SoftSpeechUserTurnStartStrategy(BaseUserTurnStartStrategy):
    """Start a user turn from a finalized transcript when VAD did not.

    Calls showed a final user transcript on screen with **zero**
    ``user_started_speaking`` / ``user_turn_started`` events: Silero never
    opened a turn for quiet speech, so the stop strategy never ran and the
    agent never fired. The caller spoke, was transcribed, and was ignored.

    Stock ``TranscriptionUserTurnStartStrategy`` is not a substitute — it also
    starts on *interim* transcripts and while the bot is speaking, which cuts
    TTS. This variant starts only from finalized text, and only while idle.
    """

    def __init__(
        self,
        *,
        is_blocked: Callable[[], bool] | None = None,
        **kwargs: object,
    ) -> None:
        # Interruptions are fine when idle; the blocked path below is what
        # keeps this from firing while the bot holds the floor.
        super().__init__(
            enable_interruptions=True,
            enable_user_speaking_frames=True,
            **kwargs,  # type: ignore[arg-type]
        )
        self._is_blocked = is_blocked or (lambda: False)

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if not isinstance(frame, TranscriptionFrame):
            return ProcessFrameResult.CONTINUE

        text = (getattr(frame, "text", None) or "").strip()
        if not text:
            return ProcessFrameResult.CONTINUE

        # Deepgram emits TranscriptionFrame only for is_final results, so this
        # is already finalized text. Deliberately NOT gated on `frame.finalized`:
        # that flag is set only after a VAD-driven Finalize request, so requiring
        # it would mean soft speech with no VAD never starts a turn — precisely
        # the bug this class exists to fix.
        if self._is_blocked():
            return ProcessFrameResult.CONTINUE

        await self.trigger_user_turn_started()
        return ProcessFrameResult.STOP

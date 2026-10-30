"""What the caller has said so far in the current turn, for speculation.

Speculation needs the caller's words at the moment of an *early* end-of-turn
signal — before the turn is committed, so before the user aggregator hands the
agent its ``LLMContextFrame``. The aggregator consumes transcription frames and
does not pass them on, so this tap sits between STT and the aggregator and keeps
a running copy: every finalized segment so far, plus the newest interim one.

Pass-through: every frame continues unchanged. The tap only reads.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from pipecat.frames.frames import Frame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

__all__ = ["TranscriptTap", "UserTurnText", "normalize_utterance"]

_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")


def normalize_utterance(text: str) -> str:
    """Compare utterances ignoring case, punctuation and spacing.

    Interim and final transcripts of the same words differ in exactly these
    ways ("Thursday at ten" / "Thursday at ten."), and a speculation must not
    be thrown away over a full stop.
    """
    return _SPACE.sub(" ", _PUNCT.sub(" ", (text or "").lower())).strip()


class UserTurnText:
    """Finals plus the live interim for the turn in progress."""

    def __init__(self) -> None:
        self._finals: list[str] = []
        self._interim = ""

    @property
    def text(self) -> str:
        parts = [*self._finals]
        if self._interim:
            parts.append(self._interim)
        return " ".join(p for p in parts if p).strip()

    def add_final(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._finals.append(text)
        self._interim = ""

    def set_interim(self, text: str) -> None:
        self._interim = (text or "").strip()

    def reset(self) -> None:
        self._finals.clear()
        self._interim = ""


class TranscriptTap(FrameProcessor):
    """Mirror transcripts into a :class:`UserTurnText` and announce changes."""

    def __init__(
        self,
        turn_text: UserTurnText,
        *,
        on_update: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self._turn_text = turn_text
        self._on_update = on_update

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        changed = False
        if isinstance(frame, TranscriptionFrame):
            self._turn_text.add_final(frame.text)
            changed = True
        elif isinstance(frame, InterimTranscriptionFrame):
            self._turn_text.set_interim(frame.text)
            changed = True
        await self.push_frame(frame, direction)
        if changed and self._on_update is not None:
            await self._on_update()

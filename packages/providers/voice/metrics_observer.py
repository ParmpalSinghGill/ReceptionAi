"""Feed Pipecat pipeline events into a call's :class:`CallMetrics`.

An observer, not a processor: it sees every frame pushed between any two
processors without sitting in the frame path, so measuring a call cannot change
how the call behaves.

**Timestamps come from the frame push, not from when the observer runs.**
Pipecat queues observer callbacks, so "now" inside ``on_push_frame`` can be
milliseconds after the event. ``FramePushed.timestamp`` is taken at the push, on
the pipeline clock; :class:`MetricsClock` exposes that clock's origin so the
value converts to the same ``time.monotonic()`` base the agent bridge uses.
"""

from __future__ import annotations

from collections import deque

from packages.voice.call_metrics import CallMetrics
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSStartedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData, TurnMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed

__all__ = ["MetricsClock", "VoiceMetricsObserver"]


class MetricsClock(SystemClock):
    """Pipecat's system clock, with its origin readable."""

    def to_monotonic(self, pipeline_ns: int) -> float:
        """Convert a pipeline-clock timestamp to ``time.monotonic()`` seconds."""
        return (self._time + pipeline_ns) / 1_000_000_000


class VoiceMetricsObserver(BaseObserver):
    """Translate the frames that matter for turn-taking into metric events."""

    def __init__(self, metrics: CallMetrics, clock: MetricsClock) -> None:
        super().__init__()
        self._metrics = metrics
        self._clock = clock
        # A frame is pushed once per processor it passes through; count it once.
        self._seen: deque[int] = deque(maxlen=4096)
        self._seen_set: set[int] = set()

    def _remember(self, frame_id: int) -> None:
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(frame_id)
        self._seen_set.add(frame_id)

    def _first_sight(self, frame: Frame) -> bool:
        """True once per event, however many processors pass the frame on.

        A broadcast frame (VAD, turn and bot-speaking frames) is two frames,
        one sent each way; the sibling is marked seen too, or every one of
        those events would be logged twice.
        """
        if frame.id in self._seen_set:
            return False
        self._remember(frame.id)
        sibling = getattr(frame, "broadcast_sibling_id", None)
        if sibling is not None:
            self._remember(sibling)
        return True

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        kind = _KINDS.get(type(frame))
        if kind is None and not isinstance(frame, MetricsFrame | TranscriptionFrame):
            return
        if not self._first_sight(frame):
            return
        t = self._clock.to_monotonic(data.timestamp)
        m = self._metrics

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            m.event("vad_stop", t=t, stop_secs=getattr(frame, "stop_secs", 0.0))
        elif isinstance(frame, InterimTranscriptionFrame):
            m.event("transcript_interim", t=t, chars=len(frame.text or ""))
        elif isinstance(frame, TranscriptionFrame):
            m.event("transcript_final", t=t, text=frame.text)
        elif isinstance(frame, MetricsFrame):
            for item in frame.data:
                if isinstance(item, TurnMetricsData):
                    m.event(
                        "turn_analysis",
                        t=t,
                        is_complete=item.is_complete,
                        probability=round(float(item.probability), 4),
                        inference_ms=round(float(item.e2e_processing_time_ms), 1),
                    )
                elif isinstance(item, TTFBMetricsData):
                    name = (item.processor or "").lower()
                    which = (
                        "tts_ttfb" if "tts" in name else "stt_ttfb" if "stt" in name else None
                    )
                    if which and item.value:
                        m.event(which, t=t, ms=round(item.value * 1000, 1))
        elif kind is not None:
            m.event(kind, t=t)


_KINDS: dict[type, str] = {
    VADUserStartedSpeakingFrame: "vad_start",
    VADUserStoppedSpeakingFrame: "vad_stop",
    UserStartedSpeakingFrame: "user_turn_start",
    UserStoppedSpeakingFrame: "user_turn_stop",
    InterimTranscriptionFrame: "transcript_interim",
    BotStartedSpeakingFrame: "bot_start",
    BotStoppedSpeakingFrame: "bot_stop",
    InterruptionFrame: "interruption",
    TTSStartedFrame: "tts_start",
}

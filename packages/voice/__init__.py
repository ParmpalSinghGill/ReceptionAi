"""``packages.voice`` — the voice pipeline seam (04-VOICE-CHANNELS §2).

This package owns the *contract* between the media shell and whatever produces
audio: :class:`~packages.voice.engine.base.VoiceEngine`. It owns no vendor SDK
and imports none. Deepgram, Pipecat and every other provider live behind
``packages/providers/`` so that swapping one is a configuration change rather
than a refactor (Core Contract §9).
"""

from __future__ import annotations

__all__: list[str] = []

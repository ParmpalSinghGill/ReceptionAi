"""Voice provider adapters — the only place Pipecat and Deepgram are imported.

``packages/providers/`` is one of the three packages permitted to import a
vendor SDK (Core Contract §9). Keeping the Pipecat pipeline here rather than in
``apps/media`` is what makes the ``VoiceEngine`` seam real: the app tier owns
process lifecycle, sessions and metrics, and could run a different engine
tomorrow without touching any of that.

Imports are deliberately **not** re-exported eagerly — importing this package
must not drag in Pipecat (a multi-second import that pulls torch-adjacent
dependencies) for a caller that only wants the type names.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_KEYTERMS",
    "SafeBotSpeakingMuteStrategy",
    "SoftSpeechUserTurnStartStrategy",
    "VoiceAgentProcessor",
    "build_composed_pipeline",
]

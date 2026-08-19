"""``apps/media`` — the realtime voice worker (Core Contract §9, amendment A1).

A separate process from ``apps/api`` because a slow LLM call in a request
handler head-of-line blocks audio frames on the same event loop. See
:mod:`apps.media.main` for the surface and 04-VOICE-CHANNELS §1.3 for the full
justification.
"""

from __future__ import annotations

__all__: list[str] = []

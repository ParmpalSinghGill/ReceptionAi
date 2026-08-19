"""``packages.providers`` — the adapter layer for contract §5's ports.

This is one of the three packages permitted to import a vendor SDK
(``providers`` | ``connectors`` | ``channels``), enforced by
``tools/check_layering.py``. Everything above this package speaks the port
dataclasses in :mod:`packages.core.ports` and never learns what an OpenAI or
Anthropic request body looks like.

Universal adapter requirements (contract §5) apply to every adapter here:
async, cancellable, timeout-bounded, retry-with-jitter, emits usage, and never
logs a secret or raw PII.
"""

from __future__ import annotations

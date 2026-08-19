"""HTTP routers. The exact surface PHASE1-API-CONTRACT freezes.

Two frontends are built against that document in parallel, so the paths,
status codes, field names and SSE event names below are a contract, not an
implementation detail. A change here is a change there.
"""

from __future__ import annotations

from apps.api.routers import auth, conversations, me, public_demo

__all__ = ["auth", "conversations", "me", "public_demo"]

"""``apps/api`` — FastAPI: HTTP, WS, SSE.

Contract §9: *"Thin — no business logic."* Everything this package does is
transport: authenticate, resolve the tenant, open an RLS session, call into
``packages/*``, and frame the result. The graph runs in ``packages/ai``, the
model is reached through ``packages/providers``, and the rows are written by
``packages/persistence``.

The exact wire surface is frozen in ``docs/architecture/PHASE1-API-CONTRACT.md``
so the two frontends can be built in parallel against it.
"""

from __future__ import annotations

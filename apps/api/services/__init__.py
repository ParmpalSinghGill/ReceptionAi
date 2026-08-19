"""Thin orchestration between the routers and ``packages/*``.

Contract §9 says ``apps/api`` holds no business logic, and that holds: nothing
here decides *what* the assistant says or *how* a booking works. What lives
here is the sequencing that two routers would otherwise duplicate — persist the
user turn, run the graph, stream, persist the assistant turn — and duplicating
it is how the public demo and the admin path drift into writing different rows
for the same event.
"""

from __future__ import annotations

from apps.api.services.conversations import ConversationService

__all__ = ["ConversationService"]

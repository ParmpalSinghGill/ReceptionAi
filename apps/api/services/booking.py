"""Wire the booking tools to the database — the one place ``ai`` meets storage.

``packages/ai/tools/builtin/booking.py`` deliberately does not import
``packages/persistence``: a graph test would then open a connection pool, and a
Temporal worker would inherit an import graph shaped by a web application.
Instead the tools call a factory this module installs at startup.

**Each tool invocation gets its own unit of work.** Not the request's session,
and not a shared one:

* The request's session is held across the whole streamed turn, which lasts
  seconds. Holding a transaction — and a pooled connection, and an MVCC snapshot
  — for that long is how an RLS application bloats a table and starves its pool
  (the same reasoning ``ConversationService`` documents for not threading its
  session through the stream).
* A shared session would put a booking write and a later, unrelated tool failure
  in the same transaction, so the failure would roll the booking back. A caller
  told "you're booked" must stay booked even if the next thing the agent tries
  goes wrong.

So: open, do one tool's work, commit, close. The tenant context comes from the
graph's state, which came from the authenticated request — never from a model
argument.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from packages.ai.tools.builtin.booking import BookingToolDeps
from packages.booking.service import BookingService
from packages.core.tenancy import TenantContext
from packages.persistence.repositories.booking import BookingRepository
from packages.persistence.session import SessionFactory

__all__ = ["build_booking_tool_deps"]


def build_booking_tool_deps(session_factory: SessionFactory) -> BookingToolDeps:
    """The factory the booking tools call, closed over the session factory."""

    @asynccontextmanager
    async def unit_of_work(
        tenant_id: UUID,
    ) -> AsyncIterator[tuple[BookingService, BookingRepository]]:
        ctx = TenantContext(tenant_id=tenant_id)
        async with session_factory.unit_of_work(ctx) as session:
            repository = BookingRepository(session)
            yield BookingService(repository), repository

    return BookingToolDeps(unit_of_work)

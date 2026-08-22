"""``ConversationStatePort`` — short-lived, cross-replica conversation state.

**What this exists to fix.** The booking tools mint a *slot handle* in
``check_availability`` and redeem it in ``book_appointment``. Held in a
module-level dict, that handle resolves only in the process that minted it, so
behind more than one API replica the redeem lands on a pod that never saw the
mint. The tools fail closed when a handle does not resolve — the agent is told
to re-check availability — so the symptom is not a wrong booking but an extra
round-trip on a random subset of calls, which is the kind of intermittent
degradation that reads as a model problem rather than a topology one.

**Why a port rather than importing Redis in the tools.** Contract §9:
``packages/ai`` may not import a vendor SDK, and ``packages/ai`` may not import
``apps``. The tools already reach the database through ``BookingToolDeps``, a
factory that ``apps/api`` installs at startup; this is the same seam for the
same reason. ``packages/ai`` speaks this Protocol, ``packages/providers``
implements it against Redis, and a graph test binds an in-memory implementation
without a server.

**The contract is deliberately a string-keyed TTL map, not a cache.** Three
methods, no invalidation, no tags, no namespaces beyond the key. A cache may
return a stale value or drop one silently as an optimisation; this may not
return a value that was never written, because the value *is* the time an
appointment will be booked at. What it may do is forget — ``get`` returning
``None`` for something that expired, was evicted, or was written by a
process that has since gone away is the expected case, and every caller must
already handle it as "re-check availability".

**Fail-closed is a property of the caller, not this port.** An implementation
that cannot reach its backing store raises rather than pretending the write
succeeded. Swallowing a write failure here would let a handle be minted that
can never resolve, and the failure would surface a minute later in
``book_appointment`` with nothing pointing back at the cause. See
``packages/providers/state/redis_conversation_state.py`` for the degradation
policy the Redis adapter applies on top.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["ConversationStatePort"]


@runtime_checkable
class ConversationStatePort(Protocol):
    """A TTL-bounded, string-keyed store shared by every replica.

    Keys are opaque to the implementation and are built by the caller. Callers
    are required to scope them by tenant — see
    ``packages.ai.tools.builtin.booking`` — because a slot handle that resolved
    across tenants would be a cross-tenant leak of an appointment time and the
    resources allocated to it.

    Values are ``str``. The port does not know what a slot is, and giving it a
    typed value would put a booking concept in ``packages/core/ports``, where
    the next caller with a different payload would have to widen it again.
    """

    async def get(self, key: str) -> str | None:
        """The stored value, or ``None`` if it is absent for any reason.

        ``None`` covers expired, evicted, never-written and
        written-by-a-process-now-gone. Callers must not distinguish these: they
        are the same recoverable condition, and a caller that treats one of them
        as an error would turn a routine expiry into an incident.
        """
        ...

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> None:
        """Store ``value`` under ``key`` for ``ttl_seconds``.

        The TTL is mandatory. Every value that flows through this port is
        ephemeral by nature — an offered slot is meaningless minutes later — and
        an optional TTL is how a store acquires entries that outlive the thing
        they describe and are never swept.

        Raises on a backing-store failure rather than returning quietly, so a
        caller can decide whether the operation it was part of may proceed.
        """
        ...

    async def delete(self, key: str) -> None:
        """Remove ``key`` if present. Absent keys are not an error."""
        ...

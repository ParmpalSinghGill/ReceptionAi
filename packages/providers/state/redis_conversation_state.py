"""``RedisConversationState`` — the cross-replica implementation of the port.

Conventions are taken from ``apps/api/state.py`` and ``apps/api/tickets.py``
rather than invented: ``set(..., ex=ttl)`` for a TTL-bounded write, a
module-level key template, and bytes-in/str-out decoding because the shared
client is built with ``decode_responses=False``. Two styles of Redis access in
one codebase is two sets of assumptions about what comes back from a ``get``.

**The degradation policy, which is the only interesting decision in this file.**

Redis being unreachable must not take booking down, and it must not silently
let the model compose a time. Those pull in opposite directions, so the split
is by operation:

* ``get`` **degrades open, into fail-closed behaviour.** A read failure returns
  ``None``, which is indistinguishable from an expired handle, and the caller
  already handles that by telling the agent to re-check availability. The
  booking still happens; it costs one extra round-trip. Raising here would turn
  a cache outage into "I can't book anything at all", which is strictly worse
  for a caller in pain and no safer — ``None`` cannot produce a wrong time,
  because the tools refuse to book an unresolvable handle.

* ``set`` **raises.** A mint that quietly failed to store would hand the model a
  handle that can never resolve, and the failure would surface later in
  ``book_appointment`` as an unrecognised reference with nothing pointing back
  to the cause. The tool layer catches this and falls back to its in-process
  map, so a write failure degrades to today's single-replica behaviour rather
  than to a broken conversation.

What neither path may ever do is let ``book_appointment`` proceed on a time the
*model* supplied when a handle was expected. That property does not live here —
it lives in the tool, which raises ``ToolBusinessError`` on an unresolvable
reference. This adapter only has to avoid inventing values, and it cannot: it
has none to invent.

**Why no retry.** Contract §5 asks adapters to retry with jitter, and this one
deliberately does not. Every call sits inside a turn a person is waiting
through, the useful TTL is minutes, and the fallback on failure is cheap and
correct. A retry loop here would add latency to the one path where latency is
audible, to salvage a value the caller can regenerate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

__all__ = ["RedisConversationState"]

log = structlog.get_logger(__name__)

#: One namespace for everything this port stores, so an operator can see the
#: whole of a conversation's ephemeral state with a single ``SCAN`` and, more
#: importantly, so these keys cannot collide with the ``conv:pending:`` and
#: ``ticket:`` families already in this database.
_KEY: Final = "convstate:{key}"


class RedisConversationState:
    """:class:`ConversationStatePort` over the shared Redis client.

    Takes the client the lifespan already built rather than a URL: one pool per
    process is the existing arrangement, and a second connection pool for a
    store this small would be a connection storm in service of nothing.
    """

    __slots__ = ("_redis",)

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get(self, key: str) -> str | None:
        """The stored value, or ``None`` — including when Redis is down.

        See the module docstring: ``None`` is a recoverable condition the
        callers already handle, so a read failure is logged and folded into it
        rather than raised.
        """
        try:
            raw = await self._redis.get(_KEY.format(key=key))
        except Exception as exc:
            # `warning`, not `error`: the booking still completes, it just costs
            # an extra availability check. Visible because a sustained rate of
            # this is a Redis problem wearing a latency costume.
            log.warning(
                "conversation_state.get_failed",
                error=type(exc).__name__,
                impact="handle treated as expired; the agent re-checks availability",
            )
            return None
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> None:
        """Store ``value`` for ``ttl_seconds``, or raise.

        Raising is the point — the caller falls back to in-process storage, and
        a silent failure here would produce a handle that resolves nowhere.
        """
        await self._redis.set(_KEY.format(key=key), value, ex=ttl_seconds)

    async def delete(self, key: str) -> None:
        """Remove ``key``. A failure is swallowed: the TTL collects it anyway."""
        try:
            await self._redis.delete(_KEY.format(key=key))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "conversation_state.delete_failed", error=type(exc).__name__
            )

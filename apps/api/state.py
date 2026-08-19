"""Process-wide resources, built once in the lifespan.

Everything here is expensive to create and safe to share: connection pools, the
Redis client, the compiled-graph cache, the LLM adapter. Constructing any of
them per request would be a connection storm.

**Why an explicit ``AppState`` object on ``app.state`` rather than module-level
singletons.** A module global is created at import time, which means importing
``apps.api.routers.conversations`` in a test opens a database pool as a side
effect. It also cannot be swapped: a test that wants a stub LLM has to
monkeypatch the module. An object hung off the app is constructed by the
lifespan, replaced wholesale in a fixture, and torn down deterministically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from fastapi import Request
from packages.ai.cache import CompiledGraphCache
from packages.ai.runner import GraphRunner
from packages.core.ports.llm import LLMPort
from packages.persistence.engine import ConnectionRouter
from packages.persistence.session import SessionFactory

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.middleware.rate_limit import RateLimiter
    from apps.api.security.tokens import TokenService
    from apps.api.services.conversations import ConversationService
    from apps.api.settings import Settings
    from apps.api.tickets import StreamTicketService
    from redis.asyncio import Redis

__all__ = ["AppState", "get_app_state"]

_PENDING_KEY: Final = "conv:pending:{conversation_id}"
#: Generous relative to the POST→GET round-trip, short enough that an abandoned
#: message does not linger. The stream deletes it the instant it reads it.
_PENDING_TTL_SECONDS: Final = 120


@dataclass(slots=True)
class AppState:
    """Everything a handler may need that outlives one request."""

    settings: Settings
    router: ConnectionRouter
    session_factory: SessionFactory
    redis: Redis | None
    tokens: TokenService
    tickets: StreamTicketService
    limiter: RateLimiter
    llm: LLMPort
    graph_cache: CompiledGraphCache
    graph_runner: GraphRunner
    conversations: ConversationService

    #: The seeded demo tenant, resolved once at startup from
    #: ``settings.demo_tenant_slug``. ``None`` when the demo has not been
    #: seeded, which the public router turns into a 503 rather than a 500.
    #:
    #: Resolved at startup rather than per request for two reasons: it is the
    #: hottest anonymous path and does not deserve a lookup, and pinning it
    #: here means a *string from configuration* never reaches a query on a
    #: request path — which is one careless refactor away from being a string
    #: from the request.
    demo_tenant_id: UUID | None = None

    # ── The pending-message handoff ───────────────────────────────────────
    #
    # A posted message and the stream that renders its answer arrive on two
    # separate HTTP connections, and behind any load balancer they land on
    # different pods. So the message is parked in Redis between them.
    # In-process state would work exactly until the second replica, then fail
    # intermittently in a way that reads as a client bug.
    #
    # Both routers use these, rather than each carrying its own copy: two
    # implementations of "hand the message across" is two chances to get the
    # atomicity below wrong.

    async def stash_pending(
        self, conversation_id: UUID, turn_id: UUID, content: str
    ) -> None:
        """Park a posted message for the stream connection to claim."""
        if self.redis is None:
            raise RuntimeError("A pending message requires Redis, which is absent.")
        await self.redis.set(
            _PENDING_KEY.format(conversation_id=conversation_id),
            json.dumps({"turn_id": str(turn_id), "content": content}),
            ex=_PENDING_TTL_SECONDS,
        )

    async def take_pending(self, conversation_id: UUID) -> tuple[UUID, str] | None:
        """Atomically claim the parked message, or ``None``.

        ``GETDEL`` for the same reason the stream ticket uses it: two stream
        connections for one conversation must not both run the turn, which
        would bill twice and write two assistant rows for one question.
        """
        if self.redis is None:
            return None
        raw = await self.redis.getdel(
            _PENDING_KEY.format(conversation_id=conversation_id)
        )
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            return UUID(payload["turn_id"]), str(payload["content"])
        except (ValueError, KeyError, TypeError):
            return None

    async def aclose(self) -> None:
        """Release every resource, in reverse dependency order.

        Each close is independent — one failing must not prevent the rest, or a
        Redis timeout at shutdown leaks database connections. Errors are
        swallowed deliberately: the process is going away, and a traceback from
        a close path buries whatever actually caused the shutdown.
        """
        import contextlib

        with contextlib.suppress(Exception):
            aclose = getattr(self.llm, "aclose", None)
            if aclose is not None:
                await aclose()
        with contextlib.suppress(Exception):
            if self.redis is not None:
                await self.redis.aclose()
        with contextlib.suppress(Exception):
            await self.router.dispose_all()


def get_app_state(request: Request) -> AppState:
    """The state, from the request's app.

    Raises:
        RuntimeError: when the lifespan did not run. That happens when a test
            constructs the app without ``TestClient``'s context manager, and
            the resulting ``AttributeError`` three frames deep in a handler is
            far harder to diagnose than this.
    """
    state = getattr(request.app.state, "app_state", None)
    if state is None:
        raise RuntimeError(
            "AppState is missing: the lifespan did not run. Use the app as a "
            "context manager (`with TestClient(app) as client:`) or an "
            "`httpx.ASGITransport` with `LifespanManager`."
        )
    return state

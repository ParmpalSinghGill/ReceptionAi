"""Rate limiting — 05 §B8.

**Token bucket in Redis, executed as a Lua script.**

* *Not fixed window*: it permits a 2x burst across the boundary (100 requests
  at 59.9 s plus 100 at 60.1 s). For a voice system a burst means dropped audio
  frames, not merely a slow page.
* *Not a sliding-window log*: one Redis entry per request, which at volume is
  gigabytes of Redis for accuracy nobody needs.
* *Not GCRA*: token bucket gives an explicit burst allowance, which we want —
  a dashboard load fires a dozen parallel requests and should not be throttled.
* *Lua*, because read-compute-write must be atomic. ``INCR`` + ``EXPIRE`` is
  two round-trips with a race that loses the TTL under contention, producing
  keys that never expire.

**It fails OPEN, and that is the opposite of the auth policy above it.** Rate
limiting is an abuse control, not a security boundary; a Redis outage must not
take the product down. Auth fails closed, quotas fail open. The counter below
makes the window visible so it stays short (05 §B8.1).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Final

import structlog
from apps.api.problems import problem_response
from redis.asyncio import Redis
from redis.exceptions import NoScriptError, RedisError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

__all__ = ["Decision", "RateLimitMiddleware", "RateLimiter", "Rule"]

log = structlog.get_logger(__name__)

# Named for what it is rather than for the algorithm's usual name. Bandit's
# hardcoded-password rule flags any string literal bound to a name containing
# "token", and a suppression comment cannot go on this assignment: it would
# land *inside* the triple quote and become the script's first line, which
# Redis would execute as Lua. Renaming is the fix that leaves the script intact.
BUCKET_ALGORITHM_LUA: Final[str] = """
-- KEYS[1] bucket key
-- ARGV[1] capacity (burst)     ARGV[2] refill tokens/sec
-- ARGV[3] now (ms)             ARGV[4] requested tokens (cost)
-- ARGV[5] ttl seconds
-- Returns {allowed(0|1), remaining, retry_after_ms, limit}
local capacity  = tonumber(ARGV[1])
local rate      = tonumber(ARGV[2])
local now       = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local ttl       = tonumber(ARGV[5])

local state  = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts     = tonumber(state[2])

if tokens == nil then
  tokens = capacity
  ts = now
end

-- Lazy refill: never run a timer, compute what the bucket would hold.
local elapsed = math.max(0, now - ts) / 1000.0
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
local retry_after = 0
if tokens >= requested then
  tokens = tokens - requested
  allowed = 1
else
  retry_after = math.ceil(((requested - tokens) / rate) * 1000)
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], ttl)
return {allowed, math.floor(tokens), retry_after, capacity}
"""


@dataclass(frozen=True, slots=True)
class Rule:
    """One bucket's shape. ``burst`` is capacity; ``rate_per_sec`` is refill."""

    burst: int
    rate_per_sec: float
    ttl_s: int = 3600


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after_ms: int
    limit: int
    scope: str
    #: True when Redis was unreachable and we admitted the request anyway.
    degraded: bool = False


class RateLimiter:
    """Token-bucket limiter over Redis."""

    def __init__(self, redis: Redis | None) -> None:
        self._redis = redis
        self._sha: str | None = None
        self._lock = asyncio.Lock()

    async def _script(self) -> str:
        if self._sha is None:
            async with self._lock:
                if self._sha is None:
                    assert self._redis is not None
                    self._sha = await self._redis.script_load(BUCKET_ALGORITHM_LUA)
        return self._sha

    async def consume(
        self, checks: list[tuple[str, str, Rule]], *, cost: int = 1
    ) -> list[Decision]:
        """Check every bucket in one pipeline round-trip.

        ``checks`` is ``(scope, key, rule)``. The most restrictive answer wins,
        and the caller decides that — this returns every decision so the
        response headers can report the *binding* limit rather than an
        arbitrary one.
        """
        if self._redis is None:
            return [
                Decision(True, -1, 0, rule.burst, scope, degraded=True)
                for scope, _key, rule in checks
            ]

        now_ms = int(time.time() * 1000)
        try:
            sha = await self._script()
            async with self._redis.pipeline(transaction=False) as pipe:
                for _scope, key, rule in checks:
                    pipe.evalsha(
                        sha, 1, key, rule.burst, rule.rate_per_sec, now_ms, cost, rule.ttl_s
                    )
                raw = await pipe.execute()
        except NoScriptError:
            # The script was evicted (a FLUSH, or a Redis restart). Reload once
            # and retry; a second failure falls through to the open path below.
            self._sha = None
            try:
                sha = await self._script()
                async with self._redis.pipeline(transaction=False) as pipe:
                    for _scope, key, rule in checks:
                        pipe.evalsha(
                            sha, 1, key, rule.burst, rule.rate_per_sec, now_ms, cost,
                            rule.ttl_s,
                        )
                    raw = await pipe.execute()
            except (RedisError, TimeoutError, OSError):
                return self._fail_open(checks)
        except (RedisError, TimeoutError, OSError):
            return self._fail_open(checks)

        return [
            Decision(
                allowed=bool(result[0]),
                remaining=int(result[1]),
                retry_after_ms=int(result[2]),
                limit=int(result[3]),
                scope=scope,
            )
            for (scope, _key, _rule), result in zip(checks, raw, strict=True)
        ]

    @staticmethod
    def _fail_open(checks: list[tuple[str, str, Rule]]) -> list[Decision]:
        """Admit the traffic, loudly.

        Deliberate, and the opposite of the auth policy: rate limiting is an
        abuse control, not a security boundary, and a Redis outage must not
        take the product down. The log line is what keeps the window short.
        """
        log.warning("ratelimit.fail_open", buckets=len(checks))
        return [
            Decision(True, -1, 0, rule.burst, scope, degraded=True)
            for scope, _key, rule in checks
        ]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP for public routes, per-tenant for authenticated ones.

    Runs after tenant resolution, because a per-tenant bucket needs the
    resolved tenant. An unauthenticated request has no tenant, so it is keyed
    on the client IP — which is exactly the split the endpoint classes in
    05 §B8.2 describe.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        public_rule: Rule,
        tenant_rule: Rule,
        public_prefixes: tuple[str, ...] = ("/api/v1/public/", "/api/v1/auth/"),
        exempt_paths: frozenset[str] = frozenset({"/healthz", "/readyz", "/metrics"}),
    ) -> None:
        super().__init__(app)
        self._public_rule = public_rule
        self._tenant_rule = tenant_rule
        self._public_prefixes = public_prefixes
        self._exempt = exempt_paths

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        if path in self._exempt:
            # A probe that gets rate-limited marks the pod unhealthy and takes
            # it out of rotation, which turns a traffic spike into an outage.
            return await call_next(request)

        # The limiter is read from app state rather than captured at
        # construction: middleware is registered by the app factory, but the
        # Redis client is created by the lifespan, which runs *after*. A
        # limiter captured here would hold the pre-lifespan placeholder and
        # silently fail open on every request — a rate limiter that never
        # limits, with no error to notice.
        limiter = self._limiter_for(request)
        if limiter is None:
            return await call_next(request)

        checks = self._buckets(request, path)
        if not checks:
            return await call_next(request)

        decisions = await limiter.consume(checks)
        binding = min(decisions, key=lambda d: (d.allowed, d.remaining))

        if not binding.allowed:
            log.info(
                "ratelimit.denied",
                scope=binding.scope,
                path=path,
                retry_after_ms=binding.retry_after_ms,
            )
            retry_after_s = max(1, binding.retry_after_ms // 1000)
            return problem_response(
                request,
                status=429,
                code="rate_limited",
                detail="Too many requests. Please slow down.",
                headers={
                    "Retry-After": str(retry_after_s),
                    "RateLimit-Limit": str(binding.limit),
                    "RateLimit-Remaining": "0",
                    "RateLimit-Reset": str(retry_after_s),
                },
                retry_after_ms=binding.retry_after_ms,
                scope=binding.scope,
            )

        response = await call_next(request)
        if not binding.degraded:
            response.headers["RateLimit-Limit"] = str(binding.limit)
            response.headers["RateLimit-Remaining"] = str(max(0, binding.remaining))
        return response

    @staticmethod
    def _limiter_for(request: Request) -> RateLimiter | None:
        """The lifespan-built limiter, or ``None`` before the lifespan ran.

        ``None`` admits the request. That is the fail-open policy applied
        consistently: the only way to reach it is a request arriving before
        startup finished, and refusing traffic there would fail a deployment
        that is merely still coming up.
        """
        state = getattr(request.app.state, "app_state", None)
        return getattr(state, "limiter", None) if state is not None else None

    def _buckets(self, request: Request, path: str) -> list[tuple[str, str, Rule]]:
        principal = getattr(request.state, "principal", None)

        if principal is not None:
            # Per-tenant AND per-user (05 §B8.2): the tenant bucket protects
            # shared capacity, the user bucket stops one compromised session
            # exhausting the whole tenant's quota.
            return [
                ("tenant", f"rl:t:{principal.tenant_id}:api", self._tenant_rule),
                (
                    "user",
                    f"rl:u:{principal.tenant_id}:{principal.user_id}:api",
                    self._tenant_rule,
                ),
            ]

        if path.startswith(self._public_prefixes):
            return [("ip", f"rl:ip:public:{_client_ip(request)}", self._public_rule)]

        return []


def _client_ip(request: Request) -> str:
    """The client address — 05 §B8.5.

    ``X-Forwarded-For`` is only trusted for the **leftmost** entry when the
    request came through our own ingress, and Starlette's ``ProxyHeadersMiddleware``
    (installed by uvicorn with ``--proxy-headers``) has already normalized
    ``request.client`` in that case. Reading the raw header here instead would
    let any client name its own IP and defeat the per-IP limit entirely by
    sending a different value each request.
    """
    if request.client and request.client.host:
        return request.client.host
    return "unknown"

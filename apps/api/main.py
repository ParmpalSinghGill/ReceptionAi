"""The FastAPI application — factory, lifespan, middleware, routers.

**A factory rather than a module-level ``app``.** A module-level app is
constructed at import time, which means importing anything from this package
opens a connection pool as a side effect, and a test cannot build a second app
with different settings. :func:`create_app` takes its settings, so a test
constructs an app against a stub LLM and a throwaway database without patching
anything.

**Middleware order is inverted.** Starlette runs middleware in *reverse*
registration order, so the last one added is the outermost. The registration
block below is therefore written bottom-up relative to the execution order
documented in :mod:`apps.api.middleware`, and that inversion is the single most
common source of "why did the rate limiter not see the tenant" — so it is
spelled out at the site rather than left to be rediscovered.

**Startup fails loudly.** :func:`~apps.api.security.route_audit.assert_all_routes_guarded`
runs at app construction. A route with no authorization decision crashes the
process before it can serve a request (05 §A2.4).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from uuid import UUID

import structlog
from apps.api.deps import AppStateDep
from apps.api.middleware import (
    RateLimiter,
    RateLimitMiddleware,
    RequestIdMiddleware,
    Rule,
    TenantContextMiddleware,
    install_exception_handlers,
)
from apps.api.observability import configure_logging, instrument_app
from apps.api.routers import auth, bookings, conversations, me, public_demo
from apps.api.security.route_audit import assert_all_routes_guarded
from apps.api.security.tokens import TokenService
from apps.api.services.booking import build_booking_tool_deps
from apps.api.services.conversations import ConversationService, build_graph_cache
from apps.api.settings import Settings, get_settings
from apps.api.state import AppState
from apps.api.tickets import StreamTicketService
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from packages.ai.runner import GraphRunner
from packages.ai.tools import default_registry
from packages.ai.tools.builtin.booking import set_booking_dependencies
from packages.observability.tracing import get_tracer, set_tracer
from packages.persistence.engine import DEFAULT_SHARD, ConnectionRouter, ShardConfig
from packages.persistence.session import SessionFactory
from packages.providers.llm import LLMProviderSettings, build_llm_adapter
from packages.providers.observability import build_langfuse_tracer
from packages.providers.state import RedisConversationState
from sqlalchemy import text

__all__ = ["build_app_state", "create_app"]

log = structlog.get_logger(__name__)


async def build_app_state(settings: Settings) -> AppState:
    """Construct every process-wide resource.

    Separate from the lifespan so a test can build the same state, swap one
    component, and hand it to :func:`create_app` — without which testing
    against a stub LLM means monkeypatching a module.
    """
    router = ConnectionRouter(
        {
            DEFAULT_SHARD: ShardConfig(
                key=DEFAULT_SHARD,
                dsn=settings.database_url,
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                echo=settings.db_echo,
                application_name=settings.service_name,
            )
        }
    )
    session_factory = SessionFactory(router)

    redis = None
    try:
        from redis.asyncio import Redis

        redis = Redis.from_url(settings.redis_url, decode_responses=False)
        await redis.ping()
    except Exception:
        # Not fatal at construction. `readyz` reports it, the rate limiter
        # fails open, and the ticket service fails closed — so the demo stops
        # working (correctly) while the admin API keeps serving. Refusing to
        # start would turn a cache outage into a total one.
        log.error(
            "redis.unavailable",
            url=settings.redis_url,
            impact=(
                "rate limiting degrades open; the public demo is unavailable "
                "because stream tickets cannot be issued single-use"
            ),
        )
        redis = None

    # LLM tracing. Installed process-wide rather than passed down, because the
    # instrumentation sites are deep inside `packages/ai` and threading a
    # tracer through every node factory would put an observability parameter in
    # a dozen domain signatures. Returns a no-op unless keys are configured, so
    # this line is safe on a clean clone.
    set_tracer(
        build_langfuse_tracer(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            environment=settings.env,
            release=settings.git_sha,
            enabled=settings.langfuse_enabled,
        )
    )

    llm = build_llm_adapter(
        LLMProviderSettings(
            provider=settings.llm_provider,
            api_key=settings.llm_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            stub_token_delay_ms=settings.stub_token_delay_ms,
        ),
        # A missing key in a deployed environment must crash the pod rather
        # than serve canned replies that every dashboard reports as healthy.
        allow_stub_fallback=settings.is_local,
    )

    tools = default_registry()

    # Hand the booking tools their database access. Until this runs they raise
    # ``ConfigurationError`` rather than reaching for a pool that does not exist
    # — fail-closed, so "the tools were never wired" is an error someone sees
    # rather than a booking that silently never happened.
    #
    # The same call installs the shared conversation store, which is what makes
    # a slot handle minted by `check_availability` on this replica redeemable by
    # `book_appointment` on another. Without Redis it is ``None`` and the tools
    # fall back to in-process maps: correct for one process, and behind several
    # it costs a cross-pod booking one extra availability check rather than
    # letting the model compose a time.
    set_booking_dependencies(
        build_booking_tool_deps(
            session_factory,
            RedisConversationState(redis) if redis is not None else None,
        )
    )

    graph_cache = build_graph_cache(
        session_factory=session_factory, llm=llm, tools=tools
    )

    state = AppState(
        settings=settings,
        router=router,
        session_factory=session_factory,
        redis=redis,
        tokens=TokenService(
            secret_key=settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            access_ttl_seconds=settings.access_token_ttl_seconds,
            refresh_ttl_seconds=settings.refresh_token_ttl_seconds,
        ),
        tickets=StreamTicketService(
            redis, ttl_seconds=settings.stream_ticket_ttl_seconds
        ),
        limiter=RateLimiter(redis),
        llm=llm,
        graph_cache=graph_cache,
        graph_runner=GraphRunner(graph_cache),
        conversations=ConversationService(
            session_factory=session_factory,
            llm=llm,
            graph_cache=graph_cache,
            graph_runner=GraphRunner(graph_cache),
            tools=tools,
        ),
    )

    state.demo_tenant_id = await _resolve_demo_tenant(state, settings)
    return state


async def _resolve_demo_tenant(state: AppState, settings: Settings) -> UUID | None:
    """Find the seeded demo tenant by slug, once at startup.

    Goes through ``public_resolve_tenant_by_slug`` (migration 0002), not a
    ``SELECT`` on ``tenants``, and that is not a stylistic preference: the
    table's policy is ``id = app_current_tenant()``, so a direct query returns
    **zero rows** here — knowing the id is the precondition for reading the row
    that contains it. A plain select would therefore report "the demo is not
    seeded" for a tenant that exists, which is the silent-wrong-answer failure
    RLS produces when you ask it the wrong question.

    The function is the sanctioned path, its return is six public columns, and
    it is the same pattern 01 §11.6 uses for the login lookup.
    """
    try:
        async with state.session_factory.ops_unit_of_work() as ops:
            row = (
                await ops.execute(
                    text("SELECT id FROM public_resolve_tenant_by_slug(:slug)"),
                    {"slug": settings.demo_tenant_slug},
                )
            ).scalar_one_or_none()
    except Exception:
        log.error("demo_tenant.lookup_failed", slug=settings.demo_tenant_slug)
        return None

    if row is None:
        log.warning(
            "demo_tenant.not_found",
            slug=settings.demo_tenant_slug,
            remediation="run tools/seed_demo.py",
        )
        return None
    log.info("demo_tenant.resolved", slug=settings.demo_tenant_slug, tenant_id=str(row))
    return row


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application."""
    settings = settings or get_settings()
    configure_logging(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = await build_app_state(settings)
        app.state.app_state = state
        log.info(
            "api.started",
            env=settings.env,
            git_sha=settings.git_sha,
            llm_provider=state.llm.provider,
        )
        try:
            yield
        finally:
            await state.aclose()
            # Flush before the process exits, or the last turns of a shutdown
            # never leave the SDK's buffer — which looks exactly like
            # instrumentation that was never written.
            get_tracer().shutdown()
            log.info("api.stopped")

    app = FastAPI(
        title="ReceptionAi API",
        version="0.1.0",
        description=(
            "Multi-tenant AI receptionist platform. The frozen Phase 1 surface "
            "is docs/architecture/PHASE1-API-CONTRACT.md."
        ),
        lifespan=lifespan,
        # No interactive docs in production: they are an inventory of the
        # attack surface, and everyone who needs them has the repository.
        docs_url="/docs" if settings.is_local else None,
        redoc_url="/redoc" if settings.is_local else None,
        openapi_url="/openapi.json" if settings.is_local else None,
    )

    install_exception_handlers(app)

    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(conversations.router)
    app.include_router(bookings.router)
    app.include_router(public_demo.router)
    _install_health_routes(app)

    # ── Middleware. READ THIS BEFORE REORDERING. ─────────────────────────
    # Starlette runs middleware in REVERSE registration order: the last one
    # added is the outermost and runs first. The intended execution order is
    #     RequestId → TenantContext → RateLimit → CORS-handled response
    # so they are added in the reverse of that.
    app.add_middleware(
        RateLimitMiddleware,
        public_rule=Rule(
            burst=settings.demo_rate_burst,
            rate_per_sec=settings.demo_rate_per_second,
        ),
        tenant_rule=Rule(
            burst=settings.ai_rate_burst, rate_per_sec=settings.ai_rate_per_second
        ),
    )
    app.add_middleware(
        TenantContextMiddleware,
        tokens=TokenService(
            secret_key=settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            access_ttl_seconds=settings.access_token_ttl_seconds,
            refresh_ttl_seconds=settings.refresh_token_ttl_seconds,
        ),
    )
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "Last-Event-ID",
            # The widget's conversation credential. Sent as a header rather
            # than relying on the cookie, because the widget is embedded
            # cross-origin on a customer's site where a SameSite=Lax cookie is
            # never sent (apps/api/deps.py, SESSION_HEADER).
            "X-Conversation-Session",
        ],
        # The frontends read these; without exposing them they are invisible to
        # JS on a cross-origin response, and X-Request-Id is what a user quotes
        # to support.
        expose_headers=["X-Request-Id", "RateLimit-Limit", "RateLimit-Remaining"],
        max_age=600,
    )

    instrument_app(app, settings)

    # Default-deny (05 §A2.4). Last, so every router is mounted. A route
    # without an authorization decision crashes the process here.
    assert_all_routes_guarded(app)

    return app


def _install_health_routes(app: FastAPI) -> None:
    """``/healthz`` and ``/readyz`` — PHASE1-API-CONTRACT.

    The split is the whole point and is routinely got wrong:

    * **liveness** answers "is this process wedged?" and must NOT check
      dependencies. A liveness probe that fails when Postgres is down gets
      every pod killed during a database blip, turning a recoverable incident
      into an outage plus a cold start.
    * **readiness** answers "should traffic come here?" and therefore *does*
      check dependencies, because a pod that cannot reach the database should
      be taken out of rotation while staying alive to recover.
    """

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(state: AppStateDep) -> dict[str, object]:
        from fastapi.responses import JSONResponse

        checks: dict[str, str] = {}

        try:
            async with state.session_factory.ops_unit_of_work() as ops:
                await ops.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = f"error: {type(exc).__name__}"

        if state.redis is None:
            checks["redis"] = "unavailable"
        else:
            try:
                await state.redis.ping()
                checks["redis"] = "ok"
            except Exception as exc:
                checks["redis"] = f"error: {type(exc).__name__}"

        checks["llm"] = state.llm.provider
        checks["demo_tenant"] = (
            "ok" if state.demo_tenant_id is not None else "not_seeded"
        )

        ready = checks["database"] == "ok" and checks["redis"] == "ok"
        body = {"status": "ready" if ready else "degraded", "checks": checks}
        # 503 when not ready, so a load balancer actually acts on it. Returning
        # 200 with a "degraded" body is a readiness check nothing reads.
        return JSONResponse(body, status_code=200 if ready else 503)


#: For ``uvicorn apps.api.main:app``. Constructed lazily by the ASGI server,
#: so importing this module for its factory does not build one.
def _default_app() -> FastAPI:
    return create_app()


app = _default_app()

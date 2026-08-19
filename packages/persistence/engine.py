"""The ``ConnectionRouter`` — 01 §15.2 as amended by contract A8.

Contract §2's escape hatch: *"Every subsystem must resolve its DB session
through the router, never a global engine."* This module is the **only** place
in the codebase that calls ``create_async_engine``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final

from packages.core.ports.connection_router import DEFAULT_SHARD, UnresolvableShardError
from packages.core.tenancy import TenantContext
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

__all__ = [
    "DEFAULT_SHARD",
    "ConnectionRouter",
    "ShardConfig",
    "UnresolvableShardError",
    "check_shard_configuration",
]

_DEFAULT_SHARD: Final = DEFAULT_SHARD


@dataclass(frozen=True, slots=True)
class ShardConfig:
    """Everything needed to build one engine.

    Frozen because a pool whose configuration changed under it is a pool with
    two behaviours and one name.
    """

    key: str
    dsn: str
    pool_size: int = 20
    max_overflow: int = 10
    pool_timeout: float = 10.0
    pool_recycle: int = 1800  # recycle before typical LB idle timeouts
    statement_timeout_ms: int = 30_000
    lock_timeout_ms: int = 5_000
    idle_in_txn_timeout_ms: int = 60_000
    application_name: str = "receptionai-api"
    echo: bool = False


class ConnectionRouter:
    """Resolves a :class:`TenantContext` to the engine that owns its data.

    Today every tenant resolves to the shared cluster. A tenant with a non-null
    ``tenants.shard_key`` resolves to a dedicated engine. Relocating an
    enterprise or HIPAA tenant is therefore a config + data-migration exercise —
    never a schema change and never a code change in any caller.

    This class is the ONLY place in the codebase that constructs an engine.

    **Fail-closed on shard resolution (contract A8).** See :meth:`_engine`.

    Satisfies :class:`~packages.core.ports.connection_router.ConnectionRouterPort`
    structurally; the protocol is what callers above ``packages/persistence``
    type against.
    """

    def __init__(self, shards: dict[str, ShardConfig]) -> None:
        if _DEFAULT_SHARD not in shards:
            raise ValueError("ConnectionRouter requires a '__default__' shard")
        self._configs: dict[str, ShardConfig] = dict(shards)
        self._engines: dict[str, AsyncEngine] = {}
        self._lock = asyncio.Lock()

    async def engine_for(self, ctx: TenantContext) -> AsyncEngine:
        # shard_key IS NULL means "never assigned to dedicated infrastructure",
        # so the default shard is genuinely correct — not a fallback.
        return await self._engine(ctx.shard_key or _DEFAULT_SHARD)

    async def default_engine(self) -> AsyncEngine:
        """For migrations, health checks, and cross-tenant ops jobs."""
        return await self._engine(_DEFAULT_SHARD)

    async def _engine(self, shard_key: str) -> AsyncEngine:
        engine = self._engines.get(shard_key)
        if engine is not None:
            return engine

        async with self._lock:
            engine = self._engines.get(shard_key)  # double-checked
            if engine is not None:
                return engine

            cfg = self._configs.get(shard_key)
            if cfg is None:
                # ── FAIL CLOSED (contract A8) ──────────────────────────────
                # Do NOT fall back to the default shard. A non-null shard_key
                # exists only for tenants moved to dedicated infrastructure
                # BECAUSE shared was contractually or legally unacceptable.
                # Falling back would place precisely those tenants on the
                # shared cluster, and would do it invisibly: RLS still scopes
                # correctly, every query succeeds, no test fails, no alert
                # fires. It fails *successfully*, and discovery comes at the
                # next audit — the exact event the arrangement existed to
                # survive.
                #
                # The trigger is routine (config not yet propagated to a newly
                # scheduled pod), and at production log volume a warning is not
                # a control. So: refuse the session.
                #
                # This is the fail-open/fail-closed split applied consistently.
                # Availability controls fail OPEN — the rate limiter in `05`
                # deliberately admits traffic when Redis is down, because an
                # outage there must not take the product down. Isolation
                # controls fail CLOSED. Shard routing is an isolation control,
                # so it fails closed, exactly as RLS does when
                # `app.tenant_id` is unset (01 §1.6).
                raise UnresolvableShardError(shard_key, sorted(self._configs))

            engine = create_async_engine(
                cfg.dsn,
                pool_size=cfg.pool_size,
                max_overflow=cfg.max_overflow,
                pool_timeout=cfg.pool_timeout,
                pool_recycle=cfg.pool_recycle,
                pool_pre_ping=True,
                echo=cfg.echo,
                # ── Why these three server settings are non-negotiable ──────
                # statement_timeout : a runaway query cannot hold a connection
                #   forever and starve the pool.
                # lock_timeout      : a booking transaction waiting on an
                #   advisory lock fails fast and retries rather than piling up.
                # idle_in_transaction_session_timeout : a leaked `SET LOCAL`
                #   transaction cannot pin an old snapshot and block VACUUM,
                #   which is the classic way an RLS app bloats a table.
                connect_args={
                    "server_settings": {
                        "application_name": cfg.application_name,
                        "statement_timeout": str(cfg.statement_timeout_ms),
                        "lock_timeout": str(cfg.lock_timeout_ms),
                        "idle_in_transaction_session_timeout": str(
                            cfg.idle_in_txn_timeout_ms
                        ),
                        "timezone": "UTC",
                        # asyncpg prepares statements server-side; with
                        # PgBouncer in transaction mode that breaks. Disabled
                        # here and re-enabled per-deployment if the pooler is
                        # session-mode or absent.
                        "jit": "off",
                    },
                    # Required when PgBouncer sits in front in transaction mode.
                    "statement_cache_size": 0,
                },
            )
            self._engines[shard_key] = engine
            return engine

    def known_shard_keys(self) -> set[str]:
        """Shards this process is configured to serve.

        Used by :func:`check_shard_configuration` so a pod with stale
        configuration fails at deploy time rather than at request time.
        """
        return set(self._configs)

    async def dispose_all(self) -> None:
        for engine in self._engines.values():
            await engine.dispose()
        self._engines.clear()


async def check_shard_configuration(
    router: ConnectionRouter, expected: set[str]
) -> None:
    """Readiness gate.

    A pod that cannot serve every configured shard does not accept traffic — so
    the A8 failure surfaces at deploy time, against a rolling update that can
    halt, instead of at request time against a real tenant.

    ``expected`` is read at startup from
    ``SELECT DISTINCT shard_key FROM tenants WHERE shard_key IS NOT NULL``: the
    database is the authority on which shards must exist, so a relocation that
    skipped the config rollout fails the *next* deploy rather than the next
    request.

    There is no legitimate bootstrap case where a non-null ``shard_key`` has no
    configuration, provided relocation is ordered correctly: provision the
    database and roll out its ``ShardConfig`` first, verify every serving pod
    has it, migrate the data, and only then set ``tenants.shard_key``. The
    commitment is made after the capability exists. Reversing that order would
    create a window in which a fallback silently served a committed tenant from
    shared — the failure A8 exists to prevent.
    """
    missing = expected - router.known_shard_keys()
    if missing:
        raise RuntimeError(
            f"Refusing readiness: no configuration for shards {sorted(missing)}. "
            f"Tenants assigned to them would be refused service (contract A8)."
        )

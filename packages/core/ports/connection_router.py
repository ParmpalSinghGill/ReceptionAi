"""``ConnectionRouterPort`` — the seam contract §2's escape hatch requires.

Contract §2: *"`tenants.shard_key` and a `ConnectionRouter` port allow
relocating an enterprise/HIPAA tenant to a dedicated database without a schema
change. Every subsystem must resolve its DB session through the router, never a
global engine."*

**Note on provenance.** Contract §2 names a "`ConnectionRouter` port" in prose
but §5's port table does not list it, and 01 §15.2 defines ``ConnectionRouter``
as a concrete class in ``packages/persistence`` with no protocol above it. This
module supplies the missing abstraction so that the §2 sentence is enforceable:
without it, "resolve through the router" is a convention that a caller can break
by importing ``create_async_engine`` directly, and nothing structural notices.
See the report accompanying this change.

**What this protocol deliberately does not expose.** No ``AsyncEngine``, no
``AsyncSession``, no SQLAlchemy type at all — core imports no I/O library
(contract §9). The engine handle is typed as ``object`` here and narrowed by the
persistence implementation. That is not a weakening: nothing above the
persistence layer should be holding an engine in the first place, and a caller
that wants one has to go through ``packages/persistence``, where the layering
check can see it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from packages.core.errors import PortUnavailableError
from packages.core.tenancy import TenantContext

__all__ = ["DEFAULT_SHARD", "ConnectionRouterPort", "UnresolvableShardError"]

#: The logical name of the shared cluster. A tenant with ``shard_key IS NULL``
#: resolves here, and that is correct rather than a fallback: NULL means "never
#: assigned to dedicated infrastructure".
DEFAULT_SHARD = "__default__"


class UnresolvableShardError(PortUnavailableError):
    """A tenant is assigned to a shard this process has no configuration for.

    Per contract amendment A8 this is FATAL to the request, never a fallback.

    ``shard_key`` is non-null *only* for tenants relocated to dedicated
    infrastructure **because shared was contractually or legally unacceptable**
    — the HIPAA/enterprise escape hatch in contract §2. Falling back to the
    default shard would therefore land precisely those tenants on the shared
    cluster, and would do it invisibly: RLS still scopes correctly, every query
    succeeds, no test fails, no alert fires. It fails *successfully*, and
    discovery comes at the next audit — which for these tenants is the event the
    arrangement existed to survive.

    Maps to **503, not 500**: the tenant's data is intact and the condition is
    transient-by-remediation, so retries and status pages behave correctly. It
    is alerted at page severity with the tenant id and shard key, because for
    these tenants it is a compliance incident in progress, not a routine error.
    """

    code = "shard_unavailable"
    http_status = 503

    def __init__(self, shard_key: str, known: list[str]) -> None:
        self.shard_key = shard_key
        self.known = known
        super().__init__(
            f"No configuration for shard {shard_key!r}; refusing to open a "
            f"session. This tenant is assigned to dedicated infrastructure and "
            f"MUST NOT be served from the shared cluster. "
            f"Known shards: {known}",
            port="ConnectionRouterPort",
            code="shard_unavailable",
            shard_key=shard_key,
        )


@runtime_checkable
class ConnectionRouterPort(Protocol):
    """Resolves a :class:`TenantContext` to the engine that owns its data.

    The single place in the platform that constructs a database engine. A
    subsystem holding a module-level engine defeats the §2 escape hatch
    entirely: relocating a tenant would then require finding and changing every
    such holder, which is exactly the code change §2 promises never to need.
    """

    async def engine_for(self, ctx: TenantContext) -> object:
        """The engine owning ``ctx``'s data.

        Raises:
            UnresolvableShardError: when ``ctx.shard_key`` is non-null and this
                process has no matching configuration (amendment A8). Never
                falls back to the default shard.
        """
        ...

    async def default_engine(self) -> object:
        """The shared cluster. For migrations, health checks, and ops jobs."""
        ...

    def known_shard_keys(self) -> set[str]:
        """Shards this process is configured to serve.

        Read by the readiness gate so a pod with stale configuration fails at
        deploy time — against a rolling update that can halt — rather than at
        request time against a real tenant.
        """
        ...

    async def dispose_all(self) -> None:
        """Close every pool. Called at process shutdown."""
        ...

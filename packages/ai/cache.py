"""The compiled-graph cache — 03 §4.4.

Keyed ``(tenant_id, manifest_version)``. Both halves are load-bearing:

* **tenant**, because a compiled graph closes over that tenant's adapters,
  template variables and customization text. Sharing one across tenants would
  put tenant A's business name in tenant B's prompt — a cross-tenant leak that
  RLS cannot see, because it never touches a row.
* **version**, because publishing a new manifest must not mutate an in-flight
  conversation's graph. A call that started on ``medical@1.0.0`` finishes on
  ``medical@1.0.0`` even if ``1.1.0`` ships mid-call. That is required for
  determinism when a checkpoint is resumed (03 §5.4): replaying old state into
  a graph whose agent set changed routes to a node that no longer exists.

**Process-local, not Redis.** A compiled graph holds Python closures over live
port adapters. It is not serializable, and pickling it would be both slow and a
deserialization attack surface. Compilation is cheap — single-digit
milliseconds for a six-agent manifest — so the cache exists to avoid paying it
on every one of thousands of turns, not because it is expensive.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import structlog
from packages.ai.compiler import CompiledGraph, GraphCompiler
from packages.verticals.manifest import VerticalManifest

__all__ = ["CompiledGraphCache", "ManifestLoader"]

log = structlog.get_logger(__name__)

#: Resolves a tenant (and optionally a pinned version) to a manifest. Supplied
#: by ``apps/api``, which reads it from the database. A callable rather than a
#: repository so this package performs no I/O and needs no session.
ManifestLoader = Callable[[UUID, str | None], Awaitable[VerticalManifest]]

#: Builds the compiler for one tenant. Per-tenant because the node factory
#: closes over that tenant's template variables and customization text.
CompilerFactory = Callable[[UUID, VerticalManifest], GraphCompiler]


@dataclass(slots=True)
class _Entry:
    graph: CompiledGraph
    compiled_at: float
    hits: int = 0


class CompiledGraphCache:
    """An LRU of compiled graphs with single-flight compilation."""

    def __init__(
        self,
        *,
        loader: ManifestLoader,
        compiler_factory: CompilerFactory,
        max_entries: int = 512,
        ttl_seconds: float = 3600.0,
    ) -> None:
        self._loader = loader
        self._compiler_factory = compiler_factory
        self._max = max_entries
        self._ttl = ttl_seconds
        self._entries: OrderedDict[tuple[UUID, str], _Entry] = OrderedDict()
        # One lock per key rather than one global lock: a cold compile for
        # tenant A must not block a cache *hit* for tenant B, and a single lock
        # would serialize every lookup behind the slowest compile.
        self._locks: dict[tuple[UUID, str], asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def get(
        self, tenant_id: UUID, manifest_version: str | None = None
    ) -> CompiledGraph:
        """``manifest_version=None`` means "whatever is live now".

        Used when a conversation starts. A resume always passes the pinned
        version it recorded, so it gets the graph it was compiled against.
        """
        manifest = await self._loader(tenant_id, manifest_version)
        key = (tenant_id, manifest.cache_version)

        entry = self._entries.get(key)
        if entry is not None and (time.monotonic() - entry.compiled_at) < self._ttl:
            entry.hits += 1
            self._entries.move_to_end(key)
            return entry.graph

        lock = await self._lock_for(key)
        async with lock:
            # Double-checked: a caller that waited on the lock while another
            # compiled the same key must take the result rather than compiling
            # it again. Without this, a cold cache under a traffic spike
            # compiles the same manifest once per concurrent call.
            entry = self._entries.get(key)
            if entry is not None and (time.monotonic() - entry.compiled_at) < self._ttl:
                entry.hits += 1
                self._entries.move_to_end(key)
                return entry.graph

            compiler = self._compiler_factory(tenant_id, manifest)
            graph = compiler.compile(manifest)
            self._entries[key] = _Entry(graph=graph, compiled_at=time.monotonic())
            self._entries.move_to_end(key)
            self._evict()
            log.info(
                "graph.cache.compiled",
                tenant_id=str(tenant_id),
                manifest=manifest.cache_version,
                entries=len(self._entries),
            )
            return graph

    async def _lock_for(self, key: tuple[UUID, str]) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def _evict(self) -> None:
        while len(self._entries) > self._max:
            evicted, _ = self._entries.popitem(last=False)
            self._locks.pop(evicted, None)

    def invalidate(self, tenant_id: UUID, manifest_version: str | None = None) -> None:
        """Drop cached graphs for a tenant.

        Called when a manifest is republished. Note it is not *required* for
        correctness — a new version produces a new key, so a republish is
        already invisible to running conversations — but it bounds memory when
        a tenant iterates on a manifest during authoring.
        """
        for key in [
            k
            for k in self._entries
            if k[0] == tenant_id
            and (manifest_version is None or k[1] == manifest_version)
        ]:
            self._entries.pop(key, None)
            self._locks.pop(key, None)

    @property
    def size(self) -> int:
        return len(self._entries)

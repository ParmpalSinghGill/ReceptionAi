"""``packages.persistence`` — SQLAlchemy models, repositories, RLS session mgmt.

Contract §9 places this package below the apps and above nothing: it imports
``packages.core`` (ports, domain types, errors) and no other first-party package.

Three things live here and the ordering between them matters:

* :mod:`~packages.persistence.engine` — the ``ConnectionRouter``, the only place
  in the codebase that constructs a database engine (contract §2).
* :mod:`~packages.persistence.session` — the session factory that sets
  ``app.tenant_id`` through a bind parameter. The most security-critical code in
  the platform.
* :mod:`~packages.persistence.models` / :mod:`~packages.persistence.repositories`
  — the schema and the sanctioned access paths.
"""

from __future__ import annotations

from packages.persistence.engine import (
    ConnectionRouter,
    ShardConfig,
    UnresolvableShardError,
    check_shard_configuration,
)
from packages.persistence.session import SessionFactory, TenantSession

__all__ = [
    "ConnectionRouter",
    "SessionFactory",
    "ShardConfig",
    "TenantSession",
    "UnresolvableShardError",
    "check_shard_configuration",
]

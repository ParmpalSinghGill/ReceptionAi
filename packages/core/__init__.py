"""``packages.core`` — the domain layer.

Contract §9: *"`core` imports nothing from other packages."* It holds the domain
model, the port definitions, the error vocabulary, and the identity types. It
performs no I/O and imports no vendor SDK. ``tools/check_layering.py`` enforces
this by parsing the AST of every file here.

The rule is not decoration. If ``core`` may import ``persistence``, then a port
is a suggestion and the hexagon is a diagram: the direction of the dependency is
the only thing that makes "swap the adapter" a config change rather than a
refactor.
"""

from __future__ import annotations

from packages.core.errors import (
    AuthenticationError,
    AuthorizationError,
    ConcurrencyConflictError,
    ConfigurationError,
    CrossTenantAccessError,
    DomainError,
    EntityNotFoundError,
    PortUnavailableError,
    RateLimitedError,
    ResourceConflictError,
    StepUpRequiredError,
    TenantStateError,
    ValidationError,
)
from packages.core.ids import new_id, uuid7, uuid7_timestamp
from packages.core.tenancy import (
    MissingTenantContextError,
    TenantContext,
    TenantSession,
    get_tenant_context,
    reset_tenant_context,
    set_tenant_context,
    try_get_tenant_context,
)

__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "ConcurrencyConflictError",
    "ConfigurationError",
    "CrossTenantAccessError",
    "DomainError",
    "EntityNotFoundError",
    "MissingTenantContextError",
    "PortUnavailableError",
    "RateLimitedError",
    "ResourceConflictError",
    "StepUpRequiredError",
    "TenantContext",
    "TenantSession",
    "TenantStateError",
    "ValidationError",
    "get_tenant_context",
    "new_id",
    "reset_tenant_context",
    "set_tenant_context",
    "try_get_tenant_context",
    "uuid7",
    "uuid7_timestamp",
]

"""Request middleware.

Order is fixed by the app factory and is not arbitrary:

1. :class:`~apps.api.middleware.request_id.RequestIdMiddleware` — first,
   because every layer below it (including the error builder) stamps the id
   into its output, and an error emitted before the id exists is an error
   nobody can correlate to a log line.
2. :class:`~apps.api.middleware.tenant_context.TenantContextMiddleware` —
   resolves the principal. Must precede the rate limiter, because limits are
   per-tenant and therefore need the resolved tenant (05 §A3.3).
3. :class:`~apps.api.middleware.rate_limit.RateLimitMiddleware` — last, so it
   can key on the tenant the layer above resolved.

Starlette applies middleware in reverse registration order, so the factory adds
them bottom-up. That inversion is a genuine trap and is called out at the
registration site.
"""

from __future__ import annotations

from apps.api.middleware.errors import install_exception_handlers
from apps.api.middleware.rate_limit import RateLimiter, RateLimitMiddleware, Rule
from apps.api.middleware.request_id import RequestIdMiddleware
from apps.api.middleware.tenant_context import (
    TenantContextMiddleware,
    current_principal,
)

__all__ = [
    "RateLimitMiddleware",
    "RateLimiter",
    "RequestIdMiddleware",
    "Rule",
    "TenantContextMiddleware",
    "current_principal",
    "install_exception_handlers",
]

"""Authentication and authorization machinery for the API layer.

The *types* live in ``packages/core`` (``Principal``, the error vocabulary);
what lives here is the HTTP-specific half — minting and verifying tokens,
hashing passwords, the RBAC route dependency, and the startup assertion that
every route carries an explicit authorization decision.
"""

from __future__ import annotations

from apps.api.security.passwords import hash_password, verify_password
from apps.api.security.permissions import (
    ROLE_GRANTS,
    STEP_UP_REQUIRED,
    Perm,
)
from apps.api.security.route_audit import PUBLIC_ROUTES, assert_all_routes_guarded
from apps.api.security.tokens import AccessClaims, TokenService

__all__ = [
    "PUBLIC_ROUTES",
    "ROLE_GRANTS",
    "STEP_UP_REQUIRED",
    "AccessClaims",
    "Perm",
    "TokenService",
    "assert_all_routes_guarded",
    "hash_password",
    "verify_password",
]

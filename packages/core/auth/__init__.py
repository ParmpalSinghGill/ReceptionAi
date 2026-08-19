"""Authentication and authorization domain types.

The :class:`~packages.core.auth.principal.Principal` is the only representation
of an authenticated caller in the platform. It lives in ``core`` because both
``apps/api`` (which constructs it from a token) and ``packages/persistence``
(which derives a :class:`~packages.core.tenancy.TenantContext` from it) need it,
and neither may depend on the other.
"""

from __future__ import annotations

from packages.core.auth.principal import PRINCIPAL_KINDS, Principal, PrincipalKind

__all__ = ["PRINCIPAL_KINDS", "Principal", "PrincipalKind"]

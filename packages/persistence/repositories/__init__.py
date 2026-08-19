"""Repositories — the only sanctioned way to reach a table.

Every one of them takes a :class:`~packages.persistence.session.TenantSession`
and carries an explicit ``tenant_id`` predicate on top of what RLS already
enforces (contract §8's "re-checked in the service layer").
"""

from __future__ import annotations

from packages.persistence.repositories.base import TenantRepository
from packages.persistence.repositories.conversations import (
    ConversationRepository,
    TurnRepository,
)
from packages.persistence.repositories.tenants import (
    LocationRepository,
    TenantRepo,
    TenantSettingRepository,
)
from packages.persistence.repositories.users import (
    MembershipRepository,
    UserRepository,
)

__all__ = [
    "ConversationRepository",
    "LocationRepository",
    "MembershipRepository",
    "TenantRepo",
    "TenantRepository",
    "TenantSettingRepository",
    "TurnRepository",
    "UserRepository",
]

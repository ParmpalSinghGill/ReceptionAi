"""Every mapped model, imported for its side effect of registering with
``Base.metadata``.

``migrations/env.py`` imports this module and nothing else from the model layer.
That single import is what makes ``target_metadata`` complete: a model in a file
nobody imports is invisible to autogenerate, so it silently never gets a
migration — and then silently never gets RLS.
"""

from __future__ import annotations

from packages.persistence.models.base import (
    Base,
    SoftDeleteMixin,
    TenantMixin,
    VersionedMixin,
    tenant_table_names,
)
from packages.persistence.models.conversations import Conversation, Turn
from packages.persistence.models.enums import (
    ConversationChannel,
    ConversationOutcome,
    ConversationStatus,
    OutcomeKind,
    TenantStatus,
    TurnRole,
    UserStatus,
)
from packages.persistence.models.identity import (
    Permission,
    Role,
    RolePermission,
    Session,
    User,
    UserTenantRole,
)
from packages.persistence.models.tenancy import Location, Tenant, TenantSetting

__all__ = [
    "Base",
    "Conversation",
    "ConversationChannel",
    "ConversationOutcome",
    "ConversationStatus",
    "Location",
    "OutcomeKind",
    "Permission",
    "Role",
    "RolePermission",
    "Session",
    "SoftDeleteMixin",
    "Tenant",
    "TenantMixin",
    "TenantSetting",
    "TenantStatus",
    "Turn",
    "TurnRole",
    "User",
    "UserStatus",
    "UserTenantRole",
    "VersionedMixin",
    "tenant_table_names",
]

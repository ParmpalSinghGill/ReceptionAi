"""PostgreSQL ENUM types — 01 §1.7, the Phase 1 subset.

Enums are used for closed vocabularies the *platform* owns and that appear in
indexed predicates (``status``, ``channel``, ``role``); ``TEXT`` + ``CHECK`` is
used for vocabularies a vertical manifest might extend.

Three reasons enums are worth their alter-pain: 4 bytes instead of a
variable-length text (which matters on a 100M-row ``turns``), far better planner
selectivity on a bounded domain, and an invalid value rejected at write time
rather than discovered in a dashboard.

``create_type=False`` on every :class:`~sqlalchemy.dialects.postgresql.ENUM`
below: the types are created once by the initial migration, in dependency order,
before any table references them. Leaving SQLAlchemy to emit ``CREATE TYPE``
lazily at table-create time produces a different ordering under
``metadata.create_all`` than under Alembic, which is exactly the kind of drift
that makes a migration pass in tests and fail on deploy.
"""

from __future__ import annotations

import enum

from sqlalchemy.dialects.postgresql import ENUM, REGCONFIG
from sqlalchemy.dialects.postgresql.base import ischema_names

# SQLAlchemy's reflection map does not know `regconfig`, so inspecting
# `tenants.default_text_search_config` emits "Did not recognize type" and
# reflects it as NullType. Registering it makes reflection — and therefore the
# schema-parity test — see the same type the model declares.
ischema_names.setdefault("regconfig", REGCONFIG)

__all__ = [
    "ConversationChannel",
    "ConversationStatus",
    "OutcomeKind",
    "TenantStatus",
    "TurnRole",
    "UserStatus",
    "conversation_channel_enum",
    "conversation_status_enum",
    "outcome_kind_enum",
    "tenant_status_enum",
    "turn_role_enum",
    "user_status_enum",
]


class TenantStatus(enum.StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


class UserStatus(enum.StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    DISABLED = "disabled"
    LOCKED = "locked"


class ConversationChannel(enum.StrEnum):
    VOICE_INBOUND = "voice_inbound"
    VOICE_OUTBOUND = "voice_outbound"
    WEB_CHAT = "web_chat"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    EMAIL = "email"
    API = "api"


class ConversationStatus(enum.StrEnum):
    ACTIVE = "active"
    ENDED = "ended"
    FAILED = "failed"
    ABANDONED = "abandoned"


class TurnRole(enum.StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    EVENT = "event"


class OutcomeKind(enum.StrEnum):
    RESOLVED = "resolved"
    BOOKED = "booked"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    TRANSFERRED = "transferred"
    ABANDONED = "abandoned"
    FAILED = "failed"
    SPAM = "spam"


def _pg_enum(python_enum: type[enum.StrEnum], name: str) -> ENUM:
    """Bind a Python StrEnum to its PostgreSQL type.

    ``values_callable`` makes SQLAlchemy persist the enum *values* rather than
    the member *names* — without it, ``TenantStatus.PAST_DUE`` would be written
    as ``'PAST_DUE'`` and rejected by a type whose labels are lowercase.
    """
    return ENUM(
        python_enum,
        name=name,
        create_type=False,
        values_callable=lambda e: [member.value for member in e],
    )


tenant_status_enum = _pg_enum(TenantStatus, "tenant_status")
user_status_enum = _pg_enum(UserStatus, "user_status")
conversation_channel_enum = _pg_enum(ConversationChannel, "conversation_channel")
conversation_status_enum = _pg_enum(ConversationStatus, "conversation_status")
turn_role_enum = _pg_enum(TurnRole, "turn_role")
outcome_kind_enum = _pg_enum(OutcomeKind, "outcome_kind")

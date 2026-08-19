"""Conversation models — 01 §6.1, §6.2.

Contract §7 fixes the vocabulary: ``Conversation → Turn[] → Transcript? →
Outcome``. Voice and chat converge on one abstraction so agents are
channel-agnostic — a ``ChannelAdapter`` normalizes any inbound message into a
``Turn`` and renders any outbound ``Turn`` back to its channel, and agents never
know which channel they are on.

Both tables are **partitioned by month** on ``created_at`` (01 §17.1). They are
the two highest-volume tables in the system, retention on both is time-based,
and their queries are almost always time-bounded — so a monthly ``DROP TABLE``
replaces a ``DELETE`` that would otherwise never keep up.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from packages.persistence.models.base import Base
from packages.persistence.models.enums import (
    ConversationChannel,
    ConversationStatus,
    TurnRole,
    conversation_channel_enum,
    conversation_status_enum,
    turn_role_enum,
)
from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

__all__ = ["Conversation", "Turn"]


class Conversation(Base):
    """One conversation on any channel. 01 §6.1.

    **Why the primary key is ``(tenant_id, created_at, id)``.** PostgreSQL
    requires the partition key to be part of every unique constraint on a
    partitioned table, so ``created_at`` must appear in the PK. Leading with
    ``tenant_id`` keeps the contract's tenant-first rule intact and makes the PK
    index itself useful for the dominant query ("this tenant's conversations,
    newest first"). The cost is that a bare ``WHERE id = ?`` must scan every
    partition — so the application always carries ``(tenant_id, created_at)``
    alongside a conversation id, which UUIDv7 makes free: ``created_at`` is
    recoverable from ``id`` and the repository injects the window automatically.
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, server_default=text("uuid_generate_v7()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )

    channel: Mapped[ConversationChannel] = mapped_column(
        conversation_channel_enum, nullable=False
    )
    status: Mapped[ConversationStatus] = mapped_column(
        conversation_status_enum, nullable=False, server_default=text("'active'")
    )

    # Channel-native identity: a Twilio CallSid, a WhatsApp wa_id, a web session
    # id. Lets an inbound webhook find its conversation without a lookup table.
    external_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # ── Participants ──────────────────────────────────────────────────────
    # The end user. PII-classified; masked in logs and prompts unless the tenant
    # opted in (contract §8).
    contact_identifier: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The resolved cross-channel identity (01 §3.9), when there is one.
    # DELIBERATELY NULLABLE, per 04 §11.2.1(2): an anonymous web visitor has no
    # resolved customer, and making this NOT NULL would force the resolver to
    # fabricate an identity record for someone who never identified themselves —
    # creating the profile the privacy model exists to avoid. NULL is the
    # correct representation of "we do not know who this is".
    #
    # No declarative FK: `conversations` is partitioned, and PostgreSQL will not
    # support an FK *from* a partitioned table whose partitions are created
    # dynamically without re-establishing it per partition. Because the
    # guarantee here is a CROSS-TENANT one, dropping it silently is not
    # acceptable — it is enforced by `trg_conversations_customer_fk`, a
    # constraint trigger raising SQLSTATE 23503 so the repository's error
    # translator maps it exactly as it would a real FK. The trigger and the
    # `customers` table arrive with the phase that introduces customer
    # resolution; the column is here now so the partitioned table is not
    # rewritten later.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    # Hash for analytics joins that must not carry the raw identifier.
    contact_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # Which agent configuration ran. Snapshot the version, because manifests
    # change and we must be able to explain a transcript months later.
    vertical_install_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    agent_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Timing ────────────────────────────────────────────────────────────
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Aggregates, maintained incrementally ──────────────────────────────
    # Rolled up here so the conversation list view needs no aggregate over
    # `turns`, which is a partitioned, 100M-row table.
    turn_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    total_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    total_output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    total_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, server_default=text("0")
    )
    cost_currency: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, server_default=text("'USD'")
    )

    # Quality signals surfaced in the dashboard.
    first_response_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    p95_response_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interruption_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    escalated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    escalated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    escalation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    language: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Redaction state: whether PII has been scrubbed by the retention job.
    redacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    correlation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "created_at", "id", name="pk_conversations"),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="duration",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="end_after_start",
        ),
        CheckConstraint(
            "status <> 'ended' OR ended_at IS NOT NULL",
            name="ended_coherent",
        ),
        CheckConstraint(
            "escalated = (escalated_at IS NOT NULL)",
            name="escalation_coherent",
        ),
        CheckConstraint("total_cost >= 0", name="cost_nonneg"),
        # Indexes are declared on the parent; PostgreSQL creates them on every
        # partition, existing and future.
        Index("ix_conversations__tenant_started", "tenant_id", text("started_at DESC")),
        Index(
            "ix_conversations__tenant_channel_started",
            "tenant_id",
            "channel",
            text("started_at DESC"),
        ),
        Index(
            "ix_conversations__tenant_status",
            "tenant_id",
            "status",
            text("started_at DESC"),
            postgresql_where=text("status = 'active'"),
        ),
        # Inbound webhook resolution: "find the conversation for CallSid X".
        # created_at is in the key because a unique index on a partitioned table
        # must contain the partition key.
        Index(
            "uq_conversations__tenant_external",
            "tenant_id",
            "channel",
            "external_ref",
            "created_at",
            unique=True,
            postgresql_where=text("external_ref IS NOT NULL"),
        ),
        Index(
            "ix_conversations__tenant_contact",
            "tenant_id",
            "contact_identifier",
            text("started_at DESC"),
            postgresql_where=text("contact_identifier IS NOT NULL"),
        ),
        # Cross-channel history: "every conversation with this customer, any
        # channel". The query the agent runs at conversation start to recall
        # context, and the one the erasure path uses to find what to redact.
        Index(
            "ix_conversations__tenant_customer",
            "tenant_id",
            "customer_id",
            text("started_at DESC"),
            postgresql_where=text("customer_id IS NOT NULL"),
        ),
        Index(
            "ix_conversations__tenant_location",
            "tenant_id",
            "location_id",
            text("started_at DESC"),
            postgresql_where=text("location_id IS NOT NULL"),
        ),
        Index(
            "ix_conversations__escalated",
            "tenant_id",
            text("escalated_at DESC"),
            postgresql_where=text("escalated"),
        ),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )


class Turn(Base):
    """One message, tool call, or system event. The single largest table.
    01 §6.2.

    **No FK to ``conversations``.** Both are partitioned by ``created_at``, and
    a turn's ``created_at`` is always ≥ its conversation's — so the FK would
    have to be ``(tenant_id, conversation_created_at, conversation_id)``,
    forcing every child row to carry the parent's partition key. The referential
    guarantee is worth less than the write cost on the hottest table in the
    system, and the relationship is enforced in the repository, which never
    creates a turn outside a conversation aggregate. A considered exception, not
    an oversight — and unlike ``conversations.customer_id``, the guarantee given
    up here is *within* a tenant, so no cross-tenant property depends on it.
    """

    __tablename__ = "turns"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, server_default=text("uuid_generate_v7()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False
    )

    # Monotonic ordering within a conversation. Explicit, not derived from
    # created_at, because two turns can share a millisecond and because a
    # retried tool call must slot deterministically.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    role: Mapped[TurnRole] = mapped_column(turn_role_enum, nullable=False)

    # Rendered text. NULL for a pure tool-call turn with no assistant prose.
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured content for multimodal or channel-specific payloads.
    content_parts: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Tool interaction, matching the OpenAI/Anthropic shape.
    tool_calls: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # For role='tool', which call this answers.
    tool_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    tool_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Model accounting (contract §7) ────────────────────────────────────
    model_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 6 dp here rather than the standard 4: a single turn's cost is sub-cent and
    # rounding it at 4 dp before the roll-up destroys per-turn attribution.
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    cost_currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)

    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_to_first_token_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    finish_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Which agent node in the LangGraph produced this turn.
    agent_node: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Voice specifics.
    audio_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_interruption: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )

    # Safety and redaction.
    pii_redacted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    safety_flags: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    correlation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "created_at", "id", name="pk_turns"),
        CheckConstraint("sequence >= 0", name="sequence_nonneg"),
        CheckConstraint(
            "COALESCE(input_tokens, 0) >= 0 AND COALESCE(output_tokens, 0) >= 0 "
            "AND COALESCE(cached_tokens, 0) >= 0",
            name="tokens_nonneg",
        ),
        CheckConstraint("cost IS NULL OR cost >= 0", name="cost_nonneg"),
        CheckConstraint(
            "(cost IS NULL) = (cost_currency IS NULL)",
            name="cost_currency_paired",
        ),
        # A tool-result turn must say which call it answers.
        CheckConstraint(
            "role <> 'tool' OR tool_call_id IS NOT NULL",
            name="tool_role_shape",
        ),
        CheckConstraint(
            "audio_end_ms IS NULL OR audio_start_ms IS NULL "
            "OR audio_end_ms >= audio_start_ms",
            name="audio_order",
        ),
        CheckConstraint(
            "content IS NOT NULL OR content_parts IS NOT NULL "
            "OR tool_calls IS NOT NULL OR tool_result IS NOT NULL "
            "OR role = 'event'",
            name="content_present",
        ),
        # The dominant query by an enormous margin: "replay this conversation in
        # order". Every agent invocation runs it to rebuild context.
        Index(
            "uq_turns__conversation_sequence",
            "tenant_id",
            "conversation_id",
            "sequence",
            "created_at",
            unique=True,
        ),
        Index("ix_turns__tenant_created", "tenant_id", text("created_at DESC")),
        Index(
            "ix_turns__tenant_tool",
            "tenant_id",
            "tool_name",
            text("created_at DESC"),
            postgresql_where=text("tool_name IS NOT NULL"),
        ),
        Index(
            "ix_turns__tenant_model",
            "tenant_id",
            "model_provider",
            "model_name",
            text("created_at DESC"),
            postgresql_where=text("model_name IS NOT NULL"),
        ),
        # Safety review queue. A GIN index on an array cannot lead with
        # tenant_id, so it is one of the §11.7 declared exceptions.
        Index(
            "ix_turns__safety",
            "safety_flags",
            postgresql_using="gin",
            postgresql_where=text("safety_flags IS NOT NULL"),
        ),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

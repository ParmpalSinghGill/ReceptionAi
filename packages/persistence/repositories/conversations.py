"""Conversation and turn repositories — the partitioned write path.

Both tables are partitioned by month on ``created_at``, which changes two things
about every access:

* A point lookup must carry a ``created_at`` predicate or it scans every
  partition. ``partitioned = True`` makes the base repository derive that window
  from the UUIDv7 id, so callers never pass a timestamp.
* There is no FK from ``turns`` to ``conversations`` (01 §6.2). The referential
  guarantee is enforced *here*: :meth:`TurnRepository.append` is the only way a
  turn is created, and it will not create one outside a conversation aggregate.
  That is the promise the schema is relying on, so it lives in one method rather
  than being spread across callers.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from packages.core.errors import EntityNotFoundError, ValidationError
from packages.persistence.models.conversations import Conversation, Turn
from packages.persistence.models.enums import (
    ConversationChannel,
    ConversationStatus,
    TurnRole,
)
from packages.persistence.repositories.base import TenantRepository
from sqlalchemy import func, select, update

__all__ = ["ConversationRepository", "TurnRepository"]


class ConversationRepository(TenantRepository[Conversation]):
    model = Conversation
    partitioned = True

    async def start(
        self,
        *,
        channel: ConversationChannel,
        location_id: UUID | None = None,
        external_ref: str | None = None,
        contact_identifier: str | None = None,
        contact_name: str | None = None,
        customer_id: UUID | None = None,
        agent_code: str | None = None,
        agent_version: str | None = None,
        vertical_install_id: UUID | None = None,
        language: str | None = None,
        correlation_id: UUID | None = None,
        channel_metadata: dict[str, Any] | None = None,
    ) -> Conversation:
        """Open a conversation.

        ``customer_id`` stays None unless the caller resolved an identity: 04
        §11.2.1(2) is explicit that fabricating one for an anonymous visitor
        creates exactly the profile the privacy model exists to avoid.
        """
        conversation = Conversation(
            tenant_id=self.tenant_id,
            location_id=location_id,
            channel=channel,
            status=ConversationStatus.ACTIVE,
            external_ref=external_ref,
            contact_identifier=contact_identifier,
            contact_name=contact_name,
            customer_id=customer_id,
            agent_code=agent_code,
            agent_version=agent_version,
            vertical_install_id=vertical_install_id,
            language=language,
            correlation_id=correlation_id,
            channel_metadata=channel_metadata or {},
            started_at=datetime.now(tz=UTC),
        )
        return await self.add(conversation)

    async def by_external_ref(
        self, *, channel: ConversationChannel, external_ref: str
    ) -> Conversation | None:
        """Inbound webhook resolution: "find the conversation for CallSid X".

        Hits ``uq_conversations__tenant_external``. Ordered newest-first and
        limited to one because the unique index includes ``created_at`` (a
        partitioned table's unique index must contain the partition key), so the
        same ``external_ref`` *can* legitimately recur in a later month — a
        recycled CallSid, or a channel that reuses session ids. The live one is
        the most recent.
        """
        stmt = (
            self._base_select()
            .where(
                Conversation.channel == channel,
                Conversation.external_ref == external_ref,
            )
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def recent(
        self,
        *,
        status: ConversationStatus | None = None,
        channel: ConversationChannel | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Conversation]:
        """The admin portal's landing query."""
        filters = []
        if status is not None:
            filters.append(Conversation.status == status)
        if channel is not None:
            filters.append(Conversation.channel == channel)
        return await self.list(
            *filters,
            limit=limit,
            offset=offset,
            order_by=Conversation.started_at.desc(),
        )

    async def end(
        self,
        conversation_id: UUID,
        *,
        status: ConversationStatus = ConversationStatus.ENDED,
        ended_at: datetime | None = None,
    ) -> Conversation:
        """Close a conversation, satisfying ``ck_conversations__ended_coherent``.

        ``duration_ms`` is computed here rather than by a trigger so the value
        and the status change land in one statement — a trigger would leave a
        window in which a row is ``ended`` with a null duration, which every
        dashboard would then have to defend against.
        """
        conversation = await self.get_or_raise(conversation_id)
        finished = ended_at or datetime.now(tz=UTC)
        if finished < conversation.started_at:
            raise ValidationError(
                "ended_at precedes started_at", field="ended_at"
            )
        conversation.status = status
        conversation.ended_at = finished
        conversation.duration_ms = int(
            (finished - conversation.started_at).total_seconds() * 1000
        )
        await self.session.flush()
        return conversation

    async def escalate(self, conversation_id: UUID, *, reason: str) -> Conversation:
        conversation = await self.get_or_raise(conversation_id)
        conversation.escalated = True
        conversation.escalated_at = datetime.now(tz=UTC)
        conversation.escalation_reason = reason
        await self.session.flush()
        return conversation


class TurnRepository(TenantRepository[Turn]):
    model = Turn
    partitioned = True

    async def append(
        self,
        *,
        conversation: Conversation,
        role: TurnRole,
        content: str | None = None,
        content_parts: dict[str, Any] | None = None,
        tool_calls: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        tool_result: dict[str, Any] | None = None,
        tool_error: str | None = None,
        tool_duration_ms: int | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        cost: Decimal | None = None,
        cost_currency: str | None = None,
        latency_ms: int | None = None,
        time_to_first_token_ms: int | None = None,
        finish_reason: str | None = None,
        agent_node: str | None = None,
        audio_start_ms: int | None = None,
        audio_end_ms: int | None = None,
        is_interruption: bool = False,
        safety_flags: list[str] | None = None,
        correlation_id: UUID | None = None,
    ) -> Turn:
        """Append a turn and roll the conversation's aggregates forward.

        **This is the only sanctioned way to create a turn**, and taking a
        :class:`Conversation` object rather than a bare id is the enforcement:
        01 §6.2 dropped the FK from ``turns`` to ``conversations`` because
        carrying the parent's partition key on the hottest table in the system
        costs more than the guarantee is worth, on the explicit condition that
        "the relationship is enforced in the repository, which never creates a
        turn outside a conversation aggregate". A signature that cannot express
        an orphan turn is that enforcement.

        The aggregate roll-up shares this transaction, so ``turn_count`` and the
        token totals can never disagree with the rows they summarize.
        """
        if conversation.tenant_id != self.tenant_id:
            # Unreachable through RLS, which would have hidden the row — but a
            # conversation loaded in one unit of work and used in another would
            # slip past that, and this is cheaper than reasoning about whether
            # it can.
            raise ValidationError(
                "conversation belongs to another tenant", field="conversation"
            )

        sequence = await self._next_sequence(conversation.id)

        turn = Turn(
            tenant_id=self.tenant_id,
            conversation_id=conversation.id,
            sequence=sequence,
            role=role,
            content=content,
            content_parts=content_parts,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_result=tool_result,
            tool_error=tool_error,
            tool_duration_ms=tool_duration_ms,
            model_provider=model_provider,
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
            cost=cost,
            cost_currency=cost_currency,
            latency_ms=latency_ms,
            time_to_first_token_ms=time_to_first_token_ms,
            finish_reason=finish_reason,
            agent_node=agent_node,
            audio_start_ms=audio_start_ms,
            audio_end_ms=audio_end_ms,
            is_interruption=is_interruption,
            safety_flags=safety_flags,
            correlation_id=correlation_id,
        )
        await self.add(turn)
        await self._roll_up(conversation, turn)
        return turn

    async def _next_sequence(self, conversation_id: UUID) -> int:
        """The next ordinal within a conversation.

        Explicit rather than derived from ``created_at``, because two turns can
        share a millisecond and because a retried tool call must slot
        deterministically. ``COALESCE(MAX(...) + 1, 0)`` under the enclosing
        transaction is sufficient: a single conversation has exactly one writer
        (its agent run), and two concurrent writers would collide on
        ``uq_turns__conversation_sequence`` rather than silently interleave.
        """
        stmt = select(func.coalesce(func.max(Turn.sequence) + 1, 0)).where(
            Turn.tenant_id == self.tenant_id, Turn.conversation_id == conversation_id
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def _roll_up(self, conversation: Conversation, turn: Turn) -> None:
        """Maintain ``conversations``' incremental aggregates.

        An UPDATE with server-side arithmetic rather than read-modify-write in
        Python: the latter would lose a concurrent increment, and these counters
        exist precisely so the list view never aggregates over a 100M-row
        partitioned table.

        The ``created_at`` predicate is what makes this a single-partition
        update rather than a scan of every month.
        """
        stmt = (
            update(Conversation)
            .where(
                Conversation.tenant_id == self.tenant_id,
                Conversation.id == conversation.id,
                Conversation.created_at == conversation.created_at,
            )
            .values(
                turn_count=Conversation.turn_count + 1,
                total_input_tokens=Conversation.total_input_tokens
                + (turn.input_tokens or 0),
                total_output_tokens=Conversation.total_output_tokens
                + (turn.output_tokens or 0),
                total_cost=Conversation.total_cost + (turn.cost or Decimal(0)),
                interruption_count=Conversation.interruption_count
                + (1 if turn.is_interruption else 0),
            )
        )
        result = await self.session.execute(stmt)
        if result.rowcount == 0:
            raise EntityNotFoundError(
                entity_type="conversations", entity_id=conversation.id
            )

    async def replay(
        self, conversation_id: UUID, *, limit: int = 500
    ) -> Sequence[Turn]:
        """"Replay this conversation in order" — the dominant query.

        Every agent invocation runs it to rebuild context, which is why
        ``uq_turns__conversation_sequence`` leads with
        ``(tenant_id, conversation_id, sequence)``.
        """
        stmt = (
            self._base_select()
            .where(Turn.conversation_id == conversation_id)
            .order_by(Turn.sequence.asc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

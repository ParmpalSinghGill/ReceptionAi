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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from packages.core.errors import EntityNotFoundError, ValidationError
from packages.persistence.models.conversations import Conversation, Turn
from packages.persistence.models.enums import (
    ConversationChannel,
    ConversationOutcome,
    ConversationStatus,
    TurnRole,
)
from packages.persistence.repositories.base import TenantRepository
from sqlalchemy import func, select, tuple_, update

__all__ = ["ConversationPage", "ConversationRepository", "ConversationRow", "TurnRepository"]


#: How far ``created_at`` may lag or lead ``started_at`` before a partition
#: predicate derived from one would exclude a row selected by the other. They
#: are set microseconds apart by :meth:`ConversationRepository.start`, so a day
#: is orders of magnitude more slack than reality needs — and still narrow
#: enough to prune all but the one or two partitions a page touches.
_PARTITION_SKEW = timedelta(days=1)


def _escape_like(term: str) -> str:
    """Neutralise LIKE wildcards in user input.

    Without this, a search for ``100%`` matches every conversation and a search
    for ``_`` matches all of them too — the user typed a literal string and got
    a pattern. The backslash must be escaped first or it would escape the
    escapes added after it.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True, slots=True)
class ConversationRow:
    """One row of the list view — a conversation plus the bits that make it
    readable at a glance.

    A dataclass rather than the ORM entity because two of these fields are not
    columns on ``conversations``: ``opening_line`` comes from the first user
    turn, and ``last_turn_at`` from the newest. Returning the entity and letting
    the router lazy-load them would be an N+1 over a partitioned table — one
    extra query per row, per page.
    """

    conversation: Conversation
    #: The first thing the caller said. ``None`` when the conversation has no
    #: user turn yet (a greeting was sent and nobody replied).
    opening_line: str | None
    last_turn_at: datetime | None


@dataclass(frozen=True, slots=True)
class ConversationPage:
    """``rows`` may hold one more than the caller's limit — see
    :func:`packages.core.api.pagination.page_from`, which trims it and turns
    the surplus into ``has_more``."""

    rows: list[ConversationRow]


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

    async def page(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, UUID] | None = None,
        channel: ConversationChannel | None = None,
        status: ConversationStatus | None = None,
        started_from: datetime | None = None,
        started_to: datetime | None = None,
        search: str | None = None,
    ) -> ConversationPage:
        """One keyset page of the list view, newest first.

        **Why the sort is ``(started_at, id)`` and not ``started_at`` alone.**
        A cursor has to name exactly one row, and ``started_at`` does not: this
        tenant opens several web chats in the same millisecond during a busy
        hour, and a cursor on the timestamp alone would either skip the rest of
        that millisecond or replay it forever. The id breaks the tie, and the
        pair is compared as a row value — ``(a, b) < (x, y)`` — which Postgres
        executes as a single index range scan. The hand-written equivalent
        (``a < x OR (a = x AND b < y)``) is an OR the planner routinely refuses
        to collapse into one scan, giving a BitmapOr over two instead: the same
        rows, a materially worse plan, and the gap widens with every partition.

        **Why ``created_at`` is constrained alongside ``started_at``.** The
        partition key is ``created_at``; the sort is ``started_at``. Ordering by
        a column the table is not partitioned on would read every partition, so
        the keyset predicate is also applied to ``created_at``, which is what
        lets the planner stop after the first partition that satisfies it. The
        two are written within microseconds of each other by
        :meth:`start`, so a window of a day either side is far wider than any
        real skew and still prunes all but one or two partitions.

        ``limit + 1`` rows are fetched so the caller can answer "is there more"
        without a COUNT over a partitioned table.
        """
        fetch = limit + 1

        stmt = self._base_select()

        if channel is not None:
            stmt = stmt.where(Conversation.channel == channel)
        if status is not None:
            stmt = stmt.where(Conversation.status == status)
        if started_from is not None:
            # Both columns, for the pruning reason above.
            stmt = stmt.where(
                Conversation.started_at >= started_from,
                Conversation.created_at >= started_from - _PARTITION_SKEW,
            )
        if started_to is not None:
            stmt = stmt.where(
                Conversation.started_at < started_to,
                Conversation.created_at < started_to + _PARTITION_SKEW,
            )

        if search:
            # A conversation matches when any of its turns does. EXISTS rather
            # than a JOIN: a JOIN multiplies the conversation row by the number
            # of matching turns and then needs a DISTINCT to undo it, which
            # forces a sort over the whole result and defeats the keyset scan.
            # EXISTS stops at the first matching turn per conversation.
            #
            # The tenant predicate is repeated inside the subquery. RLS already
            # applies it, but the planner uses the literal one to pick the
            # tenant-leading index rather than relying on the RLS predicate
            # being pushed down.
            needle = f"%{_escape_like(search)}%"
            matching_turn = (
                select(1)
                .select_from(Turn)
                .where(
                    Turn.tenant_id == self.tenant_id,
                    Turn.conversation_id == Conversation.id,
                    Turn.content.ilike(needle, escape="\\"),
                )
                .exists()
            )
            stmt = stmt.where(matching_turn)

        if cursor is not None:
            anchor_ts, anchor_id = cursor
            stmt = stmt.where(
                tuple_(Conversation.started_at, Conversation.id)
                < tuple_(anchor_ts, anchor_id),
                # Again: prune partitions on the key the table is actually
                # partitioned by. Without this the scan walks every month back
                # to the beginning of the tenant's history on every page.
                Conversation.created_at < anchor_ts + _PARTITION_SKEW,
            )

        stmt = stmt.order_by(
            Conversation.started_at.desc(), Conversation.id.desc()
        ).limit(fetch)

        conversations = list((await self.session.execute(stmt)).scalars().all())
        if not conversations:
            return ConversationPage(rows=[])

        openings, last_turns = await self._turn_previews(conversations)

        return ConversationPage(
            rows=[
                ConversationRow(
                    conversation=conversation,
                    opening_line=openings.get(conversation.id),
                    last_turn_at=last_turns.get(conversation.id),
                )
                for conversation in conversations
            ]
        )

    async def _turn_previews(
        self, conversations: list[Conversation]
    ) -> tuple[dict[UUID, str], dict[UUID, datetime]]:
        """The first caller line and the last turn time, for a whole page.

        **One query for the page, not one per row.** The obvious implementation
        — loop the conversations and ask for each one's first turn — is an N+1
        that costs 50 round trips against a partitioned table for a 50-row
        page. ``DISTINCT ON`` gets the same answer in one.

        The ``created_at`` window is derived from the page's own conversations,
        so the turn scan is bounded by the span the page covers rather than by
        the tenant's whole history. A turn is written at or after its
        conversation starts, so the window opens at the oldest conversation on
        the page (less the skew margin) and is open-ended at the top.
        """
        ids = [c.id for c in conversations]
        oldest = min(c.created_at for c in conversations) - _PARTITION_SKEW

        # DISTINCT ON (conversation_id) with an ORDER BY that puts the wanted
        # row first: first USER turn by sequence for the opening line.
        opening_stmt = (
            select(Turn.conversation_id, Turn.content)
            .distinct(Turn.conversation_id)
            .where(
                Turn.tenant_id == self.tenant_id,
                Turn.conversation_id.in_(ids),
                Turn.created_at >= oldest,
                Turn.role == TurnRole.USER,
                Turn.content.is_not(None),
            )
            .order_by(Turn.conversation_id, Turn.sequence.asc())
        )
        openings = {
            row.conversation_id: row.content
            for row in (await self.session.execute(opening_stmt)).all()
        }

        # The newest turn's timestamp, per conversation. This is what "last
        # activity" means on the list: `ended_at` is null while a conversation
        # is still open, and `started_at` does not move, so neither answers
        # "when did anything last happen here".
        last_stmt = (
            select(
                Turn.conversation_id,
                func.max(Turn.created_at).label("last_turn_at"),
            )
            .where(
                Turn.tenant_id == self.tenant_id,
                Turn.conversation_id.in_(ids),
                Turn.created_at >= oldest,
            )
            .group_by(Turn.conversation_id)
        )
        last_turns = {
            row.conversation_id: row.last_turn_at
            for row in (await self.session.execute(last_stmt)).all()
        }

        return openings, last_turns

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

    async def record_outcome(
        self, conversation_id: UUID, *, outcome: ConversationOutcome
    ) -> Conversation:
        """Write the outcome a terminal agent declared. Last writer wins.

        **The precedence rule is "the most recent turn is right", and it is a
        deliberate choice over the two obvious alternatives.**

        The alternatives are worse in ways that show up in real conversations:

        * *First writer wins* — freeze the outcome once set. Then a caller who
          books an appointment and, two turns later, changes their mind and
          cancels it is recorded forever as ``booked``. The row disagrees with
          the diary, and the "bookings from conversations" number is inflated by
          exactly the people who decided against one.
        * *Rank the outcomes* — let ``booked`` outrank ``resolved`` because it
          is the more valuable result. This is the same bug wearing a
          justification: it encodes "we would rather it were a booking" as
          "it was a booking".

        Recency is right because the outcome is a statement about *the
        conversation*, not about any single moment in it, and the terminal agent
        that speaks last is the one that saw all of it. Someone who books and
        then cancels ends ``resolved`` — the conversation did resolve; what it
        resolved to was no appointment. That is what the transcript says and it
        is what the diary says.

        Cheap enough to call on every turn that produces an outcome: it is one
        UPDATE on a row the caller already has, and the common case is a turn
        that declares nothing and never reaches here at all.
        """
        conversation = await self.get_or_raise(conversation_id)
        conversation.outcome = outcome
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

    async def retract_latest_reply(self, *, conversation: Conversation) -> Turn | None:
        """Mark the conversation's newest turn as an interrupted reply.

        The voice worker calls this when a reply was generated but never spoken:
        the caller was still talking when it was ready, so the follow-up turn
        supersedes it. The row stays — it is what the model produced and what
        it cost — but ``is_interruption`` is set, which (a) drops it from
        :meth:`replay`-fed history so the next turn does not read it as
        something the caller heard and answered, and (b) rolls into
        ``conversations.interruption_count`` like any other interruption.

        Only the NEWEST turn is eligible, and only if it is an assistant turn.
        Anything else means the caller's next utterance is already on record
        and the reply in question *was* heard; retracting it would rewrite
        history that actually happened. Returns the turn, or ``None`` when
        nothing was retracted.
        """
        if conversation.tenant_id != self.tenant_id:
            raise EntityNotFoundError(
                entity_type="conversations", entity_id=conversation.id
            )
        stmt = (
            self._base_select()
            .where(Turn.conversation_id == conversation.id)
            .order_by(Turn.sequence.desc())
            .limit(1)
        )
        latest = (await self.session.execute(stmt)).scalars().first()
        if latest is None or latest.role != TurnRole.ASSISTANT or latest.is_interruption:
            return None
        latest.is_interruption = True
        await self.session.flush()
        await self.session.execute(
            update(Conversation)
            .where(
                Conversation.tenant_id == self.tenant_id,
                Conversation.id == conversation.id,
                Conversation.created_at == conversation.created_at,
            )
            .values(interruption_count=Conversation.interruption_count + 1)
        )
        return latest

    async def withdraw_latest_user_turn(
        self, *, conversation: Conversation, content: str
    ) -> Turn | None:
        """Withdraw a caller line recorded for a speculative turn that missed.

        A speculative voice turn records the caller's words on an *early*
        end-of-turn guess. When the caller turns out to have been pausing, the
        confirmed turn carries the complete sentence — and the half-sentence row
        must leave the model's view, or history replays it in front of the
        whole: "book me for Thursday book me for Thursday at eleven".

        Uses ``is_interruption`` on a USER row to mean "withdrawn"; history
        replay skips such rows. The row itself stays, as a record of what was
        heard. ``interruption_count`` is not incremented: nobody interrupted
        anybody, the system guessed early.

        Guarded like :meth:`retract_latest_reply`: the row must be the newest
        user turn, everything after it must be an already-retracted reply or a
        tool/event row from the cancelled run, and its text must match. Anything
        else is history that really happened. Returns the row, or ``None``.
        """
        if conversation.tenant_id != self.tenant_id:
            raise EntityNotFoundError(
                entity_type="conversations", entity_id=conversation.id
            )
        stmt = (
            self._base_select()
            .where(Turn.conversation_id == conversation.id)
            .order_by(Turn.sequence.desc())
            .limit(10)
        )
        for turn in (await self.session.execute(stmt)).scalars():
            if turn.role in (TurnRole.TOOL, TurnRole.EVENT):
                continue
            if turn.role == TurnRole.ASSISTANT:
                if turn.is_interruption:
                    continue
                return None
            if turn.role != TurnRole.USER or turn.is_interruption:
                return None
            if (turn.content or "").strip() != content.strip():
                return None
            turn.is_interruption = True
            await self.session.flush()
            return turn
        return None

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

"""Conversation orchestration: persist, run, stream, persist.

The sequence one user turn goes through, and the reason for its order:

1. **Persist the user turn first**, in its own transaction, before any model
   call. If the provider is down, the customer's message is still on the record
   — losing what someone typed because our dependency failed is not acceptable,
   and it is what makes a retry meaningful.
2. **Run the graph and stream**, holding no database transaction. A turn takes
   seconds; a transaction held across it pins a connection and a snapshot for
   that whole time, which is how an RLS application bloats a table and starves
   its pool. This is the reason the session is *not* threaded through the
   stream.
3. **Persist the assistant turn afterwards**, in a fresh transaction — and
   also on the disconnect path, because a customer who closes the tab
   mid-answer must still find the conversation intact when they come back.

The service takes a *session factory* rather than a session for exactly that
reason: it opens and closes several short transactions across one streamed
turn, which a request-scoped session dependency cannot express.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from apps.api.services.manifests import ManifestInstallService
from packages.ai.cache import CompiledGraphCache
from packages.ai.compiler import GraphCompiler
from packages.ai.nodes import NodeFactory
from packages.ai.runner import GraphRunner, TurnEvent, TurnResult
from packages.ai.tools import ToolExecutor, ToolRegistry
from packages.core.errors import EntityNotFoundError
from packages.core.ids import uuid7
from packages.core.ports.llm import LLMPort, Message
from packages.core.ports.tracing import TracingPolicy
from packages.core.tenancy import TenantContext
from packages.persistence.models.conversations import Conversation
from packages.persistence.models.enums import (
    ConversationChannel,
    ConversationOutcome,
    ConversationStatus,
    TurnRole,
)
from packages.persistence.repositories.conversations import (
    ConversationRepository,
    TurnRepository,
)
from packages.persistence.repositories.tenants import TenantRepo
from packages.persistence.session import SessionFactory
from packages.verticals.manifest import VerticalManifest

__all__ = ["ConversationService", "StartedConversation"]

log = structlog.get_logger(__name__)

#: How many prior turns are replayed into the model. The prompt assembler
#: applies its own character budget on top; this bounds the *query*, so a
#: thousand-turn conversation does not read a thousand rows to then discard
#: 990 of them.
_HISTORY_LIMIT = 40


@dataclass(frozen=True, slots=True)
class StartedConversation:
    conversation_id: UUID
    greeting: str
    vertical: str
    manifest_version: str


class ConversationService:
    """Everything a conversation turn needs, across several transactions."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        llm: LLMPort,
        graph_cache: CompiledGraphCache,
        graph_runner: GraphRunner,
        tools: ToolRegistry,
    ) -> None:
        self._sessions = session_factory
        self._llm = llm
        self._cache = graph_cache
        self._runner = graph_runner
        self._tools = tools

    # ── Starting ──────────────────────────────────────────────────────────

    async def start(
        self,
        *,
        ctx: TenantContext,
        channel: ConversationChannel = ConversationChannel.WEB_CHAT,
        contact_identifier: str | None = None,
        channel_metadata: dict[str, str] | None = None,
    ) -> StartedConversation:
        """Open a conversation and return its greeting.

        The greeting comes from the manifest, not from the model. It must be
        instant (it is the first thing the visitor sees) and identical every
        time (it is the business's own words), and a generated greeting is
        neither. It is also *not* persisted as a turn: it is a rendering of
        configuration, and writing it would put a row in ``turns`` that no
        model produced and that a replay would then feed back as context.
        """
        async with self._sessions.unit_of_work(ctx) as session:
            manifest = await ManifestInstallService(session).load()
            conversation = await ConversationRepository(session).start(
                channel=channel,
                contact_identifier=contact_identifier,
                agent_code=manifest.key,
                agent_version=manifest.version,
                channel_metadata=dict(channel_metadata or {}),
            )
            conversation_id = conversation.id

        log.info(
            "conversation.started",
            tenant_id=str(ctx.tenant_id),
            conversation_id=str(conversation_id),
            channel=channel.value,
        )
        return StartedConversation(
            conversation_id=conversation_id,
            greeting=manifest.greeting,
            vertical=manifest.key,
            manifest_version=manifest.cache_version,
        )

    # ── One turn ──────────────────────────────────────────────────────────

    async def record_user_turn(
        self, *, ctx: TenantContext, conversation_id: UUID, content: str
    ) -> UUID:
        """Persist the user's message. Returns the *assistant* turn id.

        The assistant turn id is minted here, before the model runs, because
        the client needs something to correlate the ``202 Accepted`` response
        with the events that follow on a stream it may already have open. It is
        a UUIDv7, so it also carries the timestamp the partitioned write needs.
        """
        async with self._sessions.unit_of_work(ctx) as session:
            conversation = await self._require_conversation(session, conversation_id)
            await TurnRepository(session).append(
                conversation=conversation,
                role=TurnRole.USER,
                content=content,
            )
        return uuid7()

    async def retract_last_reply(
        self, *, ctx: TenantContext, conversation_id: UUID
    ) -> bool:
        """The newest reply was generated but never delivered; take it back.

        Voice only, in practice: the caller kept talking while the reply was
        being generated, so the bridge discarded it unspoken and is about to
        run the follow-up. Left in place, the next turn's history would end
        ``assistant: "…may I have your name?"`` / ``user: "no emergency"`` and
        the model would read the continuation as an answer to a question the
        caller never heard. Flagging the row as interrupted keeps the record
        and removes it from what the model sees; see
        :meth:`TurnRepository.retract_latest_reply` for the guard on *which*
        row may be retracted.

        Never raises. Returns whether anything was retracted.
        """
        try:
            async with self._sessions.unit_of_work(ctx) as session:
                conversation = await self._require_conversation(
                    session, conversation_id
                )
                turn = await TurnRepository(session).retract_latest_reply(
                    conversation=conversation
                )
        except Exception:
            log.exception(
                "conversation.retract_failed",
                tenant_id=str(ctx.tenant_id),
                conversation_id=str(conversation_id),
            )
            return False
        if turn is None:
            return False
        log.info(
            "conversation.reply_retracted",
            tenant_id=str(ctx.tenant_id),
            conversation_id=str(conversation_id),
            turn_id=str(turn.id),
        )
        return True

    async def withdraw_speculative_turn(
        self, *, ctx: TenantContext, conversation_id: UUID, user_text: str
    ) -> bool:
        """Undo a speculative voice turn the caller talked on past.

        Two rows may exist: the caller's half-sentence, and whatever partial
        reply the cancelled run persisted. The reply is retracted first (it is
        the newest row), then the caller line is withdrawn, in one transaction
        so the next turn never loads history with only one of the two undone.

        Never raises. Returns whether the caller line was withdrawn.
        """
        try:
            async with self._sessions.unit_of_work(ctx) as session:
                conversation = await self._require_conversation(
                    session, conversation_id
                )
                turns = TurnRepository(session)
                await turns.retract_latest_reply(conversation=conversation)
                withdrawn = await turns.withdraw_latest_user_turn(
                    conversation=conversation, content=user_text
                )
        except Exception:
            log.exception(
                "conversation.withdraw_failed",
                tenant_id=str(ctx.tenant_id),
                conversation_id=str(conversation_id),
            )
            return False
        log.info(
            "conversation.speculative_turn_withdrawn",
            conversation_id=str(conversation_id),
            withdrawn=withdrawn is not None,
        )
        return withdrawn is not None

    async def stream_turn(
        self,
        *,
        ctx: TenantContext,
        conversation_id: UUID,
        turn_id: UUID,
        user_text: str,
        channel: str = "web_chat",
        result: TurnResult | None = None,
    ) -> AsyncIterator[TurnEvent]:
        """Run one turn, yielding events, then persist the result.

        The persist happens in a ``finally``, so it also runs when the client
        disconnects mid-answer — a partial reply is still what the assistant
        said, and losing it would leave the conversation showing a question
        with no response.

        Args:
            channel: What the caller is using, passed through to the prompt.
                Defaults to web chat so every existing caller is unaffected.
                The voice worker passes ``voice_inbound``: an agent that knows
                it is being *spoken* to should not answer with a bulleted list
                or a reference number it never reads aloud.
        """
        async with self._sessions.unit_of_work(ctx, readonly=True) as session:
            history = await self._load_history(
                session, conversation_id, drop_trailing_user=user_text
            )
            # History ending on a *user* message means the previous utterance
            # was never answered — its reply was retracted (voice: the caller
            # kept talking before it could be spoken), or never persisted. The
            # two utterances are one thought split in two, so they go to the
            # model as one message: "No, a general check-up. No emergency."
            # Sent as two consecutive user messages the model tends to answer
            # the last one alone; merged, it answers what was actually said.
            # Persistence is untouched — both rows are already on record.
            if history and history[-1].role == "user":
                prior = history.pop()
                user_text = f"{prior.content.strip()} {user_text.strip()}".strip()
                log.info(
                    "conversation.turn_combined",
                    conversation_id=str(conversation_id),
                )
            manifest = await ManifestInstallService(session).load()
            collected = await self._load_collected_data(session, conversation_id)
            timezone = await self._business_timezone(session)
            capture = await self._tracing_capture(session)

        # `result` is an out-parameter: an async generator's return value is
        # unreachable from an `async for`, so the runner fills this in as it
        # goes and the `finally` below reads it. A caller may pass its own to
        # read usage and latency afterwards (the voice metrics do).
        result = result if result is not None else TurnResult()
        try:
            async for event in self._runner.run_turn(
                tenant_id=ctx.tenant_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                user_text=user_text,
                history=history,
                channel=channel,
                # The business's own clock. Without it the agent works from UTC
                # and every relative date a caller uses ("Tuesday morning") is
                # resolved against the wrong day — see `prompts.TEMPORAL_FRAME`.
                timezone=timezone,
                manifest_version=manifest.cache_version,
                collected_data=collected,
                result=result,
                tracing_policy=capture,
            ):
                yield event
        finally:
            # ── Shielded, and that is load-bearing ────────────────────────
            #
            # This `finally` runs on the disconnect path, and on that path the
            # task is ALREADY being cancelled. An `await` in a cancelled task
            # raises `CancelledError` at its first suspension point — so the
            # very first `await` inside these methods (acquiring a connection)
            # is cancelled, the writes never happen, and nothing is logged
            # because the exception is a cancellation rather than an error.
            #
            # The symptom is precise and baffling: every conversation persists
            # its user turns and none of its assistant turns, so the agent has
            # no memory of what it said and re-offers the same times forever.
            #
            # `asyncio.shield` lets the two writes run to completion even though
            # the surrounding task is unwinding. They are bounded (two short
            # transactions) and they never raise, so shielding them cannot
            # prolong a shutdown indefinitely.
            await asyncio.shield(
                self._finalize(
                    ctx=ctx, conversation_id=conversation_id, result=result
                )
            )

    async def _finalize(
        self, *, ctx: TenantContext, conversation_id: UUID, result: TurnResult
    ) -> None:
        """Every post-turn write, as one shieldable unit.

        Order matters only in that the turn is written first: if the process
        dies between these, a conversation with its last turn but no outcome is
        recoverable (the idle sweeper will close it), whereas an outcome on a
        conversation missing the turn that produced it is a row that contradicts
        its own transcript.
        """
        await self._persist_assistant_turn(
            ctx=ctx, conversation_id=conversation_id, result=result
        )
        await self._persist_collected_data(
            ctx=ctx, conversation_id=conversation_id, result=result
        )
        await self._persist_outcome(
            ctx=ctx, conversation_id=conversation_id, result=result
        )

    async def _persist_outcome(
        self, *, ctx: TenantContext, conversation_id: UUID, result: TurnResult
    ) -> None:
        """Record the outcome the graph declared, and close if it was terminal.

        **The value has existed all along and was simply dropped.** A terminal
        agent declares ``outcome`` in the manifest, ``NodeFactory.terminal``
        puts it in graph state, and ``GraphRunner`` copies it to
        ``TurnResult.outcome`` on every turn — the runner even puts it on the
        trace span. Nothing wrote it to the row, so ``/conversations`` derived a
        weaker answer from ``status`` while the real one went past it each turn.

        **A declared outcome means the conversation is over**, which is why this
        also closes it. That is not an inference: ``routing.route`` sends a run
        with a non-null outcome straight to ``END``, and only a ``TERMINAL``
        node may declare one (``VerticalManifest`` rejects a non-terminal agent
        that tries). The agent has said goodbye; leaving the row ``active`` so a
        sweeper can notice in half an hour would report "in progress" for a
        conversation that demonstrably finished.

        Never raises, for the same reason the other two post-turn writes do not:
        this runs inside the shielded ``finally``, including on the disconnect
        path, and an exception here would replace the real failure with a
        bookkeeping one.
        """
        if not result.outcome:
            return

        try:
            outcome = ConversationOutcome(result.outcome)
        except ValueError:
            # A manifest declaring something outside the writable vocabulary
            # would be rejected at load time, so this is a "cannot happen" —
            # logged rather than raised because losing the turn over it would
            # be a worse outcome than losing the classification.
            log.warning(
                "conversation.outcome_unknown",
                outcome=result.outcome,
                conversation_id=str(conversation_id),
            )
            return

        try:
            async with self._sessions.unit_of_work(ctx) as session:
                repository = ConversationRepository(session)
                conversation = await repository.get(conversation_id)
                if conversation is None:
                    return

                await repository.record_outcome(conversation_id, outcome=outcome)

                # Idempotent: a conversation already closed is left alone rather
                # than having its `ended_at` pushed forward. Re-closing would
                # stretch `duration_ms` every time a late turn arrived.
                if conversation.status == ConversationStatus.ACTIVE:
                    await repository.end(conversation_id)
        except Exception:
            log.exception(
                "conversation.outcome_persist_failed",
                tenant_id=str(ctx.tenant_id),
                conversation_id=str(conversation_id),
            )
        else:
            log.info(
                "conversation.ended",
                tenant_id=str(ctx.tenant_id),
                conversation_id=str(conversation_id),
                outcome=outcome.value,
            )

    async def _persist_assistant_turn(
        self, *, ctx: TenantContext, conversation_id: UUID, result: TurnResult
    ) -> None:
        """Write the assistant turn, tolerating a partial or empty result.

        Never raises. This runs on the disconnect and error paths, where an
        exception would replace the real failure with a persistence one and
        hide it.
        """
        if not result.text and result.error is None:
            return
        try:
            async with self._sessions.unit_of_work(ctx) as session:
                conversation = await self._require_conversation(
                    session, conversation_id
                )
                await TurnRepository(session).append(
                    conversation=conversation,
                    role=TurnRole.ASSISTANT,
                    content=result.text or None,
                    tool_error=result.error.message if result.error else None,
                    model_provider=self._llm.provider,
                    model_name=getattr(self._llm, "model", None),
                    input_tokens=result.usage.prompt_tokens if result.usage else None,
                    output_tokens=(
                        result.usage.completion_tokens if result.usage else None
                    ),
                    cached_tokens=(
                        result.usage.cached_prompt_tokens if result.usage else None
                    ),
                    latency_ms=result.latency_ms or None,
                    finish_reason=result.finish_reason,
                    agent_node=result.agent_key,
                    # Cost attribution arrives with the billing package; the
                    # column is left null rather than filled with a guess,
                    # because a wrong cost is worse than a missing one and the
                    # `ck_turns__cost_currency_paired` CHECK enforces the pair.
                    cost=None,
                    cost_currency=None,
                )
        except Exception:
            log.exception(
                "conversation.persist_failed",
                tenant_id=str(ctx.tenant_id),
                conversation_id=str(conversation_id),
            )

    @staticmethod
    async def _tracing_capture(session) -> TracingPolicy:
        """This tenant's prompt/completion capture setting — default OFF.

        Read from ``tenant_settings`` (namespace ``observability``, key
        ``tracing.capture``) rather than from application configuration,
        because contract §8 makes this a *tenant's* decision: one clinic
        consenting to have its transcripts readable in a dashboard says nothing
        about the clinic next door.

        Never raises and never fails open. Any error — missing row, bad value,
        database hiccup — yields the masked default, because the failure mode
        of guessing wrong in the other direction is a patient's phone number in
        a trace store.
        """
        from packages.observability.tracing import (
            CAPTURE_KEY,
            CAPTURE_NAMESPACE,
            resolve_policy,
        )
        from sqlalchemy import text

        try:
            row = (
                await session.execute(
                    text(
                        "SELECT value #>> '{}' FROM tenant_settings "
                        "WHERE tenant_id = app_current_tenant() "
                        "  AND namespace = :ns AND key = :key "
                        "  AND location_id IS NULL LIMIT 1"
                    ),
                    {"ns": CAPTURE_NAMESPACE, "key": CAPTURE_KEY},
                )
            ).scalar_one_or_none()
        except Exception:
            log.warning("conversation.tracing_capture_lookup_failed")
            return TracingPolicy()

        from apps.api.settings import get_settings

        settings = get_settings()
        return resolve_policy(
            row,
            ceiling=settings.langfuse_capture,
            max_chars=settings.langfuse_max_chars,
        )

    @staticmethod
    async def _business_timezone(session) -> str:
        """The clock the caller and the practice share.

        The **location's** zone, falling back to the tenant default. That order
        matters and matches what the booking engine does: a two-site tenant with
        a branch in another zone must have its agent talking in the branch's
        clock, or "nine tomorrow morning" means two different instants to the
        agent and to the diary.

        Never raises. A missing location degrades to the tenant default and then
        to UTC — a receptionist that cannot start because a timezone lookup
        failed is worse than one working from a slightly wrong clock, and the
        booking engine still refuses anything genuinely out of hours.
        """
        from sqlalchemy import text

        try:
            row = (
                await session.execute(
                    text(
                        "SELECT COALESCE("
                        "  (SELECT l.timezone FROM locations l "
                        "    WHERE l.tenant_id = t.id AND l.is_active "
                        "      AND l.deleted_at IS NULL "
                        "    ORDER BY (l.code = 'main') DESC, l.created_at "
                        "    LIMIT 1), "
                        "  t.default_timezone) "
                        "  FROM tenants t WHERE t.id = app_current_tenant()"
                    )
                )
            ).scalar_one_or_none()
        except Exception:
            log.warning("conversation.timezone_lookup_failed")
            return "UTC"
        return str(row) if row else "UTC"

    # ── Collected data, carried across turns ──────────────────────────────
    #
    # The graph accumulates facts as it works — the slots it offered, the
    # reference it created, the name it was given. Those live in
    # `ConversationState.collected_data`, which is per-run: without persisting
    # it, turn N+1 starts blank and the model re-derives what it already knew.
    #
    # The observable symptom is specific and bad: the agent offers three times,
    # the caller picks one, and the agent offers the same three times again —
    # because it no longer knows it made an offer. That is what this closes.
    #
    # Stored on `conversations.channel_metadata` under a reserved key rather than
    # in a new column: it is small, it is per-conversation, and 03 §5's
    # checkpointer is where this properly belongs once it lands. A column added
    # now would be a migration to undo then.

    #: The reserved key. Namespaced so a channel adapter writing its own metadata
    #: cannot collide with it.
    _COLLECTED_KEY = "_agent_collected"

    async def _load_collected_data(
        self, session, conversation_id: UUID
    ) -> dict[str, Any]:
        conversation = await ConversationRepository(session).get(conversation_id)
        if conversation is None:
            return {}
        metadata = dict(conversation.channel_metadata or {})
        value = metadata.get(self._COLLECTED_KEY)
        return dict(value) if isinstance(value, dict) else {}

    async def _persist_collected_data(
        self, *, ctx: TenantContext, conversation_id: UUID, result: TurnResult
    ) -> None:
        """Merge this turn's facts into the conversation. Never raises.

        Merged rather than replaced: a turn that collected nothing must not erase
        what an earlier one learned. Runs on the disconnect path too — a caller
        who closed the tab mid-answer should still find the agent knows their
        name when they come back.
        """
        if not result.collected_data:
            return
        try:
            async with self._sessions.unit_of_work(ctx) as session:
                repository = ConversationRepository(session)
                conversation = await repository.get(conversation_id)
                if conversation is None:
                    return
                metadata = dict(conversation.channel_metadata or {})
                merged = dict(metadata.get(self._COLLECTED_KEY) or {})
                merged.update(result.collected_data)
                metadata[self._COLLECTED_KEY] = merged
                conversation.channel_metadata = metadata
                await session.flush()
        except Exception:
            log.exception(
                "conversation.collected_data_persist_failed",
                tenant_id=str(ctx.tenant_id),
                conversation_id=str(conversation_id),
            )

    # ── Reads ─────────────────────────────────────────────────────────────

    async def get_with_turns(
        self, *, ctx: TenantContext, conversation_id: UUID
    ) -> tuple[Conversation, list]:
        async with self._sessions.unit_of_work(ctx, readonly=True) as session:
            conversation = await self._require_conversation(session, conversation_id)
            turns = list(
                await TurnRepository(session).replay(conversation_id, limit=500)
            )
            return conversation, turns

    async def _load_history(
        self, session, conversation_id: UUID, *, drop_trailing_user: str | None = None
    ) -> list[Message]:
        """Prior turns as port messages, oldest first.

        Only ``user`` and ``assistant`` rows are replayed. ``tool`` and
        ``event`` rows carry results and lifecycle markers that the graph
        rebuilds itself; feeding them back would present the model with tool
        output it never asked for in this turn.

        ``drop_trailing_user`` drops the turn being processed right now. The
        message is persisted by ``record_user_turn`` before the stream opens,
        so by the time this runs it is already a row — and ``new_state`` appends
        it AGAIN as ``user_message``. The result was every caller line appearing
        twice in the routing prompt:

            Caller: Hi i want an appointment
            Caller: Hi i want an appointment

        Harmless-looking, and not: it doubles the apparent emphasis of the most
        recent line, wastes context, and makes a repeated question look like an
        impatient caller repeating themselves.

        **Matched on content and position, not on id.** The obvious fix — pass
        the turn id and skip that row — silently does nothing, because
        ``record_user_turn`` returns a freshly minted *assistant* turn id rather
        than the id of the user row it just wrote. The two never compare equal,
        the filter never fires, and the duplicate survives a fix that reads
        correctly. Only the LAST message is considered, and only when it is a
        user turn whose text matches, so an earlier identical message ("yes"
        said twice, three turns apart) is untouched.
        """
        turns = await TurnRepository(session).replay(
            conversation_id, limit=_HISTORY_LIMIT
        )
        if (
            drop_trailing_user is not None
            and turns
            and turns[-1].role == TurnRole.USER
            and (turns[-1].content or "").strip() == drop_trailing_user.strip()
        ):
            turns = turns[:-1]

        messages: list[Message] = []
        for turn in turns:
            if turn.role == TurnRole.USER and turn.content:
                if turn.is_interruption:
                    # Withdrawn: recorded for a speculative voice turn that the
                    # caller talked on past. The confirmed turn carries the
                    # whole sentence. See `withdraw_speculative_turn`.
                    continue
                if messages and messages[-1].role == "user":
                    # Two caller utterances with no reply between them are
                    # one thought that arrived in two pieces (the reply to
                    # the first was retracted, or never written). The turn
                    # that answered them saw them as ONE message — see
                    # `stream_turn` — so every later turn must too, or the
                    # conversation reads differently depending on when it is
                    # loaded.
                    prior = messages.pop()
                    messages.append(
                        Message(
                            role="user",
                            content=f"{prior.content.strip()} {turn.content.strip()}",
                        )
                    )
                    continue
                messages.append(Message(role="user", content=turn.content))
            elif turn.role == TurnRole.ASSISTANT and turn.content:
                if turn.is_interruption:
                    # Generated, never delivered: the caller was still
                    # talking and the reply was retracted before it was
                    # spoken. The model must not treat what follows as an
                    # answer to it. See `retract_last_reply`.
                    continue
                messages.append(Message(role="assistant", content=turn.content))
        return messages

    @staticmethod
    async def _require_conversation(session, conversation_id: UUID) -> Conversation:
        """Load a conversation or 404.

        Under RLS a conversation in another tenant is *invisible*, so this
        returns 404 rather than 403 — which is the correct answer as well as
        the only available one: confirming that an id exists somewhere else
        would make the id space enumerable across tenants.
        """
        conversation = await ConversationRepository(session).get(conversation_id)
        if conversation is None:
            raise EntityNotFoundError(
                entity_type="conversations", entity_id=conversation_id
            )
        return conversation


def build_graph_cache(
    *,
    session_factory: SessionFactory,
    llm: LLMPort,
    tools: ToolRegistry,
) -> CompiledGraphCache:
    """Wire the cache to the database-backed manifest loader.

    The loader closes over the session factory rather than a session: the cache
    is process-wide and long-lived, and a request-scoped session captured here
    would be closed by the time a later tenant's first turn compiled.
    """

    # Values for the manifest's declared `template_variables`, resolved once
    # per compile and cached with the graph. Read alongside the manifest rather
    # than in the compiler factory, because the factory is synchronous — and it
    # is synchronous because compilation itself does no I/O.
    variables: dict[UUID, dict[str, str]] = {}
    customizations: dict[UUID, str] = {}

    async def loader(tenant_id: UUID, version: str | None) -> VerticalManifest:
        ctx = TenantContext(tenant_id=tenant_id)
        async with session_factory.unit_of_work(ctx, readonly=True) as session:
            installs = ManifestInstallService(session)
            manifest = await installs.load(version=version)
            tenant = await TenantRepo(session).current()
            variables[tenant_id] = {
                "business_name": tenant.display_name if tenant else "the practice",
                "timezone": tenant.default_timezone if tenant else "UTC",
                "location_name": tenant.display_name if tenant else "",
            }
            customizations[tenant_id] = await installs.customization()
            return manifest

    def compiler_factory(tenant_id: UUID, manifest: VerticalManifest) -> GraphCompiler:
        # `business_name` is what makes the same medical manifest greet as
        # Northgate Dental for one tenant and as something else for another —
        # one manifest, many businesses, which is contract §4's whole point.
        return GraphCompiler(
            NodeFactory(
                llm=llm,
                manifest=manifest,
                variables=variables.get(tenant_id, {}),
                tenant_customization=customizations.get(tenant_id, ""),
                tool_schemas=tools.schemas(),
                # The executor is what turns a granted tool from a description
                # into a capability. Built per compile rather than shared,
                # because it closes over the registry a tenant's graph was
                # compiled against — a registry change is a recompile, not a
                # silent behaviour change under a cached graph.
                executor=ToolExecutor(tools),
            )
        )

    return CompiledGraphCache(loader=loader, compiler_factory=compiler_factory)



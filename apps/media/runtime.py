"""The agent runtime shared by every media session.

This module exists to make one guarantee enforceable: **a voice call reaches the
same agent, the same tools and the same database rows as a web chat.** It does
that by not building an agent at all. It constructs the platform's ordinary
conversation stack — the manifest-compiled graph cache, the tool registry with
booking dependencies wired, the ``ConversationService`` — exactly as
``apps/api`` does, and then runs turns through ``ConversationService.stream_turn``.

The consequence is that ``booking.book_appointment`` is invoked by the same code
whether the caller typed or spoke, so a spoken booking writes the same row with
the same validation. There is no voice-specific booking path to drift.

Reusing ``apps.api.services`` from ``apps/media`` is legal and deliberate:
Core Contract §9 forbids *packages* importing apps, and constrains apps to
depend on packages — it does not forbid one app reusing another's service layer,
and duplicating this wiring would be the actual contract violation, since the
two copies would diverge on the first change to either.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import structlog
from apps.api.services.booking import build_booking_tool_deps
from apps.api.services.conversations import ConversationService, build_graph_cache
from apps.api.settings import Settings
from packages.ai.runner import GraphRunner
from packages.ai.tools import default_registry
from packages.ai.tools.builtin.booking import set_booking_dependencies
from packages.core.tenancy import TenantContext
from packages.observability.tracing import get_tracer, set_tracer
from packages.persistence.engine import DEFAULT_SHARD, ConnectionRouter, ShardConfig
from packages.persistence.models.enums import (
    ConversationChannel,
    ConversationOutcome,
    ConversationStatus,
)
from packages.persistence.repositories.conversations import ConversationRepository
from packages.persistence.session import SessionFactory
from packages.providers.llm import LLMProviderSettings, build_llm_adapter
from packages.providers.observability import build_langfuse_tracer

__all__ = ["VOICE_CHANNEL", "MediaRuntime", "build_runtime"]

log = structlog.get_logger(__name__)

#: The channel name the agent is told it is speaking on. Matches
#: ``ConversationChannel.VOICE_INBOUND`` so the prompt, the conversation row and
#: any later analytics all agree on one spelling.
VOICE_CHANNEL = ConversationChannel.VOICE_INBOUND.value


@dataclass(slots=True)
class MediaRuntime:
    """Process-wide resources for the media worker.

    Built once at startup. Constructing any of this per call would open a
    connection pool per call, and recompile the tenant's graph on every ring.
    """

    settings: Settings
    router: ConnectionRouter
    session_factory: SessionFactory
    conversations: ConversationService

    async def run_turn(
        self,
        *,
        tenant_id: UUID,
        conversation_id: UUID,
        user_text: str,
        on_interim: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """One caller utterance in, the agent's spoken reply out.

        Collapses the platform's token stream into a single string because TTS
        wants a whole utterance: synthesizing token-by-token produces the wrong
        prosody and, worse, splits one reply into several audio jobs whose gaps
        unmute the microphone mid-sentence.

        ``on_interim`` receives a tool-round *preamble* ("let me check the
        diary") the moment the graph releases it — before the tool has run and
        before the final reply is generated — so the caller hears something
        within ~1.5s instead of sitting in silence for the whole turn. The
        preamble is NOT part of the returned text: it has already been spoken.
        Without the callback it is folded into the reply as before.

        Errors are surfaced as an empty string rather than raised — the caller
        (:class:`~packages.providers.voice.agent_bridge.VoiceAgentProcessor`)
        turns that into a spoken apology. A raised exception here would end the
        call, and dropping a call is worse than a bad answer.
        """
        ctx = TenantContext(tenant_id=tenant_id)
        turn_id = await self.conversations.record_user_turn(
            ctx=ctx, conversation_id=conversation_id, content=user_text
        )

        chunks: list[str] = []
        preamble: list[str] = []

        async def flush_preamble() -> None:
            # Spoken at the first event that is NOT part of the preamble —
            # normally `tool.started`, i.e. while the tool is still running.
            text = "".join(preamble).strip()
            preamble.clear()
            if text and on_interim is not None:
                try:
                    await on_interim(text)
                except Exception:
                    log.exception("voice.turn.preamble_failed")

        async for event in self.conversations.stream_turn(
            ctx=ctx,
            conversation_id=conversation_id,
            turn_id=turn_id,
            user_text=user_text,
            # The agent is being spoken to, not typed at. It reaches the same
            # tools either way; what changes is how it phrases an answer that
            # has to be *heard* — no bullet lists, no unread reference codes.
            channel=VOICE_CHANNEL,
        ):
            if event.type == "token" and event.data.get("interim"):
                if on_interim is None:
                    chunks.append(str(event.data.get("text", "")))
                else:
                    preamble.append(str(event.data.get("text", "")))
                continue
            if preamble:
                await flush_preamble()
            if event.type == "token":
                chunks.append(str(event.data.get("text", "")))
            elif event.type == "error":
                log.warning(
                    "voice.turn.error_event",
                    code=event.data.get("code"),
                    conversation_id=str(conversation_id),
                )

        if preamble:
            await flush_preamble()
        return "".join(chunks).strip()

    async def retract_last_reply(
        self, *, tenant_id: UUID, conversation_id: UUID
    ) -> None:
        """A reply was generated but the caller talked over it before it was
        spoken. Take it out of the record the model reads. Never raises."""
        await self.conversations.retract_last_reply(
            ctx=TenantContext(tenant_id=tenant_id), conversation_id=conversation_id
        )

    async def close_conversation(
        self, *, tenant_id: UUID, conversation_id: UUID
    ) -> None:
        """The call ended. Write that down.

        **Voice is the one channel with an unambiguous hangup**, and it would be
        a waste not to use it. A web chat just stops — the visitor closes the
        tab and nothing is notified, which is why there is a 30-minute idle
        sweeper for that case. A WebRTC session *closing* is a real event with a
        real timestamp, so a voice conversation can be closed the moment it ends
        rather than half an hour later, and its `duration_ms` is the true call
        length rather than a sweeper's estimate.

        Idempotent and outcome-preserving, and both matter here. A call that
        reached ``wrap_up`` was already closed by ``_persist_outcome`` when the
        terminal agent spoke; this must not then reopen it, push its `ended_at`
        forward, or overwrite the outcome it declared. A call that was simply
        hung up mid-sentence has no declared outcome, and ``abandoned`` is the
        honest word for it — the caller left.

        Never raises. The connection is already closing and the pipeline is
        being torn down; an exception here would surface as a spurious error on
        a call that ended perfectly normally.
        """
        ctx = TenantContext(tenant_id=tenant_id)
        try:
            async with self.session_factory.unit_of_work(ctx) as session:
                repository = ConversationRepository(session)
                conversation = await repository.get(conversation_id)
                if conversation is None:
                    return
                if conversation.status != ConversationStatus.ACTIVE:
                    # A terminal agent already closed it, with an outcome it
                    # chose. Leave both alone.
                    return

                if conversation.outcome is None:
                    await repository.record_outcome(
                        conversation_id, outcome=ConversationOutcome.ABANDONED
                    )
                await repository.end(
                    conversation_id, status=ConversationStatus.ABANDONED
                )
        except Exception:
            log.exception(
                "voice.conversation.close_failed",
                conversation_id=str(conversation_id),
            )
        else:
            log.info(
                "voice.conversation.closed", conversation_id=str(conversation_id)
            )

    async def aclose(self) -> None:
        # Drain the tracer's export queue before the process exits: a voice
        # call's last turns are exactly the ones still buffered at shutdown.
        get_tracer().shutdown()
        await self.router.dispose_all()


async def build_runtime(settings: Settings) -> MediaRuntime:
    """Wire the same stack ``apps/api`` builds, minus everything HTTP."""
    # LLM tracing, process-wide — the same install ``apps/api`` performs in its
    # lifespan. Without it this process's ``get_tracer()`` stays the no-op and
    # every voice turn runs untraced while web chat shows up in Langfuse.
    set_tracer(
        build_langfuse_tracer(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            environment=settings.env,
            release=settings.git_sha,
            enabled=settings.langfuse_enabled,
        )
    )

    router = ConnectionRouter(
        {
            DEFAULT_SHARD: ShardConfig(
                key=DEFAULT_SHARD,
                dsn=settings.database_url,
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                echo=settings.db_echo,
                application_name="receptionai-media",
            )
        }
    )
    session_factory = SessionFactory(router)

    llm = build_llm_adapter(
        LLMProviderSettings(
            provider=settings.llm_provider,
            api_key=settings.llm_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            stub_token_delay_ms=settings.stub_token_delay_ms,
        ),
        allow_stub_fallback=settings.is_local,
    )

    tools = default_registry()
    # Same fail-closed wiring as the API: until this runs, the booking tools
    # raise rather than reaching for a pool that does not exist.
    set_booking_dependencies(build_booking_tool_deps(session_factory))

    graph_cache = build_graph_cache(
        session_factory=session_factory, llm=llm, tools=tools
    )

    conversations = ConversationService(
        session_factory=session_factory,
        llm=llm,
        tools=tools,
        graph_cache=graph_cache,
        graph_runner=GraphRunner(graph_cache),
    )

    log.info("media.runtime.built", llm_provider=settings.llm_provider)
    return MediaRuntime(
        settings=settings,
        router=router,
        session_factory=session_factory,
        conversations=conversations,
    )

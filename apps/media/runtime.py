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
from packages.persistence.engine import DEFAULT_SHARD, ConnectionRouter, ShardConfig
from packages.persistence.models.enums import ConversationChannel
from packages.persistence.session import SessionFactory
from packages.providers.llm import LLMProviderSettings, build_llm_adapter

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
        self, *, tenant_id: UUID, conversation_id: UUID, user_text: str
    ) -> str:
        """One caller utterance in, the agent's spoken reply out.

        Collapses the platform's token stream into a single string because TTS
        wants a whole utterance: synthesizing token-by-token produces the wrong
        prosody and, worse, splits one reply into several audio jobs whose gaps
        unmute the microphone mid-sentence.

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
            if event.type == "token":
                chunks.append(str(event.data.get("text", "")))
            elif event.type == "error":
                log.warning(
                    "voice.turn.error_event",
                    code=event.data.get("code"),
                    conversation_id=str(conversation_id),
                )

        return "".join(chunks).strip()

    async def aclose(self) -> None:
        await self.router.dispose_all()


async def build_runtime(settings: Settings) -> MediaRuntime:
    """Wire the same stack ``apps/api`` builds, minus everything HTTP."""
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

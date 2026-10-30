"""One browser voice call, from ticket redemption to hangup.

The shell that 04 §2.2 describes: it owns the session lifecycle, the metrics
gauge, the transcript fan-out to the browser, and nothing about how audio
becomes text. That belongs to the engine, which here is the Pipecat composed
pipeline behind :mod:`packages.providers.voice`.

**Credentials.** A voice session redeems the *same* single-use stream ticket the
chat widget uses (PHASE1-API-CONTRACT). Voice does not invent a second
credential path: the ticket already proves "this browser was granted this
conversation on the demo tenant", which is exactly the question being asked
here, and a second mechanism would be a second thing to get wrong.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import structlog
from apps.media.metrics import VOICE_TURNS, SessionMetrics
from apps.media.runtime import MediaRuntime
from packages.voice.call_metrics import CallMetrics, TurnRecord
from packages.voice.engine.config import TenantVoiceConfig, VoiceMode

__all__ = ["VoiceSession", "VoiceSessionParams"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class VoiceSessionParams:
    """Everything resolved before the first audio frame arrives."""

    tenant_id: UUID
    conversation_id: UUID
    cfg: TenantVoiceConfig
    greeting: str | None = None
    #: This call's latency/turn-taking metrics, or ``None`` when disabled.
    metrics: CallMetrics | None = None


class VoiceSession:
    """Drives one call's pipeline and streams its transcript to the browser."""

    def __init__(
        self,
        *,
        runtime: MediaRuntime,
        params: VoiceSessionParams,
        connection: object,
    ) -> None:
        self._runtime = runtime
        self._params = params
        self._connection = connection
        self._transcript: list[tuple[str, str]] = []
        self._state: dict[str, object] = {
            "busy": False,
            "bot_speaking": False,
            "last_user_text": "",
            "last_bot_text": "",
        }

    @property
    def transcript(self) -> list[tuple[str, str]]:
        return list(self._transcript)

    async def _run_turn(
        self,
        user_text: str,
        *,
        speak_interim: Callable[[str], Awaitable[None]] | None = None,
        record: TurnRecord | None = None,
    ) -> str:
        """Bridge to the platform agent. Same graph, same tools, same rows."""
        try:
            reply = await self._runtime.run_turn(
                tenant_id=self._params.tenant_id,
                conversation_id=self._params.conversation_id,
                user_text=user_text,
                on_interim=speak_interim,
                record=record,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "voice.session.turn_failed",
                conversation_id=str(self._params.conversation_id),
            )
            VOICE_TURNS.labels("error").inc()
            return ""
        VOICE_TURNS.labels("ok" if reply else "empty").inc()
        return reply

    async def _retract_last_reply(self) -> None:
        """The bridge discarded an unspoken reply; keep the model's view honest."""
        await self._runtime.retract_last_reply(
            tenant_id=self._params.tenant_id,
            conversation_id=self._params.conversation_id,
        )

    async def _withdraw_speculative(self, user_text: str) -> None:
        """A speculative turn missed; take its rows out of the model's view."""
        await self._runtime.withdraw_speculative_turn(
            tenant_id=self._params.tenant_id,
            conversation_id=self._params.conversation_id,
            user_text=user_text,
        )

    async def _on_transcript(self, role: str, text: str) -> None:
        """Record a transcript line and push it to the browser.

        The browser needs these for the live transcript panel, and they are
        pushed as app messages over the same WebRTC data channel the pipeline
        already has open — a second socket would be a second thing to reconnect.
        """
        self._transcript.append((role, text))
        send = getattr(self._connection, "send_app_message", None)
        if send is None:
            return
        try:
            result = send(
                {
                    "label": "rtvi-ai",
                    "type": "server-message",
                    "data": {"type": f"{role}_transcript", "text": text},
                }
            )
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            # A failed transcript push must never end a call: the caller is
            # having a conversation, not reading a panel.
            log.warning("voice.session.transcript_push_failed", exc_info=True)

    def build_pipeline(self, transport: object) -> object:
        """Construct the composed pipeline for this session.

        Imported here rather than at module scope so that importing the session
        shell — which the metrics tests do — does not pay Pipecat's multi-second
        import cost, and so the app tier has no import-time dependency on a
        vendor SDK.
        """
        from packages.providers.voice.pipecat_composed import build_composed_pipeline

        return build_composed_pipeline(
            transport,  # type: ignore[arg-type]
            cfg=self._params.cfg,
            run_turn=self._run_turn,
            retract_last_reply=self._retract_last_reply,
            on_transcript=self._on_transcript,
            shared_state=self._state,  # type: ignore[arg-type]
            metrics=self._params.metrics,
            withdraw_speculative=self._withdraw_speculative,
        )

    def pipeline_observers(self) -> tuple[object, list[object]]:
        """The worker clock and observers that feed this call's metrics.

        Returned as a pair because they must be the *same* clock instance: the
        observer converts frame timestamps using the clock's origin.
        """
        from packages.providers.voice.metrics_observer import (
            MetricsClock,
            VoiceMetricsObserver,
        )

        clock = MetricsClock()  # type: ignore[no-untyped-call]
        if self._params.metrics is None:
            return clock, []
        return clock, [VoiceMetricsObserver(self._params.metrics, clock)]

    def close_metrics(self) -> None:
        """Write this call's outstanding rows and event log. Idempotent."""
        if self._params.metrics is not None:
            self._params.metrics.close()

    def metrics(self) -> SessionMetrics:
        mode = self._params.cfg.resolved_mode
        return SessionMetrics(
            tenant_id=str(self._params.tenant_id),
            mode=mode.value if isinstance(mode, VoiceMode) else str(mode),
        )

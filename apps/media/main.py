"""The media worker — a dedicated process for realtime voice (Contract A1).

Separate from ``apps/api`` because a media session runs a tight loop with 20ms
deadlines: sharing an event loop with ordinary HTTP handlers lets one slow JSON
serialization inject jitter into every live call. Voice also scales on
concurrent calls rather than RPS, and its pods need ``maxUnavailable: 0`` with a
long drain so a deploy never cuts someone off mid-sentence.

Surface:

``POST /voice/offer``  WebRTC SDP exchange. Redeems the browser's single-use
                       stream ticket, then starts the pipeline for that call.
``GET  /healthz``      liveness.
``GET  /metrics``      Prometheus, including the two names 05's HPA reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import structlog
from apps.api.settings import Settings, get_settings
from apps.api.tickets import StreamTicketService
from apps.media.metrics import VOICE_SESSION_CAPACITY
from apps.media.runtime import MediaRuntime, build_runtime
from apps.media.session import VoiceSession, VoiceSessionParams
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from packages.voice.call_metrics import CallMeta, CallMetrics
from packages.voice.engine.config import TenantVoiceConfig
from packages.voice.turn import (
    MODEL_VAD_STOP_SECS,
    STOP_SECS_DEFAULT,
    TurnDetectionConfig,
    TurnDetector,
    clamp_stop_secs,
)
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

__all__ = ["create_app"]

log = structlog.get_logger(__name__)

#: Concurrent calls this pod accepts. Published as
#: ``receptionai_voice_session_capacity`` so the HPA computes utilisation as a
#: ratio instead of hardcoding pod capacity in the autoscaler.
MAX_SESSIONS = int(os.getenv("MEDIA_MAX_SESSIONS", "25"))


class OfferRequest(BaseModel):
    """The browser's SDP offer plus the credential that authorises the call."""

    sdp: str
    type: str
    ticket: str | None = None
    #: Present on a renegotiation; identifies the existing peer connection.
    pc_id: str | None = None
    conversation_id: str | None = None
    stop_secs: float = Field(default=STOP_SECS_DEFAULT, ge=0.1, le=3.0)
    #: Per-call override of the end-of-turn detector — `silence`,
    #: `smart_turn`, `vap` or `flux`. Honoured only when
    #: `VOICE_ALLOW_CLIENT_OVERRIDE` allows it (local by default); otherwise
    #: the server's configured detector runs.
    turn_detector: str | None = None
    speculative: bool | None = None


def turn_detection_from_settings(settings: Settings) -> TurnDetectionConfig:
    """The server's default detector configuration, from the environment."""
    return TurnDetectionConfig.parse(
        settings.voice_turn_detector,
        speculative=settings.voice_speculative,
        base=TurnDetectionConfig(
            max_silence_secs=settings.voice_max_silence_secs,
            vap_threshold=settings.voice_vap_threshold,
            vap_model_dir=settings.voice_vap_dir,
            flux_model=settings.voice_flux_model,
            flux_eot_threshold=settings.voice_flux_eot_threshold,
            flux_eager_eot_threshold=settings.voice_flux_eager_eot_threshold,
        ),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the media app. A factory for the same reason ``apps/api`` uses one."""
    cfg = settings or get_settings()
    state: dict[str, Any] = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Fails startup on a misspelt VOICE_TURN_DETECTOR rather than running
        # the baseline under the wrong label.
        default_td = turn_detection_from_settings(cfg)
        state["turn_detection"] = default_td
        log.info(
            "media.turn_detection",
            default=default_td.label,
            client_override=cfg.voice_client_override_allowed,
            metrics_dir=cfg.voice_metrics_dir if cfg.voice_metrics_enabled else None,
        )
        runtime = await build_runtime(cfg)
        state["runtime"] = runtime
        state["connections"] = {}

        redis = None
        try:
            from redis.asyncio import Redis

            redis = Redis.from_url(cfg.redis_url, decode_responses=False)
            await redis.ping()
        except Exception:
            # Fail closed for voice: tickets cannot be verified single-use
            # without Redis, and an unauthenticated media session is worse than
            # no media session.
            log.error("media.redis.unavailable", url=cfg.redis_url)
            redis = None
        state["tickets"] = StreamTicketService(
            redis, ttl_seconds=cfg.stream_ticket_ttl_seconds
        )
        VOICE_SESSION_CAPACITY.set(MAX_SESSIONS)

        log.info("media.started", capacity=MAX_SESSIONS)
        try:
            yield
        finally:
            for conn in list(state.get("connections", {}).values()):
                with contextlib.suppress(Exception):
                    await conn.disconnect()
            if redis is not None:
                with contextlib.suppress(Exception):
                    await redis.aclose()
            await runtime.aclose()
            log.info("media.stopped")

    app = FastAPI(title="ReceptionAi Media", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, object]:
        conns = state.get("connections", {})
        return {"status": "ok", "active_sessions": len(conns), "capacity": MAX_SESSIONS}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/voice/offer")
    async def offer(body: OfferRequest, request: Request) -> Any:
        return await _handle_offer(body, state, cfg)

    return app


async def _handle_offer(
    body: OfferRequest, state: dict[str, Any], settings: Settings
) -> Any:
    """Redeem the ticket, negotiate WebRTC, and start the pipeline."""
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

    connections: dict[str, Any] = state["connections"]

    # Renegotiation of a live call: no new ticket, no new session.
    if body.pc_id and body.pc_id in connections:
        conn = connections[body.pc_id]
        await conn.renegotiate(sdp=body.sdp, type=body.type)
        return conn.get_answer()

    if len(connections) >= MAX_SESSIONS:
        # Shed load explicitly rather than degrading every live call. The HPA
        # reacts to the gauge; this protects the calls already in progress.
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "title": "Voice capacity reached",
                "status": 503,
                "code": "service_unavailable",
                "detail": "This node is at capacity. Please try again shortly.",
            },
            media_type="application/problem+json",
        )

    tickets: StreamTicketService = state["tickets"]
    if not body.ticket:
        return _unauthorized("A stream ticket is required to start a voice call.")

    claim = await tickets.redeem(body.ticket)
    if claim is None:
        # Single-use and short-lived: the remedy is always "mint a new one".
        return _unauthorized("This ticket is invalid, expired, or already used.")

    tenant_id: UUID = claim.tenant_id
    conversation_id: UUID = claim.conversation_id

    runtime: MediaRuntime = state["runtime"]
    turn_detection: TurnDetectionConfig = state.get(
        "turn_detection"
    ) or turn_detection_from_settings(settings)
    if settings.voice_client_override_allowed and (
        body.turn_detector or body.speculative is not None
    ):
        try:
            turn_detection = TurnDetectionConfig.parse(
                body.turn_detector, speculative=body.speculative, base=turn_detection
            )
        except ValueError:
            return _problem(
                422,
                "Unknown turn detector",
                f"'{body.turn_detector}' is not one of: "
                + ", ".join(d.value for d in TurnDetector),
            )

    from packages.providers.voice.pipecat_composed import detector_unavailable_reason

    if reason := detector_unavailable_reason(turn_detection):
        return _problem(422, "Turn detector unavailable", reason)
    if turn_detection.detector is TurnDetector.VAP:
        # The model load takes ~1s and blocks; never on the event loop that is
        # carrying other people's audio.
        from packages.providers.voice.vap_turn import load_vap_model

        try:
            await asyncio.to_thread(load_vap_model, turn_detection.vap_model_dir)
        except Exception as exc:
            log.exception("media.vap.load_failed")
            return _problem(422, "Turn detector unavailable", f"VAP failed to load: {exc}")

    voice_cfg = TenantVoiceConfig(stop_secs=body.stop_secs, turn_detection=turn_detection)
    metrics = _call_metrics(
        settings, voice_cfg, tenant_id=tenant_id, conversation_id=conversation_id
    )

    connection = SmallWebRTCConnection()
    await connection.initialize(sdp=body.sdp, type=body.type)

    session = VoiceSession(
        runtime=runtime,
        params=VoiceSessionParams(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            cfg=voice_cfg,
            metrics=metrics,
        ),
        connection=connection,
    )

    @connection.event_handler("closed")
    async def _on_closed(conn: Any) -> None:
        connections.pop(conn.pc_id, None)
        session.close_metrics()
        # The hangup. Voice is the one channel that has a definite end, so the
        # conversation row is closed here rather than waiting for the 30-minute
        # idle sweeper to infer it. `close_conversation` is idempotent and never
        # raises: a call that reached `wrap_up` was already closed with the
        # outcome that agent declared, and this must not disturb it.
        await runtime.close_conversation(
            tenant_id=tenant_id, conversation_id=conversation_id
        )
        log.info("media.session.closed", pc_id=conn.pc_id)

    await _start_pipeline(session, connection, state)

    connections[connection.pc_id] = connection
    log.info(
        "media.session.started",
        pc_id=connection.pc_id,
        conversation_id=str(conversation_id),
        turn_detection=turn_detection.label,
        call_id=metrics.meta.call_id if metrics else None,
    )
    return connection.get_answer()


async def _start_pipeline(
    session: VoiceSession, connection: Any, state: dict[str, Any]
) -> None:
    """Build the transport + pipeline and run it as a background worker."""
    import asyncio

    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
    from pipecat.workers.runner import WorkerRunner

    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    pipeline = session.build_pipeline(transport)
    clock, observers = session.pipeline_observers()
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=observers,  # type: ignore[arg-type]
        clock=clock,  # type: ignore[arg-type]
    )

    async def _run() -> None:
        # The gauge moves with the session's lifetime, on every exit path —
        # a leaked increment makes the HPA scale out an idle tier forever.
        with session.metrics():
            runner = WorkerRunner(handle_sigint=False)
            await runner.add_workers(worker)
            try:
                await runner.run()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("media.session.pipeline_failed")
            finally:
                session.close_metrics()

    task = asyncio.create_task(_run(), name=f"voice-session-{connection.pc_id}")
    state.setdefault("tasks", set()).add(task)
    task.add_done_callback(lambda t: state["tasks"].discard(t))


def _call_metrics(
    settings: Settings,
    cfg: TenantVoiceConfig,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
) -> CallMetrics | None:
    """A metrics collector for one call, labelled with what is under test."""
    if not settings.voice_metrics_enabled:
        return None
    td = cfg.turn_detection
    if td.detector is TurnDetector.SILENCE:
        vad_stop = clamp_stop_secs(cfg.stop_secs)
    else:
        vad_stop = MODEL_VAD_STOP_SECS
    meta = CallMeta(
        call_id=str(uuid4()),
        chat_id=str(conversation_id),
        tenant_id=str(tenant_id),
        config_label=td.label,
        detector=td.detector.value,
        speculative=td.speculative,
        stop_secs=clamp_stop_secs(cfg.stop_secs),
        vad_stop_secs=vad_stop,
        max_silence_secs=None if td.detector is TurnDetector.SILENCE else td.max_silence_secs,
        vap_threshold=td.vap_threshold if td.detector is TurnDetector.VAP else None,
        flux_model=td.flux_model if td.detector is TurnDetector.FLUX else None,
        stt_model=td.flux_model if td.detector is TurnDetector.FLUX else cfg.stt_model,
        tts_voice=cfg.tts_voice,
        llm_provider=settings.llm_provider,
        llm_model=settings.deepseek_model if settings.llm_provider == "deepseek" else None,
    )
    return CallMetrics(meta, directory=Path(settings.voice_metrics_dir).expanduser())


def _problem(status_code: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "title": title,
            "status": status_code,
            "code": "unprocessable_entity" if status_code == 422 else "error",
            "detail": detail,
        },
        media_type="application/problem+json",
    )


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={
            "title": "Unauthorized",
            "status": 401,
            "code": "unauthorized",
            "detail": detail,
        },
        media_type="application/problem+json",
    )


app = create_app  # uvicorn factory target: `uvicorn apps.media.main:app --factory`

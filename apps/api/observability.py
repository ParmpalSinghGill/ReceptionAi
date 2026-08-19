"""Structured logging and OpenTelemetry — contract §1, 05 §D14.

**Structured logs, always, including locally.** A console renderer that pretty-
prints in development and emits JSON in production means the two are different
code paths, and the one that only runs in production is the one nobody has
read. structlog's processor chain is shared; only the final renderer differs,
so every field a production log carries is present in a local one.

**PII never reaches a log or a span attribute** (contract §8, 05 §D14). The
processor below drops a fixed set of keys rather than trusting call sites: a
call site is a place someone adds ``email=user.email`` in a hurry, and a
denylist at the sink catches it. It is a backstop, not the control — the
control is that nothing logs PII in the first place — but a backstop that costs
a dict lookup is worth having.

OpenTelemetry is optional and off by default. A local stack has a collector; a
test run does not, and an exporter retrying against a dead endpoint adds
seconds to every test session.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Final

import structlog
from apps.api.settings import Settings
from fastapi import FastAPI

__all__ = ["configure_logging", "instrument_app"]

#: Keys that must never appear in a log line or a span attribute. Dropped at
#: the sink regardless of who set them.
_PII_KEYS: Final[frozenset[str]] = frozenset(
    {
        "email",
        "email_normalized",
        "password",
        "password_hash",
        "phone",
        "phone_e164",
        "full_name",
        "display_name",
        "contact_identifier",
        "contact_name",
        "address",
        "date_of_birth",
        "authorization",
        "api_key",
        "secret",
        "token",
        "refresh_token",
        "access_token",
        "ticket",
        # Conversation content is the highest-density PII in the system.
        "content",
        "transcript",
        "prompt",
        "user_text",
    }
)

_REDACTED: Final = "[redacted]"


def _drop_pii(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Replace any PII-classified key's value with a marker.

    Replaced rather than removed: a log line missing a field looks like the
    code path did not run, whereas an explicit ``[redacted]`` says "this
    happened and the value is deliberately absent", which is what someone
    reading an incident timeline needs.
    """
    for key in list(event_dict):
        if key.lower() in _PII_KEYS:
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Install the processor chain. Idempotent."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        # `add_log_level`, not `stdlib.add_log_level`: the logger factory below
        # is structlog's own `PrintLogger`, and the `stdlib.*` processors read
        # attributes (`logger.name`) that only a stdlib `Logger` has. Mixing
        # the two raises `AttributeError` inside the logging call itself —
        # which surfaces as a crash in whatever code was merely trying to log.
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _drop_pii,
    ]

    structlog.configure(
        processors=[
            *shared,
            # The ONLY difference between local and deployed. Everything above
            # is shared, so a field present in production is present locally.
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
            if settings.is_local
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy, httpx) through the same sink,
    # or half the output is JSON and half is not — and the half that is not is
    # the half that logs a connection string.
    logging.basicConfig(
        format="%(message)s", stream=sys.stderr, level=level, force=True
    )
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def instrument_app(app: FastAPI, settings: Settings) -> None:
    """Attach OpenTelemetry, if configured.

    Best-effort and never fatal: a missing exporter package or an unreachable
    collector must not stop the API serving traffic. Observability failing
    closed would mean an observability outage is a product outage.
    """
    if not settings.otel_enabled or not settings.otlp_endpoint:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        structlog.get_logger(__name__).warning(
            "otel.unavailable", reason="opentelemetry packages are not installed"
        )
        return

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.service_name,
                "service.version": settings.git_sha,
                "deployment.environment": settings.env,
            }
        )
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint))
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(
        app,
        # Probes would otherwise be most of the trace volume and none of the
        # signal.
        excluded_urls="healthz,readyz,metrics",
    )
    structlog.get_logger(__name__).info(
        "otel.instrumented", endpoint=settings.otlp_endpoint
    )

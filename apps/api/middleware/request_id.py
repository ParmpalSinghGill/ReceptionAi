"""Request correlation — PHASE1-API-CONTRACT: *"Every response carries
``X-Request-Id``, echoed into logs and traces."*

Two decisions worth stating.

**An inbound ``X-Request-Id`` is accepted, but validated.** Accepting it lets a
gateway or a frontend correlate a browser action with the server work it
caused, which is most of the value. Validating it is why the header cannot
become an injection vector: the id is echoed into a response header and into
structured logs, and an unvalidated value can carry a CRLF (header splitting)
or a hundred kilobytes of junk (log flooding). A malformed value is *replaced*,
not rejected — the caller's correlation is worth less than their request.

**The id is a UUIDv7**, from the one canonical generator (amendment A2). Not
because it is stored, but because a time-ordered id sorts usefully in a log
viewer, which is what you actually do with these.
"""

from __future__ import annotations

import contextvars
import re
import time

import structlog
from packages.core.ids import uuid7
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

__all__ = ["RequestIdMiddleware", "current_request_id"]

log = structlog.get_logger(__name__)

HEADER = "X-Request-Id"

#: Conservative on purpose. Anything outside this is replaced rather than
#: sanitized: partial sanitization of an attacker-controlled string that ends
#: up in a header is a game you lose eventually.
_VALID_ID = re.compile(r"^[A-Za-z0-9_.:-]{8,64}$")

current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "receptionai_request_id", default=None
)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assign or adopt a request id, and time the request."""

    def __init__(self, app: ASGIApp, *, header: str = HEADER) -> None:
        super().__init__(app)
        self._header = header

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        inbound = request.headers.get(self._header)
        request_id = inbound if inbound and _VALID_ID.match(inbound) else str(uuid7())

        request.state.request_id = request_id
        token = current_request_id.set(request_id)
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            current_request_id.reset(token)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        response.headers[self._header] = request_id
        # Useful on every response, and the only place a client can see server
        # time without a trace backend.
        response.headers["Server-Timing"] = f"app;dur={elapsed_ms}"

        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=elapsed_ms,
        )
        structlog.contextvars.unbind_contextvars("request_id")
        return response

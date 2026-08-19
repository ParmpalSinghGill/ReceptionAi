"""Exception handlers — RFC 9457 everywhere, amendment A9.

Four handlers, covering every way an error can leave a handler:

* :class:`~apps.api.problems.ProblemException` — raised deliberately by a
  handler or a dependency.
* :class:`~packages.core.errors.DomainError` — raised by ``packages/*``. The
  status and code ride on the exception class, so this handler needs no
  registry of ``isinstance`` checks that a new error type could be added
  without updating.
* ``RequestValidationError`` — FastAPI's body/query validation. Translated
  rather than passed through: FastAPI's default body is ``{"detail": [...]}``,
  a second envelope shape for the same class of failure, and a client would
  have to parse both.
* ``Exception`` — the catch-all. **The message is never forwarded.** An
  unhandled exception's ``str()`` routinely contains a SQL fragment, a file
  path, or prompt content, and prompt content is customer PII (contract §8).
  It is logged, where the masking sweep applies; the client gets a request id
  to quote at support.
"""

from __future__ import annotations

from typing import Any

import structlog
from apps.api.problems import ProblemException, problem_from_domain_error, problem_response
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from packages.core.errors import DomainError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

__all__ = ["install_exception_handlers"]

log = structlog.get_logger(__name__)

#: Starlette raises bare ``HTTPException`` for routing-level failures (404 on
#: an unmatched path, 405 on a wrong method) before any of our code runs. They
#: still have to come out as problem+json, so the status is mapped onto the
#: frozen vocabulary here.
_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    410: "gone",
    415: "unsupported_media_type",
    422: "validation_failed",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}


def install_exception_handlers(app: FastAPI) -> None:
    """Register every handler on ``app``."""

    @app.exception_handler(ProblemException)
    async def _problem(request: Request, exc: ProblemException) -> Response:
        return problem_response(
            request,
            status=exc.status,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            headers=exc.headers,
            **exc.extensions,
        )

    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError) -> Response:
        if exc.http_status >= 500:
            # A 5xx from the domain is our bug or our dependency's outage, and
            # either way someone needs the stack. A 4xx is the caller being
            # told no, which is routine and must not page anyone.
            log.exception("api.domain_error", code=exc.code, path=request.url.path)
        else:
            log.info("api.domain_error", code=exc.code, path=request.url.path)
        return problem_from_domain_error(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        errors: list[dict[str, Any]] = [
            {
                # Drop the leading "body"/"query" segment: the client knows
                # where it put the field, and `body.content` reads worse than
                # `content` in a form error.
                "field": ".".join(str(part) for part in error["loc"][1:]) or "body",
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return problem_response(
            request,
            status=422,
            code="validation_failed",
            detail="One or more fields are invalid.",
            # A problem extension, per RFC 9457, so a form renderer can map
            # each message back to its input without parsing prose.
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        code = _STATUS_CODES.get(exc.status_code, "internal_error")
        detail = exc.detail if isinstance(exc.detail, str) else None
        return problem_response(
            request,
            status=exc.status_code,
            code=code,
            detail=detail,
            headers=dict(getattr(exc, "headers", None) or {}),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        log.exception(
            "api.unhandled_exception",
            path=request.url.path,
            method=request.method,
            error=type(exc).__name__,
        )
        return problem_response(
            request,
            status=500,
            code="internal_error",
            # Deliberately generic. See the module docstring: an exception
            # message can carry a SQL fragment, a path, or prompt content.
            detail=(
                "An unexpected error occurred. Quote the request id if you "
                "contact support."
            ),
        )

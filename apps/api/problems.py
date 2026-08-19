"""RFC 9457 ``application/problem+json`` — contract amendment A9, 06 §2.7.

**One error body, everywhere.** A9 makes the envelope uniform across
handler-level and middleware-level denials, and the only difference between
them is mechanical: middleware runs outside the exception-handler stack, so it
calls :func:`problem_response` directly rather than raising. That is why the
builder is a plain function here rather than living inside a handler.

The ``code`` field is the frozen vocabulary from
:mod:`packages.core.errors`. Clients switch on it, so it is a compatibility
surface. ``title`` comes from :func:`packages.core.errors.title_for`, which
raises ``KeyError`` on an unknown code — deliberately, per A9: a missing title
would otherwise be discoverable only by failing to render an error, at the
moment you most need the error.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from packages.core.errors import DomainError, title_for

__all__ = [
    "PROBLEM_BASE_URI",
    "ProblemException",
    "problem_from_domain_error",
    "problem_response",
]

#: ``type`` is a documentation URI derived mechanically from ``code``, so
#: adding a code never requires adding a URI by hand.
PROBLEM_BASE_URI = "https://receptionai.dev/problems"


def _type_uri(code: str) -> str:
    return f"{PROBLEM_BASE_URI}/{code.replace('_', '-')}"


class ProblemException(Exception):  # noqa: N818
    """Raise from a handler or a dependency to emit a problem response.

    Named ``...Exception`` rather than ``...Error`` deliberately, against the
    N818 convention: it mirrors ``fastapi.HTTPException``, which is the class
    every FastAPI developer reaches for and the one this replaces. A
    ``ProblemError`` sitting next to ``HTTPException`` in the same handler
    reads as a different *kind* of thing, and the whole point is that it is the
    same kind with a better body.

    Distinct from ``fastapi.HTTPException`` on purpose. FastAPI wraps
    ``HTTPException.detail`` in ``{"detail": ...}``, which for a dict detail
    produces ``{"detail": {"code": ...}}`` — a second envelope shape for the
    same 403, and a client that has to handle both. Every denial in this
    application raises this instead, and the installed handler renders the one
    shape (05 §A2.4).
    """

    def __init__(
        self,
        *,
        status: int,
        code: str,
        detail: str | None = None,
        title: str | None = None,
        headers: dict[str, str] | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(detail or code)
        self.status = status
        self.code = code
        # `title_for` rather than a default: an unknown code must fail here,
        # in a place a test reaches, rather than rendering a plausible-looking
        # title for a typo'd code that then ships.
        self.title = title or title_for(code)
        self.detail = detail
        self.headers = headers or {}
        self.extensions = {k: v for k, v in extensions.items() if v is not None}


def problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str | None = None,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
    **extensions: Any,
) -> JSONResponse:
    """Build the one error body. Callable from middleware and from handlers."""
    request_id = getattr(request.state, "request_id", None)

    body: dict[str, Any] = {
        "type": _type_uri(code),
        "title": title or title_for(code),
        "status": status,
        "code": code,
        "instance": request.url.path,
    }
    if detail:
        body["detail"] = detail
    if request_id:
        body["request_id"] = request_id
    # RFC 9457 permits extension members at the top level, which is where
    # `missing_permissions`, `required_aal` and `retry_after_ms` ride — so no
    # information is lost relative to a bespoke envelope.
    body.update({k: v for k, v in extensions.items() if v is not None})

    response_headers = dict(headers or {})
    if request_id:
        response_headers["X-Request-Id"] = request_id
    if status == 401 and "WWW-Authenticate" not in response_headers:
        # RFC 9110: a 401 must carry WWW-Authenticate, and the widget's
        # reconnect logic reads the error parameter to decide between
        # "re-mint the ticket" and "the session is genuinely gone".
        response_headers["WWW-Authenticate"] = (
            f'Bearer realm="api.receptionai", error="{code}"'
        )

    return JSONResponse(
        status_code=status,
        content=body,
        headers=response_headers,
        media_type="application/problem+json",
    )


def problem_from_domain_error(request: Request, exc: DomainError) -> JSONResponse:
    """Render any :class:`~packages.core.errors.DomainError`.

    The mapping is carried by the exception itself — ``code`` and
    ``http_status`` are class attributes on each subclass — so the API layer
    needs no registry of ``isinstance`` checks that a new error type could be
    added without updating.
    """
    headers: dict[str, str] = {}
    retry_after_ms = exc.extensions.get("retry_after_ms")
    if retry_after_ms:
        # Seconds, per RFC 9110; the millisecond value stays in the body for
        # clients that want the precision.
        headers["Retry-After"] = str(max(1, int(retry_after_ms) // 1000))

    return problem_response(
        request,
        status=exc.http_status,
        code=exc.code,
        title=exc.title,
        detail=exc.message,
        headers=headers,
        **exc.extensions,
    )

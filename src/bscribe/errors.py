"""RFC 9457 ``application/problem+json`` error handling.

The status-code table in docs/design.md is the contract. Two rules worth
noting here:

- Request validation failures return 400 ("malformed request"), overriding
  FastAPI's default 422 — the design doc reserves 422 for "supported format,
  document unparseable".
- Error responses obey the same privacy rules as logs: submitted values are
  never echoed back, and unexpected exceptions never leak internals.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

import structlog
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import FastAPI, Request, Response

PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"

logger = structlog.get_logger()


def problem_response(
    *,
    status: int,
    detail: str | None = None,
    title: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build an RFC 9457 problem response.

    Args:
        status: HTTP status code; also the ``status`` body member.
        detail: Human-readable explanation; the key is omitted when ``None``
            (allowed by RFC 9457).
        title: Short summary; defaults to the standard status phrase.
        headers: Extra response headers (e.g. ``WWW-Authenticate``).

    Returns:
        A ``JSONResponse`` with media type ``application/problem+json``.
    """
    body: dict[str, str | int] = {
        "type": "about:blank",
        "title": title if title is not None else HTTPStatus(status).phrase,
        "status": status,
    }
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(
        status_code=status,
        content=body,
        media_type=PROBLEM_JSON_MEDIA_TYPE,
        headers=dict(headers) if headers is not None else None,
    )


# Starlette types exception handlers as taking a plain Exception, so each
# handler narrows via isinstance instead of a tighter signature; the raise
# branches are unreachable because Starlette dispatches by exception type.


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    del request
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        raise exc
    return problem_response(
        status=exc.status_code, detail=exc.detail, headers=exc.headers
    )


async def _handle_validation_error(request: Request, exc: Exception) -> Response:
    del request
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise exc
    # Build detail from field locations and messages only — errors() also
    # carries the submitted input, which must never be echoed back.
    problems = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    )
    return problem_response(status=400, detail=problems or "Invalid request")


async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
    # Type name only, no exc_info and no str(exc): tracebacks and exception
    # messages can quote parser internals or user-supplied values, which the
    # privacy contract keeps out of logs (see docs/design.md — Privacy).
    logger.error(
        "unhandled_error",
        method=request.method,
        path=request.url.path,
        error_type=type(exc).__name__,
    )
    return problem_response(status=500, detail="Internal server error")


def register_error_handlers(app: FastAPI) -> None:
    """Install problem+json handlers on the app.

    Overrides FastAPI's built-in ``RequestValidationError`` handler (handlers
    are keyed by exception class, last registration wins).
    """
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)

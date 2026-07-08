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

from bscribe.domain.errors import (
    DocumentUnparseableError,
    JobTimeoutError,
    UnsupportedFormatError,
    WorkerCrashedError,
)
from bscribe.uploads import UploadTooLargeError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import FastAPI, Request, Response

PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"

# Shared so the exception handler and the app's Content-Length prefilter
# (which runs outside ExceptionMiddleware and cannot raise the exception)
# emit an identical 413 body instead of two copy-pasted literals.
UPLOAD_TOO_LARGE_DETAIL = "upload exceeds maximum size"

# Shared with the job runner (bscribe.runner), which translates the same
# parse failures into a failed job's failure_detail instead of an HTTP
# status — one definition keeps sync responses and job records in step.
UNPARSEABLE_DETAIL = "document could not be parsed"
TIMEOUT_DETAIL = "timeout"

# The 409 body for a result fetch on a failed job (GET /v1/jobs/{id}/result).
# Deliberately not composed with the job's failure_detail: callers read the
# reason from the status endpoint, and string composition would be a future
# leak surface for nothing.
JOB_FAILED_NO_RESULT_DETAIL = "job failed; no result available"

logger = structlog.get_logger()

# Domain/ingestion exceptions raised on the sync convert path, mapped to the
# status-code contract (docs/design.md). Details are fixed strings — never
# ``str(exc)`` — because exception messages can quote parser internals or
# submitted values (see Privacy). The async path catches these inside the
# job runner (bscribe.runner), so these handlers only ever fire on the sync
# path.
_DOMAIN_ERROR_STATUS: dict[type[Exception], tuple[int, str]] = {
    UnsupportedFormatError: (415, "unsupported input format"),
    UploadTooLargeError: (413, UPLOAD_TOO_LARGE_DETAIL),
    DocumentUnparseableError: (422, UNPARSEABLE_DETAIL),
    JobTimeoutError: (500, TIMEOUT_DETAIL),
    WorkerCrashedError: (500, "Internal server error"),
}


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


async def _handle_domain_error(request: Request, exc: Exception) -> Response:
    del request
    # Starlette dispatches by MRO, so a subclass of a mapped error reaches
    # this handler too; walk the MRO rather than exact-matching type(exc),
    # which would KeyError (→ generic 500) on any future subclass.
    for cls in type(exc).__mro__:
        mapping = _DOMAIN_ERROR_STATUS.get(cls)
        if mapping is not None:
            status, detail = mapping
            return problem_response(status=status, detail=detail)
    # Unreachable via normal registration (only mapped types are registered);
    # defensive so a mis-registration degrades to 500, not an unhandled error.
    return problem_response(  # pragma: no cover
        status=500, detail="Internal server error"
    )


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
    for exc_type in _DOMAIN_ERROR_STATUS:
        app.add_exception_handler(exc_type, _handle_domain_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)

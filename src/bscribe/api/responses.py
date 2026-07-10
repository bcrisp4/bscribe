"""OpenAPI documentation for the error side of the wire contract.

The runtime bodies are built in :mod:`bscribe.errors` (RFC 9457
``application/problem+json``); this module is the *documentation* mirror —
a pydantic model of that body plus a helper that turns a set of status
codes into a FastAPI ``responses`` mapping, so every ``/v1`` operation
advertises the failures it can return.

The status codes and their meanings are the contract in ``docs/design.md``
(status-code table). Keep :data:`_STATUS_DESCRIPTIONS` in step with it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# One human-readable line per documented status, taken from the design-doc
# status-code table. Not every code applies to every route — each operation
# passes the subset it can actually return to :func:`error_responses`.
_STATUS_DESCRIPTIONS: dict[int, str] = {
    400: "Malformed request (missing/invalid parameters).",
    401: "Missing or invalid bearer token.",
    404: "Unknown job id, or a job owned by a different token "
    "(indistinguishable by design).",
    409: "Result requested for a job that failed; no result is available.",
    413: "Upload exceeds the configured maximum size.",
    415: "Unsupported input format.",
    422: "Supported format, but the document could not be parsed.",
    500: "Worker crash, job timeout on the sync path, or another unexpected failure.",
}


class Problem(BaseModel):
    """An RFC 9457 ``application/problem+json`` error body.

    Documentation-only: the live response is assembled by
    :func:`bscribe.errors.problem_response`. Detail strings come from a
    fixed vocabulary and never echo submitted values (see
    ``docs/design.md`` — Privacy).
    """

    type: str = Field(default="about:blank", examples=["about:blank"])
    title: str = Field(examples=["Unauthorized"])
    status: int = Field(examples=[401])
    detail: str | None = Field(
        default=None, examples=["Invalid or missing bearer token"]
    )


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """Build a FastAPI ``responses`` mapping for the given error codes.

    Each entry documents a :class:`Problem` body. The live media type is
    ``application/problem+json``; that is stated in every description since
    FastAPI files the schema under ``application/json``.

    Args:
        statuses: The HTTP status codes this operation can return.

    Returns:
        A mapping suitable for a route's ``responses=`` argument.
    """
    return {
        status: {
            "model": Problem,
            "description": (
                f"{_STATUS_DESCRIPTIONS[status]} Body is `application/problem+json`."
            ),
        }
        for status in statuses
    }

"""Pydantic wire models for the conversion API.

Kept separate from the domain dataclasses (``ParsedDocument``, ``Job``):
these are the HTTP contract. The M3 ``pipeline`` block and the deferred
``ocr_used`` signal are deliberately absent (docs/design.md — Interfaces,
Closed issues).
"""

from __future__ import annotations

# datetime stays a runtime import: pydantic resolves field annotations at
# class-build time.
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

from bscribe.domain.models import JobStatus, OcrMode, OutputFormat

if TYPE_CHECKING:
    from bscribe.domain.models import Job


class ConvertMetadata(BaseModel):
    """Per-conversion metadata returned alongside the content."""

    pages: int
    duration_ms: int


class ConvertResponse(BaseModel):
    """The body of a successful ``POST /v1/convert``.

    Also the ``200`` body of ``GET /v1/jobs/{id}/result`` — async callers
    fetch the same result document the sync endpoint returns inline.
    """

    output: OutputFormat
    content: str
    metadata: ConvertMetadata


class JobResponse(BaseModel):
    """One job's wire representation.

    Used everywhere a job appears: the ``201`` from ``POST /v1/jobs``,
    ``GET /v1/jobs/{id}``, items of ``GET /v1/jobs``, and the ``202`` body
    of a not-yet-ready result fetch. A superset of the design-doc sample
    (``id`` + ``status``) — additive fields are allowed by the versioning
    contract. ``failure_detail`` only ever carries the fixed strings from
    :mod:`bscribe.errors`/:mod:`bscribe.runner`, never parser output.
    """

    id: str
    status: JobStatus
    output: OutputFormat
    ocr: OcrMode
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_detail: str | None = None

    @classmethod
    def from_job(cls, job: Job) -> JobResponse:
        """Map a domain job snapshot onto the wire model.

        Args:
            job: The metadata snapshot from the job store.

        Returns:
            The equivalent wire representation.
        """
        return cls(
            id=job.id,
            status=job.status,
            output=job.output,
            ocr=job.ocr,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            failure_detail=job.failure_detail,
        )


class JobListResponse(BaseModel):
    """The body of ``GET /v1/jobs`` — a wrapper object, not a bare array,
    so pagination/metadata can be added later without a ``/v2``."""

    jobs: list[JobResponse]

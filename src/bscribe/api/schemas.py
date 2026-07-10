"""Pydantic wire models for the conversion API.

Kept separate from the domain dataclasses (``ParsedDocument``, ``Job``):
these are the HTTP contract. The deferred ``ocr_used`` signal is
deliberately absent (docs/design.md — Closed issues); the ``pipeline``
block on results carries the re-ingestion contract instead.
"""

from __future__ import annotations

# datetime stays a runtime import: pydantic resolves field annotations at
# class-build time.
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

from bscribe.domain.models import JobStatus, OcrMode, OutputFormat

if TYPE_CHECKING:
    from bscribe.domain.models import Job, ParsedDocument, PipelineStamp


class PipelineBlock(BaseModel):
    """The pipeline identity a caller stores for the re-ingestion contract.

    Same shape in two places: as ``metadata.pipeline`` on a result it lists
    the components that document actually traversed; as the ``GET /v1/info``
    body it lists every component. The ``fingerprint`` is identical in both
    — a hash over *all* component versions, not just the listed subset — so
    callers compare a stored block against ``/v1/info`` field-for-field
    (docs/design.md — Re-ingestion contract).
    """

    fingerprint: str
    components: dict[str, str]

    @classmethod
    def from_stamp(cls, stamp: PipelineStamp) -> PipelineBlock:
        """Map a domain :class:`PipelineStamp` onto the wire model.

        Args:
            stamp: The app-wide or per-document stamp.

        Returns:
            The equivalent wire representation.
        """
        return cls(fingerprint=stamp.fingerprint, components=dict(stamp.components))


class ConvertMetadata(BaseModel):
    """Per-conversion metadata returned alongside the content."""

    pages: int
    duration_ms: int
    # ``None`` only for a pre-M3.1 stored async result (NULL ``result_pipeline``);
    # every freshly parsed document carries its traversed stamp.
    pipeline: PipelineBlock | None = None


class ConvertResponse(BaseModel):
    """The body of a successful ``POST /v1/convert``.

    Also the ``200`` body of ``GET /v1/jobs/{id}/result`` — async callers
    fetch the same result document the sync endpoint returns inline.
    """

    output: OutputFormat
    content: str
    metadata: ConvertMetadata

    @classmethod
    def from_result(
        cls, output: OutputFormat, result: ParsedDocument
    ) -> ConvertResponse:
        """Map a parse result onto the wire model.

        The one definition of the result → wire mapping (including the
        ``duration_ms`` rounding), so the sync and async result bodies
        cannot drift apart.

        Args:
            output: The format the caller requested.
            result: The parse result to serialize.

        Returns:
            The equivalent wire representation.
        """
        return cls(
            output=output,
            content=result.content,
            metadata=ConvertMetadata(
                pages=result.pages,
                duration_ms=round(result.duration_ms),
                pipeline=(
                    PipelineBlock.from_stamp(result.pipeline)
                    if result.pipeline is not None
                    else None
                ),
            ),
        )


class JobResponse(BaseModel):
    """One job's wire representation.

    Used everywhere a job appears: the ``201`` from ``POST /v1/jobs``,
    ``GET /v1/jobs/{id}``, items of ``GET /v1/jobs``, and the ``202`` body
    of a not-yet-ready result fetch. A superset of the design-doc sample
    (``id`` + ``status``) — additive fields are allowed by the versioning
    contract. ``failure_detail`` only ever carries the fixed strings
    defined in :mod:`bscribe.errors`, never parser output.
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

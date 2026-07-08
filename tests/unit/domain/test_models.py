"""Tests for bscribe.domain.models."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from bscribe.domain.models import (
    Component,
    Job,
    JobStatus,
    OcrMode,
    OutputFormat,
    ParsedDocument,
    PipelineStamp,
)


def make_job(
    *,
    id: str = "abcd1234abcd1234",  # noqa: A002 - mirrors the field name
    token_id: str = "feed0001",
    output: OutputFormat = OutputFormat.MARKDOWN,
    ocr: OcrMode = OcrMode.AUTO,
    status: JobStatus = JobStatus.QUEUED,
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    failure_detail: str | None = None,
) -> Job:
    """Build a Job with sensible defaults (a freshly queued job)."""
    return Job(
        id=id,
        token_id=token_id,
        output=output,
        ocr=ocr,
        status=status,
        created_at=created_at or datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
        started_at=started_at,
        finished_at=finished_at,
        failure_detail=failure_detail,
    )


def make_parsed_document(
    *,
    content: str = "# Title\n\nBody.",
    pages: int = 1,
    duration_ms: float = 12.5,
    pipeline: PipelineStamp | None = None,
) -> ParsedDocument:
    """Build a ParsedDocument with sensible defaults."""
    return ParsedDocument(
        content=content, pages=pages, duration_ms=duration_ms, pipeline=pipeline
    )


class TestOutputFormat:
    """Enum values are the wire strings from the API contract."""

    def test_markdown_value(self) -> None:
        assert OutputFormat.MARKDOWN.value == "markdown"

    def test_text_value(self) -> None:
        assert OutputFormat.TEXT.value == "text"

    def test_only_two_formats(self) -> None:
        assert len(OutputFormat) == 2


class TestOcrMode:
    """Enum values are the wire strings; force is deliberately absent."""

    def test_auto_value(self) -> None:
        assert OcrMode.AUTO.value == "auto"

    def test_off_value(self) -> None:
        assert OcrMode.OFF.value == "off"

    def test_force_absent(self) -> None:
        # liteparse has no force-OCR; see docs/design.md Closed issues.
        assert len(OcrMode) == 2


class TestJobStatus:
    """Enum values are the wire strings from the API contract."""

    def test_wire_values(self) -> None:
        assert JobStatus.QUEUED.value == "queued"
        assert JobStatus.RUNNING.value == "running"
        assert JobStatus.DONE.value == "done"
        assert JobStatus.FAILED.value == "failed"

    def test_exactly_four_states(self) -> None:
        assert len(JobStatus) == 4


class TestJob:
    """A job is an immutable snapshot; transitions happen in the store."""

    def test_carries_submission_fields(self) -> None:
        job = make_job(token_id="cafe0001", ocr=OcrMode.OFF)
        assert job.token_id == "cafe0001"
        assert job.output is OutputFormat.MARKDOWN
        assert job.ocr is OcrMode.OFF
        assert job.status is JobStatus.QUEUED

    def test_optional_fields_default_none_for_queued_job(self) -> None:
        job = make_job()
        assert job.started_at is None
        assert job.finished_at is None
        assert job.failure_detail is None

    def test_is_metadata_only(self) -> None:
        # Results never ride on Job — they are read only via the store's
        # get_result, so status/list reads never pay for the content blob.
        assert not hasattr(make_job(status=JobStatus.DONE), "result")

    def test_is_frozen(self) -> None:
        job = make_job()
        with pytest.raises(dataclasses.FrozenInstanceError):
            job.status = JobStatus.DONE  # type: ignore[misc]

    def test_lifecycle_fields_default_to_none(self) -> None:
        job = Job(
            id="abcd1234abcd1234",
            token_id="feed0001",
            output=OutputFormat.MARKDOWN,
            ocr=OcrMode.AUTO,
            status=JobStatus.QUEUED,
            created_at=datetime(2026, 7, 7, tzinfo=UTC),
        )
        assert job.started_at is None
        assert job.finished_at is None
        assert job.failure_detail is None

    def test_failed_without_detail_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="failed"):
            make_job(status=JobStatus.FAILED, failure_detail=None)

    def test_failure_detail_on_non_failed_job_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="failure_detail"):
            make_job(status=JobStatus.QUEUED, failure_detail="oops")

    @pytest.mark.parametrize("field", ["created_at", "started_at", "finished_at"])
    def test_naive_timestamps_are_rejected(self, field: str) -> None:
        # Naive datetimes serialize without an offset and break the store's
        # lexicographic ordering; the model rejects them at the source.
        overrides: dict[str, object] = {field: datetime(2026, 7, 7, 12, 0)}  # noqa: DTZ001
        if field != "created_at":
            overrides["status"] = JobStatus.RUNNING
        with pytest.raises(ValueError, match=field):
            make_job(**overrides)  # type: ignore[arg-type]


class TestParsedDocument:
    """The result type carries content plus conversion metadata, immutably."""

    def test_carries_content_and_metadata(self) -> None:
        doc = make_parsed_document(content="hello", pages=3)
        assert doc.content == "hello"
        assert doc.pages == 3
        assert doc.duration_ms == 12.5

    def test_is_frozen(self) -> None:
        doc = make_parsed_document()
        with pytest.raises(dataclasses.FrozenInstanceError):
            doc.content = "changed"  # type: ignore[misc]

    def test_pipeline_defaults_to_none(self) -> None:
        # None means "not yet stamped" or "predates this feature" — stamping
        # happens parent-side in WorkerPool, not inside the worker.
        assert make_parsed_document().pipeline is None

    def test_constructible_with_a_pipeline_stamp(self) -> None:
        stamp = PipelineStamp(
            fingerprint="abc123def456",
            components={Component.BSCRIBE.value: "0.3.0"},
        )
        doc = make_parsed_document(pipeline=stamp)
        assert doc.pipeline is stamp

"""Tests for bscribe.domain.models."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from bscribe.domain.models import (
    Job,
    JobStatus,
    OcrMode,
    OutputFormat,
    ParsedDocument,
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
    result: ParsedDocument | None = None,
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
        result=result,
    )


def make_parsed_document(
    *,
    content: str = "# Title\n\nBody.",
    pages: int = 1,
    duration_ms: float = 12.5,
) -> ParsedDocument:
    """Build a ParsedDocument with sensible defaults."""
    return ParsedDocument(content=content, pages=pages, duration_ms=duration_ms)


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
        assert job.result is None

    def test_done_job_carries_result(self) -> None:
        result = make_parsed_document()
        job = make_job(status=JobStatus.DONE, result=result)
        assert job.result == result

    def test_is_frozen(self) -> None:
        job = make_job()
        with pytest.raises(dataclasses.FrozenInstanceError):
            job.status = JobStatus.DONE  # type: ignore[misc]


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

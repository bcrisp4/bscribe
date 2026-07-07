"""Tests for bscribe.domain.jobs."""

from __future__ import annotations

from datetime import UTC, datetime

from bscribe.domain.jobs import create_job
from bscribe.domain.models import JobStatus, OcrMode, OutputFormat


def test_new_job_is_queued_with_only_created_at_set() -> None:
    job = create_job(
        token_id="feed0001", output=OutputFormat.MARKDOWN, ocr=OcrMode.AUTO
    )
    assert job.status is JobStatus.QUEUED
    assert job.started_at is None
    assert job.finished_at is None
    assert job.failure_detail is None
    assert job.result is None


def test_preserves_submission_parameters() -> None:
    job = create_job(token_id="cafe0002", output=OutputFormat.TEXT, ocr=OcrMode.OFF)
    assert job.token_id == "cafe0002"
    assert job.output is OutputFormat.TEXT
    assert job.ocr is OcrMode.OFF


def test_id_is_sixteen_hex_chars() -> None:
    job = create_job(token_id="t", output=OutputFormat.MARKDOWN, ocr=OcrMode.AUTO)
    assert len(job.id) == 16
    assert all(c in "0123456789abcdef" for c in job.id)


def test_ids_are_unique() -> None:
    ids = {
        create_job(token_id="t", output=OutputFormat.MARKDOWN, ocr=OcrMode.AUTO).id
        for _ in range(100)
    }
    assert len(ids) == 100


def test_created_at_is_utc_aware_and_current() -> None:
    before = datetime.now(tz=UTC)
    job = create_job(token_id="t", output=OutputFormat.MARKDOWN, ocr=OcrMode.AUTO)
    after = datetime.now(tz=UTC)
    assert job.created_at.tzinfo is UTC
    assert before <= job.created_at <= after

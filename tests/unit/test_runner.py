"""Tests for bscribe.runner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from bscribe.domain.errors import (
    DocumentUnparseableError,
    JobTimeoutError,
    WorkerCrashedError,
)
from bscribe.domain.jobs import create_job
from bscribe.domain.models import (
    Job,
    JobStatus,
    OcrMode,
    OutputFormat,
    ParsedDocument,
)
from bscribe.errors import TIMEOUT_DETAIL, UNPARSEABLE_DETAIL
from bscribe.runner import (
    INTERNAL_ERROR_DETAIL,
    WORKER_CRASHED_DETAIL,
    JobRunner,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class RecordingJobStore:
    """In-memory store recording transitions, with the metadata/result split.

    Mirrors the compare-and-set contract of the real adapter so runner
    races (delete-before-start, delete-mid-parse) can be simulated.
    """

    jobs: dict[str, Job] = field(default_factory=dict[str, "Job"])
    results: dict[str, ParsedDocument] = field(
        default_factory=dict[str, "ParsedDocument"]
    )

    def add(self, job: Job) -> None:
        self.jobs[job.id] = job

    def get(self, job_id: str, token_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        return job if job is not None and job.token_id == token_id else None

    def get_result(self, job_id: str, token_id: str) -> ParsedDocument | None:
        job = self.get(job_id, token_id)
        if job is None or job.status is not JobStatus.DONE:
            return None
        return self.results[job_id]

    def list_for_token(
        self, token_id: str, *, status: JobStatus | None = None
    ) -> list[Job]:
        return [
            job
            for job in self.jobs.values()
            if job.token_id == token_id and (status is None or job.status is status)
        ]

    def mark_running(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status is not JobStatus.QUEUED:
            return False
        self.jobs[job_id] = replace(
            job, status=JobStatus.RUNNING, started_at=datetime.now(tz=UTC)
        )
        return True

    def mark_done(self, job_id: str, result: ParsedDocument) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status is not JobStatus.RUNNING:
            return False
        self.jobs[job_id] = replace(
            job, status=JobStatus.DONE, finished_at=datetime.now(tz=UTC)
        )
        self.results[job_id] = result
        return True

    def mark_failed(self, job_id: str, detail: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
            return False
        self.jobs[job_id] = replace(
            job,
            status=JobStatus.FAILED,
            finished_at=datetime.now(tz=UTC),
            failure_detail=detail,
        )
        return True

    def delete(self, job_id: str, token_id: str) -> bool:
        if self.get(job_id, token_id) is None:
            return False
        del self.jobs[job_id]
        self.results.pop(job_id, None)
        return True


class GatedPool:
    """Pool fake whose parse blocks until released (or raises).

    ``started`` lets a test observe the running state deterministically;
    ``release`` lets it decide when the parse completes.
    """

    def __init__(
        self,
        *,
        result: ParsedDocument | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._result = result or ParsedDocument(
            content="# Heading", pages=2, duration_ms=41.7
        )
        self._exc = exc
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[Path] = []

    async def parse(
        self, path: Path, *, output: OutputFormat, ocr: OcrMode
    ) -> ParsedDocument:
        del output, ocr
        self.calls.append(path)
        self.started.set()
        await self.release.wait()
        if self._exc is not None:
            raise self._exc
        return self._result


def make_upload(tmp_path: Path) -> Path:
    upload = tmp_path / "upload.pdf"
    upload.write_bytes(b"%PDF-1.4 fake body")
    return upload


def submit_queued_job(
    runner: JobRunner, store: RecordingJobStore, pool: GatedPool, upload: Path
) -> Job:
    """Persist a fresh job and submit it, as POST /v1/jobs does."""
    job = create_job(
        token_id="feed0001", output=OutputFormat.MARKDOWN, ocr=OcrMode.AUTO
    )
    store.add(job)
    runner.submit(
        job_id=job.id,
        path=upload,
        output=job.output,
        ocr=job.ocr,
        pool=pool,
    )
    return job


class TestJobRunnerHappyPath:
    async def test_job_ends_done_with_result_stored(self, tmp_path: Path) -> None:
        store = RecordingJobStore()
        pool = GatedPool()
        pool.release.set()
        runner = JobRunner(store=store)
        job = submit_queued_job(runner, store, pool, make_upload(tmp_path))
        await runner.drain()
        assert store.jobs[job.id].status is JobStatus.DONE
        assert store.get_result(job.id, job.token_id) == ParsedDocument(
            content="# Heading", pages=2, duration_ms=41.7
        )

    async def test_upload_deleted_after_success(self, tmp_path: Path) -> None:
        store = RecordingJobStore()
        pool = GatedPool()
        pool.release.set()
        runner = JobRunner(store=store)
        upload = make_upload(tmp_path)
        submit_queued_job(runner, store, pool, upload)
        await runner.drain()
        assert not upload.exists()

    async def test_job_is_running_while_parse_in_flight(self, tmp_path: Path) -> None:
        store = RecordingJobStore()
        pool = GatedPool()
        runner = JobRunner(store=store)
        job = submit_queued_job(runner, store, pool, make_upload(tmp_path))
        await pool.started.wait()
        assert store.jobs[job.id].status is JobStatus.RUNNING
        pool.release.set()
        await runner.drain()

    async def test_task_mapping_exists_in_flight_and_clears_after(
        self, tmp_path: Path
    ) -> None:
        store = RecordingJobStore()
        pool = GatedPool()
        runner = JobRunner(store=store)
        job = submit_queued_job(runner, store, pool, make_upload(tmp_path))
        await pool.started.wait()
        assert runner.task_for(job.id) is not None
        pool.release.set()
        await runner.drain()
        # The done-callback that pops the mapping runs via call_soon after
        # gather returns; yield once so it fires.
        await asyncio.sleep(0)
        assert runner.task_for(job.id) is None


class TestJobRunnerFailures:
    @pytest.mark.parametrize(
        ("exc", "expected_detail"),
        [
            (DocumentUnparseableError("quotes document internals"), UNPARSEABLE_DETAIL),
            (JobTimeoutError("job timed out"), TIMEOUT_DETAIL),
            (WorkerCrashedError("worker process crashed"), WORKER_CRASHED_DETAIL),
            (RuntimeError("quotes document internals"), INTERNAL_ERROR_DETAIL),
        ],
        ids=["unparseable", "timeout", "crash", "unexpected"],
    )
    async def test_parse_failure_marks_failed_with_fixed_detail(
        self, tmp_path: Path, exc: Exception, expected_detail: str
    ) -> None:
        store = RecordingJobStore()
        pool = GatedPool(exc=exc)
        pool.release.set()
        runner = JobRunner(store=store)
        upload = make_upload(tmp_path)
        job = submit_queued_job(runner, store, pool, upload)
        await runner.drain()
        failed = store.jobs[job.id]
        assert failed.status is JobStatus.FAILED
        # Fixed constant, never str(exc) — privacy hard rule.
        assert failed.failure_detail == expected_detail
        assert "document internals" not in (failed.failure_detail or "")
        assert not upload.exists()

    async def test_skips_parse_when_job_gone_before_start(self, tmp_path: Path) -> None:
        """Delete-before-start race: mark_running refuses, nothing parses."""
        store = RecordingJobStore()
        pool = GatedPool()
        pool.release.set()
        runner = JobRunner(store=store)
        upload = make_upload(tmp_path)
        job = create_job(
            token_id="feed0001", output=OutputFormat.MARKDOWN, ocr=OcrMode.AUTO
        )
        # Deliberately never added to the store — mark_running returns False.
        runner.submit(
            job_id=job.id,
            path=upload,
            output=job.output,
            ocr=job.ocr,
            pool=pool,
        )
        await runner.drain()
        assert pool.calls == []
        assert not upload.exists()

    async def test_discards_result_when_job_deleted_mid_parse(
        self, tmp_path: Path
    ) -> None:
        """Delete-vs-complete race: a late result must not resurrect the job."""
        store = RecordingJobStore()
        pool = GatedPool()
        runner = JobRunner(store=store)
        upload = make_upload(tmp_path)
        job = submit_queued_job(runner, store, pool, upload)
        await pool.started.wait()
        store.delete(job.id, job.token_id)
        pool.release.set()
        await runner.drain()
        assert job.id not in store.jobs
        assert job.id not in store.results
        assert not upload.exists()


class TestJobRunnerCancellation:
    async def test_cancel_leaves_store_state_alone_and_deletes_upload(
        self, tmp_path: Path
    ) -> None:
        """Shutdown story: cancelled jobs stay running for the #19 sweep."""
        store = RecordingJobStore()
        pool = GatedPool()
        runner = JobRunner(store=store)
        upload = make_upload(tmp_path)
        job = submit_queued_job(runner, store, pool, upload)
        await pool.started.wait()
        task = runner.task_for(job.id)
        assert task is not None
        task.cancel()
        await runner.drain()
        assert store.jobs[job.id].status is JobStatus.RUNNING
        assert not upload.exists()

    async def test_aclose_cancels_all_in_flight_tasks(self, tmp_path: Path) -> None:
        store = RecordingJobStore()
        pool = GatedPool()
        runner = JobRunner(store=store)
        uploads: list[Path] = []
        for index in range(3):
            subdir = tmp_path / f"job{index}"
            subdir.mkdir()
            uploads.append(make_upload(subdir))
        jobs = [submit_queued_job(runner, store, pool, upload) for upload in uploads]
        await pool.started.wait()
        await runner.aclose()
        assert all(not upload.exists() for upload in uploads)
        # No terminal transition at shutdown: statuses remain queued/running.
        assert all(
            store.jobs[job.id].status in (JobStatus.QUEUED, JobStatus.RUNNING)
            for job in jobs
        )

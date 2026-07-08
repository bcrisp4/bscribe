"""Tests for bscribe.runner."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from bscribe.domain.errors import (
    DocumentUnparseableError,
    JobTimeoutError,
    WorkerCrashedError,
)
from bscribe.domain.jobs import create_job
from bscribe.domain.models import (
    JobStatus,
    OcrMode,
    OutputFormat,
    ParsedDocument,
)
from bscribe.errors import (
    INTERNAL_ERROR_DETAIL,
    TIMEOUT_DETAIL,
    UNPARSEABLE_DETAIL,
    WORKER_CRASHED_DETAIL,
)
from bscribe.runner import JobRunner
from tests.unit.fakes import FakeJobStore, GatedPool

if TYPE_CHECKING:
    from pathlib import Path

    from bscribe.domain.models import Job


class MarkDoneBrokenStore(FakeJobStore):
    """Store whose mark_done raises — a transient SQLite failure stand-in."""

    def mark_done(self, job_id: str, result: ParsedDocument) -> bool:
        del job_id, result
        raise RuntimeError("database is locked")


class FullyBrokenStore(FakeJobStore):
    """Store where every transition raises — the store is down entirely."""

    def mark_running(self, job_id: str) -> bool:
        del job_id
        raise RuntimeError("database is locked")

    def mark_failed(self, job_id: str, detail: str) -> bool:
        del job_id, detail
        raise RuntimeError("database is locked")


def make_upload(tmp_path: Path) -> Path:
    upload = tmp_path / "upload.pdf"
    upload.write_bytes(b"%PDF-1.4 fake body")
    return upload


def submit_queued_job(
    runner: JobRunner, store: FakeJobStore, pool: GatedPool, upload: Path
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
        store=store,
        pool=pool,
    )
    return job


class TestJobRunnerHappyPath:
    async def test_job_ends_done_with_result_stored(self, tmp_path: Path) -> None:
        store = FakeJobStore()
        pool = GatedPool()
        pool.release.set()
        runner = JobRunner()
        job = submit_queued_job(runner, store, pool, make_upload(tmp_path))
        await runner.drain()
        assert store.jobs[job.id].status is JobStatus.DONE
        assert store.get_result(job.id, job.token_id) == ParsedDocument(
            content="# Heading", pages=3, duration_ms=41.7
        )

    async def test_upload_deleted_after_success(self, tmp_path: Path) -> None:
        store = FakeJobStore()
        pool = GatedPool()
        pool.release.set()
        runner = JobRunner()
        upload = make_upload(tmp_path)
        submit_queued_job(runner, store, pool, upload)
        await runner.drain()
        assert not upload.exists()

    async def test_job_is_running_while_parse_in_flight(self, tmp_path: Path) -> None:
        store = FakeJobStore()
        pool = GatedPool()
        runner = JobRunner()
        job = submit_queued_job(runner, store, pool, make_upload(tmp_path))
        await pool.started.wait()
        assert store.jobs[job.id].status is JobStatus.RUNNING
        pool.release.set()
        await runner.drain()

    async def test_task_mapping_exists_in_flight_and_clears_after(
        self, tmp_path: Path
    ) -> None:
        store = FakeJobStore()
        pool = GatedPool()
        runner = JobRunner()
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
        store = FakeJobStore()
        pool = GatedPool(exc=exc)
        pool.release.set()
        runner = JobRunner()
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
        store = FakeJobStore()
        pool = GatedPool()
        pool.release.set()
        runner = JobRunner()
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
            store=store,
            pool=pool,
        )
        await runner.drain()
        assert pool.calls == []
        assert not upload.exists()

    async def test_discards_result_when_job_deleted_mid_parse(
        self, tmp_path: Path
    ) -> None:
        """Delete-vs-complete race: a late result must not resurrect the job."""
        store = FakeJobStore()
        pool = GatedPool()
        runner = JobRunner()
        upload = make_upload(tmp_path)
        job = submit_queued_job(runner, store, pool, upload)
        await pool.started.wait()
        store.delete(job.id, job.token_id)
        pool.release.set()
        await runner.drain()
        assert job.id not in store.jobs
        assert job.id not in store.results
        assert not upload.exists()


class TestJobRunnerStoreFailures:
    """A failing store must never leak an exception out of the job task."""

    async def test_mark_done_failure_marks_job_failed_best_effort(
        self, tmp_path: Path
    ) -> None:
        """The parse outcome is lost, but the job still goes terminal."""
        store = MarkDoneBrokenStore()
        pool = GatedPool()
        pool.release.set()
        runner = JobRunner()
        upload = make_upload(tmp_path)
        job = submit_queued_job(runner, store, pool, upload)
        task = runner.task_for(job.id)
        assert task is not None
        # Awaiting the task directly re-raises anything that escaped _run.
        await task
        failed = store.jobs[job.id]
        assert failed.status is JobStatus.FAILED
        assert failed.failure_detail == INTERNAL_ERROR_DETAIL
        assert not upload.exists()

    async def test_fully_broken_store_never_raises_out_of_the_task(
        self, tmp_path: Path
    ) -> None:
        """Even the best-effort failure write failing stays contained."""
        store = FullyBrokenStore()
        pool = GatedPool()
        pool.release.set()
        runner = JobRunner()
        upload = make_upload(tmp_path)
        job = submit_queued_job(runner, store, pool, upload)
        task = runner.task_for(job.id)
        assert task is not None
        await task  # must not raise
        assert store.jobs[job.id].status is JobStatus.QUEUED
        assert not upload.exists()


class TestJobRunnerCancellation:
    async def test_cancel_leaves_store_state_alone_and_deletes_upload(
        self, tmp_path: Path
    ) -> None:
        """Shutdown story: cancelled jobs stay running for the #19 sweep."""
        store = FakeJobStore()
        pool = GatedPool()
        runner = JobRunner()
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
        store = FakeJobStore()
        pool = GatedPool()
        runner = JobRunner()
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

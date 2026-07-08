"""Shared in-memory fakes for the job-execution seams.

One definition each of the two fakes multiple suites need — the
``JobStorePort`` fake (protocol-conformance tests in ``domain/test_ports``,
runner lifecycle tests in ``test_runner``, store-swap tests in
``api/test_jobs``) and the gated worker-pool fake (``test_runner``,
``api/test_jobs``) — so the store contract and the parse seam cannot drift
between suites.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bscribe.domain.models import Job, JobStatus, ParsedDocument

if TYPE_CHECKING:
    from pathlib import Path

    from bscribe.domain.models import OcrMode, OutputFormat


@dataclass
class FakeJobStore:
    """In-memory JobStorePort implementation.

    Replicates the store contract: token scoping on reads/deletes,
    compare-and-set transition guards, queued-only ``add``, and the
    metadata/result split (results live beside jobs, reachable only via
    ``get_result``).
    """

    jobs: dict[str, Job] = field(default_factory=dict[str, "Job"])
    results: dict[str, ParsedDocument] = field(
        default_factory=dict[str, "ParsedDocument"]
    )

    def add(self, job: Job) -> None:
        if job.status is not JobStatus.QUEUED:
            # Mirrors the adapter's queued-only contract (see JobStorePort).
            msg = f"job {job.id}: add requires a queued job, got {job.status.value}"
            raise ValueError(msg)
        if job.id in self.jobs:
            # The adapter raises on the duplicate PRIMARY KEY; mirroring
            # that (rather than upserting) keeps the fake honest.
            msg = f"duplicate job id: {job.id}"
            raise ValueError(msg)
        self.jobs[job.id] = job

    def get(self, job_id: str, token_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        return job if job is not None and job.token_id == token_id else None

    def get_result(self, job_id: str, token_id: str) -> ParsedDocument | None:
        job = self.get(job_id, token_id)
        if job is None or job.status is not JobStatus.DONE:
            return None
        result = self.results.get(job_id)
        if result is None:
            # Parity with the adapter's corrupt-done-row guard.
            msg = f"job {job_id}: result missing on done job"
            raise ValueError(msg)
        return result

    def list_for_token(
        self, token_id: str, *, status: JobStatus | None = None
    ) -> list[Job]:
        matches = [
            job
            for job in self.jobs.values()
            if job.token_id == token_id
            # .value on both sides: a raw string passed as status fails
            # here exactly as it would against the SQLite adapter.
            and (status is None or job.status.value == status.value)
        ]
        return sorted(matches, key=lambda j: (j.created_at, j.id), reverse=True)

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

    def sweep_incomplete(self, detail: str) -> int:
        incomplete = [
            job
            for job in self.jobs.values()
            if job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
        ]
        for job in incomplete:
            self.jobs[job.id] = replace(
                job,
                status=JobStatus.FAILED,
                finished_at=datetime.now(tz=UTC),
                failure_detail=detail,
            )
        return len(incomplete)

    def purge_older_than(self, cutoff: datetime) -> int:
        if cutoff.tzinfo is None:
            msg = "cutoff must be timezone-aware"
            raise ValueError(msg)
        stale = [job_id for job_id, job in self.jobs.items() if job.created_at < cutoff]
        for job_id in stale:
            del self.jobs[job_id]
            self.results.pop(job_id, None)
        return len(stale)


class GatedPool:
    """Stands in for WorkerPool — the runner's/endpoints' parse seam.

    ``started``/``release`` make the queued → running → terminal lifecycle
    observable deterministically; ``exc`` drives the failure paths.
    """

    def __init__(
        self,
        *,
        result: ParsedDocument | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._result = result or ParsedDocument(
            content="# Heading", pages=3, duration_ms=41.7
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

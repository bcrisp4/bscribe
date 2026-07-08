"""Background execution of async conversion jobs.

Composition layer between the job store and the worker pool, mirroring the
altitude of :mod:`bscribe.workers`: ``POST /v1/jobs`` submits here after
persisting the job, and each submission becomes one asyncio task that
drives the job through its lifecycle (``mark_running`` → ``pool.parse`` →
``mark_done``/``mark_failed``) and deletes the scratch upload when done.

The job-id → task mapping exists on purpose: cancellation (#18) is
``task_for(job_id).cancel()`` — ``WorkerPool.parse`` translates task
cancellation into killing the running worker process.

Two contracts worth stating:

* **"running" means dispatched to the pool.** ``mark_running`` fires when
  the task starts awaiting ``pool.parse``; beyond ``worker_count``
  concurrent jobs, a job reports ``running`` while still queued inside
  pebble. The design doc defines ``running`` as a lifecycle state with no
  finer precision claim, and pebble exposes no started-hook.
* **Failure details are fixed constants, never ``str(exc)``.** Parser
  exception messages may quote document internals (liteparse's
  ``ParseError``); the sync path scrubs them via the error handlers in
  :mod:`bscribe.errors`, and this module is the async path's equivalent.

Shutdown cancels every in-flight task (killing running workers) but leaves
the jobs ``queued``/``running`` in the store: the startup sweep (#19) marks
them failed on the next boot — a deliberately restart-shaped story
(docs/design.md — Startup sweep).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

import structlog

from bscribe.domain.errors import (
    DocumentUnparseableError,
    JobTimeoutError,
    WorkerCrashedError,
)
from bscribe.errors import TIMEOUT_DETAIL, UNPARSEABLE_DETAIL

if TYPE_CHECKING:
    from pathlib import Path

    from bscribe.domain.models import OcrMode, OutputFormat, ParsedDocument
    from bscribe.domain.ports import JobStorePort

logger = structlog.get_logger()

# Runner-only failure details (the sync path's WorkerCrashedError body is a
# generic 500, not a stored failure reason, so these have no errors.py twin).
WORKER_CRASHED_DETAIL = "worker crashed"
INTERNAL_ERROR_DETAIL = "internal error"


class ParsePool(Protocol):
    """The slice of :class:`bscribe.workers.WorkerPool` the runner needs."""

    async def parse(
        self, path: Path, *, output: OutputFormat, ocr: OcrMode
    ) -> ParsedDocument: ...


class JobRunner:
    """Runs submitted jobs as asyncio tasks on the shared worker pool.

    Owns no pool: the pool is lifespan-owned (and test-swapped) on
    ``app.state``, so the submitting endpoint passes it per call. The
    store, by contrast, is fixed at construction — the runner is built
    beside it at factory time in ``create_app``.
    """

    def __init__(self, *, store: JobStorePort) -> None:
        """Wire the runner to the job store it records transitions in.

        Args:
            store: Persists lifecycle transitions; called via
                ``asyncio.to_thread`` (the port contract keeps SQLite off
                the event loop — docs/adr/0002).
        """
        self._store = store
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def submit(
        self,
        *,
        job_id: str,
        path: Path,
        output: OutputFormat,
        ocr: OcrMode,
        pool: ParsePool,
    ) -> None:
        """Start a background task that runs the job to a terminal state.

        Ownership of the scratch file at ``path`` transfers here: the task
        deletes it on success, failure, and cancellation alike. Must be
        called from the event loop thread.

        Args:
            job_id: Id of an already-persisted ``queued`` job.
            path: Staged upload to parse; deleted when the task finishes.
            output: Requested output format.
            ocr: Requested OCR mode.
            pool: The shared worker pool to parse on.
        """
        task = asyncio.create_task(
            self._run(job_id=job_id, path=path, output=output, ocr=ocr, pool=pool),
            name=f"bscribe-job-{job_id}",
        )
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))

    def task_for(self, job_id: str) -> asyncio.Task[None] | None:
        """Return the live task for a job, if any (cancellation hook, #18).

        Args:
            job_id: The job's id.

        Returns:
            The task while the job is in flight; ``None`` once it finished
            (or was never submitted here — e.g. after a restart).
        """
        return self._tasks.get(job_id)

    async def drain(self) -> None:
        """Wait for every currently tracked task to finish (test hook)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel all in-flight jobs and wait for their tasks. Idempotent.

        Cancellation kills running workers via ``WorkerPool.parse``. Jobs
        stay ``queued``/``running`` in the store on purpose — the startup
        sweep (#19) marks them failed on the next boot (see module
        docstring).
        """
        for task in self._tasks.values():
            task.cancel()
        await self.drain()

    async def _run(
        self,
        *,
        job_id: str,
        path: Path,
        output: OutputFormat,
        ocr: OcrMode,
        pool: ParsePool,
    ) -> None:
        """Drive one job to a terminal state; never lets an exception escape
        (an unretrieved task exception would only surface as loop noise)."""
        try:
            if not await asyncio.to_thread(self._store.mark_running, job_id):
                # Deleted (or otherwise transitioned) between add and task
                # start — nothing to run. The finally still cleans up.
                logger.info("job_skipped", job_id=job_id)
                return
            logger.info("job_running", job_id=job_id)
            # CancelledError is a BaseException, so it propagates past these
            # handlers by design: #18's DELETE and shutdown own the state of
            # cancelled jobs; the finally below still removes the upload.
            try:
                result = await pool.parse(path, output=output, ocr=ocr)
            except DocumentUnparseableError:
                await self._fail(job_id, UNPARSEABLE_DETAIL)
            except JobTimeoutError:
                await self._fail(job_id, TIMEOUT_DETAIL)
            except WorkerCrashedError:
                await self._fail(job_id, WORKER_CRASHED_DETAIL)
            except Exception as exc:
                # Type name only — the message may quote document internals
                # (see module docstring and bscribe.errors).
                logger.error(
                    "job_unexpected_error",
                    job_id=job_id,
                    error_type=type(exc).__name__,
                )
                await self._fail(job_id, INTERNAL_ERROR_DETAIL)
            else:
                if await asyncio.to_thread(self._store.mark_done, job_id, result):
                    logger.info(
                        "job_done",
                        job_id=job_id,
                        pages=result.pages,
                        duration_ms=round(result.duration_ms),
                    )
                else:
                    # Deleted mid-parse (a #18 race): drop the result.
                    logger.info("job_result_discarded", job_id=job_id)
        finally:
            # Documents transit; delete on success and on every failure.
            # Blocking unlink on purpose (same as api.convert): a scratch
            # file unlink is sub-ms, and an await here would need
            # cancellation shielding to run reliably during aclose().
            path.unlink(missing_ok=True)  # noqa: ASYNC240

    async def _fail(self, job_id: str, detail: str) -> None:
        """Record a failure with a fixed, content-free detail string."""
        if await asyncio.to_thread(self._store.mark_failed, job_id, detail):
            logger.info("job_failed", job_id=job_id, detail=detail)

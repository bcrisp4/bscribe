"""Real-pool integration tests for ``bscribe.workers``.

Exercises the actual pebble process pool — real forkserver subprocesses,
real kills, the real pickle round-trip over the pipe — and every failure
mode the design guarantees: timeout SIGKILL, crash containment,
kill-based cancellation, worker recycling. The pool is the system under
test; ``ScriptedParser`` is a real ``ParserPort`` implementation used as
a deterministic *workload* (sleeps, crashes, and error shapes the real
engine cannot produce on demand), injected through the same
``parser_factory`` seam production uses — the engine itself is covered
by the real-liteparse test and ``test_liteparse_adapter.py``. Pools are
kept at one worker with tiny timeouts so the suite stays fast under
xdist.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bscribe.domain import (
    DocumentUnparseableError,
    JobTimeoutError,
    OcrMode,
    OutputFormat,
    ParsedDocument,
    WorkerCrashedError,
)
from bscribe.workers import WorkerPool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

SAMPLE_PDF = Path(__file__).parent / "data" / "sample.pdf"


@dataclass
class ScriptedParser:
    """Worker-side test parser scripted by the *name* of the parsed path.

    ``sleep`` blocks (timeout/cancellation target), ``crash`` hard-exits
    the worker process (segfault stand-in), ``unparseable`` raises the
    domain error with a chained fake-internals cause (privacy probe);
    anything else returns the worker's pid as content. Module-level so
    forkserver children can import it.
    """

    calls: int = 0

    def parse(
        self, path: Path, *, output: OutputFormat, ocr: OcrMode
    ) -> ParsedDocument:
        self.calls += 1
        if path.name == "sleep":
            time.sleep(30)
        if path.name == "crash":
            os._exit(139)
        if path.name == "unparseable":
            cause = ValueError("FAKE-DOCUMENT-INTERNALS")
            raise DocumentUnparseableError("document could not be parsed") from cause
        if path.name == "explode":
            raise ValueError("FAKE-DOCUMENT-INTERNALS")
        return ParsedDocument(content=str(os.getpid()), pages=1, duration_ms=0.0)


def _scripted_pool(
    *,
    worker_count: int = 1,
    job_timeout_seconds: float = 30.0,
    worker_max_tasks: int = 0,
) -> WorkerPool:
    return WorkerPool(
        worker_count=worker_count,
        job_timeout_seconds=job_timeout_seconds,
        worker_max_tasks=worker_max_tasks,
        parser_factory=ScriptedParser,
    )


@pytest.fixture
async def scripted_pool() -> AsyncIterator[WorkerPool]:
    pool = _scripted_pool()
    try:
        yield pool
    finally:
        pool.close()


async def test_parses_sample_pdf_in_worker(tmp_path: Path) -> None:
    """Full round-trip with the real liteparse parser in a real worker."""
    pool = WorkerPool(worker_count=1, job_timeout_seconds=60.0, worker_max_tasks=0)
    try:
        result = await pool.parse(
            SAMPLE_PDF, output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF
        )
    finally:
        pool.close()
    assert result.pages == 1
    assert "Sample PDF" in result.content
    assert result.duration_ms > 0


async def test_document_unparseable_propagates_from_worker(
    scripted_pool: WorkerPool, tmp_path: Path
) -> None:
    """The domain error crosses the pipe; engine internals do not."""
    with pytest.raises(DocumentUnparseableError) as excinfo:
        await scripted_pool.parse(
            tmp_path / "unparseable",
            output=OutputFormat.MARKDOWN,
            ocr=OcrMode.OFF,
        )
    assert str(excinfo.value) == "document could not be parsed"
    # pebble attaches the worker's formatted traceback to re-raised
    # exceptions; the worker boundary must have scrubbed the chained
    # cause so engine messages (which may quote document content) never
    # reach the parent process (see docs/design.md — Privacy).
    exposed = f"{excinfo.value.__cause__}{getattr(excinfo.value, 'traceback', '')}"
    assert "FAKE-DOCUMENT-INTERNALS" not in exposed


async def test_unexpected_worker_error_scrubbed_to_crash(
    scripted_pool: WorkerPool, tmp_path: Path
) -> None:
    """A non-domain exception in the worker surfaces as WorkerCrashedError
    with only the type name — its message (which may quote document
    content) must not reach the parent in any form."""
    with pytest.raises(WorkerCrashedError) as excinfo:
        await scripted_pool.parse(
            tmp_path / "explode", output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF
        )
    assert str(excinfo.value) == "unexpected ValueError in worker"
    exposed = f"{excinfo.value.__cause__}{getattr(excinfo.value, 'traceback', '')}"
    assert "FAKE-DOCUMENT-INTERNALS" not in exposed


async def test_timeout_kills_worker_and_pool_survives(tmp_path: Path) -> None:
    pool = _scripted_pool(job_timeout_seconds=0.5)
    try:
        with pytest.raises(JobTimeoutError):
            await pool.parse(
                tmp_path / "sleep",
                output=OutputFormat.MARKDOWN,
                ocr=OcrMode.OFF,
            )
        assert pool.metrics.timeout_kills == 1
        after = await pool.parse(
            tmp_path / "ok", output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF
        )
        assert after.content  # pool respawned the killed worker
    finally:
        pool.close()


async def test_crash_contained_and_worker_respawned(
    scripted_pool: WorkerPool, tmp_path: Path
) -> None:
    with pytest.raises(WorkerCrashedError):
        await scripted_pool.parse(
            tmp_path / "crash", output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF
        )
    assert scripted_pool.metrics.crashes == 1
    after = await scripted_pool.parse(
        tmp_path / "ok", output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF
    )
    assert after.content  # pool respawned the crashed worker


async def test_cancellation_kills_running_worker(
    scripted_pool: WorkerPool, tmp_path: Path
) -> None:
    task = asyncio.ensure_future(
        scripted_pool.parse(
            tmp_path / "sleep", output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF
        )
    )
    await asyncio.sleep(1.0)  # let the worker actually start the job
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert scripted_pool.metrics.cancellations == 1
    after = await scripted_pool.parse(
        tmp_path / "ok", output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF
    )
    assert after.content  # pool replaced the killed worker


async def test_workers_recycled_after_max_tasks(tmp_path: Path) -> None:
    pool = _scripted_pool(worker_max_tasks=1)
    try:
        first = await pool.parse(
            tmp_path / "ok", output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF
        )
        second = await pool.parse(
            tmp_path / "ok", output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF
        )
    finally:
        pool.close()
    assert first.content != second.content  # different worker pids


async def test_idle_worker_death_rebuilds_pool(
    scripted_pool: WorkerPool, tmp_path: Path
) -> None:
    """A worker dying with no job running breaks the whole pebble pool
    (no auto-recovery); the next parse must rebuild it, not fail forever."""
    first = await scripted_pool.parse(
        tmp_path / "ok", output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF
    )
    os.kill(int(first.content), signal.SIGKILL)
    await asyncio.sleep(1.0)  # let pebble's manager notice and mark it broken
    after = await scripted_pool.parse(
        tmp_path / "ok", output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF
    )
    assert after.content != first.content
    assert scripted_pool.metrics.pool_rebuilds == 1


async def test_close_is_idempotent_and_rejects_further_parses(
    tmp_path: Path,
) -> None:
    pool = _scripted_pool()
    await pool.parse(tmp_path / "ok", output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF)
    pool.close()
    pool.close()
    with pytest.raises(RuntimeError, match="closed"):
        await pool.parse(tmp_path / "ok", output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF)

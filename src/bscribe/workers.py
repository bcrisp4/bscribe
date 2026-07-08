"""Warm process pool that runs all document parsing.

Composition layer between the domain and pebble (see docs/design.md —
Job execution): the FastAPI parent owns HTTP and all state; workers only
parse — file path in, ``ParsedDocument`` out over a pipe. pebble types
never escape this module; failure modes surface as domain exceptions.

Privacy note: pebble captures the worker's formatted traceback and
re-attaches it to re-raised exceptions in the parent (as ``.traceback``
and a ``RemoteTraceback`` ``__cause__``). A traceback formatted inside
the worker would include any raised exception's message and cause chain,
which may quote document internals — so ``_parse_in_worker`` lets no
exception escape the worker unscrubbed: domain errors are re-raised with
the cause chain severed, everything else is replaced by a
``WorkerCrashedError`` carrying only the exception's type name (verified
against pebble 5.2.0).

Concurrency model: ``WorkerPool`` needs no lock. All mutation of shared
state (``_pool``, ``_closed``, ``metrics``) happens on the event loop
thread — ``parse()`` and ``aclose()`` are coroutines, and only the
*blocking* pool teardown is offloaded to a thread, never the state
changes around it. Interleaving is therefore possible only at ``await``
points: the pool swap in the rebuild path happens before its first
``await``, so concurrent parses cannot double-rebuild, and the rebuild
path re-checks ``_closed`` so a close during rebuild cannot resurrect a
pool. The synchronous ``close()`` is for non-async callers (tests);
async code must use ``aclose()``. If any of this moves off the loop
thread, revisit.
"""

from __future__ import annotations

import asyncio
import dataclasses
import multiprocessing
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import structlog
from pebble import ProcessExpired, ProcessPool

from bscribe.adapters.liteparse import LiteparseParser
from bscribe.domain import (
    DocumentUnparseableError,
    JobTimeoutError,
    ParsedDocument,
    WorkerCrashedError,
    traversed_stamp,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pebble import ProcessFuture

    from bscribe.domain import OcrMode, OutputFormat, ParserPort, PipelineStamp

logger = structlog.get_logger()

_worker_parser: ParserPort | None = None
"""The one parser instance of this worker process (set by the initializer)."""


def _initialize_worker(parser_factory: Callable[[], ParserPort]) -> None:
    """Build the worker's parser once, at worker start (pebble initializer)."""
    global _worker_parser  # per-process singleton by design
    _worker_parser = parser_factory()


def _parse_in_worker(path: Path, output: OutputFormat, ocr: OcrMode) -> ParsedDocument:
    """Entry point executed inside a worker process."""
    if _worker_parser is None:
        raise RuntimeError("worker not initialized")
    try:
        return _worker_parser.parse(path, output=output, ocr=ocr)
    except DocumentUnparseableError as exc:
        # Sever the cause chain so the engine exception's message (which
        # may quote document internals) never enters the traceback pebble
        # ships back to the parent — see module docstring.
        raise DocumentUnparseableError(str(exc)) from None
    except Exception as exc:
        # Anything else (engine bug, native panic surfacing as a Python
        # exception) may quote document internals in its message. Replace
        # it entirely; only the type name crosses the pipe.
        raise WorkerCrashedError(f"unexpected {type(exc).__name__} in worker") from None


def _teardown_pool(pool: ProcessPool) -> None:
    """Stop a pebble pool and reap its workers, channels, and threads.

    Blocking (SIGTERM → 3s grace → SIGKILL per running worker) — call it
    off the event loop via ``asyncio.to_thread``.
    """
    pool.stop()  # type: ignore[no-untyped-call]  # unannotated in pebble
    pool.join()


@dataclass(slots=True)
class WorkerPoolMetrics:
    """Counters for pool failure events; exposed as Prometheus metrics in M3.

    Worker recycles are not counted: pebble recycles internally with no
    parent-side hook, so an honest count must be derived later (e.g. from
    worker-pid churn) — recorded on issue #12.
    """

    timeout_kills: int = 0
    crashes: int = 0
    cancellations: int = 0
    pool_rebuilds: int = 0


class WorkerPool:
    """Warm pool of disposable parse workers with kill-based failure handling.

    All parsing — sync and async endpoints alike — goes through one
    instance, so ``worker_count`` bounds total parse concurrency. pebble
    spawns workers lazily on the first scheduled job (verified 5.2.0), so
    construction is cheap.

    Pipeline stamping (docs/design.md — Re-ingestion contract) happens
    parent-side, in :meth:`parse`, not inside the worker process: workers
    return an unstamped ``ParsedDocument`` and this class fills in
    ``pipeline`` from the app-wide :class:`~bscribe.domain.models.PipelineStamp`
    fixed at construction time, filtered to what the one document parsed
    actually traversed.
    """

    def __init__(
        self,
        *,
        worker_count: int,
        job_timeout_seconds: float,
        worker_max_tasks: int,
        pipeline_info: PipelineStamp,
        parser_factory: Callable[[], ParserPort] = LiteparseParser,
    ) -> None:
        self._worker_count = worker_count
        self._job_timeout_seconds = job_timeout_seconds
        self._worker_max_tasks = worker_max_tasks
        self._pipeline_info = pipeline_info
        self._parser_factory = parser_factory
        self._closed = False
        self.metrics = WorkerPoolMetrics()
        self._pool = self._create_pool()

    def _create_pool(self) -> ProcessPool:
        # forkserver everywhere: the Linux 3.14 default (fork from a
        # threaded parent is unsafe), cheaper worker respawn than spawn,
        # and dev-macOS/prod-Linux parity instead of silent divergence.
        return ProcessPool(
            max_workers=self._worker_count,
            max_tasks=self._worker_max_tasks,
            initializer=_initialize_worker,
            initargs=[self._parser_factory],
            context=multiprocessing.get_context("forkserver"),
        )

    async def parse(
        self, path: Path, *, output: OutputFormat, ocr: OcrMode
    ) -> ParsedDocument:
        """Parse ``path`` in a worker process (async twin of ``ParserPort``).

        On success, stamps the result's ``pipeline`` field parent-side —
        the worker returns a document with ``pipeline=None`` (see
        ``_parse_in_worker``); this method fills it in from
        ``self._pipeline_info`` filtered to ``path``/``ocr``'s traversal
        (:func:`~bscribe.domain.pipeline.traversed_stamp`). ``path.suffix``
        is trustworthy here even though it is derived from an upload:
        staged uploads are written under a random name with the original
        extension preserved (see ``bscribe.api.staging.stage_upload``), and
        extension is what liteparse itself dispatches on, so it cannot
        under- or over-report what the parse actually traversed.

        Raises:
            DocumentUnparseableError: The engine rejected the document.
            JobTimeoutError: The per-job deadline expired; the worker was
                killed and respawned.
            WorkerCrashedError: The worker process died mid-parse, raised
                an unexpected error, or the pool itself broke; the pool is
                respawned or rebuilt.
            asyncio.CancelledError: The awaiting task was cancelled; a
                running worker is killed (real cancellation), a job still
                queued inside the pool is dropped without running.
        """
        if self._closed:
            raise RuntimeError("worker pool is closed")
        try:
            future = self._schedule(path, output, ocr)
        except RuntimeError:
            # A worker dying while no job is running (OOM kill of a warm
            # worker, initializer crash) marks the whole pebble pool broken
            # with no auto-recovery. Rebuild it once rather than fail every
            # job from here on with /healthz still green. No cross-request
            # backoff: a persistently failing environment costs one pool
            # spawn per request, which the log event and counter surface.
            if self._closed:
                # The RuntimeError came from close() tearing the pool down,
                # not from a broken pool — never rebuild after close.
                raise RuntimeError("worker pool is closed") from None
            self.metrics.pool_rebuilds += 1
            logger.error("worker_pool_rebuilt")
            broken = self._pool
            self._pool = self._create_pool()
            await asyncio.to_thread(_teardown_pool, broken)
            try:
                future = self._schedule(path, output, ocr)
            except RuntimeError as retry_exc:
                # Rebuilt pool is unusable too (or close() ran while we
                # were tearing down the broken one). Do not rebuild again.
                raise WorkerCrashedError("worker pool unavailable") from retry_exc
        try:
            result = cast("ParsedDocument", await asyncio.wrap_future(future))
        except asyncio.CancelledError:
            # asyncio.wrap_future already chains cancellation to the pebble
            # future; calling cancel() again is an idempotent way to learn
            # whether the cancellation took effect — False only when the job
            # had already finished. True covers both a killed running worker
            # and a pool-queued job dropped before dispatch, so the metric
            # counts effective cancellations, not just kills. (pebble leaves
            # ProcessFuture.cancel unannotated, hence the mypy ignore.)
            if future.cancel():  # type: ignore[no-untyped-call]
                self.metrics.cancellations += 1
                logger.warning("job_cancelled")
            raise
        except TimeoutError as exc:
            self.metrics.timeout_kills += 1
            logger.warning("job_timeout", timeout_seconds=self._job_timeout_seconds)
            raise JobTimeoutError("job timed out") from exc
        except ProcessExpired as exc:
            self.metrics.crashes += 1
            logger.error("worker_crashed", exit_code=exc.exitcode)
            raise WorkerCrashedError("worker process crashed") from exc
        except RuntimeError as exc:
            # pebble sets BrokenProcessPool (a RuntimeError) on the in-flight
            # future when a worker dies before acknowledging the job. The
            # pool is broken; the next parse rebuilds it.
            self.metrics.crashes += 1
            logger.error("worker_pool_broken", error_type=type(exc).__name__)
            raise WorkerCrashedError("worker pool broken") from exc
        return dataclasses.replace(
            result,
            pipeline=traversed_stamp(
                self._pipeline_info, extension=path.suffix, ocr=ocr
            ),
        )

    def _schedule(
        self, path: Path, output: OutputFormat, ocr: OcrMode
    ) -> ProcessFuture:
        # pebble types schedule()'s function param as a bare Callable, so
        # the member is partially unknown under pyright strict.
        return self._pool.schedule(  # pyright: ignore[reportUnknownMemberType]
            _parse_in_worker,
            args=[path, output, ocr],
            timeout=self._job_timeout_seconds,
        )

    async def aclose(self) -> None:
        """Stop the pool, killing any running workers. Idempotent.

        Flips ``_closed`` on the event loop thread (so in-flight parses
        observe it reliably — see module docstring), then runs the
        blocking teardown off-loop.
        """
        self._closed = True
        await asyncio.to_thread(_teardown_pool, self._pool)

    def close(self) -> None:
        """Synchronous ``aclose`` for non-async callers. Idempotent.

        Blocks the calling thread (up to ~3s per wedged worker); async
        code must use ``aclose`` instead.
        """
        self._closed = True
        _teardown_pool(self._pool)

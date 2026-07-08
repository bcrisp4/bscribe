"""bscribe FastAPI application factory."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI, Request, Response

from bscribe import maintenance
from bscribe.adapters.sqlite import SqliteJobStore, SqliteTokenStore
from bscribe.api import v1_router
from bscribe.errors import (
    UPLOAD_TOO_LARGE_DETAIL,
    problem_response,
    register_error_handlers,
)
from bscribe.log import configure_logging
from bscribe.runner import JobRunner
from bscribe.settings import Settings
from bscribe.workers import WorkerPool

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

logger = structlog.get_logger()

# Slack above max_upload_bytes for the Content-Length prefilter: the
# multipart envelope (boundaries + part headers) inflates the body past the
# file size, so a bare `> max` threshold would false-reject a legitimate
# max-size upload. The streaming counter in spool_upload is the authoritative
# limit; this prefilter only rejects egregious bodies before receipt.
MULTIPART_OVERHEAD_SLACK = 1 << 20


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the bscribe FastAPI application.

    Args:
        settings: Application configuration; ``None`` loads it from
            ``BSCRIBE_``-prefixed environment variables.

    Returns:
        A configured FastAPI instance exposing ``/healthz`` and the
        path-versioned ``/v1`` API (``POST /v1/convert`` plus the async
        ``/v1/jobs`` endpoints).
    """
    if settings is None:
        settings = Settings()

    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Own the startup sweep, the worker pool, and the periodic purge
        task.

        The store is read from ``app.state`` here — not captured at
        factory time — so a store swapped in after ``create_app`` (as
        tests do) is the one actually swept and purged; see the
        factory-time comments below on why ``job_store`` lives on
        ``app.state`` at all. The sweep runs before the pool exists and
        before any request is served, so it can never race a freshly
        submitted job (docs/design.md — Startup sweep). pebble spawns
        workers lazily on the first job, so pool startup stays cheap;
        shutdown kills any running workers and stops the purge task.
        """
        store = app.state.job_store
        await asyncio.to_thread(maintenance.startup_sweep, store, settings.scratch_dir)
        pool = WorkerPool(
            worker_count=settings.worker_count,
            job_timeout_seconds=float(settings.job_timeout_seconds),
            worker_max_tasks=settings.worker_max_tasks,
        )
        app.state.worker_pool = pool
        app.state.purge_task = asyncio.create_task(
            maintenance.purge_loop(
                store,
                ttl_seconds=settings.result_ttl_seconds,
                interval_seconds=settings.purge_interval_seconds,
            ),
            name="bscribe-job-purge",
        )
        try:
            yield
        finally:
            # Purge task first: cancel it before tearing down the runner
            # and pool so it never touches either mid-shutdown.
            app.state.purge_task.cancel()
            await asyncio.gather(app.state.purge_task, return_exceptions=True)
            # Runner next: cancelling its tasks kills their running
            # workers via the still-live pool, then the pool tears down.
            await app.state.job_runner.aclose()
            await pool.aclose()

    app = FastAPI(title="bscribe", lifespan=lifespan)
    app.state.settings = settings
    # Factory-time (not lifespan): construction is cheap, creates the schema
    # if missing, and tests can swap in a fake before serving a request.
    # Auth reads it per request via bscribe.auth.require_token.
    app.state.token_store = SqliteTokenStore(settings.db_path)
    # Same rationale; the job endpoints read it per request.
    app.state.job_store = SqliteJobStore(settings.db_path)
    # Factory-time too (the runner is loop-agnostic until its first submit),
    # so ASGITransport tests — which never run the lifespan — get a working
    # runner. It deliberately holds no store or pool: the submitting
    # endpoint passes the pair it resolved per request, so swapping either
    # on app.state can never split writes between two stores.
    app.state.job_runner = JobRunner()
    # Ensure the upload scratch dir exists once here rather than on every
    # request. This mkdir stays for lifespan-less ASGITransport tests,
    # which never run the lifespan below; the lifespan's startup_sweep
    # (docs/design.md — Startup sweep) owns wiping it clean at boot.
    settings.scratch_dir.mkdir(parents=True, exist_ok=True)
    register_error_handlers(app)

    max_body_bytes = settings.max_upload_bytes + MULTIPART_OVERHEAD_SLACK

    @app.middleware("http")
    async def reject_oversized_body(  # pyright: ignore[reportUnusedFunction]
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Pre-receipt 413 when an *honest* Content-Length exceeds the cap.

        This runs before routing and therefore before auth, so an oversized
        unauthenticated upload is rejected here (413) rather than reaching the
        401 check — a deliberate don't-buffer-a-huge-body tradeoff. It only
        catches requests that declare an honest, oversized Content-Length;
        chunked or absent/spoofed headers slip past and are bounded (best
        effort at single-user scale) by spool_upload's copy-time counter (see
        docs/design.md — max upload size)."""
        declared = request.headers.get("content-length")
        too_big = (
            declared is not None
            and declared.isdigit()
            and int(declared) > max_body_bytes
        )
        if too_big:
            return problem_response(status=413, detail=UPLOAD_TOO_LARGE_DETAIL)
        return await call_next(request)

    # pyright strict flags decorator-registered nested handlers as unused
    # (reportUnusedFunction); the route registration is the real use.
    @app.middleware("http")
    async def access_log(  # pyright: ignore[reportUnusedFunction]
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """One INFO line per request. Path only — query strings may carry
        sensitive values (see docs/design.md — Privacy)."""
        start = time.perf_counter()
        response = await call_next(request)
        logger.info(
            "request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 3),
        )
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Liveness probe. No auth; safe for orchestrator health checks."""
        return {"status": "ok"}

    app.include_router(v1_router)

    return app

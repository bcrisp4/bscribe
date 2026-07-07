"""bscribe FastAPI application factory."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI, Request, Response

from bscribe.adapters.sqlite import SqliteTokenStore
from bscribe.api import v1_router
from bscribe.errors import problem_response, register_error_handlers
from bscribe.log import configure_logging
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
        path-versioned ``/v1`` API (``POST /v1/convert``; async job
        endpoints arrive in M2).
    """
    if settings is None:
        settings = Settings()

    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Own the worker pool's lifetime. pebble spawns workers lazily on
        the first job, so startup stays cheap; shutdown kills any running
        workers (abandoned jobs are the restart story — see docs/design.md,
        Startup sweep)."""
        pool = WorkerPool(
            worker_count=settings.worker_count,
            job_timeout_seconds=float(settings.job_timeout_seconds),
            worker_max_tasks=settings.worker_max_tasks,
        )
        app.state.worker_pool = pool
        try:
            yield
        finally:
            await pool.aclose()

    app = FastAPI(title="bscribe", lifespan=lifespan)
    app.state.settings = settings
    # Factory-time (not lifespan): construction is cheap, creates the schema
    # if missing, and tests can swap in a fake before serving a request.
    # Auth reads it per request via bscribe.auth.require_token.
    app.state.token_store = SqliteTokenStore(settings.db_path)
    register_error_handlers(app)

    max_body_bytes = settings.max_upload_bytes + MULTIPART_OVERHEAD_SLACK

    @app.middleware("http")
    async def reject_oversized_body(  # pyright: ignore[reportUnusedFunction]
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Advisory pre-receipt guard: 413 when the declared body is huge.

        Content-Length is absent or spoofable, so this only short-circuits
        obviously-oversized uploads; spool_upload's streaming counter is the
        authoritative size limit (see docs/design.md — max upload size)."""
        declared = request.headers.get("content-length")
        too_big = (
            declared is not None
            and declared.isdigit()
            and int(declared) > max_body_bytes
        )
        if too_big:
            return problem_response(status=413, detail="upload exceeds maximum size")
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

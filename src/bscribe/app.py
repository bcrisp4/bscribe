"""bscribe FastAPI application factory."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI, Request, Response

from bscribe.errors import register_error_handlers
from bscribe.log import configure_logging
from bscribe.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = structlog.get_logger()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the bscribe FastAPI application.

    Args:
        settings: Application configuration; ``None`` loads it from
            ``BSCRIBE_``-prefixed environment variables.

    Returns:
        A configured FastAPI instance. In this bootstrap it exposes only the
        ``/healthz`` liveness probe; conversion endpoints arrive in M1.
    """
    if settings is None:
        settings = Settings()

    configure_logging(settings.log_level)

    app = FastAPI(title="bscribe")
    app.state.settings = settings
    register_error_handlers(app)

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

    return app

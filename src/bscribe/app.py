"""bscribe FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build and return the bscribe FastAPI application.

    Returns:
        A configured FastAPI instance. In this bootstrap it exposes only the
        ``/healthz`` liveness probe; conversion endpoints arrive in M1.
    """
    app = FastAPI(title="bscribe")

    # pyright strict flags decorator-registered nested handlers as unused
    # (reportUnusedFunction); the route registration is the real use.
    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Liveness probe. No auth; safe for orchestrator health checks."""
        return {"status": "ok"}

    return app

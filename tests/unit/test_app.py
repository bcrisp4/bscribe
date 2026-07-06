"""Tests for the bscribe application factory."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest
import structlog
from httpx import ASGITransport

from bscribe.app import create_app
from bscribe.settings import Settings
from bscribe.workers import WorkerPool

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """create_app configures process-global structlog; undo it per test."""
    yield
    structlog.reset_defaults()


def make_client(app: object | None = None) -> httpx.AsyncClient:
    transport = ASGITransport(app=app if app is not None else create_app())  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_healthz_returns_ok() -> None:
    """The liveness probe returns 200 with a stable body."""
    async with make_client() as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_explicit_settings_exposed_on_app_state() -> None:
    settings = Settings(worker_count=2)

    app = create_app(settings=settings)

    assert app.state.settings is settings


async def test_default_settings_built_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BSCRIBE_WORKER_COUNT", "7")

    app = create_app()

    assert app.state.settings.worker_count == 7


async def test_unknown_path_returns_problem_json() -> None:
    """Error handlers are wired: 404 comes back as application/problem+json."""
    async with make_client() as client:
        response = await client.get("/nope")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_request_emits_one_access_log_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app()
    async with make_client(app) as client:
        await client.get("/healthz")

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    events = [json.loads(line) for line in lines]
    access = [event for event in events if event["event"] == "request"]
    assert len(access) == 1
    entry = access[0]
    assert entry["method"] == "GET"
    assert entry["path"] == "/healthz"
    assert entry["status_code"] == 200
    assert entry["duration_ms"] >= 0
    assert entry["level"] == "info"


async def test_access_log_excludes_query_string(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Query params may carry sensitive values; only the path is logged."""
    app = create_app()
    async with make_client(app) as client:
        await client.get("/healthz", params={"filename": "medical-letter.pdf"})

    assert "medical-letter" not in capsys.readouterr().out


async def test_lifespan_creates_and_closes_worker_pool() -> None:
    """Startup builds the pool from settings; shutdown closes it."""
    app = create_app(Settings())
    async with app.router.lifespan_context(app):
        pool = app.state.worker_pool
        assert isinstance(pool, WorkerPool)
    # close() is idempotent, so closing again after shutdown must not raise.
    pool.close()

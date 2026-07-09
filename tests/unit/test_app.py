"""Tests for the bscribe application factory."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx
import pytest
import structlog
from httpx import ASGITransport

from bscribe.adapters.sqlite import SqliteJobStore
from bscribe.app import create_app
from bscribe.domain.jobs import create_job
from bscribe.domain.models import JobStatus, OcrMode, OutputFormat
from bscribe.domain.ports import JobStorePort
from bscribe.errors import INTERRUPTED_BY_RESTART_DETAIL
from bscribe.settings import Settings
from bscribe.workers import WorkerPool
from tests.unit.fakes import CANNED_PIPELINE_STAMP, FakeJobStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


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


async def test_job_store_built_at_factory_time() -> None:
    """The job store is on app.state before any request is served."""
    app = create_app()

    assert isinstance(app.state.job_store, SqliteJobStore)
    assert isinstance(app.state.job_store, JobStorePort)


async def test_explicit_pipeline_info_exposed_on_app_state() -> None:
    """A caller-supplied stamp lands on state verbatim; discovery never runs."""
    app = create_app(pipeline_info=CANNED_PIPELINE_STAMP)

    assert app.state.pipeline_info is CANNED_PIPELINE_STAMP


async def test_default_pipeline_info_runs_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pipeline_info=None`` calls real discovery and lands its result on
    state — the autouse fixture in ``tests/unit/conftest.py`` patches this
    same target for every *other* test, so this test overrides it back to
    a locally-scoped fake to assert the call actually happens."""
    calls: list[None] = []

    def _fake_discover() -> object:
        calls.append(None)
        return CANNED_PIPELINE_STAMP

    monkeypatch.setattr("bscribe.app.discover_pipeline", _fake_discover)

    app = create_app()

    assert len(calls) == 1
    assert app.state.pipeline_info is CANNED_PIPELINE_STAMP


async def test_logging_configured_before_pipeline_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery probes may warn on failure; those warnings must go through
    structlog only after it's configured, or they'd emit unstructured."""
    call_order: list[str] = []

    def _fake_configure_logging(_level: str) -> None:
        call_order.append("configure_logging")

    def _fake_discover() -> object:
        call_order.append("discover_pipeline")
        return CANNED_PIPELINE_STAMP

    monkeypatch.setattr("bscribe.app.configure_logging", _fake_configure_logging)
    monkeypatch.setattr("bscribe.app.discover_pipeline", _fake_discover)

    create_app(pipeline_info=None)

    assert call_order == ["configure_logging", "discover_pipeline"]


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


async def test_pipeline_discovered_logged_at_factory_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One INFO line names the fingerprint and components — privacy-safe
    (version strings only, see docs/design.md — Privacy)."""
    create_app(pipeline_info=CANNED_PIPELINE_STAMP)

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    events = [json.loads(line) for line in lines]
    discovered = [event for event in events if event["event"] == "pipeline_discovered"]
    assert len(discovered) == 1
    assert discovered[0]["fingerprint"] == CANNED_PIPELINE_STAMP.fingerprint
    assert discovered[0]["components"] == dict(CANNED_PIPELINE_STAMP.components)
    assert discovered[0]["level"] == "info"


async def test_lifespan_creates_and_closes_worker_pool() -> None:
    """Startup builds the pool from settings; shutdown closes it."""
    app = create_app(Settings())
    async with app.router.lifespan_context(app):
        pool = app.state.worker_pool
        assert isinstance(pool, WorkerPool)
    # close() is idempotent, so closing again after shutdown must not raise.
    pool.close()


async def test_lifespan_sweeps_yield_time_store(tmp_path: Path) -> None:
    """The lifespan must read app.state.job_store when it runs, not the
    store that existed at create_app time — tests (and this one) swap it
    afterwards, and the sweep has to honor whichever store is live at boot.
    Swapping in a fake here, rather than asserting against the factory's
    real SqliteJobStore, is what actually exercises that contract."""
    app = create_app(
        Settings(db_path=tmp_path / "bscribe.db", scratch_dir=tmp_path / "scratch")
    )
    store = FakeJobStore()
    job = create_job(
        token_id="feed0001", output=OutputFormat.MARKDOWN, ocr=OcrMode.AUTO
    )
    store.add(job)
    store.mark_running(job.id)
    app.state.job_store = store
    stray = tmp_path / "scratch" / "stray.pdf"
    stray.write_bytes(b"leftover")

    async with app.router.lifespan_context(app):
        assert isinstance(app.state.worker_pool, WorkerPool)

    swept = store.get(job.id, job.token_id)
    assert swept is not None
    assert swept.status is JobStatus.FAILED
    assert swept.failure_detail == INTERRUPTED_BY_RESTART_DETAIL
    assert not stray.exists()
    assert (tmp_path / "scratch").exists()


async def test_lifespan_purge_task_runs_until_shutdown() -> None:
    """The periodic purge task is created at startup and cancelled cleanly
    at shutdown, alongside the worker pool."""
    app = create_app(Settings())
    async with app.router.lifespan_context(app):
        task = app.state.purge_task
        assert isinstance(task, asyncio.Task)
        assert not task.done()

    assert task.done()

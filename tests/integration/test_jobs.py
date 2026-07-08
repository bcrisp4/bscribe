"""End-to-end integration tests for the ``/v1/jobs`` endpoints.

Drives the real HTTP stack, the factory-wired ``JobRunner``, the real
``SqliteJobStore``, and a real ``WorkerPool`` (forkserver subprocess, real
liteparse engine) — the full submit → queue → parse → poll → result path
with no mocks. The ASGITransport does not run the app's lifespan, so the
pool is constructed here and torn down in ``finally``, mirroring
``test_convert.py``; ``JobRunner.drain()`` stands in for polling.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import structlog
from httpx import ASGITransport

from bscribe.app import create_app
from bscribe.domain.tokens import mint_token
from bscribe.errors import JOB_FAILED_NO_RESULT_DETAIL, UNPARSEABLE_DETAIL
from bscribe.settings import Settings
from bscribe.workers import WorkerPool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from fastapi import FastAPI

SAMPLE_PDF = Path(__file__).parent / "data" / "sample.pdf"


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    yield
    structlog.reset_defaults()


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    """Real app + real single-worker pool, torn down like the lifespan."""
    application = create_app(
        Settings(db_path=tmp_path / "bscribe.db", scratch_dir=tmp_path / "scratch")
    )
    pool = WorkerPool(worker_count=1, job_timeout_seconds=60.0, worker_max_tasks=0)
    application.state.worker_pool = pool
    try:
        yield application
    finally:
        await application.state.job_runner.aclose()
        await pool.aclose()


async def test_submit_poll_fetch_result_end_to_end(
    app: FastAPI, tmp_path: Path
) -> None:
    token, secret = mint_token("bsearch")
    app.state.token_store.add(token)
    headers = {"Authorization": f"Bearer {secret}"}

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        submitted = await client.post(
            "/v1/jobs",
            files={"file": ("sample.pdf", SAMPLE_PDF.read_bytes(), "application/pdf")},
            data={"output": "markdown", "ocr": "off"},
            headers=headers,
        )
        assert submitted.status_code == 201
        job_id = submitted.json()["id"]
        assert submitted.json()["status"] == "queued"

        await app.state.job_runner.drain()

        status = await client.get(f"/v1/jobs/{job_id}", headers=headers)
        assert status.status_code == 200
        assert status.json()["status"] == "done"

        listed = await client.get("/v1/jobs", headers=headers)
        assert [job["id"] for job in listed.json()["jobs"]] == [job_id]

        result = await client.get(f"/v1/jobs/{job_id}/result", headers=headers)

    assert result.status_code == 200
    body = result.json()
    assert body["output"] == "markdown"
    assert "Sample PDF" in body["content"]
    assert body["metadata"]["pages"] == 1
    assert body["metadata"]["duration_ms"] >= 0
    # Upload deleted as soon as parsing finished.
    assert list((tmp_path / "scratch").iterdir()) == []


async def test_delete_purges_job_end_to_end(app: FastAPI, tmp_path: Path) -> None:
    """DELETE right after submission returns 204 whatever state the job
    reached (queued/running/done — the contract covers any state) and the
    job is gone: 404 on GET, empty listing, empty scratch dir."""
    token, secret = mint_token("bsearch")
    app.state.token_store.add(token)
    headers = {"Authorization": f"Bearer {secret}"}

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        submitted = await client.post(
            "/v1/jobs",
            files={"file": ("sample.pdf", SAMPLE_PDF.read_bytes(), "application/pdf")},
            headers=headers,
        )
        assert submitted.status_code == 201
        job_id = submitted.json()["id"]

        deleted = await client.delete(f"/v1/jobs/{job_id}", headers=headers)
        assert deleted.status_code == 204

        await app.state.job_runner.drain()

        status = await client.get(f"/v1/jobs/{job_id}", headers=headers)
        assert status.status_code == 404

        listed = await client.get("/v1/jobs", headers=headers)
        assert listed.json() == {"jobs": []}

    assert list((tmp_path / "scratch").iterdir()) == []


async def test_unparseable_document_becomes_failed_job(
    app: FastAPI, tmp_path: Path
) -> None:
    token, secret = mint_token("bsearch")
    app.state.token_store.add(token)
    headers = {"Authorization": f"Bearer {secret}"}

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        submitted = await client.post(
            "/v1/jobs",
            files={
                "file": ("broken.pdf", b"%PDF-1.4 not a real pdf", "application/pdf")
            },
            headers=headers,
        )
        assert submitted.status_code == 201
        job_id = submitted.json()["id"]

        await app.state.job_runner.drain()

        status = await client.get(f"/v1/jobs/{job_id}", headers=headers)
        assert status.json()["status"] == "failed"
        assert status.json()["failure_detail"] == UNPARSEABLE_DETAIL

        result = await client.get(f"/v1/jobs/{job_id}/result", headers=headers)

    assert result.status_code == 409
    assert result.json()["detail"] == JOB_FAILED_NO_RESULT_DETAIL
    assert list((tmp_path / "scratch").iterdir()) == []

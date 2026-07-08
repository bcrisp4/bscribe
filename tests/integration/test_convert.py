"""End-to-end integration test for ``POST /v1/convert``.

Drives the real HTTP stack against a real ``WorkerPool`` (forkserver
subprocess, real liteparse engine, real pickle round-trip) — the full
upload → scratch → parse → inline-result path with no mocks. The
ASGITransport does not run the app's lifespan, so the pool is constructed
here and torn down in ``finally``, mirroring ``tests/integration/
test_workers.py``.
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
from bscribe.pipeline import discover_pipeline
from bscribe.settings import Settings
from bscribe.workers import WorkerPool

if TYPE_CHECKING:
    from collections.abc import Iterator

SAMPLE_PDF = Path(__file__).parent / "data" / "sample.pdf"


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    yield
    structlog.reset_defaults()


async def test_convert_sample_pdf_end_to_end(tmp_path: Path) -> None:
    app = create_app(
        Settings(db_path=tmp_path / "tokens.db", scratch_dir=tmp_path / "scratch")
    )
    pool = WorkerPool(
        worker_count=1,
        job_timeout_seconds=60.0,
        worker_max_tasks=0,
        pipeline_info=discover_pipeline(),
    )
    app.state.worker_pool = pool
    token, secret = mint_token("bsearch")
    app.state.token_store.add(token)

    transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/convert",
                files={
                    "file": ("sample.pdf", SAMPLE_PDF.read_bytes(), "application/pdf")
                },
                data={"output": "markdown", "ocr": "off"},
                headers={"Authorization": f"Bearer {secret}"},
            )
    finally:
        pool.close()

    assert response.status_code == 200
    body = response.json()
    assert body["output"] == "markdown"
    assert "Sample PDF" in body["content"]
    assert body["metadata"]["pages"] == 1
    assert body["metadata"]["duration_ms"] >= 0
    # Upload deleted as soon as parsing finished.
    assert list((tmp_path / "scratch").iterdir()) == []

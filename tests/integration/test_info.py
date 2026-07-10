"""End-to-end integration test for ``GET /v1/info`` and the result
``pipeline`` block.

Drives the real HTTP stack against a real ``WorkerPool`` and real component
discovery (no mocks). Asserts the two surfaces agree: ``/v1/info`` reports
every component under the current fingerprint, and a converted document's
``metadata.pipeline`` carries the *same* fingerprint over the subset of
components that document actually traversed — the relationship bsearch
connectors rely on (docs/design.md — Re-ingestion contract).

The ASGITransport does not run the app's lifespan, so the pool is built here
and torn down in ``finally``, mirroring ``tests/integration/test_convert.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import structlog
from httpx import ASGITransport

from bscribe.app import create_app
from bscribe.domain.models import Component
from bscribe.domain.tokens import mint_token
from bscribe.pipeline import discover_pipeline
from bscribe.settings import Settings
from bscribe.workers import WorkerPool

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI

SAMPLE_PDF = Path(__file__).parent / "data" / "sample.pdf"


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    yield
    structlog.reset_defaults()


def _make_app(tmp_path: Path) -> tuple[FastAPI, WorkerPool, str]:
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
    return app, pool, secret


async def test_info_reports_all_nine_components(tmp_path: Path) -> None:
    app, pool, secret = _make_app(tmp_path)
    transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get(
                "/v1/info", headers={"Authorization": f"Bearer {secret}"}
            )
    finally:
        pool.close()

    assert response.status_code == 200
    body = response.json()
    assert set(body["components"]) == {c.value for c in Component}
    assert len(body["fingerprint"]) == 12


async def test_result_pipeline_is_traversed_subset_with_matching_fingerprint(
    tmp_path: Path,
) -> None:
    app, pool, secret = _make_app(tmp_path)
    transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            info = await client.get(
                "/v1/info", headers={"Authorization": f"Bearer {secret}"}
            )
            convert = await client.post(
                "/v1/convert",
                files={
                    "file": ("sample.pdf", SAMPLE_PDF.read_bytes(), "application/pdf")
                },
                data={"output": "markdown", "ocr": "off"},
                headers={"Authorization": f"Bearer {secret}"},
            )
    finally:
        pool.close()

    assert info.status_code == 200
    assert convert.status_code == 200
    info_block = info.json()
    result_block = convert.json()["metadata"]["pipeline"]

    # Same fingerprint on both surfaces — a hash over every component, not the
    # traversed subset — so callers compare a stored block against /v1/info.
    assert result_block["fingerprint"] == info_block["fingerprint"]

    # A born-digital PDF parsed with ocr=off traverses exactly bscribe +
    # liteparse + PDFium: no office/image/OCR components on its path.
    assert set(result_block["components"]) == {
        Component.BSCRIBE.value,
        Component.LITEPARSE.value,
        Component.PDFIUM.value,
    }
    # The traversed subset carries the same versions the global block reports.
    for key, version in result_block["components"].items():
        assert version == info_block["components"][key]

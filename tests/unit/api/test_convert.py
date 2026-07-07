"""Tests for the POST /v1/convert endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
import structlog
from httpx import ASGITransport

from bscribe.app import MULTIPART_OVERHEAD_SLACK, create_app
from bscribe.domain.errors import (
    DocumentUnparseableError,
    JobTimeoutError,
    WorkerCrashedError,
)
from bscribe.domain.models import OcrMode, OutputFormat, ParsedDocument
from bscribe.domain.tokens import mint_token
from bscribe.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi import FastAPI


PDF_BYTES = b"%PDF-1.4 fake body"


@dataclass
class _ParseCall:
    path: Path
    output: OutputFormat
    ocr: OcrMode


class FakePool:
    """Stands in for WorkerPool on app.state — the endpoint's parse seam.

    Records each call and either returns a canned result or raises a
    supplied exception, so every HTTP status can be driven without the real
    process pool (which the ASGITransport lifespan never starts anyway).
    """

    def __init__(
        self,
        *,
        result: ParsedDocument | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._result = result or ParsedDocument(
            content="# Heading", pages=3, duration_ms=41.7
        )
        self._exc = exc
        self.calls: list[_ParseCall] = []

    async def parse(
        self, path: Path, *, output: OutputFormat, ocr: OcrMode
    ) -> ParsedDocument:
        self.calls.append(_ParseCall(path=path, output=output, ocr=ocr))
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    yield
    structlog.reset_defaults()


def make_app(
    tmp_path: Path, *, pool: FakePool | None = None, **settings_overrides: object
) -> tuple[FastAPI, FakePool]:
    settings = Settings(
        db_path=tmp_path / "tokens.db",
        scratch_dir=tmp_path / "scratch",
        **settings_overrides,  # type: ignore[arg-type]
    )
    app = create_app(settings)
    pool = pool or FakePool()
    app.state.worker_pool = pool
    return app, pool


def make_client(app: FastAPI) -> httpx.AsyncClient:
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def issue_token(app: FastAPI, label: str = "bsearch") -> str:
    token, secret = mint_token(label)
    app.state.token_store.add(token)
    return secret


def scratch_files(tmp_path: Path) -> list[Path]:
    scratch = tmp_path / "scratch"
    return list(scratch.iterdir()) if scratch.exists() else []


class TestConvertSuccess:
    async def test_returns_content_and_metadata(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        secret = issue_token(app)
        async with make_client(app) as client:
            response = await client.post(
                "/v1/convert",
                files={"file": ("sample.pdf", PDF_BYTES, "application/pdf")},
                data={"output": "markdown"},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 200
        assert response.json() == {
            "output": "markdown",
            "content": "# Heading",
            "metadata": {"pages": 3, "duration_ms": 42},
        }

    async def test_scratch_dir_empty_after_success(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        secret = issue_token(app)
        async with make_client(app) as client:
            await client.post(
                "/v1/convert",
                files={"file": ("sample.pdf", PDF_BYTES, "application/pdf")},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert scratch_files(tmp_path) == []

    async def test_scratch_file_keeps_original_extension(self, tmp_path: Path) -> None:
        # liteparse routes by extension, so the path handed to the pool must
        # carry the upload's extension.
        app, pool = make_app(tmp_path)
        secret = issue_token(app)
        async with make_client(app) as client:
            await client.post(
                "/v1/convert",
                files={"file": ("statement.PDF", PDF_BYTES, "application/pdf")},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert pool.calls[0].path.suffix == ".pdf"

    async def test_defaults_to_markdown_and_auto_ocr(self, tmp_path: Path) -> None:
        app, pool = make_app(tmp_path)
        secret = issue_token(app)
        async with make_client(app) as client:
            await client.post(
                "/v1/convert",
                files={"file": ("sample.pdf", PDF_BYTES, "application/pdf")},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert pool.calls[0].output is OutputFormat.MARKDOWN
        assert pool.calls[0].ocr is OcrMode.AUTO

    async def test_passes_text_and_ocr_off_through(self, tmp_path: Path) -> None:
        app, pool = make_app(tmp_path)
        secret = issue_token(app)
        async with make_client(app) as client:
            response = await client.post(
                "/v1/convert",
                files={"file": ("sample.pdf", PDF_BYTES, "application/pdf")},
                data={"output": "text", "ocr": "off"},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.json()["output"] == "text"
        assert pool.calls[0].output is OutputFormat.TEXT
        assert pool.calls[0].ocr is OcrMode.OFF


class TestConvertRejections:
    async def test_missing_token_is_401_and_skips_parse(self, tmp_path: Path) -> None:
        app, pool = make_app(tmp_path)
        issue_token(app)
        async with make_client(app) as client:
            response = await client.post(
                "/v1/convert",
                files={"file": ("sample.pdf", PDF_BYTES, "application/pdf")},
            )
        assert response.status_code == 401
        assert pool.calls == []

    async def test_missing_file_field_is_400(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        secret = issue_token(app)
        async with make_client(app) as client:
            response = await client.post(
                "/v1/convert",
                data={"output": "markdown"},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 400

    async def test_bogus_output_value_is_400(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)[0]
        secret = issue_token(app)
        async with make_client(app) as client:
            response = await client.post(
                "/v1/convert",
                files={"file": ("sample.pdf", PDF_BYTES, "application/pdf")},
                data={"output": "xml"},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 400

    async def test_oversized_upload_is_413_and_skips_parse(
        self, tmp_path: Path
    ) -> None:
        # Body stays under the prefilter's slack so the streaming counter is
        # what trips (max_upload_bytes=64 < 200 bytes << max + 1 MiB slack).
        app, pool = make_app(tmp_path, max_upload_bytes=64)
        secret = issue_token(app)
        async with make_client(app) as client:
            response = await client.post(
                "/v1/convert",
                files={"file": ("sample.pdf", b"x" * 200, "application/pdf")},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 413
        assert pool.calls == []
        assert scratch_files(tmp_path) == []

    async def test_body_over_prefilter_threshold_is_413(self, tmp_path: Path) -> None:
        # Body exceeds max_upload_bytes + slack, so the Content-Length
        # prefilter rejects it before the endpoint reads anything.
        app, pool = make_app(tmp_path, max_upload_bytes=64)
        secret = issue_token(app)
        oversized = b"x" * (64 + MULTIPART_OVERHEAD_SLACK + 128)
        async with make_client(app) as client:
            response = await client.post(
                "/v1/convert",
                files={"file": ("sample.pdf", oversized, "application/pdf")},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 413
        assert response.json()["detail"] == "upload exceeds maximum size"
        assert pool.calls == []

    async def test_unsupported_format_is_415_generic_and_skips_parse(
        self, tmp_path: Path
    ) -> None:
        app, pool = make_app(tmp_path)
        secret = issue_token(app)
        async with make_client(app) as client:
            response = await client.post(
                "/v1/convert",
                files={"file": ("x.exe", b"MZ", "application/octet-stream")},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 415
        assert response.json()["detail"] == "unsupported input format"
        assert "exe" not in response.text
        assert pool.calls == []
        assert scratch_files(tmp_path) == []


class TestConvertParseFailures:
    async def test_unparseable_document_is_422(self, tmp_path: Path) -> None:
        pool = FakePool(exc=DocumentUnparseableError("secret-doc-internals"))
        app = make_app(tmp_path, pool=pool)[0]
        secret = issue_token(app)
        async with make_client(app) as client:
            response = await client.post(
                "/v1/convert",
                files={"file": ("sample.pdf", PDF_BYTES, "application/pdf")},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 422
        assert "secret-doc-internals" not in response.text
        assert scratch_files(tmp_path) == []

    async def test_timeout_is_500_with_timeout_detail(self, tmp_path: Path) -> None:
        pool = FakePool(exc=JobTimeoutError("job timed out"))
        app = make_app(tmp_path, pool=pool)[0]
        secret = issue_token(app)
        async with make_client(app) as client:
            response = await client.post(
                "/v1/convert",
                files={"file": ("sample.pdf", PDF_BYTES, "application/pdf")},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 500
        assert response.json()["detail"] == "timeout"
        assert scratch_files(tmp_path) == []

    async def test_worker_crash_is_500_generic(self, tmp_path: Path) -> None:
        pool = FakePool(exc=WorkerCrashedError("worker process crashed"))
        app = make_app(tmp_path, pool=pool)[0]
        secret = issue_token(app)
        async with make_client(app) as client:
            response = await client.post(
                "/v1/convert",
                files={"file": ("sample.pdf", PDF_BYTES, "application/pdf")},
                headers={"Authorization": f"Bearer {secret}"},
            )
        assert response.status_code == 500
        assert response.json()["detail"] == "Internal server error"


class TestConvertPrivacy:
    async def test_filename_and_content_absent_from_info_logs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pool = FakePool(
            result=ParsedDocument(content="SENSITIVE-TEXT", pages=1, duration_ms=1.0)
        )
        app = make_app(tmp_path, pool=pool)[0]
        secret = issue_token(app)
        async with make_client(app) as client:
            await client.post(
                "/v1/convert",
                files={
                    "file": ("bank-statement-SECRET.pdf", PDF_BYTES, "application/pdf")
                },
                headers={"Authorization": f"Bearer {secret}"},
            )
        captured = capsys.readouterr()
        # Filename is DEBUG-only; extracted text is never logged at any level.
        assert "bank-statement-SECRET" not in captured.out
        assert "SENSITIVE-TEXT" not in captured.out

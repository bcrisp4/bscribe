"""Tests for the worker-side plumbing in ``bscribe.workers``.

The pool's failure modes (timeout, crash, cancellation, recycling) need
real subprocesses and live in ``tests/integration/test_workers.py``;
here we cover the pieces that run in-process.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import bscribe.workers
from bscribe.domain import (
    Component,
    DocumentUnparseableError,
    OcrMode,
    OutputFormat,
    ParsedDocument,
    PipelineStamp,
    WorkerCrashedError,
)
from bscribe.workers import (
    WorkerPool,
    WorkerPoolMetrics,
    _initialize_worker,  # pyright: ignore[reportPrivateUsage]
    _parse_in_worker,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# All nine components, present so `traversed_stamp`'s filtering in
# `WorkerPool.parse` has something to filter down from — see
# ``tests/unit/domain/test_pipeline.py`` for the pure-function tests of
# the filtering logic itself; these tests only check that `parse()`
# actually calls it with the right (extension, ocr).
ALL_COMPONENT_VERSIONS: dict[str, str] = {
    Component.BSCRIBE.value: "0.3.0",
    Component.LITEPARSE.value: "2.5.0",
    Component.PDFIUM.value: "bundled (liteparse 2.5.0)",
    Component.TESSERACT.value: "bundled (liteparse 2.5.0)",
    Component.TESSDATA.value: "sha256:abc123def456",
    Component.IMAGEMAGICK.value: "6.9.12-98",
    Component.LIBREOFFICE.value: "7.6.4.1",
    Component.GHOSTSCRIPT.value: "10.03.1",
    Component.LIBRSVG.value: "2.54.7",
}


def _completed_future(
    result: ParsedDocument,
) -> concurrent.futures.Future[ParsedDocument]:
    """A pre-resolved future, standing in for a pebble ``ProcessFuture``.

    ``asyncio.wrap_future`` accepts any ``concurrent.futures.Future``, so
    this lets ``WorkerPool.parse``'s stamping logic be exercised without
    spinning a real subprocess pool — that real-pool path (and its
    failure modes) is covered in ``tests/integration/test_workers.py``.
    """
    future: concurrent.futures.Future[ParsedDocument] = concurrent.futures.Future()
    future.set_result(result)
    return future


def _stub_schedule(
    result: ParsedDocument,
) -> Callable[[Path, OutputFormat, OcrMode], concurrent.futures.Future[ParsedDocument]]:
    """Build a ``WorkerPool._schedule`` stand-in returning ``result`` at once."""

    def _schedule(
        path: Path, output: OutputFormat, ocr: OcrMode
    ) -> concurrent.futures.Future[ParsedDocument]:
        del path, output, ocr
        return _completed_future(result)

    return _schedule


@pytest.fixture(autouse=True)
def _reset_worker_parser() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Isolate the per-process parser global between tests."""
    yield
    bscribe.workers._worker_parser = None  # pyright: ignore[reportPrivateUsage]


@dataclass
class FakeParser:
    result: ParsedDocument

    def parse(
        self, path: Path, *, output: OutputFormat, ocr: OcrMode
    ) -> ParsedDocument:
        return self.result


@dataclass
class ChainedErrorParser:
    """Raises the domain error with an engine exception chained as cause."""

    def parse(
        self, path: Path, *, output: OutputFormat, ocr: OcrMode
    ) -> ParsedDocument:
        cause = ValueError("FAKE-DOCUMENT-INTERNALS")
        raise DocumentUnparseableError("document could not be parsed") from cause


def test_parse_in_worker_raises_before_initialization() -> None:
    with pytest.raises(RuntimeError, match="worker not initialized"):
        _parse_in_worker(Path("x.pdf"), OutputFormat.MARKDOWN, OcrMode.OFF)


def test_initializer_installs_factory_product() -> None:
    document = ParsedDocument(content="hi", pages=1, duration_ms=1.0)
    _initialize_worker(lambda: FakeParser(result=document))
    parsed = _parse_in_worker(Path("x.pdf"), OutputFormat.MARKDOWN, OcrMode.OFF)
    assert parsed == document


@dataclass
class ExplodingParser:
    """Raises a non-domain error whose message mimics document content."""

    def parse(
        self, path: Path, *, output: OutputFormat, ocr: OcrMode
    ) -> ParsedDocument:
        raise ValueError("FAKE-DOCUMENT-INTERNALS")


def test_parse_in_worker_scrubs_unexpected_exceptions() -> None:
    """Non-domain worker errors are replaced wholesale — only the type
    name may cross the pipe (see docs/design.md — Privacy)."""
    _initialize_worker(ExplodingParser)
    with pytest.raises(WorkerCrashedError) as excinfo:
        _parse_in_worker(Path("x.pdf"), OutputFormat.MARKDOWN, OcrMode.OFF)
    assert str(excinfo.value) == "unexpected ValueError in worker"
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


def test_parse_in_worker_severs_unparseable_cause_chain() -> None:
    """Engine internals must not survive into the exception pebble ships
    back to the parent (see docs/design.md — Privacy)."""
    _initialize_worker(ChainedErrorParser)
    with pytest.raises(DocumentUnparseableError) as excinfo:
        _parse_in_worker(Path("x.pdf"), OutputFormat.MARKDOWN, OcrMode.OFF)
    assert str(excinfo.value) == "document could not be parsed"
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


def test_metrics_start_at_zero() -> None:
    metrics = WorkerPoolMetrics()
    assert metrics.timeout_kills == 0
    assert metrics.crashes == 0
    assert metrics.cancellations == 0
    assert metrics.pool_rebuilds == 0


class TestParentSideStamping:
    """``WorkerPool.parse`` stamps the result; the worker itself never does
    (see the module docstring's stamping note). No real subprocess is
    needed to exercise this — ``_schedule`` is monkeypatched to return an
    already-resolved future carrying the unstamped ``ParsedDocument`` a
    worker would hand back."""

    def _make_pool(self, pipeline_info: PipelineStamp) -> WorkerPool:
        return WorkerPool(
            worker_count=1,
            job_timeout_seconds=30.0,
            worker_max_tasks=0,
            pipeline_info=pipeline_info,
        )

    async def test_pdf_without_ocr_stamps_only_core_and_pdfium(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stamp = PipelineStamp(
            fingerprint="abc123def456", components=dict(ALL_COMPONENT_VERSIONS)
        )
        pool = self._make_pool(stamp)
        unstamped = ParsedDocument(content="hi", pages=1, duration_ms=1.0)
        monkeypatch.setattr(pool, "_schedule", _stub_schedule(unstamped))

        result = await pool.parse(
            Path("doc.pdf"), output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF
        )

        assert result.pipeline is not None
        assert set(result.pipeline.components) == {
            Component.BSCRIBE.value,
            Component.LITEPARSE.value,
            Component.PDFIUM.value,
        }
        assert result.pipeline.fingerprint == stamp.fingerprint
        pool.close()

    async def test_docx_with_auto_ocr_adds_office_and_ocr_components(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stamp = PipelineStamp(
            fingerprint="abc123def456", components=dict(ALL_COMPONENT_VERSIONS)
        )
        pool = self._make_pool(stamp)
        unstamped = ParsedDocument(content="hi", pages=1, duration_ms=1.0)
        monkeypatch.setattr(pool, "_schedule", _stub_schedule(unstamped))

        result = await pool.parse(
            Path("doc.docx"), output=OutputFormat.MARKDOWN, ocr=OcrMode.AUTO
        )

        assert result.pipeline is not None
        assert set(result.pipeline.components) == {
            Component.BSCRIBE.value,
            Component.LITEPARSE.value,
            Component.PDFIUM.value,
            Component.LIBREOFFICE.value,
            Component.TESSERACT.value,
            Component.TESSDATA.value,
        }
        assert result.pipeline.fingerprint == stamp.fingerprint
        pool.close()

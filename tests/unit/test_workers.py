"""Tests for the worker-side plumbing in ``bscribe.workers``.

The pool's failure modes (timeout, crash, cancellation, recycling) need
real subprocesses and live in ``tests/integration/test_workers.py``;
here we cover the pieces that run in-process.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import bscribe.workers
from bscribe.domain import (
    DocumentUnparseableError,
    OcrMode,
    OutputFormat,
    ParsedDocument,
    WorkerCrashedError,
)
from bscribe.workers import (
    WorkerPoolMetrics,
    _initialize_worker,  # pyright: ignore[reportPrivateUsage]
    _parse_in_worker,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:
    from collections.abc import Iterator


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

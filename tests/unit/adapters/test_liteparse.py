"""Unit tests for the liteparse adapter using a fake engine.

The real engine is exercised in ``tests/integration``; here liteparse is
faked at the adapter's I/O boundary to cover behavior that real documents
cannot easily trigger (e.g. a document that loads with zero pages —
PDFium rejects hand-rolled zero-page files at load time).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import liteparse
import pytest

import bscribe.adapters.liteparse as adapter_module
from bscribe.adapters.liteparse import LiteparseParser
from bscribe.domain import DocumentUnparseableError, OcrMode, OutputFormat


@dataclass
class FakeParseResult:
    """Minimal stand-in for liteparse.ParseResult."""

    text: str = ""
    pages: list[str] = field(default_factory=list[str])


@dataclass
class FakeEngine:
    """Minimal stand-in for liteparse.LiteParse."""

    result: FakeParseResult

    def parse(self, path: str | Path) -> FakeParseResult:
        del path
        return self.result

    def is_complex(self, path: str | Path) -> list[object]:
        del path
        return []


@pytest.fixture
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    """Swap the adapter's liteparse module for a fake engine factory."""
    fake = FakeEngine(result=FakeParseResult())

    def fake_lite_parse(**_kwargs: object) -> FakeEngine:
        return fake

    monkeypatch.setattr(
        adapter_module,
        "liteparse",
        SimpleNamespace(LiteParse=fake_lite_parse, ParseError=liteparse.ParseError),
    )
    return fake


class TestZeroPageResult:
    """A document that loads but yields no pages is unparseable, not empty."""

    def test_zero_pages_raises_document_unparseable(
        self, fake_engine: FakeEngine
    ) -> None:
        assert fake_engine.result.pages == []
        with pytest.raises(DocumentUnparseableError):
            LiteparseParser().parse(
                Path("whatever.pdf"), output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF
            )

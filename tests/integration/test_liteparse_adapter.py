"""Integration tests for the liteparse adapter against the real engine.

Uses the born-digital one-page fixture ``data/sample.pdf`` (provenance in
the package docstring). These tests exercise real native parsing — PDFium
via the liteparse wheel — so they are integration, not unit, tests; they
still run in the default pytest sweep (the wheel bundles everything).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bscribe.adapters.liteparse import LiteparseParser
from bscribe.domain import (
    DocumentUnparseableError,
    OcrMode,
    OutputFormat,
    ParserPort,
)

FIXTURE_PDF = Path(__file__).parent / "data" / "sample.pdf"


@pytest.fixture
def parser() -> LiteparseParser:
    return LiteparseParser()


def test_satisfies_parser_port(parser: LiteparseParser) -> None:
    assert isinstance(parser, ParserPort)


class TestBornDigitalPdf:
    def test_markdown_output(self, parser: LiteparseParser) -> None:
        doc = parser.parse(FIXTURE_PDF, output=OutputFormat.MARKDOWN, ocr=OcrMode.AUTO)
        assert "Sample PDF" in doc.content
        # Markdown structure survives: the fixture renders a heading.
        assert "#" in doc.content
        assert doc.pages == 1
        assert doc.ocr_used is False
        assert doc.duration_ms > 0

    def test_text_output(self, parser: LiteparseParser) -> None:
        doc = parser.parse(FIXTURE_PDF, output=OutputFormat.TEXT, ocr=OcrMode.AUTO)
        # Text mode preserves spatial layout, padding gaps with spaces
        # ("Sample      PDF"), so assert on a phrase from flowing body text.
        assert "Fun fun fun." in doc.content
        assert "#" not in doc.content
        assert doc.pages == 1

    def test_ocr_off(self, parser: LiteparseParser) -> None:
        doc = parser.parse(FIXTURE_PDF, output=OutputFormat.MARKDOWN, ocr=OcrMode.OFF)
        assert "Sample PDF" in doc.content
        assert doc.ocr_used is False


class TestFailureModes:
    def test_garbage_bytes_raise_document_unparseable(
        self, parser: LiteparseParser, tmp_path: Path
    ) -> None:
        garbage = tmp_path / "garbage.pdf"
        garbage.write_bytes(b"not a pdf at all \x00\x01\x02")
        with pytest.raises(DocumentUnparseableError):
            parser.parse(garbage, output=OutputFormat.MARKDOWN, ocr=OcrMode.AUTO)

    def test_missing_path_raises_file_not_found(
        self, parser: LiteparseParser, tmp_path: Path
    ) -> None:
        with pytest.raises(FileNotFoundError):
            parser.parse(
                tmp_path / "nope.pdf", output=OutputFormat.MARKDOWN, ocr=OcrMode.AUTO
            )

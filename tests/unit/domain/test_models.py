"""Tests for bscribe.domain.models."""

from __future__ import annotations

import dataclasses

import pytest

from bscribe.domain.models import OcrMode, OutputFormat, ParsedDocument


def make_parsed_document(**overrides: object) -> ParsedDocument:
    """Build a ParsedDocument with sensible defaults."""
    defaults: dict[str, object] = {
        "content": "# Title\n\nBody.",
        "pages": 1,
        "ocr_used": False,
        "duration_ms": 12.5,
    }
    defaults.update(overrides)
    return ParsedDocument(**defaults)  # type: ignore[arg-type]


class TestOutputFormat:
    """Enum values are the wire strings from the API contract."""

    def test_markdown_value(self) -> None:
        assert OutputFormat.MARKDOWN.value == "markdown"

    def test_text_value(self) -> None:
        assert OutputFormat.TEXT.value == "text"

    def test_only_two_formats(self) -> None:
        assert len(OutputFormat) == 2


class TestOcrMode:
    """Enum values are the wire strings; force is deliberately absent."""

    def test_auto_value(self) -> None:
        assert OcrMode.AUTO.value == "auto"

    def test_off_value(self) -> None:
        assert OcrMode.OFF.value == "off"

    def test_force_absent(self) -> None:
        # liteparse has no force-OCR; see docs/design.md Closed issues.
        assert len(OcrMode) == 2


class TestParsedDocument:
    def test_carries_content_and_metadata(self) -> None:
        doc = make_parsed_document(content="hello", pages=3, ocr_used=True)
        assert doc.content == "hello"
        assert doc.pages == 3
        assert doc.ocr_used is True
        assert doc.duration_ms == 12.5

    def test_is_frozen(self) -> None:
        doc = make_parsed_document()
        with pytest.raises(dataclasses.FrozenInstanceError):
            doc.content = "changed"  # type: ignore[misc]

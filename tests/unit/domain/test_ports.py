"""Tests for bscribe.domain.ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bscribe.domain.models import OcrMode, OutputFormat, ParsedDocument
from bscribe.domain.ports import ParserPort

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class FakeParser:
    """In-memory ParserPort implementation for domain-level tests."""

    result: ParsedDocument

    def parse(
        self, path: Path, *, output: OutputFormat, ocr: OcrMode
    ) -> ParsedDocument:
        del path, output, ocr
        return self.result


class TestParserPortConformance:
    def test_fake_parser_satisfies_port(self) -> None:
        fake = FakeParser(
            result=ParsedDocument(
                content="text", pages=1, ocr_used=False, duration_ms=1.0
            )
        )
        assert isinstance(fake, ParserPort)

    def test_object_without_parse_does_not_satisfy_port(self) -> None:
        assert not isinstance(object(), ParserPort)

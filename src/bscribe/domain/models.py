"""Domain models for document conversion."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OutputFormat(StrEnum):
    """Conversion output format; values are the API wire strings."""

    MARKDOWN = "markdown"
    TEXT = "text"


class OcrMode(StrEnum):
    """OCR behavior; values are the API wire strings.

    AUTO applies OCR to pages the engine's complexity detection flags;
    OFF disables OCR entirely. There is deliberately no FORCE — liteparse
    exposes OCR control only as a boolean (see docs/design.md, Closed
    issues); adding it later is an additive API change.
    """

    AUTO = "auto"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Result of parsing one document: content plus conversion metadata.

    An ``ocr_used`` quality signal is deliberately absent for now: liteparse
    does not report whether OCR ran, and deriving it cost a duplicate
    document pass — see docs/design.md, Closed issues. Adding it back when
    the engine exposes the signal is an additive change.

    Attributes:
        content: Extracted text in the requested output format.
        pages: Number of pages in the parsed document.
        duration_ms: Wall-clock parse duration in milliseconds.
    """

    content: str
    pages: int
    duration_ms: float

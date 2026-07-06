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

    Attributes:
        content: Extracted text in the requested output format.
        pages: Number of pages in the parsed document.
        ocr_used: Whether OCR contributed to the content. Derived by the
            adapter (the engine does not report it) — see docs/design.md.
        duration_ms: Wall-clock parse duration in milliseconds.
    """

    content: str
    pages: int
    ocr_used: bool
    duration_ms: float

"""Ports (Protocol interfaces) the domain core depends on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from bscribe.domain.models import OcrMode, OutputFormat, ParsedDocument


@runtime_checkable
class ParserPort(Protocol):
    """Converts one document file into text or markdown.

    Deliberately synchronous: parsing is CPU-bound native work that runs
    inside worker processes (see docs/design.md — Job execution), never on
    the event loop.
    """

    def parse(
        self, path: Path, *, output: OutputFormat, ocr: OcrMode
    ) -> ParsedDocument:
        """Parse the document at ``path``.

        Args:
            path: Document file to parse; must exist.
            output: Desired content format.
            ocr: OCR behavior for scanned/complex pages.

        Returns:
            The extracted content plus conversion metadata.

        Raises:
            DocumentUnparseableError: The engine could not parse the
                document (corrupt, encrypted, or otherwise unreadable).
            FileNotFoundError: ``path`` does not exist (caller bug, not a
                document problem).
        """
        ...

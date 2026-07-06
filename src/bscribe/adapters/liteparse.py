"""liteparse implementation of ``ParserPort``.

The only module allowed to import liteparse types; everything above the
port sees domain types only. Two engine quirks the adapter papers over
(both verified against liteparse 2.4.0 — see docs/design.md, Closed
issues):

- OCR control is a boolean ``ocr_enabled`` (True = OCR pages that need
  it), which maps exactly onto ``OcrMode.AUTO``/``OFF``.
- ``ParseResult`` reports no ``ocr_used`` flag, so it is derived from the
  engine's own ``is_complex()`` pre-check — the same per-page
  ``needs_ocr`` signal liteparse's OCR routing uses internally.
- ``liteparse.__version__`` is hardcoded to a stale ``"2.0.0"`` upstream;
  when the pipeline-version metadata lands (M3), read the real version via
  ``importlib.metadata.version("liteparse")``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import liteparse

from bscribe.domain.errors import DocumentUnparseableError
from bscribe.domain.models import OcrMode, ParsedDocument

if TYPE_CHECKING:
    from pathlib import Path

    from bscribe.domain.models import OutputFormat


class LiteparseParser:
    """``ParserPort`` adapter backed by the liteparse engine.

    Stateless and cheap to construct; each ``parse`` call builds a fresh
    ``LiteParse`` because output format and OCR mode are constructor-time
    engine config, not per-call arguments.
    """

    def parse(
        self, path: Path, *, output: OutputFormat, ocr: OcrMode
    ) -> ParsedDocument:
        """Parse ``path`` with liteparse. See ``ParserPort.parse``."""
        start = time.perf_counter()
        # quiet=True: the engine writes progress to stdout otherwise,
        # corrupting the JSON log stream.
        engine = liteparse.LiteParse(
            output_format=output.value,
            ocr_enabled=ocr is OcrMode.AUTO,
            quiet=True,
        )
        try:
            ocr_used = ocr is OcrMode.AUTO and any(
                page.needs_ocr for page in engine.is_complex(path)
            )
            result = engine.parse(path)
        except liteparse.ParseError as exc:
            # Message deliberately generic: liteparse errors can quote
            # document internals, which must not propagate (Privacy).
            raise DocumentUnparseableError("document could not be parsed") from exc
        return ParsedDocument(
            content=result.text,
            pages=len(result.pages),
            ocr_used=ocr_used,
            duration_ms=(time.perf_counter() - start) * 1000,
        )

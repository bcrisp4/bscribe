"""liteparse implementation of ``ParserPort``.

The only module allowed to import liteparse types; everything above the
port sees domain types only. Engine quirks the adapter papers over (all
verified against liteparse 2.4.0 — see docs/design.md, Closed issues):

- OCR control is a boolean ``ocr_enabled`` (True = OCR pages that need
  it), which maps exactly onto ``OcrMode.AUTO``/``OFF``.
- ``ParseResult`` reports no ocr-used flag, so ``ParsedDocument`` carries
  none: deriving it from an ``is_complex()`` pre-check meant a duplicate
  document pass and a second failure surface (see docs/design.md, Closed
  issues). Returns if liteparse exposes the signal on parse results.
- ``liteparse.__version__`` is hardcoded to a stale ``"2.0.0"`` upstream;
  when the pipeline-version metadata lands (M3), read the real version via
  ``importlib.metadata.version("liteparse")``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import liteparse

from bscribe.domain.errors import DocumentUnparseableError
from bscribe.domain.models import OcrMode, OutputFormat, ParsedDocument

if TYPE_CHECKING:
    from pathlib import Path


class LiteparseParser:
    """``ParserPort`` adapter backed by the liteparse engine.

    Engines are cached per ``(output, ocr)`` — the only two knobs bscribe
    varies, both constructor-time engine config. Construction is not cheap:
    each ``LiteParse()`` spins up a tokio worker-thread pool, so the four
    possible engines are built once and reused. Safe because the engine's
    parse methods take no per-call state and each worker process is
    single-threaded.
    """

    def __init__(self) -> None:
        self._engines: dict[tuple[OutputFormat, OcrMode], liteparse.LiteParse] = {}

    def _engine(self, output: OutputFormat, ocr: OcrMode) -> liteparse.LiteParse:
        engine = self._engines.get((output, ocr))
        if engine is None:
            # quiet=True: the engine writes progress to stdout otherwise,
            # corrupting the JSON log stream.
            engine = liteparse.LiteParse(
                output_format=output.value,
                ocr_enabled=ocr is OcrMode.AUTO,
                quiet=True,
            )
            self._engines[(output, ocr)] = engine
        return engine

    def parse(
        self, path: Path, *, output: OutputFormat, ocr: OcrMode
    ) -> ParsedDocument:
        """Parse ``path`` with liteparse. See ``ParserPort.parse``."""
        start = time.perf_counter()
        engine = self._engine(output, ocr)
        try:
            result = engine.parse(path)
        except liteparse.ParseError as exc:
            # Message deliberately generic: liteparse errors can quote
            # document internals, which must not propagate (Privacy).
            raise DocumentUnparseableError("document could not be parsed") from exc
        if not result.pages:
            # A document that loads but yields zero pages has no content to
            # return; surface it as unparseable rather than an empty success.
            raise DocumentUnparseableError("document contains no pages")
        return ParsedDocument(
            content=result.text,
            pages=len(result.pages),
            duration_ms=(time.perf_counter() - start) * 1000,
        )

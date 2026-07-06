"""bscribe domain core.

Pure domain types and ports (hexagonal architecture). This package must
never import adapter code or parsing libraries — adapters depend on the
domain, not the other way around.
"""

from __future__ import annotations

from bscribe.domain.errors import (
    DocumentUnparseableError,
    JobTimeoutError,
    WorkerCrashedError,
)
from bscribe.domain.models import OcrMode, OutputFormat, ParsedDocument
from bscribe.domain.ports import ParserPort

__all__ = [
    "DocumentUnparseableError",
    "JobTimeoutError",
    "OcrMode",
    "OutputFormat",
    "ParsedDocument",
    "ParserPort",
    "WorkerCrashedError",
]

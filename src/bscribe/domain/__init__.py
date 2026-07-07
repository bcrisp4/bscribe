"""bscribe domain core.

Pure domain types and ports (hexagonal architecture). This package must
never import adapter code or parsing libraries — adapters depend on the
domain, not the other way around.
"""

from __future__ import annotations

from bscribe.domain.errors import (
    DocumentUnparseableError,
    JobTimeoutError,
    UnsupportedFormatError,
    WorkerCrashedError,
)
from bscribe.domain.formats import SUPPORTED_EXTENSIONS, supported_extension
from bscribe.domain.models import OcrMode, OutputFormat, ParsedDocument, Token
from bscribe.domain.ports import ParserPort, TokenStorePort
from bscribe.domain.tokens import (
    SECRET_PREFIX,
    generate_secret,
    hash_secret,
    mint_token,
)

__all__ = [
    "SECRET_PREFIX",
    "SUPPORTED_EXTENSIONS",
    "DocumentUnparseableError",
    "JobTimeoutError",
    "OcrMode",
    "OutputFormat",
    "ParsedDocument",
    "ParserPort",
    "Token",
    "TokenStorePort",
    "UnsupportedFormatError",
    "WorkerCrashedError",
    "generate_secret",
    "hash_secret",
    "mint_token",
    "supported_extension",
]

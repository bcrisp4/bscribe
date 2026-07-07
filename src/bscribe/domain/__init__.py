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
from bscribe.domain.jobs import create_job
from bscribe.domain.models import (
    Job,
    JobStatus,
    OcrMode,
    OutputFormat,
    ParsedDocument,
    Token,
)
from bscribe.domain.ports import JobStorePort, ParserPort, TokenStorePort
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
    "Job",
    "JobStatus",
    "JobStorePort",
    "JobTimeoutError",
    "OcrMode",
    "OutputFormat",
    "ParsedDocument",
    "ParserPort",
    "Token",
    "TokenStorePort",
    "UnsupportedFormatError",
    "WorkerCrashedError",
    "create_job",
    "generate_secret",
    "hash_secret",
    "mint_token",
    "supported_extension",
]

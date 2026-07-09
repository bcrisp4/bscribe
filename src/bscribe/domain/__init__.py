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
    Component,
    Job,
    JobStatus,
    OcrMode,
    OutputFormat,
    ParsedDocument,
    PipelineStamp,
    Token,
)
from bscribe.domain.pipeline import (
    UNAVAILABLE,
    compute_fingerprint,
    traversed_components,
    traversed_stamp,
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
    "UNAVAILABLE",
    "Component",
    "DocumentUnparseableError",
    "Job",
    "JobStatus",
    "JobStorePort",
    "JobTimeoutError",
    "OcrMode",
    "OutputFormat",
    "ParsedDocument",
    "ParserPort",
    "PipelineStamp",
    "Token",
    "TokenStorePort",
    "UnsupportedFormatError",
    "WorkerCrashedError",
    "compute_fingerprint",
    "create_job",
    "generate_secret",
    "hash_secret",
    "mint_token",
    "supported_extension",
    "traversed_components",
    "traversed_stamp",
]

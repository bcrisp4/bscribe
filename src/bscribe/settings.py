"""Environment-driven application settings.

All configuration comes from ``BSCRIBE_``-prefixed environment variables
(e.g. ``BSCRIBE_WORKER_COUNT``), validated once at startup. Durations and
sizes carry explicit units in their names — plain integers, no ISO-8601.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_scratch_dir() -> Path:
    return Path(tempfile.gettempdir()) / "bscribe"


class Settings(BaseSettings):
    """Typed, immutable bscribe configuration.

    Attributes:
        worker_count: Parse worker processes; bounds total parse concurrency.
        job_timeout_seconds: Per-job deadline; the worker is SIGKILLed at it.
        max_upload_bytes: Global upload size limit (rejected with 413).
        scratch_dir: Transient upload storage; wiped on startup.
        db_path: SQLite database file (tokens now, jobs from M2).
        result_ttl_seconds: How long job results are retained for pickup.
        log_level: Minimum level emitted by the structlog pipeline.
    """

    model_config = SettingsConfigDict(env_prefix="BSCRIBE_", frozen=True)

    worker_count: int = Field(default=4, ge=1)
    job_timeout_seconds: int = Field(default=600, gt=0)
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    scratch_dir: Path = Field(default_factory=_default_scratch_dir)
    db_path: Path = Path("bscribe.db")
    result_ttl_seconds: int = Field(default=7 * 24 * 3600, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

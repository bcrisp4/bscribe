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


def _default_db_path() -> Path:
    # Absolute on purpose: a cwd-relative default makes the CLI and server
    # silently operate on different database files depending on where each
    # was started. The container overrides this with BSCRIBE_DB_PATH=/data.
    return Path.home() / ".local" / "share" / "bscribe" / "bscribe.db"


class Settings(BaseSettings):
    """Typed, immutable bscribe configuration.

    Attributes:
        worker_count: Parse worker processes; bounds total parse concurrency.
        job_timeout_seconds: Per-job deadline; the worker is forcibly
            killed at it (termination signal escalating to SIGKILL).
        worker_max_tasks: Jobs a worker runs before being recycled (bounds
            native-library leaks); 0 disables recycling.
        max_upload_bytes: Global upload size limit (rejected with 413).
        scratch_dir: Transient upload storage, wiped at startup (see design
            doc "Startup sweep").
        db_path: SQLite database file (tokens now, jobs from M2). Absolute
            default under the user data dir; the container image sets
            ``BSCRIBE_DB_PATH=/data/bscribe.db``.
        result_ttl_seconds: How long job results are retained for pickup;
            enforced by the periodic purge task, which deletes jobs and
            results once older than the TTL.
        purge_interval_seconds: How often the periodic purge task runs to
            delete expired jobs.
        log_level: Minimum level emitted by the structlog pipeline.
        metrics_enabled: Whether to expose Prometheus metrics. When true the
            server binds a separate HTTP port (below); when false no registry,
            instrumentation, or metrics server exists (docs/design.md —
            Monitoring).
        metrics_port: Port for the Prometheus exposition server — a separate
            listener from the API, scraped tailnet-internally.
        metrics_addr: Bind address for the metrics server. Defaults to all
            interfaces: access is gated by the tailnet, not this bind.
    """

    model_config = SettingsConfigDict(env_prefix="BSCRIBE_", frozen=True)

    worker_count: int = Field(default=4, ge=1)
    job_timeout_seconds: int = Field(default=600, gt=0)
    worker_max_tasks: int = Field(default=100, ge=0)
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    scratch_dir: Path = Field(default_factory=_default_scratch_dir)
    db_path: Path = Field(default_factory=_default_db_path)
    result_ttl_seconds: int = Field(default=7 * 24 * 3600, gt=0)
    purge_interval_seconds: int = Field(default=3600, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    metrics_enabled: bool = True
    metrics_port: int = Field(default=9090, ge=1, le=65535)
    metrics_addr: str = "0.0.0.0"  # noqa: S104 - tailnet-gated, see docstring

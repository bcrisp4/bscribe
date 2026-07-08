"""Maintenance tasks the FastAPI parent runs around the job store.

Two tasks, both driven by ``bscribe.app``'s lifespan:

* **Startup sweep** (docs/design.md — Job lifecycle details). Worker
  processes live and die with the container, so a restart abandons
  ``queued``/``running`` jobs and orphans any staged upload in the scratch
  dir. On boot, every incomplete job is marked failed and the scratch dir
  is wiped.
* **Periodic TTL purge** (docs/design.md — Data retention). Job records and
  their stored results are deleted once older than a configurable TTL.

Both sync functions take the store as a plain parameter — no clock, no
settings object — so they are directly unit-testable; ``purge_loop`` is the
only piece here that owns a clock (``interval_seconds``), and the app
lifespan runs everything via ``asyncio.to_thread`` to keep SQLite off the
event loop (docs/adr/0002).
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from bscribe.errors import INTERRUPTED_BY_RESTART_DETAIL

if TYPE_CHECKING:
    from pathlib import Path

    from bscribe.domain.ports import JobStorePort

logger = structlog.get_logger()


def startup_sweep(store: JobStorePort, scratch_dir: Path) -> int:
    """Fail every incomplete job and wipe the scratch dir. Runs once at boot.

    Args:
        store: Job store to sweep.
        scratch_dir: Upload staging directory. Removed and recreated empty:
            a restart mid-parse orphans whatever file was staged there, and
            the store transition above already accounts for its job.

    Returns:
        The number of jobs transitioned to failed.
    """
    count = store.sweep_incomplete(INTERRUPTED_BY_RESTART_DETAIL)
    # ignore_errors=True: the dir may not exist yet (first boot, or an
    # already-clean container) — that is not itself a failure to log.
    shutil.rmtree(scratch_dir, ignore_errors=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    logger.info("startup_sweep", jobs_failed=count)  # every boot, even 0
    return count


def purge_expired(store: JobStorePort, ttl_seconds: int) -> int:
    """Delete every job (any status or token) older than the retention TTL.

    Args:
        store: Job store to purge.
        ttl_seconds: Retention window in seconds; jobs created before
            ``now - ttl_seconds`` are deleted.

    Returns:
        The number of jobs deleted.
    """
    cutoff = datetime.now(tz=UTC) - timedelta(seconds=ttl_seconds)
    count = store.purge_older_than(cutoff)
    if count > 0:
        # Logged only when there's something to report — an hourly no-op
        # line would be pure noise at the default interval.
        logger.info("jobs_purged", count=count)
    return count


async def purge_loop(
    store: JobStorePort, *, ttl_seconds: int, interval_seconds: int
) -> None:
    """Run ``purge_expired`` forever, once per ``interval_seconds``.

    Purges before the first sleep, not after: a server down longer than the
    TTL should not wait a full interval to catch up on boot, and starting
    with the purge keeps tests deterministic (no need to advance a clock
    past a sleep to observe the first call). Runs until cancelled by the
    owning lifespan.

    Args:
        store: Job store to purge.
        ttl_seconds: Passed through to ``purge_expired`` each iteration.
        interval_seconds: Delay between purges.
    """
    while True:
        try:
            await asyncio.to_thread(purge_expired, store, ttl_seconds)
        except Exception as exc:
            # Except Exception deliberately excludes CancelledError (a
            # BaseException): cancellation must propagate so the owning
            # lifespan can actually stop the loop on shutdown.
            logger.error("job_purge_error", error_type=type(exc).__name__)
        await asyncio.sleep(interval_seconds)

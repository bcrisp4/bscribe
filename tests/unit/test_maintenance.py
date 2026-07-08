"""Tests for bscribe.maintenance."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from bscribe.domain.models import (
    Job,
    JobStatus,
    OcrMode,
    OutputFormat,
    ParsedDocument,
)
from bscribe.errors import INTERRUPTED_BY_RESTART_DETAIL
from bscribe.maintenance import purge_expired, purge_loop, startup_sweep
from tests.unit.fakes import FakeJobStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def make_job(**overrides: object) -> Job:
    """Job factory with sensible defaults (a freshly queued job)."""
    defaults: dict[str, object] = {
        "id": "abcd1234abcd1234",
        "token_id": "feed0001",
        "output": OutputFormat.MARKDOWN,
        "ocr": OcrMode.AUTO,
        "status": JobStatus.QUEUED,
        "created_at": datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
        "started_at": None,
        "finished_at": None,
        "failure_detail": None,
    }
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


def make_result(**overrides: object) -> ParsedDocument:
    """ParsedDocument factory with sensible defaults."""
    defaults: dict[str, object] = {
        "content": "# Title\n\nBody.",
        "pages": 3,
        "duration_ms": 412.5,
    }
    defaults.update(overrides)
    return ParsedDocument(**defaults)  # type: ignore[arg-type]


class TestStartupSweep:
    def test_fails_incomplete_jobs_leaves_done_alone(self, tmp_path: Path) -> None:
        store = FakeJobStore()
        queued = make_job(id="000000000000000a")
        running = make_job(id="000000000000000b")
        done = make_job(id="000000000000000c")
        store.add(queued)
        store.add(running)
        store.add(done)
        store.mark_running(running.id)
        store.mark_running(done.id)
        store.mark_done(done.id, make_result())
        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        count = startup_sweep(store, scratch_dir)

        assert count == 2
        for job_id in (queued.id, running.id):
            found = store.get(job_id, "feed0001")
            assert found is not None
            assert found.status is JobStatus.FAILED
            assert found.failure_detail == INTERRUPTED_BY_RESTART_DETAIL
        found_done = store.get(done.id, "feed0001")
        assert found_done is not None
        assert found_done.status is JobStatus.DONE

    def test_scratch_dir_wiped_but_recreated(self, tmp_path: Path) -> None:
        store = FakeJobStore()
        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()
        (scratch_dir / "upload.pdf").write_bytes(b"data")
        subdir = scratch_dir / "nested"
        subdir.mkdir()
        (subdir / "leftover.txt").write_text("x")

        startup_sweep(store, scratch_dir)

        assert scratch_dir.is_dir()
        assert list(scratch_dir.iterdir()) == []

    def test_wipe_failure_logged_but_boot_proceeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A genuine wipe failure (not just a missing dir) must not abort
        boot, but must leave an operator-visible log line: uploads may
        have survived the documented startup wipe."""
        store = FakeJobStore()
        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()

        def _denied(path: Path) -> None:
            raise PermissionError(13, "denied")

        monkeypatch.setattr("bscribe.maintenance.shutil.rmtree", _denied)

        count = startup_sweep(store, scratch_dir)

        assert count == 0
        assert scratch_dir.is_dir()
        assert "scratch_wipe_error" in capsys.readouterr().out

    def test_missing_scratch_dir_created_without_error(self, tmp_path: Path) -> None:
        store = FakeJobStore()
        scratch_dir = tmp_path / "does-not-exist-yet"

        startup_sweep(store, scratch_dir)

        assert scratch_dir.is_dir()

    def test_returns_zero_on_empty_store(self, tmp_path: Path) -> None:
        store = FakeJobStore()
        scratch_dir = tmp_path / "scratch"

        assert startup_sweep(store, scratch_dir) == 0


class TestPurgeExpired:
    def test_deletes_only_jobs_older_than_ttl(self) -> None:
        store = FakeJobStore()
        now = datetime.now(tz=UTC)
        old = make_job(id="000000000000000a", created_at=now - timedelta(days=8))
        fresh = make_job(id="000000000000000b", created_at=now - timedelta(minutes=1))
        store.add(old)
        store.add(fresh)

        count = purge_expired(store, ttl_seconds=7 * 24 * 3600)

        assert count == 1
        assert store.get(old.id, "feed0001") is None
        assert store.get(fresh.id, "feed0001") is not None

    def test_returns_zero_when_nothing_expired(self) -> None:
        store = FakeJobStore()
        store.add(make_job(created_at=datetime.now(tz=UTC)))

        assert purge_expired(store, ttl_seconds=7 * 24 * 3600) == 0


@dataclass
class RecordingPurgeStore(FakeJobStore):
    """Records ``purge_older_than`` calls; delegates to the real behavior."""

    calls: list[datetime] = field(default_factory=list[datetime])

    def purge_older_than(self, cutoff: datetime) -> int:
        self.calls.append(cutoff)
        return super().purge_older_than(cutoff)


@dataclass
class FlakyPurgeStore(FakeJobStore):
    """``purge_older_than`` raises once, then behaves normally — a stand-in
    for a transient SQLite error in the loop's error-survival test."""

    calls: int = 0

    def purge_older_than(self, cutoff: datetime) -> int:
        self.calls += 1
        if self.calls == 1:
            msg = "transient failure"
            raise RuntimeError(msg)
        return super().purge_older_than(cutoff)


async def _wait_for(predicate: Callable[[], bool]) -> None:
    """Poll ``predicate`` every tick, bounded so a stuck loop fails fast
    instead of hanging the suite — no real sleeps involved."""

    async def _poll() -> None:
        while not predicate():  # noqa: ASYNC110 - test polling, not prod code
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=5)


class TestPurgeLoop:
    async def test_purges_immediately_then_cancellation_propagates(self) -> None:
        """First purge happens before any sleep; cancelling the sleeping
        loop propagates CancelledError (not swallowed by its except)."""
        store = RecordingPurgeStore()
        task = asyncio.create_task(
            purge_loop(store, ttl_seconds=3600, interval_seconds=3600)
        )
        await _wait_for(lambda: len(store.calls) >= 1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    async def test_survives_store_error_and_keeps_running(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store = FlakyPurgeStore()
        task = asyncio.create_task(
            purge_loop(store, ttl_seconds=3600, interval_seconds=0)
        )
        await _wait_for(lambda: store.calls >= 2)
        assert not task.done()
        assert "job_purge_error" in capsys.readouterr().out

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

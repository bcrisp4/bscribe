"""Tests for bscribe.metrics — the pull collector and push handle.

These read the registry directly (``get_sample_value`` / ``generate_latest``);
no exposition server is started — that is the app lifespan's job, exercised in
``test_app.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, generate_latest

from bscribe.domain.jobs import create_job
from bscribe.domain.models import OcrMode, OutputFormat, PipelineStamp
from bscribe.metrics import NoopMetrics, build_metrics
from tests.unit.fakes import CANNED_PIPELINE_STAMP, FakeJobStore, GatedPool

if TYPE_CHECKING:
    from bscribe.domain.models import Job


def _queued(token_id: str = "a1b2c3d4") -> Job:
    return create_job(token_id=token_id, output=OutputFormat.MARKDOWN, ocr=OcrMode.AUTO)


def _build(
    *,
    store: FakeJobStore | None = None,
    pool: GatedPool | None = None,
    pipeline_info: PipelineStamp = CANNED_PIPELINE_STAMP,
) -> tuple[CollectorRegistry, FakeJobStore]:
    store = store if store is not None else FakeJobStore()
    registry = CollectorRegistry()
    build_metrics(
        registry,
        job_store=store,
        get_worker_pool=lambda: pool,
        pipeline_info=pipeline_info,
    )
    return registry, store


class TestJobStateMetrics:
    def test_jobs_gauge_counts_each_state(self) -> None:
        store = FakeJobStore()
        for _ in range(3):
            store.add(_queued())  # create_job mints unique ids
        running = _queued()
        store.add(running)
        store.mark_running(running.id)
        registry, _ = _build(store=store)

        assert registry.get_sample_value("bscribe_jobs", {"state": "queued"}) == 3
        assert registry.get_sample_value("bscribe_jobs", {"state": "running"}) == 1
        # States with no jobs are still present (zero-filled), not missing.
        assert registry.get_sample_value("bscribe_jobs", {"state": "done"}) == 0
        assert registry.get_sample_value("bscribe_jobs", {"state": "failed"}) == 0

    def test_queue_depth_is_the_queued_count(self) -> None:
        store = FakeJobStore()
        store.add(_queued())
        store.add(_queued())
        registry, _ = _build(store=store)

        assert registry.get_sample_value("bscribe_queue_depth") == 2

    def test_reflects_live_state_at_scrape(self) -> None:
        """The collector reads the store per scrape — no cached snapshot."""
        registry, store = _build()
        assert registry.get_sample_value("bscribe_queue_depth") == 0
        store.add(_queued())
        assert registry.get_sample_value("bscribe_queue_depth") == 1


class TestWorkerMetrics:
    def test_counters_read_pool_metrics(self) -> None:
        pool = GatedPool()
        pool.metrics.timeout_kills = 2
        pool.metrics.crashes = 1
        pool.metrics.cancellations = 4
        pool.metrics.pool_rebuilds = 3
        registry, _ = _build(pool=pool)

        assert registry.get_sample_value("bscribe_worker_timeout_kills_total") == 2
        assert registry.get_sample_value("bscribe_worker_crashes_total") == 1
        assert registry.get_sample_value("bscribe_worker_cancellations_total") == 4
        assert registry.get_sample_value("bscribe_worker_pool_rebuilds_total") == 3

    def test_counters_zeroed_when_pool_absent(self) -> None:
        """Before the lifespan builds the pool a scrape reports zeros."""
        registry, _ = _build(pool=None)
        assert registry.get_sample_value("bscribe_worker_crashes_total") == 0


class TestBuildInfo:
    def test_build_info_carries_fingerprint_and_versions(self) -> None:
        stamp = PipelineStamp(
            fingerprint="abc123def456",
            components={"bscribe": "1.2.3", "liteparse": "2.5.0"},
        )
        registry, _ = _build(pipeline_info=stamp)

        value = registry.get_sample_value(
            "bscribe_build_info",
            {"fingerprint": "abc123def456", "bscribe": "1.2.3", "liteparse": "2.5.0"},
        )
        assert value == 1.0


class TestJobDurationHistogram:
    def test_observe_job_records_a_sample(self) -> None:
        registry = CollectorRegistry()
        metrics = build_metrics(
            registry,
            job_store=FakeJobStore(),
            get_worker_pool=lambda: None,
            pipeline_info=CANNED_PIPELINE_STAMP,
        )
        metrics.observe_job(1.5)
        metrics.observe_job(0.25)

        assert registry.get_sample_value("bscribe_job_duration_seconds_count") == 2
        assert registry.get_sample_value("bscribe_job_duration_seconds_sum") == 1.75


class TestStdlibCollectors:
    def test_runtime_metrics_registered(self) -> None:
        registry, _ = _build()
        output = generate_latest(registry).decode()
        # python_info (PlatformCollector) and GC metrics are cross-platform;
        # process_* is Linux-only, so it is not asserted here.
        assert "python_info" in output
        assert "python_gc_objects_collected_total" in output


class TestNoopMetrics:
    def test_observe_job_is_a_noop(self) -> None:
        NoopMetrics().observe_job(9.9)  # must not raise or register anything

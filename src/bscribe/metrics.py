"""Prometheus metrics — collectors and the scrape-time state reader.

Exposition happens on a **separate HTTP server** (prometheus-client's
``start_http_server``), started in the app lifespan on its own configurable
port — not a route on the FastAPI app (docs/design.md — Monitoring). This
module owns only the metric definitions:

* HTTP request metrics are owned by ``prometheus-fastapi-instrumentator``
  (wired in :func:`bscribe.app.create_app`), not here.
* ``bscribe_job_duration_seconds`` is a push histogram, fed at parse
  completion via :meth:`Metrics.observe_job` (the worker pool calls it).
* Jobs-by-state, queue depth, worker-pool health counters and the build/
  pipeline info metric are *pull* metrics — read live at scrape time by
  :class:`_StateCollector`, so they always reflect current state without a
  background updater.

Everything registers into one per-app :class:`CollectorRegistry` (passed in
by the factory), never the global default — unit tests build many apps and a
shared global registry would double-register and raise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from prometheus_client import (
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
)
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

from bscribe.domain.models import JobStatus

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from prometheus_client import CollectorRegistry
    from prometheus_client.core import Metric

    from bscribe.domain.models import PipelineStamp
    from bscribe.domain.ports import JobStorePort
    from bscribe.workers import WorkerPoolMetrics


class _MetricsSource(Protocol):
    """The slice of the worker pool the collector reads — just its counters.

    Structural so the real :class:`~bscribe.workers.WorkerPool` and test fakes
    both satisfy it without this module depending on the concrete pool.
    """

    @property
    def metrics(self) -> WorkerPoolMetrics: ...


# Parse latency spans well past prometheus-client's 10s default top bucket
# (a large scanned PDF under OCR can take minutes), so the histogram uses
# explicit buckets from sub-second up to 600s — the default
# job_timeout_seconds ceiling — keeping latency near the timeout resolvable
# rather than collapsed into +Inf.
_JOB_DURATION_BUCKETS = (
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
)


class Metrics:
    """Handle for the push metrics fed by application code.

    Held on ``app.state.metrics``; the pull metrics need no handle (the
    registered :class:`_StateCollector` reads them at scrape time).
    """

    def __init__(self, registry: CollectorRegistry) -> None:
        self._job_duration = Histogram(
            "bscribe_job_duration_seconds",
            "Document parse wall-clock duration in seconds.",
            buckets=_JOB_DURATION_BUCKETS,
            registry=registry,
        )

    def observe_job(self, seconds: float) -> None:
        """Record one completed parse's wall-clock duration."""
        self._job_duration.observe(seconds)


class NoopMetrics:
    """No-op stand-in used when metrics are disabled.

    Keeps the parse-completion call site (:class:`~bscribe.workers.WorkerPool`)
    branch-free: it always calls ``observe_job``; with metrics off that is a
    no-op and nothing is registered or served.
    """

    def observe_job(self, seconds: float) -> None:
        """Accept and discard a duration — metrics are off."""
        del seconds


class _StateCollector(Collector):
    """Yields the pull metrics (jobs, queue depth, worker health, build info).

    Reads live state at every scrape: the job store's per-status counts, the
    worker pool's failure counters, and the app-wide pipeline stamp. The pool
    getter tolerates ``None`` — the pool is built in the lifespan, so a scrape
    before startup (or a lifespan-less test) simply reports zeroed counters.
    """

    # (metric name, help, WorkerPoolMetrics field) for the health counters,
    # iterated in collect() so a fifth counter (e.g. #12 recycles) is one row
    # here, not a fifth near-identical yield.
    _WORKER_METRICS = (
        (
            "bscribe_worker_timeout_kills",
            "Parse workers killed for exceeding the per-job timeout.",
            "timeout_kills",
        ),
        (
            "bscribe_worker_crashes",
            "Parse workers that died mid-parse (crash or broken pool).",
            "crashes",
        ),
        (
            "bscribe_worker_cancellations",
            "Parse jobs cancelled (client cancel or shutdown).",
            "cancellations",
        ),
        (
            "bscribe_worker_pool_rebuilds",
            "Times the worker pool was rebuilt after breaking.",
            "pool_rebuilds",
        ),
    )

    def __init__(
        self,
        *,
        job_store: JobStorePort,
        get_worker_pool: Callable[[], _MetricsSource | None],
        pipeline_info: PipelineStamp,
    ) -> None:
        self._job_store = job_store
        self._get_worker_pool = get_worker_pool
        # Build info is constant — pipeline_info is a frozen stamp fixed at
        # factory time — so build the family once rather than per scrape.
        self._build_info = _build_info_family(pipeline_info)

    def collect(self) -> Iterable[Metric]:
        yield from self._job_metrics()
        pool = self._get_worker_pool()
        for name, doc, field in self._WORKER_METRICS:
            value = getattr(pool.metrics, field) if pool is not None else 0
            yield CounterMetricFamily(name, doc, value=value)
        yield self._build_info

    def _job_metrics(self) -> Iterable[Metric]:
        counts = self._job_store.count_by_status()
        jobs = GaugeMetricFamily(
            "bscribe_jobs",
            "Number of jobs currently in each lifecycle state.",
            labels=["state"],
        )
        for status in JobStatus:
            jobs.add_metric([status.value], counts.get(status, 0))
        yield jobs
        yield GaugeMetricFamily(
            "bscribe_queue_depth",
            "Jobs queued and awaiting a worker.",
            value=counts.get(JobStatus.QUEUED, 0),
        )


def _build_info_family(pipeline_info: PipelineStamp) -> GaugeMetricFamily:
    """Build the static ``bscribe_build_info`` gauge (fingerprint + versions)."""
    components = pipeline_info.components
    keys = sorted(components)
    family = GaugeMetricFamily(
        "bscribe_build_info",
        "Pipeline identity: fingerprint and component versions (always 1).",
        labels=["fingerprint", *keys],
    )
    family.add_metric([pipeline_info.fingerprint, *(components[k] for k in keys)], 1.0)
    return family


def build_metrics(
    registry: CollectorRegistry,
    *,
    job_store: JobStorePort,
    get_worker_pool: Callable[[], _MetricsSource | None],
    pipeline_info: PipelineStamp,
) -> Metrics:
    """Wire every bscribe metric into ``registry`` and return the push handle.

    Registers the job-duration histogram, the pull-metric collector, and the
    three prometheus-client stdlib collectors (process CPU/mem/fds, GC,
    ``python_info``). The stdlib process metrics cover only this parent
    process — the pebble parse workers are separate PIDs (see issue for
    per-worker metrics) — and are a no-op off Linux (they read ``/proc``).

    Args:
        registry: The per-app registry the metrics HTTP server exposes.
        job_store: Read for jobs-by-state and queue depth at scrape time.
        get_worker_pool: Returns the live pool (or ``None`` before the
            lifespan builds it) for the worker health counters.
        pipeline_info: The app-wide stamp behind ``bscribe_build_info``.

    Returns:
        The :class:`Metrics` handle for push observations.
    """
    ProcessCollector(registry=registry)
    PlatformCollector(registry=registry)
    GCCollector(registry=registry)
    registry.register(
        _StateCollector(
            job_store=job_store,
            get_worker_pool=get_worker_pool,
            pipeline_info=pipeline_info,
        )
    )
    return Metrics(registry)

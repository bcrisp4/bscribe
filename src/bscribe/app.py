"""bscribe FastAPI application factory."""

from __future__ import annotations

import asyncio
import importlib.metadata
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

import structlog
from fastapi import FastAPI, Request, Response
from prometheus_client import CollectorRegistry, start_http_server
from prometheus_fastapi_instrumentator import Instrumentator

from bscribe import maintenance
from bscribe.adapters.sqlite import SqliteJobStore, SqliteTokenStore
from bscribe.api import v1_router
from bscribe.errors import (
    UPLOAD_TOO_LARGE_DETAIL,
    problem_response,
    register_error_handlers,
)
from bscribe.log import configure_logging
from bscribe.metrics import NoopMetrics, build_metrics
from bscribe.pipeline import discover_pipeline
from bscribe.runner import JobRunner
from bscribe.settings import Settings
from bscribe.workers import WorkerPool

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from bscribe.domain import PipelineStamp

logger = structlog.get_logger()

# Slack above max_upload_bytes for the Content-Length prefilter: the
# multipart envelope (boundaries + part headers) inflates the body past the
# file size, so a bare `> max` threshold would false-reject a legitimate
# max-size upload. The streaming counter in spool_upload is the authoritative
# limit; this prefilter only rejects egregious bodies before receipt.
MULTIPART_OVERHEAD_SLACK = 1 << 20

# One-line service summary shown at the top of the OpenAPI docs.
_API_SUMMARY = "Convert documents (PDF, office formats, images) to text or markdown."

# The app-level OpenAPI description. Markdown, rendered at the top of /docs and
# carried in /openapi.json — written so a caller (human or agent) can use the
# API from this text alone: what it does, how to authenticate, the error
# format, and the endpoint map. Kept in step with docs/design.md.
_API_DESCRIPTION = """\
**bscribe** is a self-hosted service that converts documents — PDFs, office
formats (Word/Excel/PowerPoint, OpenDocument, RTF, CSV, iWork), and images
(JPG/PNG/GIF/BMP/TIFF/WebP/SVG) — into plain **text** or **markdown**.

## Authentication

Every `/v1` endpoint requires a **bearer token**:

```
Authorization: Bearer <token>
```

Tokens are provisioned out-of-band by the operator's local admin CLI, never
over HTTP. A missing or invalid token returns `401`. In this page, click
**Authorize** and paste your token to try the endpoints. Only `/healthz`
(liveness) is unauthenticated.

## Using the API

* **`POST /v1/convert`** — synchronous: upload a document, get the extracted
  text back inline. Best for interactive or small documents.
* **`POST /v1/jobs`** — asynchronous: same parameters, returns a job id
  immediately; poll `GET /v1/jobs/{id}/result` for the result. Use this for
  OCR-heavy or large documents that may exceed request timeouts.
* **`GET /v1/info`** — the current pipeline fingerprint and component
  versions, so a caller can detect whether the conversion pipeline changed
  without submitting a document.

Conversion parameters (both convert endpoints): `file` (multipart, required),
`output` = `markdown` (default) or `text`, and `ocr` = `auto` (default) or
`off`.

## Errors

All error responses are RFC 9457 `application/problem+json`:

```json
{ "type": "about:blank", "title": "Unauthorized", "status": 401,
  "detail": "Invalid or missing bearer token" }
```

Detail strings come from a fixed vocabulary and never echo document content.
Each operation below lists the status codes it can return.
"""


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a mapping, else an empty one.

    Walking a freshly built OpenAPI schema means threading through ``Any``;
    this keeps each hop typed as ``dict[str, Any]`` (so member access stays
    known) and coerces non-mapping nodes to ``{}``. A real mapping is
    returned as-is (not copied), so mutating the result mutates the schema.
    """
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _prune_default_validation_responses(schema: dict[str, Any]) -> None:
    """Drop FastAPI's auto-injected ``422`` validation responses.

    bscribe remaps request-validation failures to ``400`` problem+json
    (:mod:`bscribe.errors`), so the framework-default ``422`` /
    ``HTTPValidationError`` that FastAPI attaches to every parameterised
    route misrepresents the contract — ``docs/design.md`` reserves ``422``
    for "supported format, document unparseable". Remove any ``422`` still
    bodied by ``HTTPValidationError`` (the routes that genuinely return
    ``422`` document it explicitly with :class:`~bscribe.api.responses.Problem`,
    which is left untouched), then drop the now-orphaned validation schemas.

    Idempotent: run against an already-pruned schema, it finds nothing.
    """
    validation_ref = "#/components/schemas/HTTPValidationError"
    # A path item also holds non-operation keys (a `parameters` list, a
    # `summary` string); _as_dict coerces those to an empty mapping so the
    # walk skips them without per-key isinstance narrowing.
    for methods in _as_dict(schema.get("paths")).values():
        for operation in _as_dict(methods).values():
            responses = _as_dict(_as_dict(operation).get("responses"))
            entry = _as_dict(responses.get("422"))
            body = _as_dict(_as_dict(entry.get("content")).get("application/json")).get(
                "schema"
            )
            if _as_dict(body).get("$ref") == validation_ref:
                responses.pop("422", None)
    schemas = _as_dict(_as_dict(schema.get("components")).get("schemas"))
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)


def _relabel_problem_media_type(schema: dict[str, Any]) -> None:
    """Advertise Problem error bodies as ``application/problem+json``.

    FastAPI files a ``model=``-documented response under ``application/json``,
    but the runtime error handler emits RFC 9457 ``application/problem+json``
    (:data:`bscribe.errors.PROBLEM_JSON_MEDIA_TYPE`). Move each ``Problem``-
    bodied response to the correct media type so a generated client infers
    the right ``Content-Type``. Success bodies (``200``/``202``, plain
    ``application/json``) reference other schemas and are left untouched.

    Idempotent: once moved, the ``application/json`` key is gone, so a second
    pass matches nothing.
    """
    problem_ref = "#/components/schemas/Problem"
    for methods in _as_dict(schema.get("paths")).values():
        for operation in _as_dict(methods).values():
            for response in _as_dict(_as_dict(operation).get("responses")).values():
                content = _as_dict(_as_dict(response).get("content"))
                json_body = _as_dict(content.get("application/json"))
                if _as_dict(json_body.get("schema")).get("$ref") == problem_ref:
                    content["application/problem+json"] = content.pop(
                        "application/json"
                    )


def _install_openapi(app: FastAPI) -> None:
    """Wrap ``app.openapi`` to post-process the generated schema.

    FastAPI builds and caches the schema on first access; we call the
    original, prune the misleading default validation responses and correct
    the error media type in place (the cached dict), and return it.
    """
    original = app.openapi

    def openapi() -> dict[str, Any]:
        schema = original()
        _prune_default_validation_responses(schema)
        _relabel_problem_media_type(schema)
        return schema

    # Reassigning app.openapi is FastAPI's documented customization hook;
    # mypy flags method assignment, pyright does not.
    app.openapi = openapi  # type: ignore[method-assign]


def _package_version() -> str:
    """Return the installed bscribe version for the OpenAPI ``info.version``.

    Read from installed distribution metadata (setuptools-scm stamps it from
    the git tag) rather than FastAPI's default ``0.1.0``. Falls back to
    ``0.0.0`` if the distribution metadata is somehow absent (e.g. running
    from an unbuilt tree) so doc generation never fails on this.
    """
    try:
        return importlib.metadata.version("bscribe")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


# Per-tag descriptions, surfaced as section headers in the docs. Tags match the
# `tags=` on each router (convert / jobs / info).
_OPENAPI_TAGS = [
    {"name": "convert", "description": "Synchronous, inline document conversion."},
    {
        "name": "jobs",
        "description": "Asynchronous conversion jobs — submit, poll, cancel. "
        "Every job is scoped to the token that created it.",
    },
    {
        "name": "info",
        "description": "Pipeline identity for the re-ingestion contract.",
    },
]


def create_app(
    settings: Settings | None = None, pipeline_info: PipelineStamp | None = None
) -> FastAPI:
    """Build and return the bscribe FastAPI application.

    Args:
        settings: Application configuration; ``None`` loads it from
            ``BSCRIBE_``-prefixed environment variables.
        pipeline_info: The app-wide pipeline fingerprint/component stamp;
            ``None`` runs real discovery (:func:`bscribe.pipeline.discover_pipeline`
            — cached process-wide, so repeated calls in one process are
            cheap after the first). Tests pass a canned stamp to avoid the
            real subprocess probes.

    Returns:
        A configured FastAPI instance exposing ``/healthz`` and the
        path-versioned ``/v1`` API (``POST /v1/convert`` plus the async
        ``/v1/jobs`` endpoints).
    """
    if settings is None:
        settings = Settings()
    # Probes inside discover_pipeline may warn on failure, so logging must
    # be configured before discovery runs — otherwise those warnings emit
    # with unconfigured structlog (not JSON).
    configure_logging(settings.log_level)
    if pipeline_info is None:
        pipeline_info = discover_pipeline()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Own the startup sweep, the worker pool, and the periodic purge
        task.

        The store is read from ``app.state`` here — not captured at
        factory time — so a store swapped in after ``create_app`` (as
        tests do) is the one actually swept and purged; see the
        factory-time comments below on why ``job_store`` lives on
        ``app.state`` at all. The sweep runs before the pool exists and
        before any request is served, so it can never race a freshly
        submitted job (docs/design.md — Startup sweep). pebble spawns
        workers lazily on the first job, so pool startup stays cheap;
        shutdown kills any running workers and stops the purge task.
        """
        store = app.state.job_store
        await asyncio.to_thread(maintenance.startup_sweep, store, settings.scratch_dir)
        pool = WorkerPool(
            worker_count=settings.worker_count,
            job_timeout_seconds=float(settings.job_timeout_seconds),
            worker_max_tasks=settings.worker_max_tasks,
            pipeline_info=app.state.pipeline_info,
            job_observer=app.state.metrics.observe_job,
        )
        app.state.worker_pool = pool
        app.state.purge_task = asyncio.create_task(
            maintenance.purge_loop(
                store,
                ttl_seconds=settings.result_ttl_seconds,
                interval_seconds=settings.purge_interval_seconds,
            ),
            name="bscribe-job-purge",
        )
        # Expose metrics on their own port via prometheus-client's background
        # WSGI server (a daemon thread), not a route on this app — the scrape
        # surface stays off the API port (docs/design.md — Monitoring). The
        # server reads the per-app registry built at factory time. Started
        # inside the try so a bind failure (e.g. port in use) still runs the
        # finally teardown below — the purge task and pool already exist.
        metrics_server = None
        try:
            if settings.metrics_enabled:
                metrics_server, _ = start_http_server(
                    settings.metrics_port,
                    addr=settings.metrics_addr,
                    registry=app.state.metrics_registry,
                )
            yield
        finally:
            if metrics_server is not None:
                # shutdown() stops the serve_forever loop but blocks on its
                # poll interval, so offload it like the pool teardown below;
                # server_close() then releases the listening socket (shutdown
                # alone leaks the bound port until process exit — a failed
                # rebind on any in-process restart).
                await asyncio.to_thread(metrics_server.shutdown)
                metrics_server.server_close()
            # Purge task first: cancellation stops any further iterations.
            # An iteration already inside to_thread cannot be interrupted
            # and may finish in the background — harmless: it only issues
            # the store's guarded DELETE (safe concurrently with anything,
            # as in normal serving), and executor threads are joined at
            # interpreter exit, so no write is ever torn by process death.
            app.state.purge_task.cancel()
            await asyncio.gather(app.state.purge_task, return_exceptions=True)
            # Runner next: cancelling its tasks kills their running
            # workers via the still-live pool, then the pool tears down —
            # unconditionally, so a runner teardown error can't leak
            # worker processes.
            try:
                await app.state.job_runner.aclose()
            finally:
                await pool.aclose()

    app = FastAPI(
        title="bscribe",
        summary=_API_SUMMARY,
        description=_API_DESCRIPTION,
        version=_package_version(),
        openapi_tags=_OPENAPI_TAGS,
        lifespan=lifespan,
    )
    app.state.settings = settings
    # Factory-time (not lifespan): construction is cheap, creates the schema
    # if missing, and tests can swap in a fake before serving a request.
    # Auth reads it per request via bscribe.auth.require_token.
    app.state.token_store = SqliteTokenStore(settings.db_path)
    # Same rationale; the job endpoints read it per request.
    app.state.job_store = SqliteJobStore(settings.db_path)
    # Factory-time too (the runner is loop-agnostic until its first submit),
    # so ASGITransport tests — which never run the lifespan — get a working
    # runner. It deliberately holds no store or pool: the submitting
    # endpoint passes the pair it resolved per request, so swapping either
    # on app.state can never split writes between two stores.
    app.state.job_runner = JobRunner()
    # Factory-time too, same rationale as token_store/job_store above: the
    # lifespan below builds the WorkerPool from whatever is on app.state,
    # and ASGITransport tests never run the lifespan, so the stamp has to
    # be here already for those tests' pools (constructed directly, not via
    # create_app) to have something to read.
    app.state.pipeline_info = pipeline_info
    # One INFO line per process at discovery time — version strings and a
    # fingerprint hash only, no document data, so this is privacy-safe at
    # INFO (see docs/design.md — Privacy).
    logger.info(
        "pipeline_discovered",
        fingerprint=pipeline_info.fingerprint,
        components=dict(pipeline_info.components),
    )
    # Ensure the upload scratch dir exists once here rather than on every
    # request. This mkdir stays for lifespan-less ASGITransport tests,
    # which never run the lifespan below; the lifespan's startup_sweep
    # (docs/design.md — Startup sweep) owns wiping it clean at boot.
    settings.scratch_dir.mkdir(parents=True, exist_ok=True)

    # Metrics: build the per-app registry (never the global default — many
    # apps per test process would double-register), the push handle, and HTTP
    # instrumentation, all when enabled. instrumentator owns the http_* metrics
    # (it resolves the route-template handler label); the exposition server is
    # started in the lifespan. Disabled → a no-op handle, no registry, no
    # instrumentation, no server (docs/design.md — Monitoring).
    if settings.metrics_enabled:
        registry = CollectorRegistry()
        app.state.metrics_registry = registry
        app.state.metrics = build_metrics(
            registry,
            job_store=app.state.job_store,
            get_worker_pool=lambda: getattr(app.state, "worker_pool", None),
            pipeline_info=pipeline_info,
        )
        Instrumentator(registry=registry).instrument(app)
    else:
        app.state.metrics = NoopMetrics()

    register_error_handlers(app)

    max_body_bytes = settings.max_upload_bytes + MULTIPART_OVERHEAD_SLACK

    @app.middleware("http")
    async def reject_oversized_body(  # pyright: ignore[reportUnusedFunction]
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Pre-receipt 413 when an *honest* Content-Length exceeds the cap.

        This runs before routing and therefore before auth, so an oversized
        unauthenticated upload is rejected here (413) rather than reaching the
        401 check — a deliberate don't-buffer-a-huge-body tradeoff. It only
        catches requests that declare an honest, oversized Content-Length;
        chunked or absent/spoofed headers slip past and are bounded (best
        effort at single-user scale) by spool_upload's copy-time counter (see
        docs/design.md — max upload size)."""
        declared = request.headers.get("content-length")
        too_big = (
            declared is not None
            and declared.isdigit()
            and int(declared) > max_body_bytes
        )
        if too_big:
            return problem_response(status=413, detail=UPLOAD_TOO_LARGE_DETAIL)
        return await call_next(request)

    # pyright strict flags decorator-registered nested handlers as unused
    # (reportUnusedFunction); the route registration is the real use.
    @app.middleware("http")
    async def access_log(  # pyright: ignore[reportUnusedFunction]
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """One INFO line per request. Path only — query strings may carry
        sensitive values (see docs/design.md — Privacy)."""
        start = time.perf_counter()
        response = await call_next(request)
        logger.info(
            "request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 3),
        )
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Liveness probe. No auth; safe for orchestrator health checks."""
        return {"status": "ok"}

    app.include_router(v1_router)

    _install_openapi(app)

    return app

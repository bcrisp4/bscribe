"""``/v1/jobs`` — asynchronous conversion jobs (docs/design.md — M2).

Submission mirrors ``POST /v1/convert`` exactly (same parameters, same
rejection ladder: 401 → 400 → 415 → 413, via the shared
:func:`bscribe.api.staging.stage_upload`) but returns ``201`` with a job id
immediately and hands the staged upload to :class:`bscribe.runner.JobRunner`,
which parses it on the same worker pool the sync path uses — one bound
governs total parse concurrency.

Every job endpoint after submission is token-scoped: another token's job
(or an unknown id) is a ``404`` raised from one place with one detail
string, so the two cases are indistinguishable by construction
(docs/design.md — Ownership). Status and list reads return metadata only;
the stored content is read exclusively by the result endpoint
(``JobStorePort``'s metadata/result split). ``DELETE`` cancels and purges
in any state.

The GET handlers are sync ``def`` on purpose: FastAPI runs them on the
threadpool, which is where the SQLite-backed store must be called from
(docs/adr/0002) — the same pattern as ``bscribe.auth.require_token``.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import structlog
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response

from bscribe.api.responses import error_responses
from bscribe.api.schemas import ConvertResponse, JobListResponse, JobResponse
from bscribe.api.staging import stage_upload
from bscribe.auth import require_token
from bscribe.domain.jobs import create_job

# Enums/Token appear only in annotations, but FastAPI resolves route
# annotations at runtime (get_type_hints), so they must be runtime imports.
from bscribe.domain.models import JobStatus, OcrMode, OutputFormat, Token
from bscribe.errors import JOB_FAILED_NO_RESULT_DETAIL

logger = structlog.get_logger()

router = APIRouter(prefix="/jobs", tags=["jobs"])

JSON_MEDIA_TYPE = "application/json"

# One definition site so unknown-id and cross-token 404s are byte-identical.
_JOB_NOT_FOUND_DETAIL = "job not found"


def _job_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=_JOB_NOT_FOUND_DETAIL)


@router.post(
    "",
    response_model=JobResponse,
    status_code=201,
    summary="Submit an async conversion job",
    responses=error_responses(400, 413, 415),
)
async def submit_job(
    request: Request,
    token: Annotated[Token, Depends(require_token)],
    file: Annotated[UploadFile, File()],
    output: Annotated[OutputFormat, Form()] = OutputFormat.MARKDOWN,
    ocr: Annotated[OcrMode, Form()] = OcrMode.AUTO,
) -> JobResponse:
    """Accept one uploaded document as an async job and return its id.

    Same parameters as `POST /v1/convert` (`file`, `output`, `ocr`) but
    returns `201` immediately with the job object (id, status, submission
    parameters, timestamps). The document is parsed on the shared worker
    pool; poll `GET /v1/jobs/{id}/result` for the outcome. A supported
    document that fails to parse becomes a `failed` job rather than a `422`.
    """
    settings = request.app.state.settings
    pool = request.app.state.worker_pool
    store = request.app.state.job_store
    runner = request.app.state.job_runner

    dest = await stage_upload(
        file, settings=settings, token=token, log_event="job_upload"
    )
    job = create_job(token_id=token.id, output=output, ocr=ocr)
    try:
        await asyncio.to_thread(store.add, job)
    finally:
        # Hand off unconditionally. The INSERT may have committed even when
        # this await was cancelled (a client disconnect cancels the handler,
        # but the to_thread worker is uninterruptible), so a conditional
        # handoff could orphan a committed queued row forever. If the row
        # never made it (add raised, or the cancel won the race), the task's
        # mark_running finds nothing and it just deletes the upload — the
        # runner task owns dest from this point on every path.
        runner.submit(
            job_id=job.id,
            path=dest,
            output=output,
            ocr=ocr,
            store=store,
            pool=pool,
        )
    logger.info("job_submitted", job_id=job.id, token_id=token.id)
    return JobResponse.from_job(job)


@router.get(
    "",
    response_model=JobListResponse,
    summary="List the token's jobs",
    responses=error_responses(400),
)
def list_jobs(
    request: Request,
    token: Annotated[Token, Depends(require_token)],
    status: Annotated[JobStatus | None, Query()] = None,
) -> JobListResponse:
    """List the calling token's jobs, newest first.

    Optional `?status=` filters by lifecycle state (`queued`, `running`,
    `done`, `failed`). Returns a `{"jobs": [...]}` wrapper (not a bare
    array) so pagination can be added later without a new API version. Only
    the caller's own jobs are ever listed.
    """
    store = request.app.state.job_store
    jobs = store.list_for_token(token.id, status=status)
    return JobListResponse(jobs=[JobResponse.from_job(job) for job in jobs])


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    summary="Get one job's status",
    responses=error_responses(404),
)
def get_job(
    request: Request,
    token: Annotated[Token, Depends(require_token)],
    job_id: str,
) -> JobResponse:
    """Return one job's status and metadata.

    `failure_detail` carries a fixed reason string on `failed` jobs. A
    job owned by another token is `404`, indistinguishable from an unknown
    id.
    """
    store = request.app.state.job_store
    job = store.get(job_id, token.id)
    if job is None:
        raise _job_not_found()
    return JobResponse.from_job(job)


@router.delete(
    "/{job_id}",
    status_code=204,
    summary="Cancel and purge a job",
    responses=error_responses(404),
)
async def delete_job(
    request: Request,
    token: Annotated[Token, Depends(require_token)],
    job_id: str,
) -> Response:
    """Cancel and purge a job in any state (docs/design.md — Endpoints).

    Returns `204` on success. For a `running` job the worker process is
    killed — real cancellation. An unknown or other-token id is `404`.

    The row (and any stored result) is purged before the response; the
    in-flight task, if any, is cancelled fire-and-forget — ``WorkerPool.parse``
    turns the cancellation into killing the running worker process, and the
    runner's cleanup deletes the scratch upload. Deleting the row first makes
    the task's late ``mark_*`` calls safe no-ops (the races the runner
    already handles), so no per-state branching is needed here.
    """
    store = request.app.state.job_store
    runner = request.app.state.job_runner
    if not await asyncio.to_thread(store.delete, job_id, token.id):
        raise _job_not_found()
    task = runner.task_for(job_id)
    # cancel() returns False for a task that already finished (one can linger
    # in the runner until its done-callback runs) — log what actually
    # happened, not mere task presence.
    cancelled = task is not None and task.cancel()
    logger.info("job_deleted", job_id=job_id, token_id=token.id, cancelled=cancelled)
    return Response(status_code=204)


@router.get(
    "/{job_id}/result",
    response_model=None,
    summary="Fetch a job's result",
    responses={
        200: {
            "model": ConvertResponse,
            "description": "Job is `done`; the converted document, same shape "
            "as `POST /v1/convert`.",
        },
        202: {
            "model": JobResponse,
            "description": "Job is still `queued`/`running`; body is the job "
            "object carrying its current status. Poll again.",
        },
        **error_responses(404, 409),
    },
)
def get_job_result(
    request: Request,
    token: Annotated[Token, Depends(require_token)],
    job_id: str,
) -> Response:
    """Fetch a job's converted result.

    * `200` — job is `done`: the result document, identical in shape to a
      `POST /v1/convert` response.
    * `202` — job is `queued`/`running`: the job object with its current
      status; poll again.
    * `409` — job `failed`: no result; read the reason from
      `GET /v1/jobs/{id}`.
    * `404` — unknown or other-token id.
    """
    store = request.app.state.job_store
    job = store.get(job_id, token.id)
    if job is None:
        raise _job_not_found()
    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        # Standard request-reply polling: 202 + current status, plain JSON
        # (not problem+json — an in-progress job is not an error).
        return Response(
            content=JobResponse.from_job(job).model_dump_json(),
            status_code=202,
            media_type=JSON_MEDIA_TYPE,
        )
    if job.status is JobStatus.FAILED:
        raise HTTPException(status_code=409, detail=JOB_FAILED_NO_RESULT_DETAIL)
    result = store.get_result(job_id, token.id)
    if result is None:
        # Deleted between the two reads; same 404 as never-existed.
        raise _job_not_found()
    # model_dump_json serializes once in pydantic-core; content may be large
    # (the whole extracted document), so skip the dict + json.dumps detour.
    return Response(
        content=ConvertResponse.from_result(job.output, result).model_dump_json(),
        media_type=JSON_MEDIA_TYPE,
    )

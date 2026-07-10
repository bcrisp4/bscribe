"""``POST /v1/convert`` — synchronous document conversion.

Stages the multipart upload to the scratch dir, dispatches it to the warm
worker pool, and returns the extracted text inline. The upload is deleted
as soon as parsing finishes, success or failure (docs/design.md — Data
retention). Once past the app's Content-Length prefilter (which may return
413 before this handler, and before auth, on an oversized declared body),
this handler applies 401 → 400 → 415 → 413 (copy-time cap) → 422/500, each
mapped to the status-code contract by the handlers in ``bscribe.errors``.
Note FastAPI has already received and spooled the body by the time these
checks run, so they gate processing, not receipt.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from bscribe.api.responses import error_responses
from bscribe.api.schemas import ConvertResponse
from bscribe.api.staging import stage_upload
from bscribe.auth import require_token

# Token appears only in an annotation, but FastAPI resolves route
# annotations at runtime (get_type_hints), so it must be a runtime import.
from bscribe.domain.models import OcrMode, OutputFormat, Token

router = APIRouter(tags=["convert"])


@router.post(
    "/convert",
    response_model=ConvertResponse,
    summary="Convert a document synchronously",
    responses=error_responses(400, 413, 415, 422),
)
async def convert(
    request: Request,
    token: Annotated[Token, Depends(require_token)],
    file: Annotated[UploadFile, File()],
    output: Annotated[OutputFormat, Form()] = OutputFormat.MARKDOWN,
    ocr: Annotated[OcrMode, Form()] = OcrMode.AUTO,
) -> ConvertResponse:
    """Convert one uploaded document and return the extracted text inline.

    Upload the document as multipart `file`; choose `output` (`markdown` or
    `text`) and `ocr` (`auto` or `off`). The response carries the converted
    `content` plus `metadata` (page count, duration, and the pipeline block
    the document traversed). The upload is deleted as soon as parsing
    finishes.

    Runs on the same bounded worker pool as async jobs, so a request waits
    for a free slot and then against the per-job timeout — send OCR-heavy or
    large documents to `POST /v1/jobs` instead.
    """
    settings = request.app.state.settings
    pool = request.app.state.worker_pool

    dest = await stage_upload(
        file, settings=settings, token=token, log_event="convert_upload"
    )
    try:
        result = await pool.parse(dest, output=output, ocr=ocr)
    finally:
        # Documents transit; delete on success and on every failure.
        dest.unlink(missing_ok=True)

    return ConvertResponse.from_result(output, result)

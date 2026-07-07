"""``POST /v1/convert`` — synchronous document conversion.

Stages the multipart upload to the scratch dir, dispatches it to the warm
worker pool, and returns the extracted text inline. The upload is deleted
as soon as parsing finishes, success or failure (docs/design.md — Data
retention). Validation order is 401 → 400 → 415 → 413 → 422/500; each maps
to the status-code contract via the handlers in ``bscribe.errors``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from bscribe.api.schemas import ConvertMetadata, ConvertResponse
from bscribe.auth import require_token
from bscribe.domain.formats import supported_extension

# Token appears only in an annotation, but FastAPI resolves route
# annotations at runtime (get_type_hints), so it must be a runtime import.
from bscribe.domain.models import OcrMode, OutputFormat, Token
from bscribe.uploads import spool_upload

logger = structlog.get_logger()

router = APIRouter(tags=["convert"])


@router.post("/convert", response_model=ConvertResponse)
async def convert(
    request: Request,
    token: Annotated[Token, Depends(require_token)],
    file: Annotated[UploadFile, File()],
    output: Annotated[OutputFormat, Form()] = OutputFormat.MARKDOWN,
    ocr: Annotated[OcrMode, Form()] = OcrMode.AUTO,
) -> ConvertResponse:
    """Convert one uploaded document and return the result inline."""
    settings = request.app.state.settings
    pool = request.app.state.worker_pool

    # 415 before any disk work: liteparse routes by extension, so an
    # unsupported one would otherwise surface as a generic parse failure.
    ext = supported_extension(file.filename)
    settings.scratch_dir.mkdir(parents=True, exist_ok=True)
    # Random name, extension preserved (liteparse dispatches on it); the
    # caller's filename never lands in the on-disk path.
    dest = settings.scratch_dir / f"{uuid4().hex}{ext}"
    # Filename only at DEBUG (Privacy); the token label attributes the request.
    logger.debug("convert_upload", filename=file.filename, token_id=token.id)
    try:
        await spool_upload(file, dest=dest, max_bytes=settings.max_upload_bytes)
        result = await pool.parse(dest, output=output, ocr=ocr)
    finally:
        # Documents transit; delete on success and on every failure.
        dest.unlink(missing_ok=True)

    return ConvertResponse(
        output=output,
        content=result.content,
        metadata=ConvertMetadata(
            pages=result.pages, duration_ms=round(result.duration_ms)
        ),
    )

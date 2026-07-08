"""Shared upload staging for the conversion endpoints.

One definition of the staging ladder both ``POST /v1/convert`` and
``POST /v1/jobs`` promise to share (docs/design.md — same parameters, same
rejections). It encodes three load-bearing rules in one place: the 415
format gate runs before anything touches disk; the caller's filename never
lands in the on-disk path (random name, extension preserved — liteparse
dispatches on it); and the filename is logged at DEBUG only, attributed by
token id + label (docs/design.md — Privacy, Security).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import structlog

from bscribe.domain.formats import supported_extension
from bscribe.uploads import spool_upload

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import UploadFile

    from bscribe.domain.models import Token
    from bscribe.settings import Settings

logger = structlog.get_logger()


async def stage_upload(
    file: UploadFile, *, settings: Settings, token: Token, log_event: str
) -> Path:
    """Stage one multipart upload into the scratch dir.

    Args:
        file: The uploaded file to stage.
        settings: Supplies the scratch dir and the upload size cap.
        token: The authenticated caller, for the DEBUG attribution log.
        log_event: Per-endpoint event name for that log line.

    Returns:
        The staged file's path. Ownership transfers to the caller, who
        must delete it when processing finishes (success or failure).

    Raises:
        UnsupportedFormatError: Unrecognized extension (→ 415); nothing
            was written.
        UploadTooLargeError: Upload exceeded the size cap (→ 413); the
            partial file has already been deleted.
    """
    ext = supported_extension(file.filename)
    dest = settings.scratch_dir / f"{uuid4().hex}{ext}"
    logger.debug(
        log_event,
        filename=file.filename,
        token_id=token.id,
        token_label=token.label,
    )
    try:
        await spool_upload(file, dest=dest, max_bytes=settings.max_upload_bytes)
    except BaseException:
        # The staged path has not been handed to the caller yet, so a
        # partial file (oversize abort, disconnect) is cleaned up here.
        dest.unlink(missing_ok=True)
        raise
    return dest

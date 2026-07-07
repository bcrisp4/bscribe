"""Job creation.

One definition site for job identity policy, mirroring
:mod:`bscribe.domain.tokens`: every submission path (HTTP now, anything
later) mints jobs here, so id format and initial state never diverge.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bscribe.domain.models import Job, JobStatus

if TYPE_CHECKING:
    from bscribe.domain.models import OcrMode, OutputFormat


def create_job(*, token_id: str, output: OutputFormat, ocr: OcrMode) -> Job:
    """Mint a freshly queued job for a submission.

    Ids are 64-bit random hex (twice the tokens' 32 bits — jobs are minted
    far more often over the service lifetime, and collisions here would
    surface as spurious INSERT failures).

    Args:
        token_id: Id of the bearer token submitting the job — the
            ownership stamp every job endpoint scopes to.
        output: Requested output format.
        ocr: Requested OCR mode.

    Returns:
        A queued job with a fresh id and UTC-aware ``created_at``; all
        lifecycle fields (``started_at`` etc.) unset.
    """
    return Job(
        id=secrets.token_hex(8),
        token_id=token_id,
        output=output,
        ocr=ocr,
        status=JobStatus.QUEUED,
        created_at=datetime.now(tz=UTC),
    )

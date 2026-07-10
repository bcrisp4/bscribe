"""``GET /v1/info`` — service/pipeline identity.

Returns the current ``pipeline_fingerprint`` and every pipeline component's
version, letting a caller ask "has the pipeline changed?" without submitting
a document (docs/design.md — Re-ingestion contract). The body is the same
:class:`bscribe.api.schemas.PipelineBlock` shape as a result's
``metadata.pipeline``, so a caller compares a stored block against this
endpoint field-for-field.

Token-scoped like every other ``/v1`` route: the pipeline identity is the
stability contract bsearch connectors hold a token to build against; only
``/healthz`` and ``/metrics`` are open. The handler is a sync ``def`` — it
reads factory-time app state and does no I/O.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from bscribe.api.schemas import PipelineBlock
from bscribe.auth import require_token

router = APIRouter(tags=["info"])


# Auth-only dependency: the endpoint gates on a valid token but does not need
# the principal (nothing here is token-scoped), so ``require_token`` runs via
# ``dependencies`` rather than binding an unused parameter.
@router.get(
    "/info", response_model=PipelineBlock, dependencies=[Depends(require_token)]
)
def info(request: Request) -> PipelineBlock:
    """Return the current pipeline fingerprint and all component versions."""
    return PipelineBlock.from_stamp(request.app.state.pipeline_info)

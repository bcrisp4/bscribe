"""bscribe HTTP API — the path-versioned ``/v1`` router.

Routes live in per-resource modules (``convert``, ``jobs``) and mount under
a single version prefix. Breaking changes require a ``/v2`` router;
additive changes do not (docs/design.md — API contract).
"""

from __future__ import annotations

from fastapi import APIRouter

from bscribe.api import convert, info, jobs
from bscribe.api.responses import error_responses

# Every /v1 route is token-scoped, so 401 is documented once here rather than
# on each operation; per-operation `responses=` add the codes specific to
# that route (docs/design.md — status-code table).
v1_router = APIRouter(prefix="/v1", responses=error_responses(401))
v1_router.include_router(convert.router)
v1_router.include_router(info.router)
v1_router.include_router(jobs.router)

__all__ = ["v1_router"]

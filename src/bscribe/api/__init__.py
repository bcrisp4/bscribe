"""bscribe HTTP API — the path-versioned ``/v1`` router.

Routes live in per-resource modules (``convert``, ``jobs``) and mount under
a single version prefix. Breaking changes require a ``/v2`` router;
additive changes do not (docs/design.md — API contract).
"""

from __future__ import annotations

from fastapi import APIRouter

from bscribe.api import convert, info, jobs
from bscribe.api.responses import error_responses

# Two failures are universal across /v1, so they are documented once here
# rather than on each operation: 401 (every route is token-scoped) and 500
# (the app-wide catch-all Exception handler in bscribe.errors emits an RFC
# 9457 body for any unexpected error). Per-operation `responses=` add the
# codes specific to that route (docs/design.md — status-code table).
v1_router = APIRouter(prefix="/v1", responses=error_responses(401, 500))
v1_router.include_router(convert.router)
v1_router.include_router(info.router)
v1_router.include_router(jobs.router)

__all__ = ["v1_router"]

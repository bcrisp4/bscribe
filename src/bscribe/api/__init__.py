"""bscribe HTTP API — the path-versioned ``/v1`` router.

Routes live in per-resource modules (``convert`` now; ``jobs`` from M2) and
mount under a single version prefix. Breaking changes require a ``/v2``
router; additive changes do not (docs/design.md — API contract).
"""

from __future__ import annotations

from fastapi import APIRouter

from bscribe.api import convert

v1_router = APIRouter(prefix="/v1")
v1_router.include_router(convert.router)

__all__ = ["v1_router"]

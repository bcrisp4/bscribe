"""Tests for the bscribe application factory."""

from __future__ import annotations

import httpx
from httpx import ASGITransport

from bscribe.app import create_app


async def test_healthz_returns_ok() -> None:
    """The liveness probe returns 200 with a stable body."""
    transport = ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

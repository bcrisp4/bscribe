"""Tests for the GET /v1/info endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from httpx import ASGITransport

from bscribe.app import create_app
from bscribe.domain.tokens import mint_token
from bscribe.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI


def make_app(tmp_path: Path) -> FastAPI:
    # pipeline_info=None → factory runs discovery, which the autouse conftest
    # fixture patches to CANNED_PIPELINE_STAMP.
    settings = Settings(
        db_path=tmp_path / "tokens.db", scratch_dir=tmp_path / "scratch"
    )
    return create_app(settings)


def make_client(app: FastAPI) -> httpx.AsyncClient:
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def issue_token(app: FastAPI, label: str = "bsearch") -> str:
    token, secret = mint_token(label)
    app.state.token_store.add(token)
    return secret


class TestInfo:
    async def test_returns_fingerprint_and_components(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)
        secret = issue_token(app)
        async with make_client(app) as client:
            response = await client.get(
                "/v1/info", headers={"Authorization": f"Bearer {secret}"}
            )
        assert response.status_code == 200
        # Bare block, identical shape to a result's metadata.pipeline.
        assert response.json() == {
            "fingerprint": "fakefinger12",
            "components": {
                "bscribe": "0.0.0-test",
                "liteparse": "0.0.0-test",
            },
        }

    async def test_missing_token_is_401(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)
        issue_token(app)
        async with make_client(app) as client:
            response = await client.get("/v1/info")
        assert response.status_code == 401

    async def test_bad_token_is_401(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)
        issue_token(app)
        async with make_client(app) as client:
            response = await client.get(
                "/v1/info", headers={"Authorization": "Bearer nope"}
            )
        assert response.status_code == 401

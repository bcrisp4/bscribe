"""Tests for bscribe.auth."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import httpx
import pytest
import structlog
from fastapi import Depends
from httpx import ASGITransport

from bscribe.app import create_app
from bscribe.auth import require_token

# FastAPI resolves route annotations at runtime (get_type_hints), so Token
# must stay a real import despite only appearing in annotations.
from bscribe.domain.models import Token  # noqa: TC001
from bscribe.domain.tokens import mint_token
from bscribe.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi import FastAPI


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """create_app configures process-global structlog; undo it per test."""
    yield
    structlog.reset_defaults()


def make_protected_app(tmp_path: Path) -> FastAPI:
    """App with a throwaway route guarded by require_token.

    No production route consumes the dependency until M1.5, so tests attach
    their own.
    """
    app = create_app(Settings(db_path=tmp_path / "tokens.db"))

    @app.get("/protected")
    def protected(  # pyright: ignore[reportUnusedFunction]
        token: Annotated[Token, Depends(require_token)],
    ) -> dict[str, str]:
        return {"token_id": token.id, "label": token.label}

    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def issue_token(app: FastAPI, label: str = "bsearch") -> tuple[Token, str]:
    """Mint a token straight into the app's store."""
    token, secret = mint_token(label)
    app.state.token_store.add(token)
    return token, secret


class TestRequireToken:
    async def test_valid_token_reaches_handler_with_token(self, tmp_path: Path) -> None:
        app = make_protected_app(tmp_path)
        token, secret = issue_token(app)
        async with make_client(app) as client:
            response = await client.get(
                "/protected", headers={"Authorization": f"Bearer {secret}"}
            )
        assert response.status_code == 200
        assert response.json() == {"token_id": token.id, "label": "bsearch"}

    async def test_missing_header_returns_401_problem(self, tmp_path: Path) -> None:
        app = make_protected_app(tmp_path)
        async with make_client(app) as client:
            response = await client.get("/protected")
        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.headers["www-authenticate"] == "Bearer"

    async def test_wrong_scheme_returns_401(self, tmp_path: Path) -> None:
        app = make_protected_app(tmp_path)
        _, secret = issue_token(app)
        async with make_client(app) as client:
            response = await client.get(
                "/protected", headers={"Authorization": f"Basic {secret}"}
            )
        assert response.status_code == 401

    async def test_unknown_secret_returns_401(self, tmp_path: Path) -> None:
        app = make_protected_app(tmp_path)
        issue_token(app)
        async with make_client(app) as client:
            response = await client.get(
                "/protected",
                headers={"Authorization": "Bearer bscribe_not-a-real-secret"},
            )
        assert response.status_code == 401

    async def test_missing_and_unknown_responses_are_identical(
        self, tmp_path: Path
    ) -> None:
        """One code path: no oracle distinguishing 'no header' from 'bad token'."""
        app = make_protected_app(tmp_path)
        async with make_client(app) as client:
            missing = await client.get("/protected")
            unknown = await client.get(
                "/protected", headers={"Authorization": "Bearer bscribe_nope"}
            )
        assert missing.json() == unknown.json()
        assert (
            missing.headers["www-authenticate"] == (unknown.headers["www-authenticate"])
        )

    async def test_deleted_token_is_revoked_immediately(self, tmp_path: Path) -> None:
        app = make_protected_app(tmp_path)
        token, secret = issue_token(app)
        headers = {"Authorization": f"Bearer {secret}"}
        async with make_client(app) as client:
            before = await client.get("/protected", headers=headers)
            app.state.token_store.delete(token.id)
            after = await client.get("/protected", headers=headers)
        assert before.status_code == 200
        assert after.status_code == 401

    async def test_secret_never_appears_in_logs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        app = make_protected_app(tmp_path)
        _, secret = issue_token(app)
        async with make_client(app) as client:
            await client.get(
                "/protected", headers={"Authorization": f"Bearer {secret}"}
            )
            await client.get(
                "/protected", headers={"Authorization": "Bearer bscribe_wrong"}
            )
        captured = capsys.readouterr()
        assert secret not in captured.out
        assert secret not in captured.err
        assert "bscribe_wrong" not in captured.out


class TestAppWiring:
    async def test_app_state_carries_sqlite_token_store(self, tmp_path: Path) -> None:
        from bscribe.adapters.sqlite import SqliteTokenStore
        from bscribe.domain.ports import TokenStorePort

        app = create_app(Settings(db_path=tmp_path / "tokens.db"))
        assert isinstance(app.state.token_store, SqliteTokenStore)
        assert isinstance(app.state.token_store, TokenStorePort)

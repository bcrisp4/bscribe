"""Tests for bscribe.errors (RFC 9457 problem+json)."""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from bscribe.errors import problem_response, register_error_handlers

PROBLEM_JSON = "application/problem+json"


def make_test_app() -> FastAPI:
    """Bare app with error handlers and throwaway routes to trigger them."""
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/ping")
    def ping() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"ping": "pong"}

    @app.get("/typed")
    def typed(count: int) -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]
        return {"count": count}

    @app.get("/boom")
    def boom() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("secret-internals")

    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    # raise_app_exceptions=False: ServerErrorMiddleware re-raises after
    # responding, and we want to assert on the 500 response body instead.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestHTTPExceptions:
    async def test_unknown_path_is_404_problem(self) -> None:
        async with make_client(make_test_app()) as client:
            response = await client.get("/nope")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith(PROBLEM_JSON)
        assert response.json() == {
            "type": "about:blank",
            "title": "Not Found",
            "status": 404,
            "detail": "Not Found",
        }

    async def test_wrong_method_is_405_problem(self) -> None:
        async with make_client(make_test_app()) as client:
            response = await client.post("/ping")

        assert response.status_code == 405
        assert response.headers["content-type"].startswith(PROBLEM_JSON)
        assert response.json()["title"] == "Method Not Allowed"


class TestValidationErrors:
    async def test_bad_query_param_is_400_not_422(self) -> None:
        async with make_client(make_test_app()) as client:
            response = await client.get("/typed", params={"count": "not-a-number"})

        assert response.status_code == 400
        assert response.headers["content-type"].startswith(PROBLEM_JSON)
        body = response.json()
        assert body["status"] == 400
        assert body["title"] == "Bad Request"

    async def test_validation_detail_names_field_but_not_value(self) -> None:
        submitted_value = "sensitive-user-payload"
        async with make_client(make_test_app()) as client:
            response = await client.get("/typed", params={"count": submitted_value})

        assert "count" in response.json()["detail"]
        assert submitted_value not in response.text


class TestUnexpectedErrors:
    async def test_crash_is_500_problem_without_internals(self) -> None:
        async with make_client(make_test_app()) as client:
            response = await client.get("/boom")

        assert response.status_code == 500
        assert response.headers["content-type"].startswith(PROBLEM_JSON)
        assert response.json() == {
            "type": "about:blank",
            "title": "Internal Server Error",
            "status": 500,
            "detail": "Internal server error",
        }
        assert "secret-internals" not in response.text


class TestProblemResponse:
    def test_detail_key_omitted_when_none(self) -> None:
        response = problem_response(status=404)

        assert b'"detail"' not in response.body
        assert response.media_type == PROBLEM_JSON

    def test_title_defaults_to_status_phrase(self) -> None:
        import json

        response = problem_response(status=413)

        body = json.loads(bytes(response.body))
        assert body["title"] == "Content Too Large"
        assert body["status"] == 413

    def test_headers_forwarded(self) -> None:
        response = problem_response(
            status=401,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

        assert response.headers["www-authenticate"] == "Bearer"

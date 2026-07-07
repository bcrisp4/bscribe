"""Tests for the bscribe CLI."""

from __future__ import annotations

import http.server
import threading
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from bscribe.adapters.sqlite import SqliteTokenStore
from bscribe.cli import app
from bscribe.domain.tokens import SECRET_PREFIX, hash_secret, mint_token

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

runner = CliRunner()


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI's Settings at a per-test database file."""
    path = tmp_path / "cli-tokens.db"
    monkeypatch.setenv("BSCRIBE_DB_PATH", str(path))
    return path


class TestTokenAdd:
    def test_prints_id_and_secret_once(self, db_path: Path) -> None:
        result = runner.invoke(app, ["token", "add", "bsearch"])
        assert result.exit_code == 0
        assert SECRET_PREFIX in result.output
        stored = SqliteTokenStore(db_path).list_all()
        assert len(stored) == 1
        assert stored[0].id in result.output

    def test_stores_hash_matching_printed_secret(self, db_path: Path) -> None:
        result = runner.invoke(app, ["token", "add", "bsearch"])
        secret = next(
            word
            for line in result.output.splitlines()
            for word in line.split()
            if word.startswith(SECRET_PREFIX)
        )
        stored = SqliteTokenStore(db_path).find_by_secret_hash(hash_secret(secret))
        assert stored is not None
        assert stored.label == "bsearch"


class TestTokenList:
    def test_shows_id_and_label_never_secret_or_hash(self, db_path: Path) -> None:
        store = SqliteTokenStore(db_path)
        token, secret = mint_token("bsearch")
        store.add(token)
        result = runner.invoke(app, ["token", "list"])
        assert result.exit_code == 0
        assert token.id in result.output
        assert "bsearch" in result.output
        assert secret not in result.output
        assert token.secret_hash not in result.output

    def test_empty_store_exits_zero(self, db_path: Path) -> None:
        del db_path
        result = runner.invoke(app, ["token", "list"])
        assert result.exit_code == 0


class TestTokenDelete:
    def test_deletes_existing_token(self, db_path: Path) -> None:
        store = SqliteTokenStore(db_path)
        token, _ = mint_token("bsearch")
        store.add(token)
        result = runner.invoke(app, ["token", "delete", token.id])
        assert result.exit_code == 0
        assert store.list_all() == []

    def test_unknown_id_exits_one(self, db_path: Path) -> None:
        del db_path
        result = runner.invoke(app, ["token", "delete", "deadbeef"])
        assert result.exit_code == 1


@pytest.fixture
def health_server() -> Iterator[str]:
    """Threaded stdlib HTTP server answering 200 on /healthz; port 0 = no flake."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # stdlib naming contract
            self.send_response(200 if self.path == "/healthz" else 404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
            del format, args

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/healthz"
    finally:
        server.shutdown()


class TestHealthcheck:
    def test_healthy_server_exits_zero(self, health_server: str) -> None:
        result = runner.invoke(app, ["healthcheck", "--url", health_server])
        assert result.exit_code == 0

    def test_dead_server_exits_one(self) -> None:
        # Port from the reserved TEST-NET style range; nothing listens.
        result = runner.invoke(
            app, ["healthcheck", "--url", "http://127.0.0.1:1/healthz"]
        )
        assert result.exit_code == 1

    def test_non_http_url_rejected(self) -> None:
        result = runner.invoke(app, ["healthcheck", "--url", "file:///etc/passwd"])
        assert result.exit_code == 1


class TestServe:
    def test_invokes_uvicorn_with_app_factory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[object, dict[str, object]]] = []

        def fake_run(app_ref: object, **kwargs: object) -> None:
            calls.append((app_ref, kwargs))

        monkeypatch.setattr("uvicorn.run", fake_run)
        result = runner.invoke(app, ["serve"])
        assert result.exit_code == 0
        assert calls == [
            (
                "bscribe.app:create_app",
                {"factory": True, "host": "0.0.0.0", "port": 8000},  # noqa: S104
            )
        ]

    def test_host_and_port_flags_forwarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, object]] = []

        def fake_run(app_ref: object, **kwargs: object) -> None:
            del app_ref
            calls.append(kwargs)

        monkeypatch.setattr("uvicorn.run", fake_run)
        result = runner.invoke(app, ["serve", "--host", "127.0.0.1", "--port", "9000"])
        assert result.exit_code == 0
        assert calls[0]["host"] == "127.0.0.1"
        assert calls[0]["port"] == 9000

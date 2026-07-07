"""Tests for the bscribe CLI."""

from __future__ import annotations

import http.server
import os
import socket
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

    def test_empty_existing_store_exits_zero(self, db_path: Path) -> None:
        SqliteTokenStore(db_path)  # create the database, no tokens
        result = runner.invoke(app, ["token", "list"])
        assert result.exit_code == 0

    def test_missing_database_exits_one_without_creating_it(
        self, db_path: Path
    ) -> None:
        """A wrong path must not fabricate an empty DB and report 'no tokens'."""
        result = runner.invoke(app, ["token", "list"])
        assert result.exit_code == 1
        assert not db_path.exists()


class TestTokenDelete:
    def test_deletes_existing_token(self, db_path: Path) -> None:
        store = SqliteTokenStore(db_path)
        token, _ = mint_token("bsearch")
        store.add(token)
        result = runner.invoke(app, ["token", "delete", token.id])
        assert result.exit_code == 0
        assert store.list_all() == []

    def test_unknown_id_exits_one(self, db_path: Path) -> None:
        SqliteTokenStore(db_path)  # create the database, no tokens
        result = runner.invoke(app, ["token", "delete", "deadbeef"])
        assert result.exit_code == 1

    def test_missing_database_exits_one_without_creating_it(
        self, db_path: Path
    ) -> None:
        result = runner.invoke(app, ["token", "delete", "deadbeef"])
        assert result.exit_code == 1
        assert not db_path.exists()


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


@pytest.fixture
def garbage_server() -> Iterator[str]:
    """Socket that answers any connection with non-HTTP bytes."""
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve_one() -> None:
        conn, _ = listener.accept()
        conn.sendall(b"definitely not http\n")
        conn.close()

    thread = threading.Thread(target=serve_one, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/healthz"
    finally:
        listener.close()


class TestHealthcheck:
    def test_healthy_server_exits_zero(self, health_server: str) -> None:
        result = runner.invoke(app, ["healthcheck", "--url", health_server])
        assert result.exit_code == 0

    def test_dead_server_exits_one(self) -> None:
        result = runner.invoke(
            app, ["healthcheck", "--url", "http://127.0.0.1:1/healthz"]
        )
        assert result.exit_code == 1

    def test_non_http_url_rejected(self) -> None:
        result = runner.invoke(app, ["healthcheck", "--url", "file:///etc/passwd"])
        assert result.exit_code == 1

    def test_malformed_port_exits_one_not_traceback(self) -> None:
        """http.client.InvalidURL is a ValueError, not an OSError."""
        result = runner.invoke(
            app, ["healthcheck", "--url", "http://127.0.0.1:notaport/healthz"]
        )
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_non_http_responder_exits_one_not_traceback(
        self, garbage_server: str
    ) -> None:
        """http.client.BadStatusLine is an HTTPException, not an OSError."""
        result = runner.invoke(app, ["healthcheck", "--url", garbage_server])
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_default_url_follows_bscribe_port_env(
        self, monkeypatch: pytest.MonkeyPatch, health_server: str
    ) -> None:
        """Container HEALTHCHECK stays correct when the port moves via env."""
        port = health_server.rsplit(":", 1)[1].split("/")[0]
        monkeypatch.setenv("BSCRIBE_PORT", port)
        result = runner.invoke(app, ["healthcheck"])
        assert result.exit_code == 0


class TestServe:
    @pytest.fixture
    def execvp_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> list[tuple[str, list[str]]]:
        calls: list[tuple[str, list[str]]] = []

        def fake_execvp(file: str, args: list[str]) -> None:
            calls.append((file, args))

        monkeypatch.setattr(os, "execvp", fake_execvp)
        return calls

    def test_execs_uvicorn_with_factory_and_loopback_default(
        self, execvp_calls: list[tuple[str, list[str]]]
    ) -> None:
        result = runner.invoke(app, ["serve"])
        assert result.exit_code == 0
        assert execvp_calls == [
            (
                "uvicorn",
                [
                    "uvicorn",
                    "--factory",
                    "bscribe.app:create_app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8000",
                ],
            )
        ]

    def test_host_and_port_flags_forwarded(
        self, execvp_calls: list[tuple[str, list[str]]]
    ) -> None:
        result = runner.invoke(
            app,
            ["serve", "--host", "0.0.0.0", "--port", "9000"],  # noqa: S104
        )
        assert result.exit_code == 0
        argv = execvp_calls[0][1]
        assert argv[argv.index("--host") + 1] == "0.0.0.0"  # noqa: S104
        assert argv[argv.index("--port") + 1] == "9000"

    def test_host_and_port_env_defaults(
        self,
        execvp_calls: list[tuple[str, list[str]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The container sets BSCRIBE_HOST/BSCRIBE_PORT instead of CLI flags."""
        monkeypatch.setenv("BSCRIBE_HOST", "0.0.0.0")  # noqa: S104
        monkeypatch.setenv("BSCRIBE_PORT", "9001")
        result = runner.invoke(app, ["serve"])
        assert result.exit_code == 0
        argv = execvp_calls[0][1]
        assert argv[argv.index("--host") + 1] == "0.0.0.0"  # noqa: S104
        assert argv[argv.index("--port") + 1] == "9001"

    def test_extra_args_passed_through_to_uvicorn(
        self, execvp_calls: list[tuple[str, list[str]]]
    ) -> None:
        """Operators keep the full uvicorn flag surface (proxy headers etc.)."""
        result = runner.invoke(
            app, ["serve", "--proxy-headers", "--root-path", "/bscribe"]
        )
        assert result.exit_code == 0
        argv = execvp_calls[0][1]
        assert argv[-3:] == ["--proxy-headers", "--root-path", "/bscribe"]

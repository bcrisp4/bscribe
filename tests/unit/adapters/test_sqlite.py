"""Tests for bscribe.adapters.sqlite."""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from bscribe.adapters.sqlite import SqliteTokenStore
from bscribe.domain.models import Token
from bscribe.domain.ports import TokenStorePort
from bscribe.domain.tokens import mint_token

if TYPE_CHECKING:
    from pathlib import Path


def make_token(**overrides: object) -> Token:
    """Token factory with sensible defaults."""
    defaults: dict[str, object] = {
        "id": "a1b2c3d4",
        "label": "bsearch",
        "secret_hash": "0" * 64,
        "created_at": datetime(2026, 7, 6, 12, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Token(**defaults)  # type: ignore[arg-type]


class TestSqliteTokenStore:
    def test_satisfies_token_store_port(self, tmp_path: Path) -> None:
        store = SqliteTokenStore(tmp_path / "tokens.db")
        assert isinstance(store, TokenStorePort)

    def test_add_then_find_by_secret_hash_roundtrip(self, tmp_path: Path) -> None:
        store = SqliteTokenStore(tmp_path / "tokens.db")
        token = make_token()
        store.add(token)
        assert store.find_by_secret_hash(token.secret_hash) == token

    def test_find_unknown_hash_returns_none(self, tmp_path: Path) -> None:
        store = SqliteTokenStore(tmp_path / "tokens.db")
        assert store.find_by_secret_hash("f" * 64) is None

    def test_created_at_roundtrip_preserves_utc(self, tmp_path: Path) -> None:
        store = SqliteTokenStore(tmp_path / "tokens.db")
        token, _ = mint_token("bsearch")
        store.add(token)
        found = store.find_by_secret_hash(token.secret_hash)
        assert found is not None
        assert found.created_at == token.created_at
        assert found.created_at.tzinfo is not None
        assert found.created_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]

    def test_list_all_newest_first(self, tmp_path: Path) -> None:
        store = SqliteTokenStore(tmp_path / "tokens.db")
        older = make_token(
            id="00000001",
            secret_hash="1" * 64,
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        newer = make_token(
            id="00000002",
            secret_hash="2" * 64,
            created_at=datetime(2026, 7, 5, tzinfo=UTC),
        )
        store.add(older)
        store.add(newer)
        assert store.list_all() == [newer, older]

    def test_delete_existing_returns_true_and_revokes(self, tmp_path: Path) -> None:
        store = SqliteTokenStore(tmp_path / "tokens.db")
        token = make_token()
        store.add(token)
        assert store.delete(token.id) is True
        assert store.find_by_secret_hash(token.secret_hash) is None

    def test_delete_unknown_returns_false(self, tmp_path: Path) -> None:
        store = SqliteTokenStore(tmp_path / "tokens.db")
        assert store.delete("deadbeef") is False

    def test_tokens_persist_across_store_instances(self, tmp_path: Path) -> None:
        db = tmp_path / "tokens.db"
        token = make_token()
        SqliteTokenStore(db).add(token)
        assert SqliteTokenStore(db).find_by_secret_hash(token.secret_hash) == token

    def test_database_uses_wal_journal_mode(self, tmp_path: Path) -> None:
        db = tmp_path / "tokens.db"
        SqliteTokenStore(db)
        with sqlite3.connect(db) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_creates_missing_parent_directory(self, tmp_path: Path) -> None:
        db = tmp_path / "data" / "nested" / "tokens.db"
        SqliteTokenStore(db).add(make_token())
        assert db.exists()

    def test_two_stores_interleave_writes(self, tmp_path: Path) -> None:
        """CLI writing while the server runs — two connections, one file."""
        db = tmp_path / "tokens.db"
        server_store = SqliteTokenStore(db)
        cli_store = SqliteTokenStore(db)
        a = make_token(id="0000000a", secret_hash="a" * 64)
        b = make_token(id="0000000b", secret_hash="b" * 64)
        server_store.add(a)
        cli_store.add(b)
        assert cli_store.delete(a.id) is True
        assert server_store.find_by_secret_hash(b.secret_hash) == b

    def test_concurrent_init_on_fresh_db_is_safe(self, tmp_path: Path) -> None:
        """Server startup racing `podman exec … token add` on a fresh DB."""
        db = tmp_path / "tokens.db"
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(SqliteTokenStore, db) for _ in range(8)]
            stores = [future.result() for future in futures]
        stores[0].add(make_token())
        assert len(stores[-1].list_all()) == 1

    def test_permanent_open_failure_fails_fast_without_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the lock race is retryable; a bad path must surface at once."""
        sleeps: list[float] = []
        monkeypatch.setattr("bscribe.adapters.sqlite.time.sleep", sleeps.append)
        with pytest.raises(sqlite3.OperationalError):
            # A directory is not a database file: unable-to-open, permanent.
            SqliteTokenStore(tmp_path)
        assert sleeps == []

    def test_init_restores_wal_if_externally_disabled(self, tmp_path: Path) -> None:
        """ADR 0002 depends on WAL; a flipped journal mode must not persist."""
        db = tmp_path / "tokens.db"
        SqliteTokenStore(db)
        with sqlite3.connect(db, autocommit=True) as conn:
            conn.execute("PRAGMA journal_mode = DELETE")
        SqliteTokenStore(db)
        with sqlite3.connect(db) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_init_on_current_schema_takes_no_write_lock(self, tmp_path: Path) -> None:
        """Read-only CLI commands must not queue behind server writers."""
        db = tmp_path / "tokens.db"
        SqliteTokenStore(db)  # migrate once
        blocker = sqlite3.connect(db, autocommit=True)
        try:
            blocker.execute("BEGIN IMMEDIATE")  # simulate in-flight writer
            start = time.monotonic()
            SqliteTokenStore(db)  # must not wait on busy_timeout
            assert time.monotonic() - start < 1.0
        finally:
            blocker.close()

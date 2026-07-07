"""SQLite adapter for token persistence.

The only module that speaks SQL (docs/adr/0002). Raw stdlib ``sqlite3``,
one short-lived connection per operation: fresh connections sidestep
``check_same_thread`` across FastAPI's threadpool threads, and WAL plus
``busy_timeout`` absorb the admin CLI writing from a second process while
the server runs (docs/design.md — Admin CLI).
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING

from bscribe.domain.models import Token

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

_BUSY_TIMEOUT_MS = 5000

# user_version-gated migrations: entry N runs when user_version == N, then
# bumps to N+1. Append-only — M2's jobs table is the next entry.
_MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE tokens (
        id          TEXT PRIMARY KEY,
        label       TEXT NOT NULL,
        secret_hash TEXT NOT NULL UNIQUE,
        created_at  TEXT NOT NULL
    ) STRICT
    """,
)


def _row_to_token(row: tuple[str, str, str, str]) -> Token:
    token_id, label, secret_hash, created_at = row
    return Token(
        id=token_id,
        label=label,
        secret_hash=secret_hash,
        created_at=datetime.fromisoformat(created_at),
    )


class SqliteTokenStore:
    """TokenStorePort implementation backed by a SQLite file.

    Construction creates the parent directory and the schema if missing;
    concurrent construction against one fresh file is safe (the migration
    runs under ``BEGIN IMMEDIATE`` and re-checks ``user_version`` after
    taking the write lock).
    """

    def __init__(self, db_path: Path) -> None:
        """Open (creating if necessary) the token database.

        Args:
            db_path: SQLite file location; parent directories are created.
        """
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        # Two connections switching a fresh database into WAL can collide
        # with "database is locked" *despite* busy_timeout — the busy
        # handler does not cover the journal-mode switch. Server startup
        # racing `podman exec … token add` hits exactly this, so retry with
        # backoff rather than die. Only lock contention is retryable;
        # permanent failures (unwritable volume, corrupt file) surface
        # immediately with their real error.
        for delay_seconds in (0.05, 0.1, 0.2, 0.4, None):
            try:
                self._init_schema_once()
            except sqlite3.OperationalError as exc:
                retryable = exc.sqlite_errorcode in (
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                )
                if delay_seconds is None or not retryable:
                    raise
                time.sleep(delay_seconds)
            else:
                return

    def _init_schema_once(self) -> None:
        # autocommit=True: journal_mode cannot change inside a transaction,
        # and the migration manages its own BEGIN IMMEDIATE explicitly.
        conn = sqlite3.connect(self._db_path, autocommit=True)
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            # Fast path: schema current AND journal mode still WAL (it
            # persists in the file but can be flipped externally, and ADR
            # 0002's second-writer story depends on it). Reading both takes
            # no write lock, so store construction — including read-only
            # CLI commands — never queues behind an in-flight writer.
            (version,) = conn.execute("PRAGMA user_version").fetchone()
            (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
            if version >= len(_MIGRATIONS) and journal_mode == "wal":
                return
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Re-read under the write lock: another process may have
                # migrated between our connect and the BEGIN.
                (version,) = conn.execute("PRAGMA user_version").fetchone()
                for number, ddl in enumerate(_MIGRATIONS[version:], start=version):
                    conn.execute(ddl)
                    # PRAGMA cannot be parameterized; `number` is a local int.
                    conn.execute(f"PRAGMA user_version = {number + 1}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        """One transaction per operation: commit on success, always close.

        ``with sqlite3.connect(...)`` alone commits but never closes —
        per-operation connections must close to release WAL file handles.
        """
        conn = sqlite3.connect(self._db_path, autocommit=False)
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add(self, token: Token) -> None:
        """Persist a new token.

        Args:
            token: Record to store; ``id`` and ``secret_hash`` are unique.

        Raises:
            sqlite3.IntegrityError: Duplicate id or secret hash
                (astronomically rare with generated values — see
                docs/adr/0002).
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tokens (id, label, secret_hash, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    token.id,
                    token.label,
                    token.secret_hash,
                    token.created_at.isoformat(),
                ),
            )

    def find_by_secret_hash(self, secret_hash: str) -> Token | None:
        """Look up the token matching a presented secret's hash.

        Args:
            secret_hash: SHA-256 hex digest of the presented bearer token.

        Returns:
            The matching token, or ``None``.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, label, secret_hash, created_at FROM tokens"
                " WHERE secret_hash = ?",
                (secret_hash,),
            ).fetchone()
        return _row_to_token(row) if row is not None else None

    def list_all(self) -> list[Token]:
        """Return every stored token, newest first.

        Returns:
            All token records, ``created_at`` descending.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, label, secret_hash, created_at FROM tokens"
                " ORDER BY created_at DESC"
            ).fetchall()
        return [_row_to_token(row) for row in rows]

    def delete(self, token_id: str) -> bool:
        """Delete a token by id, revoking it immediately.

        Args:
            token_id: The token's immutable id.

        Returns:
            ``True`` if a row was deleted, ``False`` for an unknown id.
        """
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
            return cursor.rowcount > 0

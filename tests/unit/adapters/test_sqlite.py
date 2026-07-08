"""Tests for bscribe.adapters.sqlite."""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from bscribe.adapters.sqlite import SqliteJobStore, SqliteTokenStore
from bscribe.domain.models import (
    Job,
    JobStatus,
    OcrMode,
    OutputFormat,
    ParsedDocument,
    Token,
)
from bscribe.domain.ports import JobStorePort, TokenStorePort
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


def make_job(**overrides: object) -> Job:
    """Job factory with sensible defaults (a freshly queued job)."""
    defaults: dict[str, object] = {
        "id": "abcd1234abcd1234",
        "token_id": "a1b2c3d4",
        "output": OutputFormat.MARKDOWN,
        "ocr": OcrMode.AUTO,
        "status": JobStatus.QUEUED,
        "created_at": datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
        "started_at": None,
        "finished_at": None,
        "failure_detail": None,
    }
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


def make_result(**overrides: object) -> ParsedDocument:
    """ParsedDocument factory with sensible defaults."""
    defaults: dict[str, object] = {
        "content": "# Title\n\nBody.",
        "pages": 3,
        "duration_ms": 412.5,
    }
    defaults.update(overrides)
    return ParsedDocument(**defaults)  # type: ignore[arg-type]


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


class TestSqliteJobStore:
    def test_satisfies_job_store_port(self, tmp_path: Path) -> None:
        # Typed assignment: pyright verifies full structural conformance
        # (signatures, not just method names like the isinstance check).
        store: JobStorePort = SqliteJobStore(tmp_path / "jobs.db")
        assert isinstance(store, JobStorePort)

    def test_add_then_get_roundtrip(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job()
        store.add(job)
        assert store.get(job.id, job.token_id) == job

    def test_add_then_get_roundtrip_of_populated_snapshot(self, tmp_path: Path) -> None:
        """Every metadata column, not just the fresh-queued subset, must
        roundtrip."""
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job(
            status=JobStatus.FAILED,
            started_at=datetime(2026, 7, 7, 12, 1, tzinfo=UTC),
            finished_at=datetime(2026, 7, 7, 12, 2, tzinfo=UTC),
            failure_detail="timeout",
        )
        store.add(job)
        assert store.get(job.id, job.token_id) == job

    def test_created_at_is_stored_normalized_to_utc(self, tmp_path: Path) -> None:
        """Non-UTC offsets must not break the lexicographic newest-first order."""
        store = SqliteJobStore(tmp_path / "jobs.db")
        older_plus2 = make_job(
            id="000000000000000a",
            # 14:00+02:00 == 12:00Z — older, but the raw string would sort last.
            created_at=datetime(2026, 7, 7, 14, 0, tzinfo=timezone(timedelta(hours=2))),
        )
        newer_utc = make_job(
            id="000000000000000b",
            created_at=datetime(2026, 7, 7, 12, 30, tzinfo=UTC),
        )
        store.add(older_plus2)
        store.add(newer_utc)
        listed = store.list_for_token("a1b2c3d4")
        assert [job.id for job in listed] == [newer_utc.id, older_plus2.id]
        # Roundtrip preserves the instant (datetime equality is offset-aware).
        assert listed[1].created_at == older_plus2.created_at

    def test_get_ignores_result_columns(self, tmp_path: Path) -> None:
        """Metadata reads must not touch stored content: a hand-corrupted
        result column is invisible to get/list, caught only by get_result."""
        db = tmp_path / "jobs.db"
        store = SqliteJobStore(db)
        job = make_job()
        store.add(job)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute(
                "UPDATE jobs SET result_content = 'orphan text' WHERE id = ?",
                (job.id,),
            )
        assert store.get(job.id, job.token_id) == job
        assert store.list_for_token(job.token_id) == [job]

    def test_get_result_raises_on_incomplete_result_columns(
        self, tmp_path: Path
    ) -> None:
        """A hand-edited done row must fail loudly, not build a corrupt
        result — and the error must not quote the stored content."""
        db = tmp_path / "jobs.db"
        store = SqliteJobStore(db)
        job = make_job()
        store.add(job)
        store.mark_running(job.id)
        store.mark_done(job.id, make_result(content="orphan text"))
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE jobs SET result_pages = NULL WHERE id = ?", (job.id,))
        with pytest.raises(ValueError, match=job.id) as excinfo:
            store.get_result(job.id, job.token_id)
        # Privacy hard rule: the error must not quote the stored content.
        assert "orphan text" not in str(excinfo.value)

    def test_get_roundtrip_preserves_utc(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job(created_at=datetime.now(tz=UTC))
        store.add(job)
        found = store.get(job.id, job.token_id)
        assert found is not None
        assert found.created_at == job.created_at
        assert found.created_at.tzinfo is not None
        assert found.created_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]

    def test_get_unknown_id_returns_none(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        assert store.get("deadbeefdeadbeef", "a1b2c3d4") is None

    def test_get_with_wrong_token_returns_none(self, tmp_path: Path) -> None:
        """Cross-token access is indistinguishable from a missing job."""
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job(token_id="a1b2c3d4")
        store.add(job)
        assert store.get(job.id, "0therT0k") is None

    def test_get_result_unknown_id_returns_none(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        assert store.get_result("deadbeefdeadbeef", "a1b2c3d4") is None

    def test_get_result_with_wrong_token_returns_none(self, tmp_path: Path) -> None:
        """Cross-token access is indistinguishable from a missing job."""
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job(token_id="a1b2c3d4")
        store.add(job)
        store.mark_running(job.id)
        store.mark_done(job.id, make_result())
        assert store.get_result(job.id, "0therT0k") is None

    @pytest.mark.parametrize("terminal_detail", [None, "timeout"])
    def test_get_result_on_non_done_job_returns_none(
        self, tmp_path: Path, terminal_detail: str | None
    ) -> None:
        """Queued/running and failed jobs alike expose no result."""
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job()
        store.add(job)
        if terminal_detail is not None:
            store.mark_failed(job.id, terminal_detail)
        assert store.get_result(job.id, job.token_id) is None

    def test_mark_running_from_queued(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job()
        store.add(job)
        before = datetime.now(tz=UTC)
        assert store.mark_running(job.id) is True
        after = datetime.now(tz=UTC)
        found = store.get(job.id, job.token_id)
        assert found is not None
        assert found.status is JobStatus.RUNNING
        assert found.started_at is not None
        assert before <= found.started_at <= after

    def test_mark_running_from_terminal_state_is_refused(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job()
        store.add(job)
        store.mark_running(job.id)
        store.mark_failed(job.id, "timeout")
        assert store.mark_running(job.id) is False
        found = store.get(job.id, job.token_id)
        assert found is not None
        assert found.status is JobStatus.FAILED

    def test_mark_running_unknown_id_returns_false(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        assert store.mark_running("deadbeefdeadbeef") is False

    def test_mark_done_from_running_stores_result(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job()
        store.add(job)
        store.mark_running(job.id)
        result = make_result()
        assert store.mark_done(job.id, result) is True
        found = store.get(job.id, job.token_id)
        assert found is not None
        assert found.status is JobStatus.DONE
        assert found.finished_at is not None
        assert found.failure_detail is None
        assert store.get_result(job.id, job.token_id) == result

    def test_mark_done_from_queued_is_refused(self, tmp_path: Path) -> None:
        """A job must pass through running before it can complete."""
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job()
        store.add(job)
        assert store.mark_done(job.id, make_result()) is False
        found = store.get(job.id, job.token_id)
        assert found is not None
        assert found.status is JobStatus.QUEUED
        assert store.get_result(job.id, job.token_id) is None

    def test_mark_done_after_delete_is_a_noop(self, tmp_path: Path) -> None:
        """Cancel-vs-complete race: a late result must not resurrect the job."""
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job()
        store.add(job)
        store.mark_running(job.id)
        assert store.delete(job.id, job.token_id) is True
        assert store.mark_done(job.id, make_result()) is False
        assert store.get(job.id, job.token_id) is None

    def test_mark_failed_from_queued(self, tmp_path: Path) -> None:
        """Jobs can fail before starting (e.g. pool rejects the submission)."""
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job()
        store.add(job)
        assert store.mark_failed(job.id, "pool unavailable") is True
        found = store.get(job.id, job.token_id)
        assert found is not None
        assert found.status is JobStatus.FAILED
        assert found.failure_detail == "pool unavailable"
        assert found.finished_at is not None

    def test_mark_failed_from_running(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job()
        store.add(job)
        store.mark_running(job.id)
        assert store.mark_failed(job.id, "timeout") is True
        found = store.get(job.id, job.token_id)
        assert found is not None
        assert found.status is JobStatus.FAILED
        assert found.failure_detail == "timeout"
        assert store.get_result(job.id, job.token_id) is None

    def test_mark_failed_never_clobbers_a_done_result(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job()
        store.add(job)
        store.mark_running(job.id)
        result = make_result()
        store.mark_done(job.id, result)
        assert store.mark_failed(job.id, "late timeout") is False
        found = store.get(job.id, job.token_id)
        assert found is not None
        assert found.status is JobStatus.DONE
        assert store.get_result(job.id, job.token_id) == result

    def test_list_for_token_newest_first(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        older = make_job(
            id="000000000000000a", created_at=datetime(2026, 7, 1, tzinfo=UTC)
        )
        newer = make_job(
            id="000000000000000b", created_at=datetime(2026, 7, 5, tzinfo=UTC)
        )
        store.add(older)
        store.add(newer)
        assert store.list_for_token("a1b2c3d4") == [newer, older]

    def test_list_for_token_excludes_other_tokens(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        mine = make_job(id="000000000000000a", token_id="a1b2c3d4")
        theirs = make_job(id="000000000000000b", token_id="0therT0k")
        store.add(mine)
        store.add(theirs)
        assert store.list_for_token("a1b2c3d4") == [mine]

    def test_list_for_token_filters_by_status(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        queued = make_job(id="000000000000000a")
        running = make_job(id="000000000000000b")
        store.add(queued)
        store.add(running)
        store.mark_running(running.id)
        listed = store.list_for_token("a1b2c3d4", status=JobStatus.RUNNING)
        assert [job.id for job in listed] == [running.id]

    def test_list_for_token_with_no_jobs_returns_empty(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        assert store.list_for_token("a1b2c3d4") == []

    def test_delete_own_job_returns_true_and_purges(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job()
        store.add(job)
        assert store.delete(job.id, job.token_id) is True
        assert store.get(job.id, job.token_id) is None

    def test_delete_with_wrong_token_returns_false_and_keeps_job(
        self, tmp_path: Path
    ) -> None:
        """Cross-token delete is indistinguishable from a missing job."""
        store = SqliteJobStore(tmp_path / "jobs.db")
        job = make_job(token_id="a1b2c3d4")
        store.add(job)
        assert store.delete(job.id, "0therT0k") is False
        assert store.get(job.id, job.token_id) == job

    def test_delete_unknown_id_returns_false(self, tmp_path: Path) -> None:
        store = SqliteJobStore(tmp_path / "jobs.db")
        assert store.delete("deadbeefdeadbeef", "a1b2c3d4") is False

    def test_jobs_persist_across_store_instances(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        job = make_job()
        SqliteJobStore(db).add(job)
        assert SqliteJobStore(db).get(job.id, job.token_id) == job

    def test_upgrades_existing_m1_database(self, tmp_path: Path) -> None:
        """A tokens-only M1 file (user_version=1) must upgrade in place."""
        db = tmp_path / "bscribe.db"
        # closing(), not the connection context manager: the latter only
        # commits and would leave a WAL handle open under the migration.
        with closing(sqlite3.connect(db, autocommit=True)) as conn:
            # Replica of the M1 schema as shipped (migration entry 1).
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE tokens (
                    id          TEXT PRIMARY KEY,
                    label       TEXT NOT NULL,
                    secret_hash TEXT NOT NULL UNIQUE,
                    created_at  TEXT NOT NULL
                ) STRICT
                """
            )
            conn.execute(
                "INSERT INTO tokens VALUES ('a1b2c3d4', 'bsearch', ?, ?)",
                ("0" * 64, datetime(2026, 7, 6, tzinfo=UTC).isoformat()),
            )
            conn.execute("PRAGMA user_version = 1")

        job_store = SqliteJobStore(db)
        job = make_job()
        job_store.add(job)
        assert job_store.get(job.id, job.token_id) == job
        # The M1 token survived the migration and is still readable.
        assert SqliteTokenStore(db).find_by_secret_hash("0" * 64) is not None

    def test_deleting_token_orphans_jobs_without_breaking_them(
        self, tmp_path: Path
    ) -> None:
        """No FK to tokens: orphaned jobs age out via the TTL purge (#19)."""
        db = tmp_path / "bscribe.db"
        token_store = SqliteTokenStore(db)
        job_store = SqliteJobStore(db)
        token = make_token()
        token_store.add(token)
        job = make_job(token_id=token.id)
        job_store.add(job)
        assert token_store.delete(token.id) is True
        assert job_store.get(job.id, token.id) == job

    def test_concurrent_init_with_mixed_store_types_is_safe(
        self, tmp_path: Path
    ) -> None:
        """Fresh-DB migration race across both store classes."""
        db = tmp_path / "bscribe.db"
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(SqliteJobStore, db)
                if index % 2
                else pool.submit(SqliteTokenStore, db)
                for index in range(8)
            ]
            stores = [future.result() for future in futures]
        job_store = next(s for s in stores if isinstance(s, SqliteJobStore))
        job = make_job()
        job_store.add(job)
        assert job_store.get(job.id, job.token_id) == job

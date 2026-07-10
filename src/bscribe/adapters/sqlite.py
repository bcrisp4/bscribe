"""SQLite adapter for token and job persistence.

The only module that speaks SQL (docs/adr/0002). Raw stdlib ``sqlite3``,
one short-lived connection per operation: fresh connections sidestep
``check_same_thread`` across FastAPI's threadpool threads, and WAL plus
``busy_timeout`` absorb the admin CLI writing from a second process while
the server runs (docs/design.md — Admin CLI).
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from bscribe.domain.models import (
    Job,
    JobStatus,
    OcrMode,
    OutputFormat,
    ParsedDocument,
    PipelineStamp,
    Token,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

_BUSY_TIMEOUT_MS = 5000

# user_version-gated migrations: entry N runs when user_version == N, then
# bumps to N+1. Append-only, one statement per entry (the runner uses
# conn.execute, which takes exactly one statement).
_MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE tokens (
        id          TEXT PRIMARY KEY,
        label       TEXT NOT NULL,
        secret_hash TEXT NOT NULL UNIQUE,
        created_at  TEXT NOT NULL
    ) STRICT
    """,
    # Deliberately no FOREIGN KEY to tokens: a deleted token's jobs orphan
    # until the TTL purge (docs/design.md — Admin CLI), and we never enable
    # PRAGMA foreign_keys anyway. Terminal-state invariants (done => result,
    # failed => failure_detail) are enforced by the guarded transitions in
    # SqliteJobStore, not by CHECKs.
    """
    CREATE TABLE jobs (
        id                 TEXT PRIMARY KEY,
        token_id           TEXT NOT NULL,
        output             TEXT NOT NULL,
        ocr                TEXT NOT NULL,
        status             TEXT NOT NULL
            CHECK (status IN ('queued', 'running', 'done', 'failed')),
        failure_detail     TEXT,
        created_at         TEXT NOT NULL,
        started_at         TEXT,
        finished_at        TEXT,
        result_content     TEXT,
        result_pages       INTEGER,
        result_duration_ms REAL
    ) STRICT
    """,
    # Serves the one job query shape: per-token listing, newest first. The
    # id tiebreak in that ORDER BY still needs a small temp B-tree over
    # created_at ties (id is not the rowid) — immaterial at this scale.
    "CREATE INDEX jobs_token_created ON jobs (token_id, created_at DESC)",
    # NULL on existing done rows is a legitimate pre-upgrade state (get_result
    # must not treat it as corruption) — only a genuinely missing result_*
    # trio still means external corruption. See _stamp_from_json.
    "ALTER TABLE jobs ADD COLUMN result_pipeline TEXT",
    # Covering index for count_by_status's `GROUP BY status` (the Prometheus
    # jobs-by-state scrape, ~every 15s): lets the count run off the index
    # instead of scanning the table, which otherwise grows with done/failed
    # rows retained until the TTL purge.
    "CREATE INDEX jobs_status ON jobs (status)",
)


def _ensure_schema(db_path: Path) -> None:
    """Create parent directories and bring the schema up to date.

    Shared by every store class in this module — one ``user_version``
    governs the whole file, so whichever store is constructed first runs
    all pending migrations.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Two connections switching a fresh database into WAL can collide
    # with "database is locked" *despite* busy_timeout — the busy
    # handler does not cover the journal-mode switch. Server startup
    # racing `podman exec … token add` hits exactly this, so retry with
    # backoff rather than die. Only lock contention is retryable;
    # permanent failures (unwritable volume, corrupt file) surface
    # immediately with their real error.
    for delay_seconds in (0.05, 0.1, 0.2, 0.4, None):
        try:
            _init_schema_once(db_path)
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


def _init_schema_once(db_path: Path) -> None:
    """Run one migration attempt; raises on lock contention (see caller)."""
    # autocommit=True: journal_mode cannot change inside a transaction,
    # and the migration manages its own BEGIN IMMEDIATE explicitly.
    conn = sqlite3.connect(db_path, autocommit=True)
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
def _connect(db_path: Path) -> Generator[sqlite3.Connection]:
    """One transaction per operation: commit on success, always close.

    ``with sqlite3.connect(...)`` alone commits but never closes —
    per-operation connections must close to release WAL file handles.
    """
    conn = sqlite3.connect(db_path, autocommit=False)
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        yield conn
        conn.commit()
    finally:
        conn.close()


# Metadata columns only — the result_* columns are deliberately absent so
# get/list_for_token never read stored content (JobStorePort's
# metadata/result split); get_result is the one reader of the blob.
_JOB_COLUMNS = (
    "id, token_id, output, ocr, status, failure_detail,"
    " created_at, started_at, finished_at"
)

# The queued/running -> failed transition, shared by mark_failed (one job)
# and sweep_incomplete (all incomplete jobs) so the two can never drift:
# the same SET clause, differing only in their WHERE scope.
_FAIL_TRANSITION_SET = "UPDATE jobs SET status = ?, finished_at = ?, failure_detail = ?"


def _to_utc_iso(value: datetime) -> str:
    """Serialize an aware datetime as UTC isoformat TEXT (sortable)."""
    return value.astimezone(UTC).isoformat()


def _row_to_job(
    row: tuple[
        str,
        str,
        str,
        str,
        str,
        str | None,
        str,
        str | None,
        str | None,
    ],
) -> Job:
    """Map a jobs row (in ``_JOB_COLUMNS`` order) back to a :class:`Job`.

    Raises:
        ValueError: The row violates a Job invariant — a state combination
            :meth:`Job.__post_init__` rejects. Only reachable through
            external edits (the operator's sanctioned debug path is raw
            SQLite); the message carries the job id, never stored content.
    """
    (
        job_id,
        token_id,
        output,
        ocr,
        status,
        failure_detail,
        created_at,
        started_at,
        finished_at,
    ) = row
    return Job(
        id=job_id,
        token_id=token_id,
        output=OutputFormat(output),
        ocr=OcrMode(ocr),
        status=JobStatus(status),
        created_at=datetime.fromisoformat(created_at),
        started_at=(
            datetime.fromisoformat(started_at) if started_at is not None else None
        ),
        finished_at=(
            datetime.fromisoformat(finished_at) if finished_at is not None else None
        ),
        failure_detail=failure_detail,
    )


def _stamp_to_json(stamp: PipelineStamp | None) -> str | None:
    """Serialize a pipeline stamp as compact canonical JSON for storage.

    Args:
        stamp: The result's pipeline stamp, or ``None`` if unstamped.

    Returns:
        Compact JSON with ``components`` keys sorted (so equivalent stamps
        always produce byte-identical text), or ``None`` for ``stamp is
        None``.
    """
    if stamp is None:
        return None
    return json.dumps(
        {
            "fingerprint": stamp.fingerprint,
            "components": dict(sorted(stamp.components.items())),
        },
        separators=(",", ":"),
    )


def _stamp_from_json(job_id: str, text: str | None) -> PipelineStamp | None:
    """Deserialize a stored pipeline stamp.

    Args:
        job_id: The owning job's id, for the error message only.
        text: The raw ``result_pipeline`` column value. ``None`` is a
            legitimate state — a pre-upgrade row or a result stored before
            stamping existed — never treated as corruption.

    Returns:
        The decoded stamp, or ``None`` for ``text is None``.

    Raises:
        ValueError: ``text`` is not valid JSON, or is valid JSON with the
            wrong shape (e.g. ``{}``, ``null``, a missing ``"components"``
            key) — both only reachable through external edits (the
            operator's sanctioned debug path is raw SQLite); the message
            never quotes the stored text.
    """
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"job {job_id}: result_pipeline is not valid JSON"
        raise ValueError(msg) from exc
    shape_error = ValueError(f"job {job_id}: result_pipeline has an unexpected shape")
    if not isinstance(data, dict):
        raise shape_error
    obj = cast("dict[str, object]", data)
    fingerprint = obj.get("fingerprint")
    components = obj.get("components")
    if not isinstance(fingerprint, str):
        raise shape_error
    if not isinstance(components, dict):
        raise shape_error
    components_obj = cast("dict[object, object]", components)
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in components_obj.items()
    ):
        raise shape_error
    return PipelineStamp(
        fingerprint=fingerprint, components=cast("dict[str, str]", components_obj)
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
        _ensure_schema(db_path)

    def add(self, token: Token) -> None:
        """Persist a new token.

        Args:
            token: Record to store; ``id`` and ``secret_hash`` are unique.

        Raises:
            sqlite3.IntegrityError: Duplicate id or secret hash
                (astronomically rare with generated values — see
                docs/adr/0002).
        """
        with _connect(self._db_path) as conn:
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
        with _connect(self._db_path) as conn:
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
        with _connect(self._db_path) as conn:
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
        with _connect(self._db_path) as conn:
            cursor = conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
            return cursor.rowcount > 0


class SqliteJobStore:
    """JobStorePort implementation backed by a SQLite file.

    Shares the database file, migrations, and connection discipline with
    :class:`SqliteTokenStore`. Transitions are compare-and-set UPDATEs
    (prior status in the WHERE clause), so a lost race — including a job
    deleted mid-parse — matches zero rows and reports ``False`` instead of
    resurrecting or overwriting anything.
    """

    def __init__(self, db_path: Path) -> None:
        """Open (creating if necessary) the job database.

        Args:
            db_path: SQLite file location; parent directories are created.
        """
        self._db_path = db_path
        _ensure_schema(db_path)

    def add(self, job: Job) -> None:
        """Persist a freshly queued job.

        Args:
            job: The job record to store; ``id`` must be unique and
                ``status`` must be ``queued``.

        Raises:
            ValueError: ``job`` is not ``queued``. Since ``Job`` carries no
                result, admitting a terminal snapshot would create states
                the transitions can never produce — most dangerously a
                ``done`` row with no stored result, which ``get_result``
                treats as external corruption.
            sqlite3.IntegrityError: Duplicate id (astronomically rare with
                generated values — see docs/adr/0002).
        """
        if job.status is not JobStatus.QUEUED:
            msg = f"job {job.id}: add requires a queued job, got {job.status.value}"
            raise ValueError(msg)
        # Timestamps are normalized to UTC before serializing: newest-first
        # ordering compares the isoformat TEXT lexicographically, which is
        # only chronological when every stored value shares one offset.
        # (Job rejects naive datetimes at construction.)
        # Metadata columns only; the result_* columns start NULL and are
        # written solely by mark_done.
        with _connect(self._db_path) as conn:
            conn.execute(
                f"INSERT INTO jobs ({_JOB_COLUMNS})"  # noqa: S608 - column list constant
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.id,
                    job.token_id,
                    job.output.value,
                    job.ocr.value,
                    job.status.value,
                    job.failure_detail,
                    _to_utc_iso(job.created_at),
                    _to_utc_iso(job.started_at) if job.started_at else None,
                    _to_utc_iso(job.finished_at) if job.finished_at else None,
                ),
            )

    def get(self, job_id: str, token_id: str) -> Job | None:
        """Fetch a job owned by ``token_id``.

        Args:
            job_id: The job's id.
            token_id: The calling token's id — the ownership scope.

        Returns:
            The job, or ``None`` for an unknown id or another token's job
            (indistinguishable by design — docs/design.md, Ownership).
        """
        with _connect(self._db_path) as conn:
            row = conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs"  # noqa: S608
                " WHERE id = ? AND token_id = ?",
                (job_id, token_id),
            ).fetchone()
        return _row_to_job(row) if row is not None else None

    def get_result(self, job_id: str, token_id: str) -> ParsedDocument | None:
        """Fetch a done job's stored result, owned by ``token_id``.

        The one read path that touches the result columns (JobStorePort's
        metadata/result split). The ``status = 'done'`` predicate lives in
        the SQL so a non-done row can never leak a partial result.

        Args:
            job_id: The job's id.
            token_id: The calling token's id — the ownership scope.

        Returns:
            The stored result, or ``None`` for an unknown id, another
            token's job, or any non-``done`` status.

        Raises:
            ValueError: The done row's result columns are incomplete, or
                ``result_pipeline`` holds unparseable JSON — only reachable
                through external edits (the operator's sanctioned debug path
                is raw SQLite); the message carries the job id, never stored
                content.
        """
        with _connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT result_content, result_pages, result_duration_ms,"
                " result_pipeline"
                " FROM jobs WHERE id = ? AND token_id = ? AND status = ?",
                (job_id, token_id, JobStatus.DONE.value),
            ).fetchone()
        if row is None:
            return None
        content, pages, duration_ms, pipeline_json = row
        # mark_done writes the content/pages/duration trio atomically; NULLs
        # on a done row mean it was edited outside the store. Fail loudly —
        # an explicit raise, not an assert, so the guard survives python -O.
        # result_pipeline is exempt: NULL there is a legitimate pre-upgrade
        # or never-stamped row, not corruption (see _stamp_from_json).
        if content is None or pages is None or duration_ms is None:
            msg = f"job {job_id}: result columns incomplete on done row"
            raise ValueError(msg)
        return ParsedDocument(
            content=content,
            pages=pages,
            duration_ms=duration_ms,
            pipeline=_stamp_from_json(job_id, pipeline_json),
        )

    def list_for_token(
        self, token_id: str, *, status: JobStatus | None = None
    ) -> list[Job]:
        """List a token's jobs, newest first.

        Args:
            token_id: The calling token's id.
            status: If given, only jobs currently in this state.

        Returns:
            The token's matching jobs, ``created_at`` descending (id as a
            deterministic tiebreak); never another token's.
        """
        query = f"SELECT {_JOB_COLUMNS} FROM jobs WHERE token_id = ?"  # noqa: S608
        params: list[str] = [token_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        query += " ORDER BY created_at DESC, id DESC"
        with _connect(self._db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_job(row) for row in rows]

    def mark_running(self, job_id: str) -> bool:
        """Transition ``queued`` → ``running``, stamping ``started_at``.

        Args:
            job_id: The job's id.

        Returns:
            ``True`` if the transition applied; ``False`` for a missing
            job or any other prior status.
        """
        with _connect(self._db_path) as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = ?, started_at = ?"
                " WHERE id = ? AND status = ?",
                (
                    JobStatus.RUNNING.value,
                    datetime.now(tz=UTC).isoformat(),
                    job_id,
                    JobStatus.QUEUED.value,
                ),
            )
            return cursor.rowcount > 0

    def mark_done(self, job_id: str, result: ParsedDocument) -> bool:
        """Transition ``running`` → ``done``, storing the result inline.

        Args:
            job_id: The job's id.
            result: The parse result; stamped with ``finished_at``. Its
                ``pipeline`` stamp (if any) is persisted as canonical JSON;
                ``None`` stores NULL.

        Returns:
            ``True`` if the transition applied; ``False`` for a missing
            job (e.g. cancelled mid-parse) or any other prior status — the
            caller should then discard ``result``.
        """
        with _connect(self._db_path) as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ?,"
                " result_content = ?, result_pages = ?, result_duration_ms = ?,"
                " result_pipeline = ?"
                " WHERE id = ? AND status = ?",
                (
                    JobStatus.DONE.value,
                    datetime.now(tz=UTC).isoformat(),
                    result.content,
                    result.pages,
                    result.duration_ms,
                    _stamp_to_json(result.pipeline),
                    job_id,
                    JobStatus.RUNNING.value,
                ),
            )
            return cursor.rowcount > 0

    def mark_failed(self, job_id: str, detail: str) -> bool:
        """Transition ``queued``/``running`` → ``failed``.

        Queued jobs can fail directly (the pool rejected the submission);
        a ``done`` job's result is never clobbered.

        Args:
            job_id: The job's id.
            detail: Human-readable failure reason (e.g. ``"timeout"``).

        Returns:
            ``True`` if the transition applied; ``False`` for a missing
            job or a terminal prior status.
        """
        with _connect(self._db_path) as conn:
            cursor = conn.execute(
                _FAIL_TRANSITION_SET + " WHERE id = ? AND status IN (?, ?)",
                (
                    JobStatus.FAILED.value,
                    datetime.now(tz=UTC).isoformat(),
                    detail,
                    job_id,
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                ),
            )
            return cursor.rowcount > 0

    def delete(self, job_id: str, token_id: str) -> bool:
        """Delete a job owned by ``token_id``, removing its stored result.

        Args:
            job_id: The job's id.
            token_id: The calling token's id — the ownership scope.

        Returns:
            ``True`` if a job was deleted; ``False`` for an unknown id or
            another token's job (indistinguishable by design).
        """
        with _connect(self._db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM jobs WHERE id = ? AND token_id = ?",
                (job_id, token_id),
            )
            return cursor.rowcount > 0

    def count_by_status(self) -> dict[JobStatus, int]:
        """Count all jobs grouped by status, across every token.

        Returns:
            A mapping from :class:`JobStatus` to its count; states with no
            jobs are omitted (callers zero-fill).
        """
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM jobs GROUP BY status"
            ).fetchall()
        # ``status`` is CHECK-constrained to the JobStatus values, so the cast
        # never raises; were it to (only DB corruption could), letting it
        # surface beats silently dropping a state from the metrics.
        return {JobStatus(status): count for status, count in rows}

    def sweep_incomplete(self, detail: str) -> int:
        """Mark every queued/running job failed with ``detail``.

        Args:
            detail: Fixed failure reason to stamp on every transitioned job.

        Returns:
            The number of jobs transitioned.
        """
        with _connect(self._db_path) as conn:
            cursor = conn.execute(
                _FAIL_TRANSITION_SET + " WHERE status IN (?, ?)",
                (
                    JobStatus.FAILED.value,
                    _to_utc_iso(datetime.now(tz=UTC)),
                    detail,
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                ),
            )
            return cursor.rowcount

    def purge_older_than(self, cutoff: datetime) -> int:
        """Delete every job created before ``cutoff``, any status or token.

        Args:
            cutoff: Timezone-aware boundary; jobs created strictly before
                it, including their stored results, are deleted.

        Returns:
            The number of jobs deleted.

        Raises:
            ValueError: ``cutoff`` is naive.
        """
        if cutoff.tzinfo is None:
            msg = "cutoff must be timezone-aware"
            raise ValueError(msg)
        # Full table scan is deliberate — no index; dozens of rows at
        # single-user scale with 7-day retention.
        with _connect(self._db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM jobs WHERE created_at < ?",
                (_to_utc_iso(cutoff),),
            )
            return cursor.rowcount

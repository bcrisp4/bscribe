"""Ports (Protocol interfaces) the domain core depends on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from bscribe.domain.models import (
        Job,
        JobStatus,
        OcrMode,
        OutputFormat,
        ParsedDocument,
        Token,
    )


@runtime_checkable
class ParserPort(Protocol):
    """Converts one document file into text or markdown.

    Deliberately synchronous: parsing is CPU-bound native work that runs
    inside worker processes (see docs/design.md — Job execution), never on
    the event loop.
    """

    def parse(
        self, path: Path, *, output: OutputFormat, ocr: OcrMode
    ) -> ParsedDocument:
        """Parse the document at ``path``.

        Args:
            path: Document file to parse; must exist.
            output: Desired content format.
            ocr: OCR behavior for scanned/complex pages.

        Returns:
            The extracted content plus conversion metadata.

        Raises:
            DocumentUnparseableError: The engine could not parse the
                document (corrupt, encrypted, or otherwise unreadable).
            FileNotFoundError: ``path`` does not exist (caller bug, not a
                document problem).
        """
        ...


@runtime_checkable
class JobStorePort(Protocol):
    """Persists async conversion jobs (docs/design.md — Interfaces, M2).

    Deliberately synchronous, like :class:`TokenStorePort`: implementations
    back onto fast local storage and are called from code running on the
    threadpool, never on the event loop (docs/adr/0002). Workers never see
    this port — the FastAPI parent owns all job state.

    Three contracts every implementation must honor:

    * **Token scoping.** ``get``, ``get_result``, ``list_for_token`` and
      ``delete`` are scoped to the owning token; a wrong ``token_id``
      behaves exactly like a missing job (``None``/``False``), so job
      existence never leaks across tokens (the indistinguishable-404 rule).
    * **Guarded transitions.** The ``mark_*`` methods are compare-and-set:
      they apply only from the expected prior status and report ``False``
      otherwise — a late transition after a delete or a competing
      transition must be a no-op, never an upsert or an overwrite.
    * **Metadata/result split.** ``get`` and ``list_for_token`` return
      metadata-only :class:`Job` snapshots and must not read the stored
      content; the result blob is written by ``mark_done`` and read back
      only through ``get_result``, so status polling and listings never
      pay for stored text (it scales with result size, not job count).
    """

    def add(self, job: Job) -> None:
        """Persist a freshly queued job (as minted by ``create_job``).

        Only ``queued`` jobs may enter this way — every later state is
        reached through the ``mark_*`` transitions. This stands in for the
        old ``Job``-level "result iff done" invariant: since ``Job`` is
        metadata-only, admitting a terminal snapshot here would create a
        ``done`` row that never had a result to store.

        Args:
            job: The job record to store; ``id`` must be unique and
                ``status`` must be ``queued``.

        Raises:
            ValueError: ``job`` is not ``queued``.
        """
        ...

    def get(self, job_id: str, token_id: str) -> Job | None:
        """Fetch a job's metadata, owned by ``token_id``.

        Args:
            job_id: The job's id.
            token_id: The calling token's id — the ownership scope.

        Returns:
            The job (metadata only — see the class docstring), or ``None``
            for an unknown id *or* a job owned by a different token
            (indistinguishable by design).
        """
        ...

    def get_result(self, job_id: str, token_id: str) -> ParsedDocument | None:
        """Fetch a done job's stored result, owned by ``token_id``.

        The only read path for stored content — see the metadata/result
        split in the class docstring.

        Args:
            job_id: The job's id.
            token_id: The calling token's id — the ownership scope.

        Returns:
            The stored result iff the job exists, is owned by ``token_id``
            and is ``done``; ``None`` otherwise (unknown id, another
            token's job, or any non-``done`` status — the caller
            distinguishes those cases via ``get``).
        """
        ...

    def list_for_token(
        self, token_id: str, *, status: JobStatus | None = None
    ) -> list[Job]:
        """List a token's jobs, newest first.

        Ordering is ``created_at`` descending with ``id`` descending as a
        deterministic tiebreak — part of the contract, so callers can rely
        on a stable order across implementations.

        Args:
            token_id: The calling token's id.
            status: If given, only jobs currently in this state.

        Returns:
            The token's matching jobs (metadata only — see the class
            docstring); never another token's.
        """
        ...

    def mark_running(self, job_id: str) -> bool:
        """Transition ``queued`` → ``running``, stamping ``started_at``.

        Args:
            job_id: The job's id.

        Returns:
            ``True`` if the transition applied; ``False`` for a missing
            job or any other prior status.
        """
        ...

    def mark_done(self, job_id: str, result: ParsedDocument) -> bool:
        """Transition ``running`` → ``done``, storing the result.

        Stamps ``finished_at``.

        Args:
            job_id: The job's id.
            result: The parse result to store inline.

        Returns:
            ``True`` if the transition applied; ``False`` for a missing
            job (e.g. cancelled mid-parse) or any other prior status —
            the caller should then discard ``result``.
        """
        ...

    def mark_failed(self, job_id: str, detail: str) -> bool:
        """Transition ``queued``/``running`` → ``failed``.

        Stamps ``finished_at``. Queued jobs can fail directly (e.g. the
        pool rejects the submission); a ``done`` job's result is never
        clobbered.

        Args:
            job_id: The job's id.
            detail: Human-readable failure reason (e.g. ``"timeout"``).
                Stored and later surfaced to callers via the job endpoints,
                so it must never carry document content — in particular,
                never pass a parser exception message here: liteparse's
                ``ParseError`` may quote document internals (see
                docs/design.md — Privacy, and CLAUDE.md's liteparse notes).

        Returns:
            ``True`` if the transition applied; ``False`` for a missing
            job or a terminal prior status.
        """
        ...

    def delete(self, job_id: str, token_id: str) -> bool:
        """Delete a job owned by ``token_id``, removing its stored result.

        Removal is logical: an implementation need not scrub freed storage
        (SQLite keeps deleted pages until checkpoint/vacuum). At-rest
        protection of the underlying volume is the operator's
        responsibility (docs/design.md — Security).

        Args:
            job_id: The job's id.
            token_id: The calling token's id — the ownership scope.

        Returns:
            ``True`` if a job was deleted; ``False`` for an unknown id or
            another token's job (indistinguishable by design).
        """
        ...


@runtime_checkable
class TokenStorePort(Protocol):
    """Persists bearer-token principals (docs/design.md — Admin CLI).

    Deliberately synchronous: implementations back onto fast local storage,
    and the server calls it from sync FastAPI dependencies that run on the
    threadpool, never on the event loop (docs/adr/0002).

    Stores only ever see secret hashes — plaintext secrets exist solely at
    mint time (see :mod:`bscribe.domain.tokens`).
    """

    def add(self, token: Token) -> None:
        """Persist a new token.

        Args:
            token: The token record to store; ``id`` and ``secret_hash``
                must be unique.
        """
        ...

    def find_by_secret_hash(self, secret_hash: str) -> Token | None:
        """Look up the token whose secret hashes to ``secret_hash``.

        Args:
            secret_hash: SHA-256 hex digest of a presented bearer token.

        Returns:
            The matching token, or ``None`` — the auth failure path.
        """
        ...

    def list_all(self) -> list[Token]:
        """Return every stored token, newest first (``created_at`` desc).

        Returns:
            All token records; hashes only, never plaintext secrets.
        """
        ...

    def delete(self, token_id: str) -> bool:
        """Delete a token by id, revoking it immediately.

        Args:
            token_id: The token's immutable id.

        Returns:
            ``True`` if a token was deleted, ``False`` for an unknown id.
        """
        ...

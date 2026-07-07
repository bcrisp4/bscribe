"""Domain models for document conversion and caller identity."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


class OutputFormat(StrEnum):
    """Conversion output format; values are the API wire strings."""

    MARKDOWN = "markdown"
    TEXT = "text"


class OcrMode(StrEnum):
    """OCR behavior; values are the API wire strings.

    AUTO applies OCR to pages the engine's complexity detection flags;
    OFF disables OCR entirely. There is deliberately no FORCE — liteparse
    exposes OCR control only as a boolean (see docs/design.md, Closed
    issues); adding it later is an additive API change.
    """

    AUTO = "auto"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Result of parsing one document: content plus conversion metadata.

    An ``ocr_used`` quality signal is deliberately absent for now: liteparse
    does not report whether OCR ran, and deriving it cost a duplicate
    document pass — see docs/design.md, Closed issues. Adding it back when
    the engine exposes the signal is an additive change.

    Attributes:
        content: Extracted text in the requested output format.
        pages: Number of pages in the parsed document.
        duration_ms: Wall-clock parse duration in milliseconds.
    """

    content: str
    pages: int
    duration_ms: float


class JobStatus(StrEnum):
    """Async job lifecycle state; values are the API wire strings.

    Transitions (enforced by the job store, not this type):
    queued → running → done | failed, plus queued → failed for jobs that
    never start (docs/design.md — job lifecycle).
    """

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Job:
    """An async conversion job (docs/design.md — Interfaces, M2).

    A point-in-time snapshot: instances are immutable, and lifecycle
    transitions happen through job-store methods that stamp timestamps and
    guard the state machine — never by mutating a ``Job``.

    Attributes:
        id: Opaque immutable identifier (see
            :func:`bscribe.domain.jobs.create_job`).
        token_id: Owning bearer token's id — the ownership stamp; every job
            endpoint is scoped to it. Deliberately no foreign key semantics:
            a deleted token's jobs orphan until the TTL purge.
        output: Requested output format, needed to build the result response.
        ocr: Requested OCR mode, recorded for operator debugging only
            (this is the request parameter, not the deferred ``ocr_used``
            signal — see docs/design.md, Closed issues).
        status: Current lifecycle state.
        created_at: Submission time, UTC-aware.
        started_at: When parsing began; ``None`` until running.
        finished_at: When the job reached a terminal state; ``None`` before.
        failure_detail: Human-readable failure reason (e.g. ``"timeout"``);
            non-``None`` iff ``status`` is ``FAILED``.
        result: Parse result; non-``None`` iff ``status`` is ``DONE``.
    """

    id: str
    token_id: str
    output: OutputFormat
    ocr: OcrMode
    status: JobStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    failure_detail: str | None
    result: ParsedDocument | None


@dataclass(frozen=True, slots=True)
class Token:
    """A bearer-token principal (docs/design.md — Admin CLI, Security).

    Carries only the SHA-256 hash of the secret, never the plaintext —
    the plaintext exists once, at mint time, and is shown to the operator
    exactly once (see :func:`bscribe.domain.tokens.mint_token`).

    Attributes:
        id: Short opaque immutable identifier; jobs stamp it, so relabeling
            never orphans jobs.
        label: Human-readable caller name (e.g. ``bsearch``); may appear in
            logs, unlike secrets.
        secret_hash: SHA-256 hex digest of the full secret string.
        created_at: Creation time, UTC-aware.
    """

    id: str
    label: str
    secret_hash: str
    created_at: datetime

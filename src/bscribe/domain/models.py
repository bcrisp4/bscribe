"""Domain models for document conversion and caller identity."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime


class Component(StrEnum):
    """Pipeline component identity; values are the wire/fingerprint keys."""

    BSCRIBE = "bscribe"
    LITEPARSE = "liteparse"
    PDFIUM = "pdfium"
    TESSERACT = "tesseract"
    TESSDATA = "tessdata"
    IMAGEMAGICK = "imagemagick"
    LIBREOFFICE = "libreoffice"
    GHOSTSCRIPT = "ghostscript"
    LIBRSVG = "librsvg"


@dataclass(frozen=True, slots=True)
class PipelineStamp:
    """A pipeline fingerprint plus the component versions behind it.

    One type serves two roles: the app-wide identity (``components`` covers
    all nine :class:`Component` values, as discovered at startup) and a
    parse result's traversed subset (``components`` filtered to only what
    that document went through — see
    :func:`bscribe.domain.pipeline.traversed_stamp`). Both share the same
    ``fingerprint``, since it hashes the full app-wide set regardless of
    what any one document traversed (docs/design.md, Re-ingestion contract).

    ``components`` is a plain ``str``-keyed mapping rather than
    ``Component``-keyed: stamps round-trip through storage (SQLite), and a
    str-keyed shape tolerates an unrecognized or since-removed component key
    on read without raising, instead of a strict enum lookup that would.

    Attributes:
        fingerprint: Twelve lowercase hex characters — see
            :func:`bscribe.domain.pipeline.compute_fingerprint`.
        components: Component wire key -> version string, e.g.
            ``{"bscribe": "0.3.0", "liteparse": "2.5.0"}``.
    """

    fingerprint: str
    components: Mapping[str, str]


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
        pipeline: The components-traversed stamp as of parse time (see
            :class:`PipelineStamp`). ``None`` for results stored before this
            feature existed, or not yet stamped — stamping happens
            parent-side in ``WorkerPool``, not inside the worker process
            that produces this ``ParsedDocument``.
    """

    content: str
    pages: int
    duration_ms: float
    pipeline: PipelineStamp | None = None


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
    """An async conversion job's metadata (docs/design.md — Interfaces, M2).

    A point-in-time snapshot: instances are immutable, and lifecycle
    transitions happen through job-store methods that stamp timestamps and
    guard the state machine — never by mutating a ``Job``.

    Deliberately metadata-only: a done job's :class:`ParsedDocument` is
    never carried here — it is written by ``JobStorePort.mark_done`` and
    read back only through ``JobStorePort.get_result``, so status polling
    and listings never pay for the stored content blob.

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

    Raises:
        ValueError: On construction, if the terminal-state invariants above
            are violated or any timestamp is naive (naive datetimes would
            corrupt the store's chronological ordering).
    """

    id: str
    token_id: str
    output: OutputFormat
    ocr: OcrMode
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_detail: str | None = None

    def __post_init__(self) -> None:
        for name in ("created_at", "started_at", "finished_at"):
            value: datetime | None = getattr(self, name)
            if value is not None and value.tzinfo is None:
                msg = f"{name} must be timezone-aware"
                raise ValueError(msg)
        if (self.failure_detail is not None) != (self.status is JobStatus.FAILED):
            msg = "failure_detail must be set iff status is failed"
            raise ValueError(msg)


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

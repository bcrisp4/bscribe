"""Tests for bscribe.domain.ports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from bscribe.domain.models import (
    Job,
    JobStatus,
    OcrMode,
    OutputFormat,
    ParsedDocument,
    Token,
)
from bscribe.domain.ports import JobStorePort, ParserPort, TokenStorePort
from tests.unit.fakes import FakeJobStore

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class FakeParser:
    """In-memory ParserPort implementation for domain-level tests."""

    result: ParsedDocument

    def parse(
        self, path: Path, *, output: OutputFormat, ocr: OcrMode
    ) -> ParsedDocument:
        del path, output, ocr
        return self.result


class TestParserPortConformance:
    """ParserPort is runtime-checkable: structural isinstance checks work."""

    def test_fake_parser_satisfies_port(self) -> None:
        fake = FakeParser(
            result=ParsedDocument(content="text", pages=1, duration_ms=1.0)
        )
        assert isinstance(fake, ParserPort)

    def test_object_without_parse_does_not_satisfy_port(self) -> None:
        assert not isinstance(object(), ParserPort)


@dataclass
class FakeTokenStore:
    """In-memory TokenStorePort implementation for domain-level tests."""

    tokens: dict[str, Token] = field(default_factory=dict[str, "Token"])

    def add(self, token: Token) -> None:
        self.tokens[token.id] = token

    def find_by_secret_hash(self, secret_hash: str) -> Token | None:
        return next(
            (t for t in self.tokens.values() if t.secret_hash == secret_hash),
            None,
        )

    def list_all(self) -> list[Token]:
        return sorted(self.tokens.values(), key=lambda t: t.created_at, reverse=True)

    def delete(self, token_id: str) -> bool:
        return self.tokens.pop(token_id, None) is not None


class TestJobStorePortConformance:
    """JobStorePort is runtime-checkable: structural isinstance checks work."""

    def test_fake_job_store_satisfies_port(self) -> None:
        # Typed assignment: pyright verifies full structural conformance
        # (signatures, not just method names like the isinstance check).
        store: JobStorePort = FakeJobStore()
        assert isinstance(store, JobStorePort)

    def test_fake_add_rejects_duplicate_id_like_the_adapter(self) -> None:
        """SqliteJobStore raises on a duplicate PRIMARY KEY; the fake must
        not silently upsert, or domain tests would mask double-submit bugs."""
        store = FakeJobStore()
        job = Job(
            id="abcd1234abcd1234",
            token_id="feed0001",
            output=OutputFormat.MARKDOWN,
            ocr=OcrMode.AUTO,
            status=JobStatus.QUEUED,
            created_at=datetime(2026, 7, 7, tzinfo=UTC),
        )
        store.add(job)
        with pytest.raises(ValueError, match="duplicate"):
            store.add(job)

    def test_fake_add_rejects_non_queued_job_like_the_adapter(self) -> None:
        """The port's queued-only add contract must hold in the fake too."""
        store = FakeJobStore()
        job = Job(
            id="abcd1234abcd1234",
            token_id="feed0001",
            output=OutputFormat.MARKDOWN,
            ocr=OcrMode.AUTO,
            status=JobStatus.FAILED,
            created_at=datetime(2026, 7, 7, tzinfo=UTC),
            failure_detail="timeout",
        )
        with pytest.raises(ValueError, match="queued"):
            store.add(job)

    def test_object_without_methods_does_not_satisfy_port(self) -> None:
        assert not isinstance(object(), JobStorePort)

    def test_token_store_does_not_satisfy_job_store_port(self) -> None:
        assert not isinstance(FakeTokenStore(), JobStorePort)

    def test_sweep_transitions_queued_and_running_to_failed(self) -> None:
        store = FakeJobStore()
        queued = Job(
            id="000000000000000a",
            token_id="feed0001",
            output=OutputFormat.MARKDOWN,
            ocr=OcrMode.AUTO,
            status=JobStatus.QUEUED,
            created_at=datetime(2026, 7, 7, tzinfo=UTC),
        )
        running = Job(
            id="000000000000000b",
            token_id="feed0001",
            output=OutputFormat.MARKDOWN,
            ocr=OcrMode.AUTO,
            status=JobStatus.QUEUED,
            created_at=datetime(2026, 7, 7, tzinfo=UTC),
        )
        store.add(queued)
        store.add(running)
        store.mark_running(running.id)

        assert store.sweep_incomplete("interrupted by restart — resubmit") == 2

        for job_id in (queued.id, running.id):
            found = store.get(job_id, "feed0001")
            assert found is not None
            assert found.status is JobStatus.FAILED
            assert found.failure_detail == "interrupted by restart — resubmit"
            assert found.finished_at is not None

    def test_sweep_leaves_terminal_jobs_untouched(self) -> None:
        store = FakeJobStore()
        done = Job(
            id="000000000000000a",
            token_id="feed0001",
            output=OutputFormat.MARKDOWN,
            ocr=OcrMode.AUTO,
            status=JobStatus.QUEUED,
            created_at=datetime(2026, 7, 7, tzinfo=UTC),
        )
        failed = Job(
            id="000000000000000b",
            token_id="feed0001",
            output=OutputFormat.MARKDOWN,
            ocr=OcrMode.AUTO,
            status=JobStatus.QUEUED,
            created_at=datetime(2026, 7, 7, tzinfo=UTC),
        )
        store.add(done)
        store.add(failed)
        store.mark_running(done.id)
        store.mark_done(
            done.id, ParsedDocument(content="text", pages=1, duration_ms=1.0)
        )
        store.mark_failed(failed.id, "timeout")

        assert store.sweep_incomplete("interrupted by restart — resubmit") == 0

        assert store.get(done.id, "feed0001").status is JobStatus.DONE  # type: ignore[union-attr]
        found_failed = store.get(failed.id, "feed0001")
        assert found_failed is not None
        assert found_failed.failure_detail == "timeout"

    def test_sweep_on_empty_store_returns_zero(self) -> None:
        assert FakeJobStore().sweep_incomplete("interrupted by restart") == 0

    def test_purge_deletes_strictly_older_rows_across_tokens(self) -> None:
        store = FakeJobStore()
        old = Job(
            id="000000000000000a",
            token_id="feed0001",
            output=OutputFormat.MARKDOWN,
            ocr=OcrMode.AUTO,
            status=JobStatus.QUEUED,
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        new = Job(
            id="000000000000000b",
            token_id="0therT0k",
            output=OutputFormat.MARKDOWN,
            ocr=OcrMode.AUTO,
            status=JobStatus.QUEUED,
            created_at=datetime(2026, 7, 5, tzinfo=UTC),
        )
        store.add(old)
        store.add(new)
        store.mark_running(old.id)
        store.mark_done(
            old.id, ParsedDocument(content="text", pages=1, duration_ms=1.0)
        )
        cutoff = datetime(2026, 7, 3, tzinfo=UTC)

        assert store.purge_older_than(cutoff) == 1

        assert store.get(old.id, "feed0001") is None
        assert store.get_result(old.id, "feed0001") is None
        assert store.get(new.id, "0therT0k") == new

    def test_purge_boundary_is_strictly_before_cutoff(self) -> None:
        """A job created at exactly the cutoff instant survives (strict <)."""
        store = FakeJobStore()
        cutoff = datetime(2026, 7, 3, tzinfo=UTC)
        at_cutoff = Job(
            id="000000000000000a",
            token_id="feed0001",
            output=OutputFormat.MARKDOWN,
            ocr=OcrMode.AUTO,
            status=JobStatus.QUEUED,
            created_at=cutoff,
        )
        just_under = Job(
            id="000000000000000b",
            token_id="feed0001",
            output=OutputFormat.MARKDOWN,
            ocr=OcrMode.AUTO,
            status=JobStatus.QUEUED,
            created_at=cutoff - timedelta(microseconds=1),
        )
        store.add(at_cutoff)
        store.add(just_under)

        assert store.purge_older_than(cutoff) == 1

        assert store.get(at_cutoff.id, "feed0001") == at_cutoff
        assert store.get(just_under.id, "feed0001") is None

    def test_purge_with_naive_cutoff_raises(self) -> None:
        store = FakeJobStore()
        with pytest.raises(ValueError, match="aware"):
            store.purge_older_than(datetime(2026, 7, 1))  # noqa: DTZ001


class TestTokenStorePortConformance:
    """TokenStorePort is runtime-checkable: structural isinstance checks work."""

    def test_fake_token_store_satisfies_port(self) -> None:
        assert isinstance(FakeTokenStore(), TokenStorePort)

    def test_object_without_methods_does_not_satisfy_port(self) -> None:
        assert not isinstance(object(), TokenStorePort)

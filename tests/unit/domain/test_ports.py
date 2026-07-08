"""Tests for bscribe.domain.ports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
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


class TestTokenStorePortConformance:
    """TokenStorePort is runtime-checkable: structural isinstance checks work."""

    def test_fake_token_store_satisfies_port(self) -> None:
        assert isinstance(FakeTokenStore(), TokenStorePort)

    def test_object_without_methods_does_not_satisfy_port(self) -> None:
        assert not isinstance(object(), TokenStorePort)

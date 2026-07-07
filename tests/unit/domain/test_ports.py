"""Tests for bscribe.domain.ports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bscribe.domain.models import OcrMode, OutputFormat, ParsedDocument, Token
from bscribe.domain.ports import ParserPort, TokenStorePort

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


class TestTokenStorePortConformance:
    """TokenStorePort is runtime-checkable: structural isinstance checks work."""

    def test_fake_token_store_satisfies_port(self) -> None:
        assert isinstance(FakeTokenStore(), TokenStorePort)

    def test_object_without_methods_does_not_satisfy_port(self) -> None:
        assert not isinstance(object(), TokenStorePort)

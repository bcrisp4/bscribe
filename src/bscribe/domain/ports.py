"""Ports (Protocol interfaces) the domain core depends on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from bscribe.domain.models import OcrMode, OutputFormat, ParsedDocument, Token


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

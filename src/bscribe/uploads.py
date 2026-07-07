"""Streaming upload staging with a hard size cap.

Copies a multipart upload to a scratch file in bounded-memory chunks and
enforces the global upload limit as it writes. This streaming byte count is
the authoritative size guard (the Content-Length prefilter in
``bscribe.app`` is only an advisory pre-receipt reject — headers are absent
or spoofable). On overflow the partial file is left for the caller's
cleanup (the endpoint unlinks the scratch file in a ``finally``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

# 1 MiB balances syscall count against peak memory; well under the Pi's RAM.
CHUNK_SIZE = 1 << 20


class UploadTooLargeError(Exception):
    """The upload exceeded the configured limit; maps to 413."""


class AsyncChunkReader(Protocol):
    """The slice of Starlette's ``UploadFile`` that ``spool_upload`` needs."""

    async def read(self, size: int = -1, /) -> bytes: ...


async def spool_upload(upload: AsyncChunkReader, *, dest: Path, max_bytes: int) -> None:
    """Stream ``upload`` to ``dest``, failing if it exceeds ``max_bytes``.

    Args:
        upload: The uploaded file to drain, read in ``CHUNK_SIZE`` pieces.
        dest: Scratch path to write the bytes to (created/truncated).
        max_bytes: Inclusive size limit; a strictly larger upload is rejected.

    Raises:
        UploadTooLargeError: The cumulative bytes read exceeded ``max_bytes``.
            Writing stops immediately; the partial ``dest`` is left for the
            caller to unlink.
    """
    written = 0
    with dest.open("wb") as fh:
        while chunk := await upload.read(CHUNK_SIZE):
            written += len(chunk)
            if written > max_bytes:
                raise UploadTooLargeError
            fh.write(chunk)

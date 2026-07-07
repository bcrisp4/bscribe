"""Tests for bscribe.uploads."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bscribe.uploads import UploadTooLargeError, spool_upload

if TYPE_CHECKING:
    from pathlib import Path


class FakeUpload:
    """Minimal async chunk reader standing in for Starlette's UploadFile.

    UploadFile is the I/O boundary; a fake keeps the test off the real
    multipart machinery while exercising the exact chunked-read contract.
    """

    def __init__(self, data: bytes, *, chunk_size: int) -> None:
        self._data = data
        self._chunk_size = chunk_size
        self._pos = 0

    async def read(self, size: int = -1, /) -> bytes:
        # Honor a bounded request but never hand back more than one chunk,
        # so the test drives the same many-reads path as a real upload.
        want = self._chunk_size if size < 0 else min(size, self._chunk_size)
        chunk = self._data[self._pos : self._pos + want]
        self._pos += len(chunk)
        return chunk


class TestSpoolUpload:
    async def test_writes_full_small_file(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.bin"
        payload = b"hello world" * 10
        await spool_upload(FakeUpload(payload, chunk_size=8), dest=dest, max_bytes=1000)
        assert dest.read_bytes() == payload

    async def test_accepts_exactly_max_bytes(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.bin"
        payload = b"x" * 64
        await spool_upload(FakeUpload(payload, chunk_size=8), dest=dest, max_bytes=64)
        assert dest.read_bytes() == payload

    async def test_raises_when_one_byte_over_limit(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.bin"
        payload = b"x" * 65
        with pytest.raises(UploadTooLargeError):
            await spool_upload(
                FakeUpload(payload, chunk_size=8), dest=dest, max_bytes=64
            )

    async def test_reads_in_multiple_chunks(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.bin"
        payload = b"abcdefghij"
        upload = FakeUpload(payload, chunk_size=3)
        await spool_upload(upload, dest=dest, max_bytes=1000)
        # chunk_size 3 over 10 bytes means the loop iterated more than once.
        assert dest.read_bytes() == payload

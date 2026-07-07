"""Tests for bscribe.domain.formats."""

from __future__ import annotations

import pytest

from bscribe.domain.errors import UnsupportedFormatError
from bscribe.domain.formats import supported_extension


class TestSupportedExtension:
    @pytest.mark.parametrize(
        "filename",
        [
            "scan.pdf",
            "report.docx",
            "legacy.doc",
            "macro.docm",
            "sheet.xlsx",
            "sheet.xlsb",
            "data.csv",
            "data.tsv",
            "deck.pptx",
            "deck.pptm",
            "notes.odt",
            "book.ods",
            "slides.odp",
            "letter.rtf",
            "keynote.key",
            "apple.pages",
            "apple.numbers",
            "photo.png",
            "photo.jpg",
            "photo.jpeg",
            "scan.tiff",
            "scan.tif",
            "web.webp",
            "vector.svg",
        ],
    )
    def test_accepts_supported_family(self, filename: str) -> None:
        # Returns the normalized extension so the caller can reuse it.
        assert supported_extension(filename) == f".{filename.rsplit('.', 1)[1]}"

    def test_normalizes_extension_case(self) -> None:
        assert supported_extension("SCAN.PDF") == ".pdf"

    @pytest.mark.parametrize(
        "filename",
        ["archive.zip", "program.exe", "notes.txt", "readme.md", "server.log"],
    )
    def test_rejects_unsupported_extension(self, filename: str) -> None:
        with pytest.raises(UnsupportedFormatError):
            supported_extension(filename)

    @pytest.mark.parametrize("filename", [None, "", "noextension"])
    def test_rejects_missing_extension(self, filename: str | None) -> None:
        with pytest.raises(UnsupportedFormatError):
            supported_extension(filename)

    def test_does_not_echo_extension_in_exception(self) -> None:
        # Privacy/convention: submitted values are never surfaced. The 415
        # handler emits a generic detail, so the exception carries no ext.
        with pytest.raises(UnsupportedFormatError) as excinfo:
            supported_extension("program.exe")
        assert "exe" not in str(excinfo.value)

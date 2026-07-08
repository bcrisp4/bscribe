"""Tests for bscribe.domain.formats."""

from __future__ import annotations

import pytest

from bscribe.domain.errors import UnsupportedFormatError
from bscribe.domain.formats import (
    IMAGE_EXTENSIONS,
    OFFICE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    supported_extension,
)

# Frozen copy of the allowlist as it stood before the OFFICE/IMAGE split —
# proves the refactor preserved membership exactly.
_EXPECTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".docm",
        ".dot",
        ".dotm",
        ".dotx",
        ".odt",
        ".ott",
        ".rtf",
        ".pages",
        ".ppt",
        ".pptx",
        ".pptm",
        ".pot",
        ".potm",
        ".potx",
        ".odp",
        ".otp",
        ".key",
        ".xls",
        ".xlsx",
        ".xlsm",
        ".xlsb",
        ".ods",
        ".ots",
        ".csv",
        ".tsv",
        ".numbers",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".tiff",
        ".tif",
        ".webp",
        ".svg",
    }
)


class TestSupportedExtensionsGroups:
    def test_supported_extensions_is_pdf_plus_office_plus_image(self) -> None:
        assert OFFICE_EXTENSIONS | IMAGE_EXTENSIONS | {".pdf"} == SUPPORTED_EXTENSIONS

    def test_membership_unchanged_by_the_office_image_split(self) -> None:
        assert SUPPORTED_EXTENSIONS == _EXPECTED_EXTENSIONS

    def test_office_and_image_groups_are_disjoint(self) -> None:
        assert not (OFFICE_EXTENSIONS & IMAGE_EXTENSIONS)


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

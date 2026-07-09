"""Tests for bscribe.domain.pipeline."""

from __future__ import annotations

import string

import pytest

from bscribe.domain.formats import SUPPORTED_EXTENSIONS
from bscribe.domain.models import Component, OcrMode, PipelineStamp
from bscribe.domain.pipeline import (
    compute_fingerprint,
    traversed_components,
    traversed_stamp,
)

ALL_COMPONENT_VERSIONS: dict[str, str] = {
    Component.BSCRIBE: "0.3.0",
    Component.LITEPARSE: "2.5.0",
    Component.PDFIUM: "119.0.6045.0",
    Component.TESSERACT: "5.3.4",
    Component.TESSDATA: "abc123def456",
    Component.IMAGEMAGICK: "6.9.12-98",
    Component.LIBREOFFICE: "7.6.4.1",
    Component.GHOSTSCRIPT: "10.03.1",
    Component.LIBRSVG: "2.54.7",
}


class TestTraversedComponents:
    """Traversal is a pure function of (extension, ocr) — no I/O, no state."""

    @pytest.mark.parametrize(
        ("extension", "ocr", "expected"),
        [
            (
                ".pdf",
                OcrMode.OFF,
                {Component.BSCRIBE, Component.LITEPARSE, Component.PDFIUM},
            ),
            (
                ".pdf",
                OcrMode.AUTO,
                {
                    Component.BSCRIBE,
                    Component.LITEPARSE,
                    Component.PDFIUM,
                    Component.TESSERACT,
                    Component.TESSDATA,
                },
            ),
            (
                ".docx",
                OcrMode.OFF,
                {
                    Component.BSCRIBE,
                    Component.LITEPARSE,
                    Component.PDFIUM,
                    Component.LIBREOFFICE,
                },
            ),
            (
                ".csv",
                OcrMode.AUTO,
                {
                    Component.BSCRIBE,
                    Component.LITEPARSE,
                    Component.PDFIUM,
                    Component.LIBREOFFICE,
                    Component.TESSERACT,
                    Component.TESSDATA,
                },
            ),
            (
                ".png",
                OcrMode.OFF,
                {
                    Component.BSCRIBE,
                    Component.LITEPARSE,
                    Component.PDFIUM,
                    Component.IMAGEMAGICK,
                },
            ),
            (
                ".svg",
                OcrMode.AUTO,
                {
                    Component.BSCRIBE,
                    Component.LITEPARSE,
                    Component.PDFIUM,
                    Component.IMAGEMAGICK,
                    Component.LIBRSVG,
                    Component.TESSERACT,
                    Component.TESSDATA,
                },
            ),
        ],
        ids=[
            "pdf-off",
            "pdf-auto",
            "docx-off",
            "csv-auto",
            "png-off",
            "svg-auto",
        ],
    )
    def test_traversal_table(
        self, extension: str, ocr: OcrMode, expected: set[Component]
    ) -> None:
        assert traversed_components(extension, ocr) == frozenset(expected)

    @pytest.mark.parametrize("extension", sorted(SUPPORTED_EXTENSIONS))
    @pytest.mark.parametrize("ocr", list(OcrMode))
    def test_ghostscript_never_traversed(self, extension: str, ocr: OcrMode) -> None:
        # liteparse only gates .svg support on gs presence; the actual SVG
        # render goes through librsvg, so Ghostscript is fingerprint-only.
        assert Component.GHOSTSCRIPT not in traversed_components(extension, ocr)

    def test_core_trio_always_traversed(self) -> None:
        # Every supported extension traverses bscribe + liteparse + pdfium,
        # regardless of format or OCR mode.
        core = {Component.BSCRIBE, Component.LITEPARSE, Component.PDFIUM}
        for extension in SUPPORTED_EXTENSIONS:
            for ocr in OcrMode:
                assert core <= traversed_components(extension, ocr)


class TestComputeFingerprint:
    """Fingerprint = a deterministic 12-char hex digest over all versions."""

    def test_deterministic_for_same_input(self) -> None:
        first = compute_fingerprint(ALL_COMPONENT_VERSIONS)
        second = compute_fingerprint(dict(ALL_COMPONENT_VERSIONS))
        assert first == second

    def test_insensitive_to_key_insertion_order(self) -> None:
        reordered = dict(reversed(ALL_COMPONENT_VERSIONS.items()))
        assert compute_fingerprint(ALL_COMPONENT_VERSIONS) == compute_fingerprint(
            reordered
        )

    @pytest.mark.parametrize("component", sorted(ALL_COMPONENT_VERSIONS))
    def test_changes_when_any_single_version_changes(self, component: str) -> None:
        bumped = dict(ALL_COMPONENT_VERSIONS)
        bumped[component] = f"{bumped[component]}-bumped"
        assert compute_fingerprint(bumped) != compute_fingerprint(
            ALL_COMPONENT_VERSIONS
        )

    def test_is_twelve_lowercase_hex_characters(self) -> None:
        fingerprint = compute_fingerprint(ALL_COMPONENT_VERSIONS)
        assert len(fingerprint) == 12
        assert all(c in string.hexdigits.lower() for c in fingerprint)
        assert fingerprint == fingerprint.lower()


class TestTraversedStamp:
    """Filters an app-wide stamp down to one document's traversed subset."""

    def test_preserves_fingerprint(self) -> None:
        info = PipelineStamp(
            fingerprint="abc123def456", components=dict(ALL_COMPONENT_VERSIONS)
        )
        stamp = traversed_stamp(info, extension=".pdf", ocr=OcrMode.OFF)
        assert stamp.fingerprint == info.fingerprint

    def test_filters_components_to_traversed_set(self) -> None:
        info = PipelineStamp(
            fingerprint="abc123def456", components=dict(ALL_COMPONENT_VERSIONS)
        )
        stamp = traversed_stamp(info, extension=".docx", ocr=OcrMode.OFF)
        assert set(stamp.components) == {
            Component.BSCRIBE.value,
            Component.LITEPARSE.value,
            Component.PDFIUM.value,
            Component.LIBREOFFICE.value,
        }
        expected = ALL_COMPONENT_VERSIONS[Component.LIBREOFFICE]
        assert stamp.components[Component.LIBREOFFICE.value] == expected

    def test_tolerates_a_traversed_component_missing_from_info(self) -> None:
        # info predating a component being added to discovery should not
        # raise KeyError — it is silently omitted from the filtered stamp.
        sparse = dict(ALL_COMPONENT_VERSIONS)
        del sparse[Component.LIBREOFFICE]
        info = PipelineStamp(fingerprint="abc123def456", components=sparse)
        stamp = traversed_stamp(info, extension=".docx", ocr=OcrMode.OFF)
        assert Component.LIBREOFFICE.value not in stamp.components
        expected = ALL_COMPONENT_VERSIONS[Component.BSCRIBE]
        assert stamp.components[Component.BSCRIBE.value] == expected

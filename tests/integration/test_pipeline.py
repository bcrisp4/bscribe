"""Integration tests for real pipeline component discovery.

No mocks: runs the actual probes (subprocess version checks, importlib
metadata lookups, tessdata hashing) against whatever is actually installed
on the machine running the suite. External tools (ImageMagick, LibreOffice,
Ghostscript, librsvg) are allowed to be absent on a developer machine — the
assertions below tolerate :data:`~bscribe.domain.pipeline.UNAVAILABLE` for
those — but bscribe and liteparse are always-installed dependencies of this
very test run, so they must resolve to a real version.
"""

from __future__ import annotations

import importlib.metadata
import re

from bscribe.domain.models import Component
from bscribe.domain.pipeline import UNAVAILABLE
from bscribe.pipeline import _discover  # pyright: ignore[reportPrivateUsage]

_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{12}$")


class TestDiscoverRealEnvironment:
    def test_all_nine_component_keys_present(self) -> None:
        stamp = _discover()

        assert set(stamp.components) == {c.value for c in Component}

    def test_fingerprint_is_twelve_lowercase_hex_chars(self) -> None:
        stamp = _discover()

        assert _FINGERPRINT_PATTERN.match(stamp.fingerprint)

    def test_bscribe_and_liteparse_resolve_to_real_versions(self) -> None:
        stamp = _discover()

        assert stamp.components[Component.BSCRIBE.value] != UNAVAILABLE
        assert stamp.components[Component.LITEPARSE.value] != UNAVAILABLE

    def test_liteparse_version_matches_importlib_metadata(self) -> None:
        stamp = _discover()

        assert stamp.components[
            Component.LITEPARSE.value
        ] == importlib.metadata.version("liteparse")

    def test_pdfium_and_tesseract_derive_from_liteparse_when_available(self) -> None:
        stamp = _discover()

        liteparse_version = stamp.components[Component.LITEPARSE.value]
        expected = f"bundled (liteparse {liteparse_version})"
        assert stamp.components[Component.PDFIUM.value] == expected
        assert stamp.components[Component.TESSERACT.value] == expected

    def test_external_tools_are_unavailable_or_real_versions(self) -> None:
        """External tools may legitimately be missing on a dev machine —
        only assert that when present, the value is non-empty and not the
        placeholder :data:`UNAVAILABLE` string used for absence."""
        stamp = _discover()

        for component in (
            Component.IMAGEMAGICK,
            Component.LIBREOFFICE,
            Component.GHOSTSCRIPT,
            Component.LIBRSVG,
            Component.TESSDATA,
        ):
            value = stamp.components[component.value]
            assert value
            if value != UNAVAILABLE:
                assert value != ""

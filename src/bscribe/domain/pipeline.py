"""Pipeline fingerprint and components-traversed policy (pure, no I/O).

Supports the re-ingestion contract (docs/design.md, "Re-ingestion contract"):
a document's ``pipeline.fingerprint`` changes whenever any output-affecting
component version changes anywhere in the app, but a caller only needs to
re-parse a given document when a component *on that document's traversed
path* changed.

Traversal is derived from two request-time inputs — the file extension and
the requested :class:`~bscribe.domain.models.OcrMode` — rather than observed
from the parse itself: liteparse routes deterministically by extension and
exposes no "OCR actually applied" signal (see docs/design.md, Closed
issues). Requesting ``ocr=auto`` therefore over-reports Tesseract/tessdata
as traversed even on a born-digital page the engine's complexity detection
skipped; this is accepted (docs/design.md, Re-ingestion contract) — the
cost is an occasional unnecessary re-parse, not an incorrect one.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from bscribe.domain.formats import IMAGE_EXTENSIONS, OFFICE_EXTENSIONS
from bscribe.domain.models import Component, OcrMode, PipelineStamp

if TYPE_CHECKING:
    from collections.abc import Mapping

# Sentinel recorded in place of a version when a component's probe fails.
# It is hashed like any other version string, so a probe failure still
# changes the fingerprint (and is visible in the components map) rather
# than silently omitting the component.
UNAVAILABLE = "unavailable"


def compute_fingerprint(components: Mapping[str, str]) -> str:
    """Hash a component-version map into a short, deterministic fingerprint.

    Args:
        components: Component wire key -> version string. The full
            app-wide set is expected here (see :class:`PipelineStamp`), not
            a per-document traversed subset — the fingerprint is one global
            identity, independent of what any single document traversed.

    Returns:
        Twelve lowercase hex characters. Insertion order of ``components``
        does not affect the result.
    """
    canonical = "\n".join(f"{k}={v}" for k, v in sorted(components.items()))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def traversed_components(extension: str, ocr: OcrMode) -> frozenset[Component]:
    """Return the components a document with this shape would traverse.

    Args:
        extension: Normalized, dot-prefixed lowercase extension (e.g.
            ``.pdf``) — as returned by
            :func:`bscribe.domain.formats.supported_extension` and preserved
            on staged uploads.
        ocr: The requested OCR mode for the parse.

    Returns:
        The set of :class:`Component` values traversed. ``GHOSTSCRIPT`` is
        never included: liteparse only gates ``.svg`` support on Ghostscript
        being *present* on the host, but the actual SVG render goes through
        librsvg (see docs/design.md, Security) — Ghostscript is
        fingerprint-only, not a traversed component.
    """
    traversed = {Component.BSCRIBE, Component.LITEPARSE, Component.PDFIUM}
    if extension in OFFICE_EXTENSIONS:
        traversed.add(Component.LIBREOFFICE)
    if extension in IMAGE_EXTENSIONS:
        traversed.add(Component.IMAGEMAGICK)
    if extension == ".svg":
        traversed.add(Component.LIBRSVG)
    if ocr is OcrMode.AUTO:
        traversed.add(Component.TESSERACT)
        traversed.add(Component.TESSDATA)
    return frozenset(traversed)


def traversed_stamp(
    info: PipelineStamp, *, extension: str, ocr: OcrMode
) -> PipelineStamp:
    """Filter an app-wide :class:`PipelineStamp` to one document's traversal.

    Args:
        info: The app-wide pipeline stamp (all known components).
        extension: Normalized, dot-prefixed lowercase extension of the
            parsed document.
        ocr: The OCR mode requested for the parse.

    Returns:
        A :class:`PipelineStamp` with the same ``fingerprint`` as ``info``
        but ``components`` filtered to the traversed set from
        :func:`traversed_components`. A traversed component missing from
        ``info.components`` is silently skipped rather than raising —
        ``info`` may predate a component being added to discovery.
    """
    traversed = traversed_components(extension, ocr)
    components = {
        component.value: info.components[component.value]
        for component in traversed
        if component.value in info.components
    }
    return PipelineStamp(fingerprint=info.fingerprint, components=components)

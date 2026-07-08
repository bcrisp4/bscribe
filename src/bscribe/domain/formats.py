"""Supported input format allowlist and the 415 gate.

Pure policy, no I/O — reused by the sync convert endpoint (and the async
submission path from M2). The allowlist mirrors liteparse's
``is_supported_extension`` (verified against liteparse 2.4.0,
``crates/liteparse/src/conversion.rs``: the OFFICE/PRESENTATION/SPREADSHEET/
IMAGE extension tables plus ``pdf``). That predicate is not exposed to
Python, so bscribe keeps its own copy; re-verify it on liteparse upgrades.

liteparse dispatches to its converters by file extension, so an extension
allowlist is the same key the engine routes on — one source of truth, and
it avoids magic-byte sniffing that cannot tell office ZIP containers apart
(all ``PK\\x03\\x04``) or recognize the text-based SVG/CSV/TSV formats.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from bscribe.domain.errors import UnsupportedFormatError

# Source of truth: liteparse conversion.rs is_supported_extension.
# Re-verify on liteparse upgrades (the adapter targets 2.4.0).
OFFICE_EXTENSIONS: frozenset[str] = frozenset(
    {
        # office documents (converted to PDF via LibreOffice)
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
        # presentations (LibreOffice)
        ".ppt",
        ".pptx",
        ".pptm",
        ".pot",
        ".potm",
        ".potx",
        ".odp",
        ".otp",
        ".key",
        # spreadsheets (LibreOffice)
        ".xls",
        ".xlsx",
        ".xlsm",
        ".xlsb",
        ".ods",
        ".ots",
        ".csv",
        ".tsv",
        ".numbers",
    }
)

# images (converted to PDF via ImageMagick; .svg also needs Ghostscript)
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
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

SUPPORTED_EXTENSIONS: frozenset[str] = (
    frozenset({".pdf"}) | OFFICE_EXTENSIONS | IMAGE_EXTENSIONS
)


def supported_extension(filename: str | None) -> str:
    """Return the normalized extension of ``filename`` if it is supported.

    Args:
        filename: The client-supplied upload filename (may be ``None``).

    Returns:
        The lower-cased extension including the leading dot (e.g. ``.pdf``),
        suitable for naming the scratch file liteparse routes by.

    Raises:
        UnsupportedFormatError: The extension is missing or not on the
            allowlist. The exception carries no extension value, so callers
            cannot accidentally echo it back to the client.
    """
    ext = PurePosixPath(filename or "").suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError
    return ext

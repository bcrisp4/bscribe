"""Pipeline component version discovery (I/O; app-factory time).

Environment counterpart to the pure :mod:`bscribe.domain.pipeline`: that
module knows how to hash a component-version map into a fingerprint and
how to filter it to one document's traversal, but has no way to *find out*
what is actually installed. This module does the finding out — reading
package metadata, hashing the tessdata directory, and shelling out to the
image/office/PDF toolchain for `--version` output — then hands the result
to :func:`~bscribe.domain.pipeline.compute_fingerprint` to build one
:class:`~bscribe.domain.models.PipelineStamp`.

Runs once per process, at app-factory time (``bscribe.app:create_app``),
via the cached :data:`discover_pipeline`. The uncached :func:`_discover` is
a test seam only.

Failure semantics: a probe can fail for entirely mundane reasons — a dev
machine without LibreOffice installed, a container image that never bakes
Ghostscript. No probe here ever raises; each failure degrades that one
component to :data:`bscribe.domain.pipeline.UNAVAILABLE` and logs a
warning, but discovery as a whole always completes and a fingerprint is
always computed. This is deliberate, not a fallback of last resort: an
environment legitimately missing a tool fingerprints differently from one
that has it, and callers (the re-ingestion contract) need that distinction
more than they need every probe to succeed.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.metadata
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from bscribe.domain.models import Component, PipelineStamp
from bscribe.domain.pipeline import UNAVAILABLE, compute_fingerprint

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = structlog.get_logger()

_PROBE_TIMEOUT_SECONDS = 10.0

_IMAGEMAGICK_PATTERN = r"Version: ImageMagick (\S+)"
_LIBREOFFICE_PATTERN = r"LibreOffice (\S+)"
_LIBRSVG_PATTERN = r"rsvg-convert version (\S+)"
_GHOSTSCRIPT_VALIDATE_PATTERN = r"\d+\.\d+"


def _run(
    component: Component, argv: Sequence[str], *, warn_on_failure: bool = True
) -> str | None:
    """Run a version-probe subprocess; return stdout, or ``None`` on failure.

    Never raises: a missing binary (``FileNotFoundError``), permission
    error, other OS failure, timeout, or nonzero exit all degrade to
    ``None``, plus a warning log naming ``component`` unless
    ``warn_on_failure`` is ``False`` — set by a caller that has its own
    fallback and doesn't want a mid-fallback failure logged as if it were
    final (see :func:`_discover_imagemagick`). ``argv`` is always a
    fixed, hardcoded list supplied by a caller in this module — never user
    input — so this is not the shell-injection or PATH-hijack surface
    ruff's S603/S607 warn about; probing tools by bare name (relying on
    PATH) is deliberate, since install layout varies by host. Decoding
    uses ``errors="replace"`` rather than the strict decode
    ``text=True`` implies — locale-dependent tool output could otherwise
    raise ``UnicodeDecodeError`` and escape the ``except`` clause below.
    """
    try:
        result = subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if warn_on_failure:
            logger.warning(
                "pipeline_probe_failed",
                component=component.value,
                error_type=type(exc).__name__,
            )
        return None
    if result.returncode != 0:
        if warn_on_failure:
            logger.warning(
                "pipeline_probe_failed",
                component=component.value,
                reason="nonzero_returncode",
                returncode=result.returncode,
            )
        return None
    return result.stdout


def _probe(
    component: Component,
    argv: Sequence[str],
    pattern: str,
    *,
    warn_on_failure: bool = True,
) -> str:
    """Run a version probe and extract ``pattern``'s first capture group.

    Returns :data:`UNAVAILABLE` (with a warning logged, unless
    ``warn_on_failure`` is ``False``) if the subprocess itself failed, or
    if it succeeded but the output didn't match ``pattern`` — a probe
    stdout without the expected banner is as unusable as no stdout at
    all.
    """
    stdout = _run(component, argv, warn_on_failure=warn_on_failure)
    if stdout is None:
        return UNAVAILABLE
    match = re.search(pattern, stdout)
    if match is None:
        if warn_on_failure:
            logger.warning(
                "pipeline_probe_failed", component=component.value, reason="no_match"
            )
        return UNAVAILABLE
    return match.group(1)


def _discover_package_version(component: Component, distribution: str) -> str:
    """Read an installed distribution's version via importlib.metadata.

    Never ``__version__`` attributes — see ``bscribe.adapters.liteparse``'s
    module docstring for why that's actively wrong for liteparse (a
    hardcoded stale value upstream).
    """
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        logger.warning(
            "pipeline_probe_failed",
            component=component.value,
            error_type="PackageNotFoundError",
        )
        return UNAVAILABLE


def _bundled_with_liteparse(liteparse_version: str) -> str:
    """Derive the PDFium/Tesseract version string from liteparse's own.

    Neither is independently versioned from bscribe's vantage point — both
    ship as native dependencies inside the liteparse wheel — so the best
    available signal is liteparse's own version, wrapped to say so.
    """
    if liteparse_version == UNAVAILABLE:
        return UNAVAILABLE
    return f"bundled (liteparse {liteparse_version})"


def _tessdata_dir() -> Path:
    """Resolve the tessdata directory liteparse itself would use.

    Mirrors liteparse's resolution order minus its optional explicit
    config override (this module has no engine instance to ask): honor
    ``TESSDATA_PREFIX`` if set, else the container-baked default (see
    Dockerfile:86-89).
    """
    prefix = os.environ.get("TESSDATA_PREFIX")
    if prefix:
        return Path(prefix)
    return Path.home() / ".tesseract-rs" / "tessdata"


def _discover_tessdata() -> str:
    """Hash the tessdata directory's language files into a short digest.

    The digest covers every ``*.traineddata`` file's name and content, so
    it changes if a language pack is added, removed, or upgraded in place
    — any of which changes OCR output for some documents.
    """
    directory = _tessdata_dir()
    if not directory.is_dir():
        logger.warning(
            "pipeline_probe_failed",
            component=Component.TESSDATA.value,
            reason="tessdata_dir_missing",
        )
        return UNAVAILABLE
    files = sorted(directory.glob("*.traineddata"))
    if not files:
        logger.warning(
            "pipeline_probe_failed",
            component=Component.TESSDATA.value,
            reason="no_traineddata_files",
        )
        return UNAVAILABLE
    digest = hashlib.sha256()
    try:
        for file in files:
            # file.name.encode() can raise UnicodeEncodeError on a
            # filename with an unpaired surrogate (unusual, but not
            # something OSError catches) — treated the same as any other
            # digest failure below.
            digest.update(file.name.encode() + b"\0" + file.read_bytes())
    except (OSError, ValueError) as exc:
        logger.warning(
            "pipeline_probe_failed",
            component=Component.TESSDATA.value,
            error_type=type(exc).__name__,
        )
        return UNAVAILABLE
    return f"sha256:{digest.hexdigest()[:12]}"


def _discover_imagemagick() -> str:
    """Probe ``convert -version``, falling back to ``magick`` (IM7 hosts).

    Debian ships IM6 as ``convert``; IM7 (e.g. Homebrew on macOS) drops
    that name in favor of a ``magick`` subcommand dispatcher. ``convert``
    failing is expected and unremarkable on an IM7-only host, so it's
    probed quietly; a warning is only logged if ``magick`` fails too,
    naming the component once rather than falsely signaling degradation
    on every IM7 host along the way.
    """
    version = _probe(
        Component.IMAGEMAGICK,
        ["convert", "-version"],
        _IMAGEMAGICK_PATTERN,
        warn_on_failure=False,
    )
    if version != UNAVAILABLE:
        return version
    version = _probe(
        Component.IMAGEMAGICK,
        ["magick", "-version"],
        _IMAGEMAGICK_PATTERN,
        warn_on_failure=False,
    )
    if version != UNAVAILABLE:
        return version
    logger.warning(
        "pipeline_probe_failed",
        component=Component.IMAGEMAGICK.value,
        reason="convert_and_magick_failed",
    )
    return UNAVAILABLE


def _discover_libreoffice() -> str:
    """Probe ``soffice --version`` with a scratch profile directory.

    ``-env:UserInstallation`` points LibreOffice at a fresh, throwaway
    profile dir instead of its default (a real user profile path) — the
    container runs read-only rootfs as non-root, where the default would
    fail to create. The directory is removed immediately after the probe.

    Creating that scratch directory can itself fail (``OSError``) if
    ``TMPDIR`` isn't writable — a read-only-rootfs edge distinct from the
    LibreOffice binary being missing — and must degrade the same way as
    any other probe failure rather than escape and abort discovery.
    """
    try:
        with tempfile.TemporaryDirectory() as user_installation_dir:
            # Path.as_uri() percent-encodes unusual TMPDIR characters; the
            # f-string form was already canonical for plain paths.
            profile_uri = Path(user_installation_dir).as_uri()
            argv = [
                "soffice",
                "--version",
                f"-env:UserInstallation={profile_uri}",
            ]
            return _probe(Component.LIBREOFFICE, argv, _LIBREOFFICE_PATTERN)
    except OSError as exc:
        logger.warning(
            "pipeline_probe_failed",
            component=Component.LIBREOFFICE.value,
            error_type=type(exc).__name__,
        )
        return UNAVAILABLE


def _discover_ghostscript() -> str:
    """Probe ``gs --version``; stdout is a bare version number, no banner.

    Validated (not just trusted) against a loose ``\\d+\\.\\d+`` pattern —
    the one probe here without a fixed banner to anchor a capture group on
    — then returned verbatim (stripped), so e.g. ``"10.00.0"`` (three
    version segments) round-trips whole rather than being truncated to two.
    """
    stdout = _run(Component.GHOSTSCRIPT, ["gs", "--version"])
    if stdout is None:
        return UNAVAILABLE
    version = stdout.strip()
    if re.search(_GHOSTSCRIPT_VALIDATE_PATTERN, version) is None:
        logger.warning(
            "pipeline_probe_failed",
            component=Component.GHOSTSCRIPT.value,
            reason="no_match",
        )
        return UNAVAILABLE
    return version


def _discover_librsvg() -> str:
    """Probe ``rsvg-convert --version`` (the SVG renderer; see design doc)."""
    return _probe(Component.LIBRSVG, ["rsvg-convert", "--version"], _LIBRSVG_PATTERN)


def _discover() -> PipelineStamp:
    """Uncached pipeline discovery: probe every component, once.

    Test seam — production code calls :data:`discover_pipeline` instead, so
    the probes run at most once per process. See module docstring for
    failure semantics.
    """
    liteparse_version = _discover_package_version(Component.LITEPARSE, "liteparse")
    bundled = _bundled_with_liteparse(liteparse_version)
    components: dict[str, str] = {
        Component.BSCRIBE.value: _discover_package_version(
            Component.BSCRIBE, "bscribe"
        ),
        Component.LITEPARSE.value: liteparse_version,
        Component.PDFIUM.value: bundled,
        Component.TESSERACT.value: bundled,
        Component.TESSDATA.value: _discover_tessdata(),
        Component.IMAGEMAGICK.value: _discover_imagemagick(),
        Component.LIBREOFFICE.value: _discover_libreoffice(),
        Component.GHOSTSCRIPT.value: _discover_ghostscript(),
        Component.LIBRSVG.value: _discover_librsvg(),
    }
    fingerprint = compute_fingerprint(components)
    return PipelineStamp(fingerprint=fingerprint, components=components)


discover_pipeline = functools.cache(_discover)
"""Process-wide cached :class:`PipelineStamp`; probes run once, at first call."""

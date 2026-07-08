"""Tests for bscribe.pipeline."""

from __future__ import annotations

import hashlib
import importlib.metadata
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest  # noqa: TC002 - used only in annotations, but idiomatic at runtime here

from bscribe.domain.models import Component
from bscribe.domain.pipeline import UNAVAILABLE, compute_fingerprint
from bscribe.pipeline import (
    _IMAGEMAGICK_PATTERN,  # pyright: ignore[reportPrivateUsage]
    _discover,  # pyright: ignore[reportPrivateUsage]
    _discover_ghostscript,  # pyright: ignore[reportPrivateUsage]
    _discover_imagemagick,  # pyright: ignore[reportPrivateUsage]
    _discover_libreoffice,  # pyright: ignore[reportPrivateUsage]
    _discover_librsvg,  # pyright: ignore[reportPrivateUsage]
    _discover_tessdata,  # pyright: ignore[reportPrivateUsage]
    _probe,  # pyright: ignore[reportPrivateUsage]
    discover_pipeline,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# Canned real `--version` output, one per supported host shape.
IMAGEMAGICK_6_STDOUT = (
    "Version: ImageMagick 6.9.11-60 Q16 aarch64 20210408 "
    "https://imagemagick.org\n"
    "Copyright: (C) 1999 ImageMagick Studio LLC\n"
)
IMAGEMAGICK_7_STDOUT = (
    "Version: ImageMagick 7.1.x x86_64 2024-01-01 https://imagemagick.org\n"
)
LIBREOFFICE_STDOUT = "LibreOffice 7.4.7.2 40(Build:2)\n"
GHOSTSCRIPT_STDOUT = "10.00.0\n"
LIBRSVG_STDOUT = "rsvg-convert version 2.54.7\n"


def _completed(
    stdout: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["fake"], returncode=returncode, stdout=stdout, stderr=""
    )


def _stub_run(
    stdout: str, returncode: int = 0
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Typed stand-in for ``subprocess.run`` that always returns fixed output.

    A bare ``lambda argv, **_kw: ...`` has unannotated parameters, which
    pyright strict flags once handed to ``monkeypatch.setattr``'s untyped
    ``(str, object)`` overload — this factory returns a fully-typed
    callable instead.
    """

    def _run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(stdout, returncode)

    return _run


def _const_version(_component: Component, _distribution: str) -> str:
    """Typed stand-in for ``_discover_package_version``, always "1.0.0"."""
    return "1.0.0"


class TestImageMagickProbe:
    def test_parses_im6_convert_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "bscribe.pipeline.subprocess.run",
            _stub_run(IMAGEMAGICK_6_STDOUT),
        )

        assert _discover_imagemagick() == "6.9.11-60"

    def test_parses_im7_magick_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "bscribe.pipeline.subprocess.run",
            _stub_run(IMAGEMAGICK_7_STDOUT),
        )

        assert _discover_imagemagick() == "7.1.x"

    def test_falls_back_to_magick_when_convert_missing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _fake_run(
            argv: list[str], **_kw: object
        ) -> subprocess.CompletedProcess[str]:
            if argv[0] == "convert":
                raise FileNotFoundError("no such file")
            return _completed(IMAGEMAGICK_7_STDOUT)

        monkeypatch.setattr("bscribe.pipeline.subprocess.run", _fake_run)

        assert _discover_imagemagick() == "7.1.x"
        # convert's failure is expected on an IM7-only host and must not
        # be logged as a degradation — only a failure of *both* probes
        # warrants a warning.
        assert "pipeline_probe_failed" not in capsys.readouterr().out

    def test_unavailable_when_both_convert_and_magick_missing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _fake_run(
            argv: list[str], **_kw: object
        ) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("no such file")

        monkeypatch.setattr("bscribe.pipeline.subprocess.run", _fake_run)

        assert _discover_imagemagick() == UNAVAILABLE
        event = capsys.readouterr().out
        assert event.count("pipeline_probe_failed") == 1
        assert Component.IMAGEMAGICK.value in event


class TestLibreOfficeProbe:
    def test_parses_version_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "bscribe.pipeline.subprocess.run",
            _stub_run(LIBREOFFICE_STDOUT),
        )

        assert _discover_libreoffice() == "7.4.7.2"

    def test_passes_scratch_user_installation_arg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[list[str]] = []

        def _fake_run(
            argv: list[str], **_kw: object
        ) -> subprocess.CompletedProcess[str]:
            captured.append(argv)
            return _completed(LIBREOFFICE_STDOUT)

        monkeypatch.setattr("bscribe.pipeline.subprocess.run", _fake_run)

        _discover_libreoffice()

        assert captured[0][0] == "soffice"
        # Path.as_uri() on an absolute POSIX path yields a triple-slash
        # ``file:///...`` URI, not the bare ``file://`` prefix an f-string
        # would produce.
        assert any(
            arg.startswith("-env:UserInstallation=file:///") for arg in captured[0]
        )

    def test_unwritable_tmpdir_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A read-only-rootfs host where TMPDIR isn't writable makes
        ``tempfile.TemporaryDirectory()`` itself raise OSError — this must
        degrade like any other probe failure, not escape and abort
        discovery."""

        def _fake_temporary_directory(*_args: object, **_kwargs: object) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(
            "bscribe.pipeline.tempfile.TemporaryDirectory",
            _fake_temporary_directory,
        )

        assert _discover_libreoffice() == UNAVAILABLE
        event = capsys.readouterr().out
        assert "pipeline_probe_failed" in event
        assert Component.LIBREOFFICE.value in event


class TestGhostscriptProbe:
    def test_parses_bare_version_number(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "bscribe.pipeline.subprocess.run",
            _stub_run(GHOSTSCRIPT_STDOUT),
        )

        assert _discover_ghostscript() == "10.00.0"

    def test_garbage_stdout_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            "bscribe.pipeline.subprocess.run",
            _stub_run("not a version\n"),
        )

        assert _discover_ghostscript() == UNAVAILABLE
        assert "pipeline_probe_failed" in capsys.readouterr().out


class TestLibrsvgProbe:
    def test_parses_version_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "bscribe.pipeline.subprocess.run",
            _stub_run(LIBRSVG_STDOUT),
        )

        assert _discover_librsvg() == "2.54.7"


class TestProbeFailureModes:
    """Shared failure-mode coverage via the ImageMagick probe (any probe
    goes through the same ``_run``/``_probe`` machinery)."""

    def test_file_not_found_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError

        monkeypatch.setattr("bscribe.pipeline.subprocess.run", _fake_run)

        assert _discover_librsvg() == UNAVAILABLE

    def test_timeout_is_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="rsvg-convert", timeout=10.0)

        monkeypatch.setattr("bscribe.pipeline.subprocess.run", _fake_run)

        assert _discover_librsvg() == UNAVAILABLE

    def test_nonzero_returncode_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "bscribe.pipeline.subprocess.run",
            _stub_run("", returncode=1),
        )

        assert _discover_librsvg() == UNAVAILABLE

    def test_probe_failure_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
            raise OSError("boom")

        monkeypatch.setattr("bscribe.pipeline.subprocess.run", _fake_run)

        # Must not raise.
        assert _discover_librsvg() == UNAVAILABLE

    def test_probe_failure_logs_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError

        monkeypatch.setattr("bscribe.pipeline.subprocess.run", _fake_run)

        _discover_librsvg()

        event = capsys.readouterr().out
        assert "pipeline_probe_failed" in event
        assert Component.LIBRSVG.value in event


class TestProbeDecodeSafety:
    """``subprocess.run`` decodes with ``errors="replace"``, not the strict
    decode ``text=True`` implies — locale-dependent tool output could
    otherwise raise ``UnicodeDecodeError`` and escape the probe's
    ``except (OSError, TimeoutExpired)`` guard. These run a real
    subprocess (a throwaway Python interpreter) so the assertion covers
    actual OS-level decode behavior, not just this module's own code."""

    def test_invalid_utf8_bytes_outside_match_still_parse(self) -> None:
        argv = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write("
            r"b'Version: ImageMagick 6.9.11-60 Q16 \xff\xfe trailer\n')",
        ]

        version = _probe(Component.IMAGEMAGICK, argv, _IMAGEMAGICK_PATTERN)

        assert version == "6.9.11-60"

    def test_invalid_utf8_bytes_with_no_match_is_unavailable(self) -> None:
        argv = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'garbage \\xff\\xfe output\\n')",
        ]

        # Must not raise, and no match in the (mangled but still decoded)
        # output degrades the same as any other no-match probe.
        assert _probe(Component.IMAGEMAGICK, argv, _IMAGEMAGICK_PATTERN) == UNAVAILABLE


class TestTessdataDigest:
    def test_digest_is_stable_across_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TESSDATA_PREFIX", str(tmp_path))
        (tmp_path / "eng.traineddata").write_bytes(b"english model bytes")

        assert _discover_tessdata() == _discover_tessdata()

    def test_digest_changes_with_file_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TESSDATA_PREFIX", str(tmp_path))
        (tmp_path / "eng.traineddata").write_bytes(b"version one")
        first = _discover_tessdata()

        (tmp_path / "eng.traineddata").write_bytes(b"version two")
        second = _discover_tessdata()

        assert first != second

    def test_digest_changes_with_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TESSDATA_PREFIX", str(tmp_path))
        (tmp_path / "eng.traineddata").write_bytes(b"same bytes")
        first = _discover_tessdata()

        (tmp_path / "eng.traineddata").unlink()
        (tmp_path / "fra.traineddata").write_bytes(b"same bytes")
        second = _discover_tessdata()

        assert first != second

    def test_digest_matches_manual_computation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TESSDATA_PREFIX", str(tmp_path))
        (tmp_path / "eng.traineddata").write_bytes(b"english model bytes")
        (tmp_path / "fra.traineddata").write_bytes(b"french model bytes")

        expected = hashlib.sha256()
        expected.update(b"eng.traineddata" + b"\0" + b"english model bytes")
        expected.update(b"fra.traineddata" + b"\0" + b"french model bytes")

        assert _discover_tessdata() == f"sha256:{expected.hexdigest()[:12]}"

    def test_tessdata_prefix_env_respected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        (custom_dir / "eng.traineddata").write_bytes(b"data")
        monkeypatch.setenv("TESSDATA_PREFIX", str(custom_dir))

        assert _discover_tessdata() != UNAVAILABLE

    def test_missing_dir_is_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TESSDATA_PREFIX", str(tmp_path / "does-not-exist"))

        assert _discover_tessdata() == UNAVAILABLE

    def test_empty_dir_is_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TESSDATA_PREFIX", str(tmp_path))

        assert _discover_tessdata() == UNAVAILABLE


class TestDiscoverComposition:
    def test_all_nine_component_keys_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "bscribe.pipeline._discover_package_version", _const_version
        )
        monkeypatch.setattr("bscribe.pipeline._discover_tessdata", lambda: "sha256:abc")
        monkeypatch.setattr("bscribe.pipeline._discover_imagemagick", lambda: "6.9.0")
        monkeypatch.setattr("bscribe.pipeline._discover_libreoffice", lambda: "7.4.0")
        monkeypatch.setattr("bscribe.pipeline._discover_ghostscript", lambda: "10.0.0")
        monkeypatch.setattr("bscribe.pipeline._discover_librsvg", lambda: "2.54.0")

        stamp = _discover()

        assert set(stamp.components) == {c.value for c in Component}

    def test_fingerprint_matches_compute_fingerprint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "bscribe.pipeline._discover_package_version", _const_version
        )
        monkeypatch.setattr("bscribe.pipeline._discover_tessdata", lambda: "sha256:abc")
        monkeypatch.setattr("bscribe.pipeline._discover_imagemagick", lambda: "6.9.0")
        monkeypatch.setattr("bscribe.pipeline._discover_libreoffice", lambda: "7.4.0")
        monkeypatch.setattr("bscribe.pipeline._discover_ghostscript", lambda: "10.0.0")
        monkeypatch.setattr("bscribe.pipeline._discover_librsvg", lambda: "2.54.0")

        stamp = _discover()

        assert stamp.fingerprint == compute_fingerprint(stamp.components)

    def test_bscribe_and_liteparse_come_from_importlib_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_version(distribution: str) -> str:
            return {"bscribe": "0.3.0", "liteparse": "2.5.0"}[distribution]

        monkeypatch.setattr(
            "bscribe.pipeline.importlib.metadata.version", _fake_version
        )
        monkeypatch.setattr("bscribe.pipeline._discover_tessdata", lambda: "sha256:abc")
        monkeypatch.setattr("bscribe.pipeline._discover_imagemagick", lambda: "6.9.0")
        monkeypatch.setattr("bscribe.pipeline._discover_libreoffice", lambda: "7.4.0")
        monkeypatch.setattr("bscribe.pipeline._discover_ghostscript", lambda: "10.0.0")
        monkeypatch.setattr("bscribe.pipeline._discover_librsvg", lambda: "2.54.0")

        stamp = _discover()

        expected_bundled = "bundled (liteparse 2.5.0)"
        assert stamp.components[Component.BSCRIBE.value] == "0.3.0"
        assert stamp.components[Component.LITEPARSE.value] == "2.5.0"
        assert stamp.components[Component.PDFIUM.value] == expected_bundled
        assert stamp.components[Component.TESSERACT.value] == expected_bundled

    def test_bscribe_unavailable_when_package_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_version(distribution: str) -> str:
            raise importlib.metadata.PackageNotFoundError(distribution)

        monkeypatch.setattr(
            "bscribe.pipeline.importlib.metadata.version", _fake_version
        )
        monkeypatch.setattr("bscribe.pipeline._discover_tessdata", lambda: "sha256:abc")
        monkeypatch.setattr("bscribe.pipeline._discover_imagemagick", lambda: "6.9.0")
        monkeypatch.setattr("bscribe.pipeline._discover_libreoffice", lambda: "7.4.0")
        monkeypatch.setattr("bscribe.pipeline._discover_ghostscript", lambda: "10.0.0")
        monkeypatch.setattr("bscribe.pipeline._discover_librsvg", lambda: "2.54.0")

        stamp = _discover()

        assert stamp.components[Component.BSCRIBE.value] == UNAVAILABLE
        assert stamp.components[Component.LITEPARSE.value] == UNAVAILABLE
        assert stamp.components[Component.PDFIUM.value] == UNAVAILABLE
        assert stamp.components[Component.TESSERACT.value] == UNAVAILABLE


class TestDiscoverPipelineCaching:
    def test_cached_across_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Stub every per-component probe (as TestDiscoverComposition does)
        # so this stays a unit test — no real subprocess shells out, no
        # real tessdata directory is read. Only the caching behavior of
        # discover_pipeline itself is under test here.
        discover_pipeline.cache_clear()
        calls: list[None] = []

        def _counting_version(distribution: str) -> str:
            calls.append(None)
            return "0.0.0"

        monkeypatch.setattr(
            "bscribe.pipeline.importlib.metadata.version", _counting_version
        )
        monkeypatch.setattr("bscribe.pipeline._discover_tessdata", lambda: "sha256:abc")
        monkeypatch.setattr("bscribe.pipeline._discover_imagemagick", lambda: "6.9.0")
        monkeypatch.setattr("bscribe.pipeline._discover_libreoffice", lambda: "7.4.0")
        monkeypatch.setattr("bscribe.pipeline._discover_ghostscript", lambda: "10.0.0")
        monkeypatch.setattr("bscribe.pipeline._discover_librsvg", lambda: "2.54.0")

        first = discover_pipeline()
        second = discover_pipeline()

        assert first is second
        # Exactly one discovery pass ran (bscribe + liteparse = 2 calls),
        # not two.
        assert len(calls) == 2

        discover_pipeline.cache_clear()

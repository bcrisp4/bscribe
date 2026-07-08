"""Shared fixtures for unit tests."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from tests.unit.fakes import CANNED_PIPELINE_STAMP

if TYPE_CHECKING:
    from pathlib import Path


# pyright strict flags fixtures as unused (reportUnusedFunction);
# pytest discovers and calls them by name.
@pytest.fixture(autouse=True)
def _isolate_bscribe_env(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Strip ambient BSCRIBE_* env vars so a developer's shell can't skew tests.

    Also points ``BSCRIBE_DB_PATH`` at a per-test temp file: the default is
    an absolute path under the developer's ``~/.local/share``, and
    ``create_app`` builds the token store eagerly — without this, every test
    that builds an app would write to (and share) the developer's real
    database file, and xdist workers would race each other on it.

    ``BSCRIBE_SCRATCH_DIR`` gets the same treatment: the app lifespan's
    startup sweep deletes the scratch dir's contents, so a test running the
    real lifespan against the default (a shared path under the host's temp
    dir) would wipe files that other processes staged there.
    """
    # Snapshot the keys: delenv mutates os.environ immediately, and deleting
    # while iterating it raises RuntimeError.
    for name in list(os.environ):
        if name.startswith("BSCRIBE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("BSCRIBE_DB_PATH", str(tmp_path / "bscribe.db"))
    monkeypatch.setenv("BSCRIBE_SCRATCH_DIR", str(tmp_path / "scratch"))


# pyright strict flags fixtures as unused (reportUnusedFunction);
# pytest discovers and calls them by name.
@pytest.fixture(autouse=True)
def _fake_pipeline_discovery(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip real pipeline discovery for every bare ``create_app()`` call.

    Real discovery (``bscribe.pipeline.discover_pipeline``) shells out to
    probe LibreOffice/ImageMagick/etc and is process-cached — so without
    this, every xdist worker process would pay the real subprocess-probe
    cost once, on whichever test happens to build the first app. That's
    slow and non-hermetic (results vary by dev machine vs CI image).

    ``tests/unit/test_app.py``'s test of the *default* discovery path
    monkeypatches ``bscribe.app.discover_pipeline`` itself, which simply
    overrides this fixture's patch for that one test — no conflict, but
    its assertions are against whatever stamp *that* test installs, not
    against this fixture's canned one.
    """
    monkeypatch.setattr("bscribe.app.discover_pipeline", lambda: CANNED_PIPELINE_STAMP)

"""Shared fixtures for unit tests."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

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
    """
    # Snapshot the keys: delenv mutates os.environ immediately, and deleting
    # while iterating it raises RuntimeError.
    for name in list(os.environ):
        if name.startswith("BSCRIBE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("BSCRIBE_DB_PATH", str(tmp_path / "bscribe.db"))

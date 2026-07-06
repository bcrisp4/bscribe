"""Shared fixtures for unit tests."""

from __future__ import annotations

import os

import pytest


# pyright strict flags fixtures as unused (reportUnusedFunction);
# pytest discovers and calls them by name.
@pytest.fixture(autouse=True)
def _isolate_bscribe_env(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strip ambient BSCRIBE_* env vars so a developer's shell can't skew tests."""
    for name in os.environ:
        if name.startswith("BSCRIBE_"):
            monkeypatch.delenv(name)

"""Shared fixtures for integration tests.

Integration tests build a real ``create_app`` and enter its lifespan (unlike
the ASGITransport unit tests), so — like ``tests/unit/conftest.py`` — they must
not start the Prometheus metrics server: it binds a fixed port and would race
xdist workers on the same socket. Metrics behaviour is covered by the unit
suite against the registry directly, so disabling exposition here loses no
coverage.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_metrics_server(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the real lifespan from binding the metrics port under xdist."""
    monkeypatch.setenv("BSCRIBE_METRICS_ENABLED", "false")

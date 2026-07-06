"""Tests for bscribe.log."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest
import structlog

from bscribe.log import configure_logging

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Undo the process-global structlog config after each test.

    ``cache_logger_on_first_use`` makes configuration sticky; without a reset
    the first test to configure would leak into every later test.
    """
    yield
    structlog.reset_defaults()
    logging.getLogger("uvicorn.access").disabled = False


def _last_json_line(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert lines, "expected at least one log line on stdout"
    parsed: dict[str, object] = json.loads(lines[-1])
    return parsed


def test_info_event_emits_json_line_with_kwargs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")

    # Logger must be created after capsys patches stdout, or PrintLogger
    # binds the real stream and the assertion sees nothing.
    structlog.get_logger().info("something_happened", key="value")

    event = _last_json_line(capsys)
    assert event["event"] == "something_happened"
    assert event["key"] == "value"
    assert event["level"] == "info"
    assert "timestamp" in event


def test_debug_suppressed_at_info_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")

    structlog.get_logger().debug("noise")

    assert capsys.readouterr().out == ""


def test_debug_emitted_at_debug_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG")

    structlog.get_logger().debug("detail")

    assert _last_json_line(capsys)["event"] == "detail"


def test_uvicorn_access_logger_disabled() -> None:
    configure_logging("INFO")

    # bscribe's own access-log middleware replaces uvicorn's access lines;
    # leaving both on would double-log every request.
    assert logging.getLogger("uvicorn.access").disabled

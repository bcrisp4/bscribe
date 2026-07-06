"""Tests for domain exceptions."""

from __future__ import annotations

import pickle

import pytest

from bscribe.domain import (
    DocumentUnparseableError,
    JobTimeoutError,
    WorkerCrashedError,
)


@pytest.mark.parametrize(
    "exc_type",
    [DocumentUnparseableError, JobTimeoutError, WorkerCrashedError],
)
def test_pickle_round_trip_preserves_type_and_message(
    exc_type: type[Exception],
) -> None:
    """Domain errors cross the worker-process pipe boundary intact."""
    original = exc_type("a generic message")
    # Safe: round-tripping a value constructed on the line above, mirroring
    # the trusted parent<->worker pipe (never untrusted input).
    restored = pickle.loads(pickle.dumps(original))  # noqa: S301
    assert type(restored) is exc_type
    assert str(restored) == "a generic message"

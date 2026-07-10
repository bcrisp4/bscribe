"""Tests for the OpenAPI error-response documentation helpers."""

from __future__ import annotations

import pytest

from bscribe.api.responses import Problem, error_responses


def test_problem_model_matches_rfc9457_shape() -> None:
    """The documented body carries the RFC 9457 members bscribe emits."""
    problem = Problem(title="Unauthorized", status=401)

    assert problem.type == "about:blank"
    assert problem.detail is None
    assert set(problem.model_dump()) == {"type", "title", "status", "detail"}


def test_error_responses_bodies_each_code_with_problem() -> None:
    """Every requested status maps to a Problem-bodied entry."""
    responses = error_responses(401, 404)

    assert set(responses) == {401, 404}
    for entry in responses.values():
        assert entry["model"] is Problem
        assert entry["description"]


def test_error_responses_descriptions_are_status_specific() -> None:
    """Descriptions come from the per-status table, not a generic string."""
    responses = error_responses(401, 409)

    assert "bearer token" in responses[401]["description"]
    assert "failed" in responses[409]["description"]


def test_error_responses_rejects_undocumented_status() -> None:
    """A status with no table entry is a programming error, not a blank doc."""
    with pytest.raises(KeyError):
        error_responses(418)

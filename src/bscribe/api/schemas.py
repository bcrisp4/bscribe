"""Pydantic wire models for the conversion API.

Kept separate from the domain dataclasses (``ParsedDocument``): these are
the HTTP contract, reused by M2's ``GET /v1/jobs/{id}/result``. The M3
``pipeline`` block and the deferred ``ocr_used`` signal are deliberately
absent (docs/design.md — Interfaces, Closed issues).
"""

from __future__ import annotations

from pydantic import BaseModel

from bscribe.domain.models import OutputFormat


class ConvertMetadata(BaseModel):
    """Per-conversion metadata returned alongside the content."""

    pages: int
    duration_ms: int


class ConvertResponse(BaseModel):
    """The body of a successful ``POST /v1/convert``."""

    output: OutputFormat
    content: str
    metadata: ConvertMetadata

"""Domain exceptions."""

from __future__ import annotations


class DocumentUnparseableError(Exception):
    """A supported-format document that the parsing engine cannot parse.

    Maps to 422 on the sync HTTP path and a ``failed`` job on the async
    path (per the status-code table in docs/design.md).
    """

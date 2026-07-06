"""Domain exceptions."""

from __future__ import annotations


class DocumentUnparseableError(Exception):
    """A supported-format document that the parsing engine cannot parse.

    Maps to 422 on the sync HTTP path and a ``failed`` job on the async
    path (per the status-code table in docs/design.md).
    """


class JobTimeoutError(Exception):
    """A job hit the per-job deadline and its worker process was killed.

    Maps to 500 with detail ``"timeout"`` on the sync HTTP path and a
    ``failed`` job with the same detail on the async path (see
    docs/design.md — Job lifecycle).
    """


class WorkerCrashedError(Exception):
    """The worker or its pool failed outside the document's own fault.

    Covers a worker process dying mid-parse (segfault, OOM kill, hard
    exit), a worker raising an unexpected non-domain error, and the pool
    itself breaking. The pool contains the failure to that one job and
    respawns the worker (or rebuilds the pool). Maps to 500 on the sync
    HTTP path and a ``failed`` job on the async path.
    """

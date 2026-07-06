"""structlog configuration: JSON lines to stdout.

Privacy contract (see docs/design.md — Privacy):

- Document content and extracted text are NEVER logged, at any level.
- Filenames appear only at DEBUG; INFO references documents by id and size.
- Bearer token values are never logged; token labels may be.
- Log data is passed as keyword arguments, never interpolated into f-strings.
"""

from __future__ import annotations

import logging

import structlog


def configure_logging(level: str) -> None:
    """Configure the process-global structlog pipeline.

    Args:
        level: Minimum level name to emit (e.g. ``"INFO"``, ``"DEBUG"``).

    Also disables uvicorn's access logger: bscribe's own middleware emits one
    structured line per request, and keeping both would double-log. uvicorn's
    error/startup loggers are left alone until the ``bscribe serve`` CLI owns
    logging end-to-end (mixed formats on stdout accepted until then).
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level]
        ),
        cache_logger_on_first_use=True,
    )
    logging.getLogger("uvicorn.access").disabled = True

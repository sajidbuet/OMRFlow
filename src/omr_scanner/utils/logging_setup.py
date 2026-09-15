"""Application logging configuration.

Purpose:
    Configure the standard library logging tree once, in one place, so that every
    layer can simply call ``logging.getLogger(__name__)``.

Responsibilities:
    * Install a console handler and a rotating application log file handler.
    * Allow a per-project log file to be attached while a project is open and
      detached when it closes.

What does NOT belong here:
    * ``logging.getLogger(...)`` calls for other modules; each module owns its
      own logger.
    * Emitting log records. This module configures, it does not log events.

Privacy invariant:
    Log records must never contain candidate names, roll numbers or answer keys.
    Log *counts*, file names and identifiers instead. See ``docs/TESTING.md``
    and ``docs/ARCHITECTURE.md`` (Logging and privacy).
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_LOG_BYTES = 2 * 1024 * 1024
"""Rotate the log file at 2 MiB; large enough for one processing session."""

LOG_BACKUP_COUNT = 5

_MANAGED_MARKER = "_omrflow_managed"
"""Handlers installed by this module are tagged so that repeated configuration
(for example in tests) replaces them instead of stacking duplicates."""


def configure_logging(*, level: int = logging.INFO, log_file: Path | None = None) -> None:
    """Install OMRFlow's handlers on the root logger.

    Safe to call more than once: handlers previously installed by this function
    are removed first, so repeated calls never duplicate output. Handlers
    installed by anyone else (pytest's ``caplog``, for instance) are left alone.

    Args:
        level: Threshold for the root logger, e.g. ``logging.DEBUG``.
        log_file: Optional application log file. Its parent directory is created
            if necessary. When ``None``, only console logging is configured.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MANAGED_MARKER, False):
            root.removeHandler(handler)
            handler.close()

    root.setLevel(level)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    setattr(console, _MANAGED_MARKER, True)
    root.addHandler(console)

    if log_file is not None:
        root.addHandler(_build_file_handler(log_file, level=level))


def attach_log_file(log_file: Path, *, level: int = logging.INFO) -> logging.Handler:
    """Add an extra log file, typically the log of the project being opened.

    Args:
        log_file: Destination file; parent directories are created if missing.
        level: Threshold for this handler only.

    Returns:
        The installed handler, to be passed to :func:`detach_log_file` later.
    """
    handler = _build_file_handler(log_file, level=level)
    logging.getLogger().addHandler(handler)
    return handler


def detach_log_file(handler: logging.Handler) -> None:
    """Remove and close a handler previously returned by :func:`attach_log_file`."""
    logging.getLogger().removeHandler(handler)
    handler.close()


def _build_file_handler(log_file: Path, *, level: int) -> logging.Handler:
    """Create a rotating file handler tagged as OMRFlow-managed."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    setattr(handler, _MANAGED_MARKER, True)
    return handler

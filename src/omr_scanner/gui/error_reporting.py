"""Turning exceptions into messages a user can act on.

Purpose:
    One place that decides how a failure is presented, so no widget ever shows a
    raw traceback and no service has to know what a dialog is.

Responsibilities:
    * Show :class:`omr_scanner.errors.OMRScannerError` using its ``user_message``
      and log the technical detail.
    * Show unexpected exceptions with a generic message plus a pointer to the log.

What does NOT belong here:
    * Deciding whether an operation should be retried or aborted; that is the
      caller's decision.
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import QMessageBox, QWidget

from omr_scanner.errors import OMRScannerError

logger = logging.getLogger(__name__)

UNEXPECTED_ERROR_TEXT = (
    "An unexpected error occurred. The application log contains the technical details."
)


def report_error(parent: QWidget | None, exc: BaseException, *, context: str) -> None:
    """Log ``exc`` and show it to the user.

    Args:
        parent: Widget the dialog is centred on; may be ``None``.
        exc: The exception that was caught.
        context: Short description of the attempted operation, used as the dialog
            title and as the log message prefix, e.g. ``"Open project"``.
    """
    if isinstance(exc, OMRScannerError):
        logger.error("%s failed: %s", context, exc)
        message = exc.user_message
    else:
        logger.exception("%s failed with an unexpected error", context)
        message = UNEXPECTED_ERROR_TEXT

    QMessageBox.warning(parent, context, message)

"""Turning exceptions into messages a user can act on.

Purpose:
    One place that decides how a failure is presented, so no widget ever shows a
    raw traceback and no service has to know what a dialog is.

Responsibilities:
    * Show :class:`omr_scanner.errors.OMRScannerError` using its ``user_message``
      and log the technical detail.
    * Show unexpected exceptions with a generic message plus a pointer to the log.
    * :func:`install_global_exception_handler` - a last-resort net for an
      exception that reaches the top of a Qt slot without anything along
      the way catching it (Phase 10, §41).

What does NOT belong here:
    * Deciding whether an operation should be retried or aborted; that is the
      caller's decision.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import sys
from collections.abc import Callable
from types import TracebackType

from PySide6.QtWidgets import QMessageBox, QWidget

from omr_scanner.errors import OMRScannerError

logger = logging.getLogger(__name__)

_ExceptHook = Callable[[type[BaseException], BaseException, TracebackType | None], object]

UNEXPECTED_ERROR_TEXT = (
    "An unexpected error occurred. The application log contains the technical details."
)

GLOBAL_HANDLER_TEXT = (
    "An unexpected internal error occurred and was not handled by the action you "
    "were performing.\n\n"
    "Previously saved project data is unaffected - OMRFlow only marks work as saved "
    "once it is actually committed. Any change you were making at the moment of "
    "this error may not have been saved; please check the affected screen before "
    "continuing.\n\n"
    "The application log contains the technical details."
)

_original_excepthook: _ExceptHook | None = None
"""Sentinel doubling as an installed-once guard: `None` until
:func:`install_global_exception_handler` runs, then the previous hook
(usually `sys.__excepthook__`), which every future exception - including one
raised while Qt has no window left to parent a dialog to - is forwarded to
after logging, so nothing this module does can make Python's own crash
reporting *disappear*, only add a visible, user-facing step in front of it."""


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


def install_global_exception_handler(parent: QWidget | None = None) -> None:
    """Install a last-resort handler for exceptions that escape a Qt slot.

    Args:
        parent: Widget the fallback dialog is centred on (typically the main
            window); may be ``None``.

    An exception raised inside a Qt slot with nothing along its call chain
    catching it does not crash the process the way an uncaught exception in
    plain Python does - Qt's own C++ layer catches it at the slot boundary
    and reports it through :data:`sys.excepthook`, which by default
    (:data:`sys.__excepthook__`) only prints to standard error. In a
    windowed application with no visible console, that is silence: the
    action the operator triggered simply appears to do nothing, and nothing
    is logged anywhere the operator can find. This installs a replacement
    hook that logs the full traceback to the application log and shows a
    plain-language dialog instead - and then still forwards to whatever
    hook was previously installed, so nothing here can suppress the
    platform's own crash reporting.

    Idempotent: a second call is a no-op, so it can safely be called from
    both the real entry point and a test.
    """
    global _original_excepthook
    if _original_excepthook is not None:
        return
    _original_excepthook = sys.excepthook
    sys.excepthook = functools.partial(_handle_uncaught_exception, parent)


def _handle_uncaught_exception(
    parent: QWidget | None,
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
) -> None:
    """The installed hook itself.

    Never raises - a crash handler that crashes would report nothing at all.
    """
    if issubclass(exc_type, KeyboardInterrupt):
        _forward_to_previous_hook(exc_type, exc_value, exc_traceback)
        return

    with contextlib.suppress(Exception):  # logging itself must never crash this
        logger.critical(
            "Unhandled exception reached the top level",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    with contextlib.suppress(Exception):  # showing the dialog must never crash this
        QMessageBox.critical(parent, "Unexpected error", GLOBAL_HANDLER_TEXT)

    _forward_to_previous_hook(exc_type, exc_value, exc_traceback)


def _forward_to_previous_hook(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
) -> None:
    if _original_excepthook is not None:
        _original_excepthook(exc_type, exc_value, exc_traceback)

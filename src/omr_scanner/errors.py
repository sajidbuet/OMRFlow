"""Application exception hierarchy.

Purpose:
    Single definition point for every exception raised deliberately by OMRFlow,
    so that the GUI can catch one base class and translate failures into user
    facing messages instead of tracebacks.

Responsibilities:
    * Define the exception tree.
    * Carry a ``user_message`` suitable for direct display in a dialog, while the
      exception ``str()`` keeps the full technical detail for the log file.

What does NOT belong here:
    * Any logging, Qt import or message-box code. Presentation of an error is the
      GUI layer's job (see ``omr_scanner.gui.error_reporting``).
    * Error *recovery* logic; that belongs to the service raising the error.

Design notes:
    Only the exceptions actually raised in Phase 0 are implemented with real
    behaviour. ``TemplateError``, ``ImagingError`` and ``RecognitionError`` are
    declared now because later phases must not invent parallel hierarchies -
    see ``docs/ARCHITECTURE.md`` (Error handling).
"""

from __future__ import annotations


class OMRScannerError(Exception):
    """Base class for all deliberate OMRFlow failures.

    Args:
        message: Technical description, written to logs.
        user_message: Optional plain-language text for end users. Falls back to
            ``message`` when not supplied.
    """

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message if user_message is not None else message


class ConfigurationError(OMRScannerError):
    """Application-level configuration could not be read, parsed or written."""


class ProjectError(OMRScannerError):
    """Base class for failures while creating, opening or closing a project."""


class ProjectValidationError(ProjectError):
    """A directory does not describe a usable OMRFlow project.

    Raised when ``project.json`` is missing, unreadable, malformed, or declares a
    project format version this build cannot open.
    """


class ProjectExistsError(ProjectError):
    """The target directory already contains a project or is not empty."""


class DatabaseError(OMRScannerError):
    """The project database could not be opened, initialised or migrated."""


class SchemaVersionError(DatabaseError):
    """The project database schema is newer than this build understands.

    Downgrading is never attempted automatically: an older build must not guess
    how to reinterpret a schema written by a newer one.
    """


class TemplateError(OMRScannerError):
    """An ``.omrt`` template is malformed, unreadable or semantically invalid."""


class ImagingError(OMRScannerError):
    """Reserved for Phase 1: geometric normalisation and marker detection failures."""


class RecognitionError(OMRScannerError):
    """Reserved for Phase 3: bubble measurement and field interpretation failures."""


class ReportingError(OMRScannerError):
    """Reserved for Phase 9: Excel/PDF export failures."""

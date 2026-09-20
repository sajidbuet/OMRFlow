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
    Only the exceptions actually raised in Phase 0 and Phase 1 are implemented
    with real behaviour. ``RecognitionError`` and ``ReportingError`` are declared
    now because later phases must not invent parallel hierarchies - see
    ``docs/ARCHITECTURE.md`` (Error handling).

    Every :class:`ImagingError` subclass carries a stable, machine-readable
    ``code``. Callers (the future conflict queue, the batch pipeline, tests) can
    branch on the code without parsing English prose, and the code is what gets
    recorded against a sheet that failed to align.
"""

from __future__ import annotations

from typing import ClassVar


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
    """Base class for geometric normalisation and marker detection failures.

    Attributes:
        code: Stable machine-readable identifier for the failure mode. Subclasses
            override it; it is deliberately not derived from the class name so
            that renaming a class cannot silently change a persisted status.
    """

    code: ClassVar[str] = "IMAGING_ERROR"


class ImageValidationError(ImagingError):
    """The supplied array is not an image this pipeline can process.

    Raised for empty arrays, wrong dimensionality, an unsupported channel count
    or dtype, and images too small to carry a printed marker. Exists so that an
    OpenCV assertion never becomes the application's error interface.
    """

    code: ClassVar[str] = "INVALID_IMAGE"


class MarkerDetectionError(ImagingError):
    """Base class for failures while locating the registration markers."""

    code: ClassVar[str] = "MARKER_DETECTION_FAILED"


class InsufficientMarkersError(MarkerDetectionError):
    """At least one page corner produced no acceptable registration marker.

    The deliberate Phase 1 behaviour for a torn, dirty or cropped corner: fail
    rather than extrapolate the missing corner, because a silently wrong
    rectification produces a confidently wrong answer sheet.
    """

    code: ClassVar[str] = "INSUFFICIENT_MARKERS"


class AmbiguousMarkerError(MarkerDetectionError):
    """Corner candidates could not be assigned to four distinct markers.

    Raised when the best scoring candidate for two different corners is the same
    contour, or when no injective assignment of candidates to corners exists.
    """

    code: ClassVar[str] = "AMBIGUOUS_MARKERS"


class OrientationDetectionError(ImagingError):
    """The orientation marker could not be located confidently.

    The four corner markers are symmetric, so without this marker the page could
    be upside down. Guessing is only permitted when
    ``OrientationConfig.allow_fallback`` is enabled, and is then reported as a
    warning on the result.
    """

    code: ClassVar[str] = "ORIENTATION_NOT_FOUND"


class InvalidPageGeometryError(ImagingError):
    """The four selected markers do not form a plausible page quadrilateral.

    Covers non-convex or self-intersecting arrangements, a degenerate area, two
    markers closer together than a page could allow, and an aspect ratio too far
    from the one the template declares.
    """

    code: ClassVar[str] = "INVALID_PAGE_GEOMETRY"


class AlignmentTransformError(ImagingError):
    """The perspective transform could not be computed or inverted."""

    code: ClassVar[str] = "ALIGNMENT_TRANSFORM_FAILED"


class RecognitionError(OMRScannerError):
    """Reserved for Phase 3: bubble measurement and field interpretation failures."""


class ReportingError(OMRScannerError):
    """Excel/PDF export failures (Phase 9).

    The `omr_scanner.reporting` package's own base - see
    :class:`omr_scanner.reporting.excel.ExcelReportError` and
    :class:`omr_scanner.reporting.pdf.PdfExportError`. Service-layer failures
    that merely *use* reporting (reading a template, persisting a generation
    record) are their own independent `OMRScannerError` subclasses instead -
    :class:`omr_scanner.services.report_template.ReportTemplateError`,
    :class:`omr_scanner.services.report_store.ReportStoreError` - following
    the same pattern Phase 7's `CandidateImportError` and
    `ReconciliationError` already set for services that are closely related
    but not the same layer.
    """

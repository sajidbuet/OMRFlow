"""Low-level image processing: geometry now, bubble metrics from Phase 3.

Purpose:
    Everything that operates on pixels: preprocessing, registration-marker
    detection, orientation determination, corner ordering, perspective
    transformation, scale normalisation, thresholding and (later) bubble
    metrics.

Modules:
    * ``models.py``           - the pipeline's typed vocabulary.
    * ``config.py``           - every tunable number, named and documented.
    * ``preprocessing.py``    - validation, grayscale, downscale, threshold.
    * ``marker_detection.py`` - candidate measurement, filtering, corner choice.
    * ``orientation.py``      - which corner of the scan is the sheet's top-left.
    * ``orientation_marker.py`` - where the orientation mark is inside a region
      the designer's user drew around it.
    * ``geometry.py``         - point ordering, quadrilateral checks, homography.
    * ``alignment.py``        - the orchestrator; :func:`align_sheet`.
    * ``diagnostics.py``      - optional overlays and textual reports.
    * ``synthetic.py``        - synthetic sheets and reproducible distortions.
    * ``metrics.py``          - per-bubble fill measurements *(Phase 3)*.

The public entry point is :func:`align_sheet`::

    from omr_scanner.imaging import AlignmentConfig, align_sheet

    result = align_sheet(image=scan, config=config)
    canonical_page = result.normalized_image

What does NOT belong here:
    * Any PySide6/Qt import. This layer must be usable from a headless worker and
      from tests without a display. Converting a result into a ``QImage`` for
      display is the GUI layer's job.
    * Interpretation of measurements ("this is the digit 7"); that is
      ``recognition``.
    * File system layout knowledge, project structure or database access.
      Reading a scan from disk and building an
      :class:`~omr_scanner.imaging.config.AlignmentConfig` from a template are
      both :mod:`omr_scanner.services.alignment_service`.

Contract:
    * Functions take and return NumPy arrays plus plain data types; they never
      take an :class:`~omr_scanner.domain.template.OmrTemplate`, so they stay
      unit-testable with synthetic images.
    * Coordinates follow the convention in
      :mod:`omr_scanner.domain.geometry`: origin top-left, ``y`` downward, and
      four-corner sequences are always TL, TR, BR, BL.
    * The caller's image is never modified.
    * Failures raise a subclass of :class:`omr_scanner.errors.ImagingError`
      carrying a stable ``code``; nothing returns ``None`` to mean failure.

Specification:
    ``docs/IMAGE_PROCESSING.md`` describes the algorithm, its failure modes and
    its measured accuracy, and must be updated together with this package.
"""

from __future__ import annotations

from omr_scanner.imaging.alignment import align_sheet, canonical_target_for
from omr_scanner.imaging.config import (
    DEFAULT_CANONICAL_HEIGHT_PX,
    DEFAULT_CANONICAL_WIDTH_PX,
    DEFAULT_MARKER_TARGETS,
    AlignmentConfig,
    GeometryConfig,
    MarkerDetectionConfig,
    OrientationConfig,
    PreprocessingConfig,
    ThresholdStrategy,
)
from omr_scanner.imaging.models import (
    CANONICAL_CORNER_ORDER,
    IMAGE_CORNER_ORDER,
    AlignmentDiagnostics,
    AlignmentMetrics,
    AlignmentResult,
    AlignmentWarning,
    BoundingBox,
    ImageCorner,
    MarkerCandidate,
    OrientationHypothesis,
    OrientationResult,
    Point,
    RegistrationMarkerDetection,
    RejectedCandidate,
    ScoredCandidate,
)
from omr_scanner.imaging.orientation_marker import (
    OrientationCandidate,
    OrientationMarkerConfig,
    OrientationMarkerDetection,
    detect_orientation_marker,
)

__all__ = [
    "CANONICAL_CORNER_ORDER",
    "DEFAULT_CANONICAL_HEIGHT_PX",
    "DEFAULT_CANONICAL_WIDTH_PX",
    "DEFAULT_MARKER_TARGETS",
    "IMAGE_CORNER_ORDER",
    "AlignmentConfig",
    "AlignmentDiagnostics",
    "AlignmentMetrics",
    "AlignmentResult",
    "AlignmentWarning",
    "BoundingBox",
    "GeometryConfig",
    "ImageCorner",
    "MarkerCandidate",
    "MarkerDetectionConfig",
    "OrientationCandidate",
    "OrientationConfig",
    "OrientationHypothesis",
    "OrientationMarkerConfig",
    "OrientationMarkerDetection",
    "OrientationResult",
    "Point",
    "PreprocessingConfig",
    "RegistrationMarkerDetection",
    "RejectedCandidate",
    "ScoredCandidate",
    "ThresholdStrategy",
    "align_sheet",
    "canonical_target_for",
    "detect_orientation_marker",
]

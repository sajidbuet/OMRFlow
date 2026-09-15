"""Low-level image processing. RESERVED - no implementation in Phase 0.

Purpose:
    Everything that operates on pixels: preprocessing, registration-marker
    detection, orientation determination, corner ordering, perspective
    transformation, scale normalisation, thresholding and bubble metrics.

Planned modules (Phase 1 and Phase 3, see ``docs/IMAGE_PROCESSING.md``):
    * ``preprocess.py``  - grayscale conversion, denoising, adaptive threshold.
    * ``markers.py``     - registration and orientation marker detection.
    * ``normalize.py``   - corner ordering, perspective transform, rectification.
    * ``metrics.py``     - per-bubble fill measurements.

What does NOT belong here:
    * Any PySide6/Qt import. This layer must be usable from a headless worker and
      from tests without a display. Converting a result into a ``QImage`` for
      display is the GUI layer's job.
    * Interpretation of measurements ("this is the digit 7"); that is
      ``recognition``.
    * File system layout knowledge, project structure or database access.

Contract for later phases:
    * Functions take and return NumPy arrays plus plain data types; they never
      take a :class:`~omr_scanner.domain.template.OmrTemplate` apart from the
      geometry they need, so they stay unit-testable with synthetic images.
    * Coordinates follow the convention in
      :mod:`omr_scanner.domain.geometry`: origin top-left, ``y`` downward.
    * Failures raise :class:`omr_scanner.errors.ImagingError`.
"""

"""Runtime data types of the geometric normalisation pipeline.

Purpose:
    Give every stage of the alignment pipeline an explicit, typed vocabulary, so
    that detection results travel as named structures rather than as anonymous
    tuples that only the calling function understands.

Responsibilities:
    * Pixel-space primitives (:class:`Point`, :class:`BoundingBox`).
    * The measured properties of one dark contour (:class:`MarkerCandidate`) and
      the reason a contour was discarded (:class:`RejectedCandidate`).
    * The selected corner markers, the orientation outcome, the measured quality
      of an alignment and the alignment result itself.
    * The vocabulary of non-fatal warnings (:class:`AlignmentWarning`).

What does NOT belong here:
    * Tunable numbers. Those live in :mod:`omr_scanner.imaging.config`.
    * Algorithms. These types are data; the code that fills them lives in
      ``preprocessing``, ``marker_detection``, ``orientation`` and ``alignment``.
    * Persistence. These are runtime dataclasses, deliberately not Pydantic or
      SQLAlchemy models: they carry NumPy arrays, which do not belong in a
      document that has to be serialised or diffed.

Coordinate convention:
    Every pixel coordinate in this module follows
    :mod:`omr_scanner.domain.geometry`: origin at the top-left of the image,
    ``x`` to the right, ``y`` downward. Coordinates are floats because marker
    centroids are sub-pixel quantities.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from omr_scanner.domain.template import MarkerRole

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    import numpy as np
    from numpy.typing import NDArray

CANONICAL_CORNER_ORDER: tuple[MarkerRole, ...] = (
    MarkerRole.TOP_LEFT,
    MarkerRole.TOP_RIGHT,
    MarkerRole.BOTTOM_RIGHT,
    MarkerRole.BOTTOM_LEFT,
)
"""The one corner ordering used throughout OMRFlow: TL, TR, BR, BL (clockwise).

Clockwise in *image* coordinates, where ``y`` grows downward. Every sequence of
four corner points in this package - detected, ordered, canonical target - is in
this order, so an index is always meaningful without a comment.
"""


class ImageCorner(StrEnum):
    """A corner of the **scanned image**, before orientation has been resolved.

    Deliberately distinct from
    :class:`~omr_scanner.domain.template.MarkerRole`, which names a corner of
    the **canonical page**. Detection can only say "this marker is near the
    top-left of the scan"; whether that is the sheet's top-left depends on how
    the page was fed, and is decided by :mod:`omr_scanner.imaging.orientation`.
    Conflating the two is how an upside-down sheet becomes a confident wrong
    answer, so the type system keeps them apart.
    """

    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_RIGHT = "bottom_right"
    BOTTOM_LEFT = "bottom_left"


IMAGE_CORNER_ORDER: tuple[ImageCorner, ...] = (
    ImageCorner.TOP_LEFT,
    ImageCorner.TOP_RIGHT,
    ImageCorner.BOTTOM_RIGHT,
    ImageCorner.BOTTOM_LEFT,
)
"""Image corners in the same clockwise order as :data:`CANONICAL_CORNER_ORDER`."""


class AlignmentWarning(StrEnum):
    """Non-fatal observations attached to a successful alignment.

    A warning never changes the produced image. It records that a measurement
    was closer to its limit than is comfortable, so that Phase 4 calibration and
    the Phase 6 conflict queue can surface marginal sheets to a human.
    """

    LOW_MARKER_SCORE = "LOW_MARKER_SCORE"
    """A selected corner marker scored close to the acceptance threshold."""

    LOW_MARKER_RECTANGULARITY = "LOW_MARKER_RECTANGULARITY"
    """A selected marker's outline is a poor fit to its minimum-area rectangle."""

    MULTIPLE_CORNER_CANDIDATES = "MULTIPLE_CORNER_CANDIDATES"
    """A corner had more than one plausible candidate; the best one was used."""

    MARKER_NEAR_IMAGE_EDGE = "MARKER_NEAR_IMAGE_EDGE"
    """A marker touches the image border, so the scan may be cropped."""

    LOW_ORIENTATION_CONFIDENCE = "LOW_ORIENTATION_CONFIDENCE"
    """The orientation marker was found, but faintly."""

    ORIENTATION_ASSUMED = "ORIENTATION_ASSUMED"
    """No orientation marker was found and the configured fallback was used."""

    ASPECT_RATIO_DEVIATION = "ASPECT_RATIO_DEVIATION"
    """The marker quadrilateral's aspect ratio is far from the canonical one."""

    LARGE_REPROJECTION_ERROR = "LARGE_REPROJECTION_ERROR"
    """The fitted transform does not reproduce its own control points exactly."""


@dataclass(frozen=True, slots=True)
class Point:
    """A position in image pixels (origin top-left, ``y`` downward)."""

    x: float
    y: float

    def distance_to(self, other: Point) -> float:
        """Return the Euclidean distance to ``other`` in pixels."""
        return math.hypot(self.x - other.x, self.y - other.y)

    def scaled(self, factor: float) -> Point:
        """Return this point multiplied by ``factor``.

        Used to map a position measured on the downscaled working image back to
        full-resolution coordinates.
        """
        return Point(x=self.x * factor, y=self.y * factor)

    def as_tuple(self) -> tuple[float, float]:
        """Return ``(x, y)``."""
        return (self.x, self.y)


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """An axis-aligned rectangle in image pixels."""

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        """Pixel x coordinate of the right edge."""
        return self.x + self.width

    @property
    def bottom(self) -> float:
        """Pixel y coordinate of the bottom edge."""
        return self.y + self.height

    @property
    def center(self) -> Point:
        """Centre of the rectangle."""
        return Point(x=self.x + self.width / 2.0, y=self.y + self.height / 2.0)

    @property
    def area(self) -> float:
        """Area of the rectangle in square pixels."""
        return self.width * self.height

    def scaled(self, factor: float) -> BoundingBox:
        """Return this box multiplied by ``factor`` in both axes."""
        return BoundingBox(
            x=self.x * factor,
            y=self.y * factor,
            width=self.width * factor,
            height=self.height * factor,
        )


@dataclass(frozen=True, slots=True)
class MarkerCandidate:
    """One dark contour, with every property the selector needs to judge it.

    All ratios are dimensionless and therefore resolution independent, which is
    what lets one configuration serve a 150 dpi and a 300 dpi scan
    (``docs/IMAGE_PROCESSING.md``, "Resolution independence").

    Attributes:
        center: Contour centroid from image moments - the marker's position.
            Chosen over the bounding-rectangle centre because a centroid is an
            average over the whole filled area, so a nicked or blotched edge
            moves it in proportion to the area affected, while a bounding
            rectangle is defined by extreme pixels and jumps with a single one.
        bounding_box: Axis-aligned bounding rectangle, used for windowing.
        area_px: Contour area in square pixels at working resolution.
        area_ratio: ``area_px`` divided by the working image area.
        aspect_ratio: Longer side divided by shorter side of the minimum-area
            rectangle, so the measure is invariant to rotation and is always at
            least 1.0; a square gives exactly 1.0.
        rectangularity: ``area_px`` divided by the minimum-area rectangle's
            area. 1.0 for a perfect rectangle of any rotation.
        solidity: ``area_px`` divided by the convex hull area. Separates a solid
            blob from a ring or a jagged smear.
        fill_ratio: Fraction of the contour's *interior* that is ink. Near 1.0
            for a printed registration square; low for an empty answer box,
            whose external contour encloses mostly paper. This is the property
            that distinguishes a solid marker from a printed frame - contour
            shape alone cannot, because both have the same outline.
        extent: ``area_px`` divided by the axis-aligned bounding-box area.
            Reported for diagnostics; it falls with rotation, so it is not used
            for acceptance.
    """

    center: Point
    bounding_box: BoundingBox
    area_px: float
    area_ratio: float
    aspect_ratio: float
    rectangularity: float
    solidity: float
    fill_ratio: float
    extent: float

    def scaled(self, factor: float) -> MarkerCandidate:
        """Return the candidate with its pixel measurements multiplied by ``factor``.

        Ratios are scale invariant and are carried across unchanged.
        """
        return MarkerCandidate(
            center=self.center.scaled(factor),
            bounding_box=self.bounding_box.scaled(factor),
            area_px=self.area_px * factor * factor,
            area_ratio=self.area_ratio,
            aspect_ratio=self.aspect_ratio,
            rectangularity=self.rectangularity,
            solidity=self.solidity,
            fill_ratio=self.fill_ratio,
            extent=self.extent,
        )


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    """A contour that failed a shape filter, kept only for diagnostics."""

    candidate: MarkerCandidate
    reason: str


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """A candidate evaluated against one particular page corner.

    Attributes:
        candidate: The measured contour.
        corner: Which corner of the *scanned image* it was scored against.
        score: Combined score in ``[0, 1]``; higher is better.
        breakdown: The individual component scores that produced ``score``,
            keyed by component name, for diagnostics and calibration.
    """

    candidate: MarkerCandidate
    corner: ImageCorner
    score: float
    breakdown: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class RegistrationMarkerDetection:
    """A corner marker that was selected for use in the transform.

    Attributes:
        role: The corner this marker plays **in the canonical frame**, i.e.
            after orientation has been resolved.
        candidate: The measured contour, in full-resolution image pixels.
        score: The selection score of the candidate in ``[0, 1]``.
        breakdown: Component scores behind ``score``.
        alternative_count: How many other candidates competed for this corner.
    """

    role: MarkerRole
    candidate: MarkerCandidate
    score: float
    breakdown: Mapping[str, float]
    alternative_count: int = 0

    @property
    def center(self) -> Point:
        """Centre of the marker in full-resolution image pixels."""
        return self.candidate.center


@dataclass(frozen=True, slots=True)
class OrientationHypothesis:
    """One of the four possible assignments of image corners to canonical roles.

    Attributes:
        quarter_turns: The index, in the clockwise image-corner sequence
            (top-left, top-right, bottom-right, bottom-left *of the scan*), of
            the corner that is the sheet's canonical top-left. Equivalently, the
            number of 90 degree clockwise turns the page underwent relative to
            upright: a page fed a quarter turn clockwise puts its top-left
            corner at the scan's top-right, which is index 1.
        geometry_valid: Whether the resulting quadrilateral passes geometry
            validation. An invalid hypothesis cannot be the right answer, so it
            is pruned before the orientation mark is consulted.
        confidence: Measured evidence for the orientation mark in ``[0, 1]``:
            the ink found in the expected window divided by the ink a correctly
            printed mark would put there.
        sample_window: Where the mark was looked for, in image pixels.
    """

    quarter_turns: int
    geometry_valid: bool
    confidence: float
    sample_window: BoundingBox


@dataclass(frozen=True, slots=True)
class OrientationResult:
    """How the page was found to be oriented, and how sure the engine is.

    Attributes:
        quarter_turns: Clockwise 90 degree turns the page underwent relative to
            upright; see :attr:`OrientationHypothesis.quarter_turns`. ``0`` means
            the scan was already the right way up.
        confidence: Evidence for the winning hypothesis in ``[0, 1]``.
        margin: Winning confidence minus the runner-up's. A large margin means
            the answer is unambiguous, which matters far more than the absolute
            confidence when the print is faint.
        marker_center: Centroid of the located orientation mark in image pixels,
            or ``None`` when the fallback was used.
        marker_box: Bounding box of the located mark, or ``None``.
        assumed: ``True`` when no mark was found and the configured fallback
            supplied the orientation instead of a measurement.
        hypotheses: All four evaluated hypotheses, for diagnostics.
    """

    quarter_turns: int
    confidence: float
    margin: float
    marker_center: Point | None
    marker_box: BoundingBox | None
    assumed: bool
    hypotheses: tuple[OrientationHypothesis, ...] = ()


@dataclass(frozen=True, slots=True)
class AlignmentMetrics:
    """Measured quality of one alignment.

    Deliberately several named numbers rather than one opaque "confidence":
    a single blended score cannot tell a faint orientation mark apart from a
    skewed page, and those two need different human responses.

    Attributes:
        candidate_count: Contours that survived the shape filters.
        rejected_count: Contours discarded by the shape filters.
        marker_scores: Selection score per canonical corner role.
        min_marker_score: Lowest of ``marker_scores``.
        quadrilateral_area_ratio: Area of the marker quadrilateral divided by
            the area of the scanned image. Small values mean the page occupies
            little of the scan, which costs effective resolution.
        source_aspect_ratio: Width/height of the detected marker quadrilateral,
            averaging opposite sides.
        expected_aspect_ratio: Width/height of the canonical marker rectangle.
        aspect_ratio_deviation: ``source/expected - 1``; zero for a perfectly
            square-on scan.
        min_marker_separation_px: Shortest distance between any two selected
            markers, in pixels of the source image.
        orientation_confidence: :attr:`OrientationResult.confidence`.
        orientation_margin: :attr:`OrientationResult.margin`.
        mean_reprojection_error_px: Mean distance between each detected marker
            mapped through the transform and its canonical target. See
            ``docs/IMAGE_PROCESSING.md``: with exactly four correspondences the
            homography is exact, so this measures numerical conditioning, **not**
            geometric accuracy. Interior control points measure that.
        max_reprojection_error_px: Largest of the same distances.
        working_scale: Factor by which the image was downscaled for detection;
            ``1.0`` when detection ran at full resolution.
        elapsed_seconds: Wall-clock duration of the alignment call.
    """

    candidate_count: int
    rejected_count: int
    marker_scores: Mapping[MarkerRole, float]
    min_marker_score: float
    quadrilateral_area_ratio: float
    source_aspect_ratio: float
    expected_aspect_ratio: float
    aspect_ratio_deviation: float
    min_marker_separation_px: float
    orientation_confidence: float
    orientation_margin: float
    mean_reprojection_error_px: float
    max_reprojection_error_px: float
    working_scale: float
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class AlignmentDiagnostics:
    """Intermediate state kept only when diagnostics are enabled.

    Generating this must never change what the pipeline decides; it is recorded
    alongside the decision, never consulted by it.

    Attributes:
        working_scale: Downscale factor detection ran at.
        grayscale: The working-resolution grayscale image.
        binary: The working-resolution binary image contours were found in.
        candidates: Every contour that passed the shape filters, at full
            resolution.
        rejected: Every contour that did not, with the failing property named.
        scored: Per-corner scoring of the surviving candidates.
        source_quadrilateral: The four selected marker centres in canonical
            order, in source-image pixels.
    """

    working_scale: float
    grayscale: NDArray[np.uint8]
    binary: NDArray[np.uint8]
    candidates: tuple[MarkerCandidate, ...]
    rejected: tuple[RejectedCandidate, ...]
    scored: tuple[ScoredCandidate, ...]
    source_quadrilateral: tuple[Point, ...]


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    """The outcome of normalising one scan into canonical template coordinates.

    Attributes:
        normalized_image: The rectified page, exactly
            ``canonical_width`` x ``canonical_height`` pixels, with the same
            channel layout as the input. This is the contract every later phase
            consumes.
        original_width: Width of the supplied scan in pixels.
        original_height: Height of the supplied scan.
        canonical_width: Width of :attr:`normalized_image`.
        canonical_height: Height of :attr:`normalized_image`.
        corner_markers: The four selected markers in
            :data:`CANONICAL_CORNER_ORDER`.
        orientation: How the page was found to be oriented.
        transform_matrix: 3x3 homography mapping source-image pixels to
            canonical pixels.
        inverse_transform_matrix: Its inverse, mapping canonical pixels back to
            the source scan - what a GUI overlay needs to point at the original
            paper during conflict review.
        metrics: Measured quality of the alignment.
        warnings: Non-fatal observations, from :class:`AlignmentWarning`.
        diagnostics: Intermediate state, present only when diagnostics were
            requested.
    """

    normalized_image: NDArray[np.uint8]
    original_width: int
    original_height: int
    canonical_width: int
    canonical_height: int
    corner_markers: tuple[RegistrationMarkerDetection, ...]
    orientation: OrientationResult
    transform_matrix: NDArray[np.float64]
    inverse_transform_matrix: NDArray[np.float64]
    metrics: AlignmentMetrics
    warnings: tuple[AlignmentWarning, ...] = ()
    diagnostics: AlignmentDiagnostics | None = field(default=None, repr=False)

    def marker(self, role: MarkerRole) -> RegistrationMarkerDetection:
        """Return the selected marker playing ``role`` in the canonical frame.

        Raises:
            KeyError: No marker carries that role, which cannot happen for a
                result produced by :func:`omr_scanner.imaging.align_sheet`.
        """
        for detection in self.corner_markers:
            if detection.role is role:
                return detection
        raise KeyError(f"No registration marker with role {role!r}")

    @property
    def source_quadrilateral(self) -> tuple[Point, ...]:
        """The four detected marker centres in canonical order, in source pixels."""
        return tuple(detection.center for detection in self.corner_markers)


def warnings_as_strings(warnings: Sequence[AlignmentWarning]) -> tuple[str, ...]:
    """Return ``warnings`` as plain strings, for logging and serialisation."""
    return tuple(warning.value for warning in warnings)

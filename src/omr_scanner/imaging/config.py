"""Tunable parameters of the geometric normalisation pipeline.

Purpose:
    Hold every number the alignment engine depends on in one place, named and
    documented, so that no threshold is ever buried in an ``if`` statement.

Responsibilities:
    * Declare the configuration groups (preprocessing, marker detection,
      orientation, geometry) and the top-level :class:`AlignmentConfig`.
    * Validate them, so an impossible configuration fails at construction rather
      than halfway through a batch.
    * Derive the quantities the algorithms actually compare against - accepted
      marker area and aspect ranges, canonical marker-centre targets, the
      expected quadrilateral aspect ratio.

What does NOT belong here:
    * Reading an ``.omrt`` document. The template is the *source* of these values
      in production, but the conversion lives in
      :mod:`omr_scanner.services.alignment_service`, so that the imaging layer
      stays testable with synthetic geometry and never depends on a particular
      file format (``docs/ARCHITECTURE.md``).
    * Any image data.

Units:
    Every ``*_ratio``, ``*_x``, ``*_y``, ``*_width`` and ``*_height`` value is a
    fraction of the canonical page (or, where the docstring says so, of the
    scanned image). Only values whose name ends in ``_px`` are pixels.

Defaults:
    The shipped defaults describe the example sheet design in
    ``resources/templates/example_answer_sheet.omrt`` - A4 at 150 dpi with four
    3 mm corner squares. They exist so that the developer tools and the tests
    have something to run against, not because A4 is privileged; every value is
    replaced from the template in production.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from omr_scanner.domain.geometry import NormalizedPoint
from omr_scanner.domain.template import MarkerRole
from omr_scanner.imaging.models import CANONICAL_CORNER_ORDER, Point

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

DEFAULT_CANONICAL_WIDTH_PX = 1240
"""Width of the rectified page produced by default: A4 at 150 dpi."""

DEFAULT_CANONICAL_HEIGHT_PX = 1754
"""Height of the rectified page produced by default: A4 at 150 dpi."""

DEFAULT_MARKER_TARGETS: Mapping[MarkerRole, NormalizedPoint] = MappingProxyType(
    {
        MarkerRole.TOP_LEFT: NormalizedPoint(x=0.05, y=0.035),
        MarkerRole.TOP_RIGHT: NormalizedPoint(x=0.95, y=0.035),
        MarkerRole.BOTTOM_RIGHT: NormalizedPoint(x=0.95, y=0.965),
        MarkerRole.BOTTOM_LEFT: NormalizedPoint(x=0.05, y=0.965),
    }
)
"""Expected canonical **centres** of the four registration markers.

These are marker centres, not page corners. Alignment maps each detected centre
onto the corresponding point here, which is why the printed margin outside the
markers is preserved rather than being stretched across the whole output (see
``docs/IMAGE_PROCESSING.md``, "What the transform maps onto what").
"""


class ThresholdStrategy(StrEnum):
    """How a grayscale page is reduced to ink and paper.

    ``OTSU`` picks one global threshold from the image histogram. On an OMR
    sheet - mostly white paper with sparse, strongly contrasting print - the
    histogram is cleanly bimodal, so a global split is both accurate and cheap.

    The adaptive strategies compare each pixel with the mean of its
    neighbourhood. They survive a strong illumination gradient (a page lifted
    off the platen, a shadow from a bound spine), at the cost of turning large
    uniform areas into noise, which produces many more spurious contours.
    """

    OTSU = "otsu"
    ADAPTIVE_MEAN = "adaptive_mean"
    ADAPTIVE_GAUSSIAN = "adaptive_gaussian"


@dataclass(frozen=True, slots=True)
class PreprocessingConfig:
    """Grayscale, denoising and thresholding parameters.

    Attributes:
        min_image_dimension_px: Shortest side an input image may have. Anything
            smaller cannot resolve a printed marker, so it is rejected as
            invalid input rather than silently failing detection.
        working_max_dimension_px: Detection runs on a copy downscaled so that
            its longer side is at most this many pixels, bounding the cost of
            contour finding on a very large scan. Marker geometry is judged by
            ratios, so *which* contour is chosen is unaffected; what the
            downscale costs is precision, because a marker centroid measured on
            the working copy is multiplied back up. Control-point error is
            therefore roughly inversely proportional to the achieved scale: on
            A4 at 300 dpi the measured maximum is 0.14 px at full resolution,
            0.45 px at 0.63 and 1.12 px at 0.42. The default leaves a 300 dpi A4
            scan close to full resolution and only starts to bite at 600 dpi.
            The *output* is always warped from the full-resolution original.
        blur_kernel_px: Side of the Gaussian kernel applied before thresholding,
            in pixels at working resolution. Must be odd; ``0`` disables
            blurring. Suppresses scanner speckle that would otherwise become
            hundreds of tiny contours.
        threshold_strategy: See :class:`ThresholdStrategy`.
        adaptive_block_ratio: Neighbourhood side for the adaptive strategies,
            as a fraction of the working image's shorter side. Expressed as a
            ratio so that the neighbourhood covers the same amount of *paper*
            at any scan resolution. It must stay **larger than the marker's own
            normalised size**: a solid shape wider than its neighbourhood sets
            that neighbourhood's mean itself, so its interior reads as
            background and the marker comes out hollow. The default leaves
            comfortable room above a 3 per cent marker.
        adaptive_offset: Constant subtracted from the local mean by the adaptive
            strategies, in grey levels (0-255). Larger values classify fewer
            pixels as ink.
    """

    min_image_dimension_px: int = 64
    working_max_dimension_px: int = 3600
    blur_kernel_px: int = 3
    threshold_strategy: ThresholdStrategy = ThresholdStrategy.OTSU
    adaptive_block_ratio: float = 0.04
    adaptive_offset: int = 8

    def __post_init__(self) -> None:
        """Reject configurations that could not produce a usable binary image."""
        if self.min_image_dimension_px < 1:
            raise ValueError("min_image_dimension_px must be at least 1")
        if self.working_max_dimension_px < self.min_image_dimension_px:
            raise ValueError("working_max_dimension_px must be >= min_image_dimension_px")
        if self.blur_kernel_px < 0:
            raise ValueError("blur_kernel_px must be zero or positive")
        if self.blur_kernel_px and self.blur_kernel_px % 2 == 0:
            raise ValueError("blur_kernel_px must be odd (OpenCV requires an odd kernel)")
        if not 0.0 < self.adaptive_block_ratio < 1.0:
            raise ValueError("adaptive_block_ratio must lie in (0, 1)")


@dataclass(frozen=True, slots=True)
class MarkerDetectionConfig:
    """Which dark contours may be registration markers, and how they are ranked.

    Acceptance is deliberately a *combination* of properties. A single test such
    as "the polygon approximation has four vertices" also accepts table borders,
    answer-box outlines and printed frames, all of which appear on real sheets.

    Attributes:
        expected_marker_width: Printed marker width as a fraction of canonical
            page width.
        expected_marker_height: Printed marker height as a fraction of canonical
            page height.
        marker_area_tolerance: Multiplicative tolerance on the expected marker
            area ratio; a candidate is accepted when its area ratio lies between
            ``expected / tolerance`` and ``expected * tolerance``. A ratio is
            used rather than pixels because a marker's share of the image area
            is unchanged by scan resolution. The tolerance must absorb a real
            effect: rotating a page enlarges its bounding canvas, which dilutes
            every area ratio (a 10 degree rotation of A4 dilutes by about 1.4).
        marker_aspect_tolerance: Multiplicative tolerance on the expected
            width/height ratio of the marker's minimum-area rectangle.
        min_marker_area_px: Absolute floor in working-resolution pixels. Below
            this a contour has too few pixels for its shape statistics to mean
            anything, whatever the ratios say.
        min_rectangularity: Lowest accepted contour-area / minimum-area-rectangle
            ratio. Rejects crosses, ticks and torn shapes.
        min_solidity: Lowest accepted contour-area / convex-hull-area ratio.
            Rejects rings, outlines and jagged smears.
        min_fill_ratio: Lowest accepted fraction of the contour's interior that
            is ink. A registration square is printed solid; an empty answer box
            traces the same outline but encloses paper. Contour shape cannot
            separate the two, so this measurement is what does.
        corner_search_width: Width of each corner search region, as a fraction
            of the *scanned image* width. Detection only considers a candidate
            for a corner if it falls inside that corner's region.
        corner_search_height: Height of each corner search region, as a fraction
            of the scanned image height.
        min_candidate_score: Lowest combined score at which a candidate may be
            selected for a corner.
        max_candidates_per_corner: How many ranked candidates per corner are
            carried into the assignment search. Bounds the combinatorics and the
            diagnostic output; the assignment is exhaustive within this set.
        shape_weight: Weight of the shape component in the combined score.
        proximity_weight: Weight of the corner-proximity component.
        low_score_warning_ratio: A selected marker whose score is below this
            multiple of ``min_candidate_score`` raises
            ``LOW_MARKER_SCORE``.
        low_rectangularity_warning: A selected marker below this rectangularity
            raises ``LOW_MARKER_RECTANGULARITY``.
        edge_margin_ratio: A marker whose bounding box comes within this
            fraction of the image's shorter side of the border raises
            ``MARKER_NEAR_IMAGE_EDGE``: the scan may be cropped.
    """

    expected_marker_width: float = 0.03
    expected_marker_height: float = 0.021
    marker_area_tolerance: float = 4.0
    marker_aspect_tolerance: float = 2.0
    min_marker_area_px: float = 12.0
    min_rectangularity: float = 0.75
    min_solidity: float = 0.85
    min_fill_ratio: float = 0.70
    corner_search_width: float = 0.32
    corner_search_height: float = 0.32
    min_candidate_score: float = 0.45
    max_candidates_per_corner: int = 6
    shape_weight: float = 0.6
    proximity_weight: float = 0.4
    low_score_warning_ratio: float = 1.25
    low_rectangularity_warning: float = 0.85
    edge_margin_ratio: float = 0.005

    def __post_init__(self) -> None:
        """Reject configurations that could never accept a marker."""
        if not 0.0 < self.expected_marker_width <= 1.0:
            raise ValueError("expected_marker_width must lie in (0, 1]")
        if not 0.0 < self.expected_marker_height <= 1.0:
            raise ValueError("expected_marker_height must lie in (0, 1]")
        if self.marker_area_tolerance <= 1.0:
            raise ValueError("marker_area_tolerance must be greater than 1")
        if self.marker_aspect_tolerance <= 1.0:
            raise ValueError("marker_aspect_tolerance must be greater than 1")
        if not 0.0 < self.corner_search_width <= 1.0:
            raise ValueError("corner_search_width must lie in (0, 1]")
        if not 0.0 < self.corner_search_height <= 1.0:
            raise ValueError("corner_search_height must lie in (0, 1]")
        if self.max_candidates_per_corner < 1:
            raise ValueError("max_candidates_per_corner must be at least 1")
        if self.shape_weight < 0.0 or self.proximity_weight < 0.0:
            raise ValueError("Score weights must not be negative")
        if self.shape_weight + self.proximity_weight <= 0.0:
            raise ValueError("At least one score weight must be positive")


@dataclass(frozen=True, slots=True)
class OrientationConfig:
    """Where the orientation mark is expected, and how firmly it must be seen.

    Attributes:
        marker_center_x: Canonical x of the mark's centre, normalised.
        marker_center_y: Canonical y of the mark's centre, normalised.
        marker_width: Canonical width of the mark, normalised to page width.
        marker_height: Canonical height of the mark, normalised to page height.
        window_margin: The evidence window is the printed mark scaled by this
            factor about its centre. A margin larger than 1 tolerates a small
            residual misalignment; the ink a correct mark puts in that window is
            therefore ``1 / margin**2`` of it, which is what confidence is
            measured against.
        search_radius: Half-size of the larger window used to *locate* the mark
            once the orientation is decided, normalised to the page. Distinct
            from ``window_margin``, which sizes the window used to *decide*.
        min_confidence: Lowest winning confidence that counts as a detection.
        min_margin: Lowest gap between the winning and runner-up confidence. A
            clear winner matters more than a high absolute score: faint print
            lowers every hypothesis equally, a wrong assignment lowers only the
            wrong ones.
        low_confidence_warning: Confidence below which the result carries
            ``LOW_ORIENTATION_CONFIDENCE``.
        allow_fallback: When ``True``, a page whose orientation mark cannot be
            seen is aligned using ``fallback_quarter_turns`` and the result
            carries ``ORIENTATION_ASSUMED``. Off by default: an undetected
            upside-down sheet produces a complete, confident and wrong set of
            answers.
        fallback_quarter_turns: Orientation assumed when the fallback is used.
    """

    marker_center_x: float = 0.14
    marker_center_y: float = 0.035
    marker_width: float = 0.05
    marker_height: float = 0.012
    window_margin: float = 1.6
    search_radius: float = 0.06
    min_confidence: float = 0.35
    min_margin: float = 0.15
    low_confidence_warning: float = 0.60
    allow_fallback: bool = False
    fallback_quarter_turns: int = 0

    def __post_init__(self) -> None:
        """Reject configurations whose window could not contain the mark."""
        if not 0.0 <= self.marker_center_x <= 1.0 or not 0.0 <= self.marker_center_y <= 1.0:
            raise ValueError("Orientation marker centre must lie on the page")
        if not 0.0 < self.marker_width <= 1.0 or not 0.0 < self.marker_height <= 1.0:
            raise ValueError("Orientation marker size must lie in (0, 1]")
        if self.window_margin < 1.0:
            raise ValueError("window_margin must be at least 1 (the window holds the mark)")
        if not 0.0 < self.search_radius <= 0.5:
            raise ValueError("search_radius must lie in (0, 0.5]")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must lie in [0, 1]")
        if self.fallback_quarter_turns not in (0, 1, 2, 3):
            raise ValueError("fallback_quarter_turns must be 0, 1, 2 or 3")

    @property
    def expected_window_fill(self) -> float:
        """Fraction of the evidence window a correctly printed mark covers."""
        return 1.0 / (self.window_margin * self.window_margin)


@dataclass(frozen=True, slots=True)
class GeometryConfig:
    """What counts as a plausible page quadrilateral.

    Attributes:
        min_quadrilateral_area_ratio: Smallest accepted marker-quadrilateral
            area as a fraction of the scanned image area. Catches four contours
            that happen to sit in one corner of a page.
        min_marker_separation_ratio: Smallest accepted distance between any two
            markers, as a fraction of the largest distance between any two. A
            genuine page rectangle cannot be nearly degenerate.
        max_aspect_ratio_deviation: Largest accepted ``measured / expected - 1``
            for the quadrilateral's width-to-height ratio, where expected comes
            from the canonical marker rectangle. Perspective and skew move this
            a little; a swapped page orientation moves it a lot, which is why
            the same test also prunes impossible orientation hypotheses.
        aspect_ratio_warning_deviation: Deviation above which the result carries
            ``ASPECT_RATIO_DEVIATION``.
        max_reprojection_error_px: Largest control-point reprojection error, in
            canonical pixels, that does not raise
            ``LARGE_REPROJECTION_ERROR``. With exactly four correspondences the
            homography is exact, so this only guards against an ill-conditioned
            solve.
    """

    min_quadrilateral_area_ratio: float = 0.10
    min_marker_separation_ratio: float = 0.20
    max_aspect_ratio_deviation: float = 0.30
    aspect_ratio_warning_deviation: float = 0.15
    max_reprojection_error_px: float = 1.0

    def __post_init__(self) -> None:
        """Reject impossible validation bounds."""
        if not 0.0 < self.min_quadrilateral_area_ratio < 1.0:
            raise ValueError("min_quadrilateral_area_ratio must lie in (0, 1)")
        if not 0.0 < self.min_marker_separation_ratio < 1.0:
            raise ValueError("min_marker_separation_ratio must lie in (0, 1)")
        if self.max_aspect_ratio_deviation <= 0.0:
            raise ValueError("max_aspect_ratio_deviation must be positive")
        if self.max_reprojection_error_px <= 0.0:
            raise ValueError("max_reprojection_error_px must be positive")


@dataclass(frozen=True, slots=True)
class AlignmentConfig:
    """Everything :func:`omr_scanner.imaging.align_sheet` needs besides the image.

    Attributes:
        canonical_width: Width in pixels of the rectified page produced.
        canonical_height: Height in pixels of the rectified page produced.
        marker_targets: Expected canonical centre of each registration marker,
            normalised. All four corner roles must be present.
        preprocessing: See :class:`PreprocessingConfig`.
        marker_detection: See :class:`MarkerDetectionConfig`.
        orientation: See :class:`OrientationConfig`.
        geometry: See :class:`GeometryConfig`.
        diagnostics: When ``True``, the result carries
            :class:`~omr_scanner.imaging.models.AlignmentDiagnostics`. Off by
            default because it retains two working-resolution images per sheet.
    """

    canonical_width: int = DEFAULT_CANONICAL_WIDTH_PX
    canonical_height: int = DEFAULT_CANONICAL_HEIGHT_PX
    marker_targets: Mapping[MarkerRole, NormalizedPoint] = DEFAULT_MARKER_TARGETS
    preprocessing: PreprocessingConfig = field(default_factory=PreprocessingConfig)
    marker_detection: MarkerDetectionConfig = field(default_factory=MarkerDetectionConfig)
    orientation: OrientationConfig = field(default_factory=OrientationConfig)
    geometry: GeometryConfig = field(default_factory=GeometryConfig)
    diagnostics: bool = False

    def __post_init__(self) -> None:
        """Reject a canonical page or marker layout that cannot be transformed onto."""
        if self.canonical_width < 1 or self.canonical_height < 1:
            raise ValueError("Canonical page dimensions must be positive")
        missing = set(CANONICAL_CORNER_ORDER) - set(self.marker_targets)
        if missing:
            names = ", ".join(sorted(role.value for role in missing))
            raise ValueError(f"marker_targets is missing corner role(s): {names}")

    @property
    def canonical_size(self) -> tuple[int, int]:
        """``(width, height)`` of the rectified page, in pixels."""
        return (self.canonical_width, self.canonical_height)

    def normalized_to_canonical(self, point: NormalizedPoint) -> Point:
        """Convert a normalised page coordinate to canonical pixels.

        Args:
            point: Position as a fraction of the canonical page.

        Returns:
            The same position in canonical pixels; ``(0.035, 0.025)`` on a
            1240 x 1754 page becomes ``(43.4, 43.85)``.
        """
        return Point(x=point.x * self.canonical_width, y=point.y * self.canonical_height)

    def canonical_marker_points(self) -> tuple[Point, ...]:
        """Return the four canonical marker centres in :data:`CANONICAL_CORNER_ORDER`."""
        return tuple(
            self.normalized_to_canonical(self.marker_targets[role])
            for role in CANONICAL_CORNER_ORDER
        )

    @property
    def expected_marker_area_ratio(self) -> float:
        """Share of the page area one printed marker covers."""
        return (
            self.marker_detection.expected_marker_width
            * self.marker_detection.expected_marker_height
        )

    @property
    def marker_area_ratio_bounds(self) -> tuple[float, float]:
        """Accepted ``(minimum, maximum)`` marker area ratio."""
        tolerance = self.marker_detection.marker_area_tolerance
        expected = self.expected_marker_area_ratio
        return (expected / tolerance, min(1.0, expected * tolerance))

    @property
    def expected_marker_aspect_ratio(self) -> float:
        """Printed marker longer side divided by its shorter side, in canonical pixels.

        The normalised width and height use different denominators, so the
        printed aspect ratio only exists once both are projected onto the
        canonical page. The longer side is used as the numerator so the value is
        invariant to how the page was fed, matching
        :attr:`~omr_scanner.imaging.models.MarkerCandidate.aspect_ratio`.
        """
        width_px = self.marker_detection.expected_marker_width * self.canonical_width
        height_px = self.marker_detection.expected_marker_height * self.canonical_height
        return max(width_px, height_px) / min(width_px, height_px)

    @property
    def marker_aspect_ratio_bounds(self) -> tuple[float, float]:
        """Accepted ``(minimum, maximum)`` marker aspect ratio.

        The lower bound never drops below 1.0, because a measured aspect ratio
        is defined as longer over shorter and cannot be less than that.
        """
        tolerance = self.marker_detection.marker_aspect_tolerance
        expected = self.expected_marker_aspect_ratio
        return (max(1.0, expected / tolerance), expected * tolerance)

    @property
    def expected_quadrilateral_aspect_ratio(self) -> float:
        """Width divided by height of the canonical marker rectangle.

        This is *not* the page aspect ratio: the markers sit inside the page
        margins, so the rectangle they span is slightly different, and comparing
        against the page would build a constant bias into the check.
        """
        top_left, top_right, bottom_right, bottom_left = self.canonical_marker_points()
        width = (top_left.distance_to(top_right) + bottom_left.distance_to(bottom_right)) / 2.0
        height = (top_left.distance_to(bottom_left) + top_right.distance_to(bottom_right)) / 2.0
        if height <= 0.0 or not math.isfinite(width / height):
            raise ValueError("Canonical marker targets are degenerate")
        return width / height

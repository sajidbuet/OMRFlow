"""Reusable planar geometry for the alignment engine.

Purpose:
    Provide the small, exactly testable geometric operations the pipeline is
    built from: ordering four points, measuring a quadrilateral, deciding
    whether it could be a page, and computing the homography between two sets of
    four points.

Responsibilities:
    * Pure functions over :class:`~omr_scanner.imaging.models.Point` values.
    * One validation entry point, :func:`validate_page_quadrilateral`, which
      raises :class:`~omr_scanner.errors.InvalidPageGeometryError` with a
      message naming the property that failed.

What does NOT belong here:
    * Pixels. Nothing in this module reads an image; it operates on coordinates
      that were measured elsewhere. That is what makes the rotation, skew and
      perspective cases testable without rendering anything.
    * Detection or selection policy.

Coordinate convention:
    Origin top-left, ``x`` right, ``y`` down. Because ``y`` grows downward, the
    visually clockwise order TL, TR, BR, BL has a *positive* shoelace area; the
    sign conventions in this module follow from that and are not arbitrary.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import cv2
import numpy as np

from omr_scanner.errors import AlignmentTransformError, InvalidPageGeometryError
from omr_scanner.imaging.models import Point

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from omr_scanner.imaging.config import GeometryConfig

QUADRILATERAL_VERTICES = 4
"""A page is bounded by exactly four registration markers."""

_TOP_LEFT_DIRECTION_RADIANS = -3.0 * math.pi / 4.0
"""Direction from a quadrilateral's centroid towards its top-left vertex.

In image coordinates the top-left vertex lies up and to the left of the centre,
so ``atan2(negative, negative)`` gives -135 degrees.
"""


def centroid(points: Sequence[Point]) -> Point:
    """Return the arithmetic mean of ``points``.

    Raises:
        ValueError: ``points`` is empty.
    """
    if not points:
        raise ValueError("Cannot take the centroid of an empty point set")
    return Point(
        x=sum(point.x for point in points) / len(points),
        y=sum(point.y for point in points) / len(points),
    )


def order_clockwise(points: Sequence[Point]) -> tuple[Point, ...]:
    """Order four points as top-left, top-right, bottom-right, bottom-left.

    The points are sorted by their angle about the centroid, then the sequence
    is rotated to start at the vertex closest to the "up and to the left"
    direction. Sorting by angle is stable under rotation, scaling and
    perspective, which a sum/difference heuristic on raw coordinates is not: for
    a sheared quadrilateral the smallest ``x + y`` can belong to the top-right
    vertex.

    Assumption:
        The quadrilateral is within 45 degrees of upright. Beyond that the
        anchor vertex is genuinely ambiguous from geometry alone, which is
        exactly the ambiguity the orientation marker resolves - see
        :mod:`omr_scanner.imaging.orientation`. This function therefore
        establishes the *cyclic* order; the pipeline then rotates it to the
        orientation the marker proves.

    Args:
        points: Exactly four distinct points.

    Returns:
        The same four points in canonical corner order.

    Raises:
        ValueError: ``points`` does not hold exactly four entries, or they are
            degenerate (two or more coincide with the centroid).
    """
    if len(points) != QUADRILATERAL_VERTICES:
        raise ValueError(f"Expected {QUADRILATERAL_VERTICES} points, received {len(points)}")

    center = centroid(points)
    angled: list[tuple[float, Point]] = []
    for point in points:
        dx = point.x - center.x
        dy = point.y - center.y
        if dx == 0.0 and dy == 0.0:
            raise ValueError("A quadrilateral vertex coincides with the centroid")
        angled.append((math.atan2(dy, dx), point))

    angled.sort(key=lambda item: item[0])
    anchor = min(
        range(len(angled)),
        key=lambda index: _angular_distance(angled[index][0], _TOP_LEFT_DIRECTION_RADIANS),
    )
    rotated = angled[anchor:] + angled[:anchor]
    return tuple(point for _, point in rotated)


def rotate_corner_order(ordered: Sequence[Point], quarter_turns: int) -> tuple[Point, ...]:
    """Re-anchor a cyclic corner sequence by ``quarter_turns`` steps.

    Args:
        ordered: Four points in cyclic (clockwise) order.
        quarter_turns: How many 90 degree clockwise steps the page must turn to
            become upright, i.e. how far along the sequence the true top-left
            vertex sits.

    Returns:
        The sequence rotated so that index ``quarter_turns`` becomes index 0.

    Raises:
        ValueError: The sequence is not four points, or ``quarter_turns`` is not
            0, 1, 2 or 3.
    """
    if len(ordered) != QUADRILATERAL_VERTICES:
        raise ValueError(f"Expected {QUADRILATERAL_VERTICES} points, received {len(ordered)}")
    if quarter_turns not in (0, 1, 2, 3):
        raise ValueError("quarter_turns must be 0, 1, 2 or 3")
    return tuple(ordered[(index + quarter_turns) % QUADRILATERAL_VERTICES] for index in range(4))


def signed_area(points: Sequence[Point]) -> float:
    """Return the shoelace area of a polygon.

    Positive for a visually clockwise polygon, because ``y`` grows downward.
    """
    total = 0.0
    count = len(points)
    for index in range(count):
        current = points[index]
        following = points[(index + 1) % count]
        total += current.x * following.y - following.x * current.y
    return total / 2.0


def polygon_area(points: Sequence[Point]) -> float:
    """Return the unsigned area of a polygon."""
    return abs(signed_area(points))


def is_convex(points: Sequence[Point]) -> bool:
    """Return whether a polygon is strictly convex.

    Three collinear vertices count as non-convex: a page whose markers are
    collinear is degenerate, and the homography through them is ill-conditioned.
    """
    count = len(points)
    if count < 3:
        return False
    signs: list[float] = []
    for index in range(count):
        a = points[index]
        b = points[(index + 1) % count]
        c = points[(index + 2) % count]
        cross = (b.x - a.x) * (c.y - b.y) - (b.y - a.y) * (c.x - b.x)
        if cross == 0.0:
            return False
        signs.append(math.copysign(1.0, cross))
    return all(sign == signs[0] for sign in signs)


def is_simple_quadrilateral(points: Sequence[Point]) -> bool:
    """Return whether a four-point sequence traces a non-self-intersecting outline.

    A "bow tie" arises when two markers are assigned to swapped corners; the
    homography through such an assignment mirrors half the page, so the check is
    worth making explicitly even though convexity implies it.
    """
    if len(points) != QUADRILATERAL_VERTICES:
        return False
    return not (
        _segments_cross(points[0], points[1], points[2], points[3])
        or _segments_cross(points[1], points[2], points[3], points[0])
    )


def quadrilateral_aspect_ratio(ordered: Sequence[Point]) -> float:
    """Return width divided by height of an ordered quadrilateral.

    Opposite sides are averaged, so a modest perspective (one edge nearer the
    lens than the other) does not bias the measurement towards either edge.

    Raises:
        ValueError: The sequence is not four points, or its height is zero.
    """
    if len(ordered) != QUADRILATERAL_VERTICES:
        raise ValueError(f"Expected {QUADRILATERAL_VERTICES} points, received {len(ordered)}")
    top_left, top_right, bottom_right, bottom_left = ordered
    width = (top_left.distance_to(top_right) + bottom_left.distance_to(bottom_right)) / 2.0
    height = (top_left.distance_to(bottom_left) + top_right.distance_to(bottom_right)) / 2.0
    if height <= 0.0:
        raise ValueError("Quadrilateral has zero height")
    return width / height


def pairwise_distances(points: Sequence[Point]) -> tuple[float, ...]:
    """Return the distance between every unordered pair of ``points``."""
    return tuple(
        points[i].distance_to(points[j])
        for i in range(len(points))
        for j in range(i + 1, len(points))
    )


def validate_page_quadrilateral(
    ordered: Sequence[Point],
    *,
    image_area_px: float,
    expected_aspect_ratio: float,
    config: GeometryConfig,
) -> float:
    """Check that four ordered markers could bound a page, and return the deviation.

    Args:
        ordered: Four marker centres in canonical corner order.
        image_area_px: Area of the scanned image, for the relative-area test.
        expected_aspect_ratio: Width/height of the canonical marker rectangle.
        config: Validation bounds.

    Returns:
        The signed aspect-ratio deviation ``measured / expected - 1``, which the
        caller records as a quality metric.

    Raises:
        InvalidPageGeometryError: Any check failed; the message names which.
    """
    if len(ordered) != QUADRILATERAL_VERTICES:
        raise InvalidPageGeometryError(
            f"Expected {QUADRILATERAL_VERTICES} marker centres, received {len(ordered)}",
            user_message="The sheet could not be aligned: four corner markers are required.",
        )

    if not is_convex(ordered):
        raise InvalidPageGeometryError(
            f"Marker quadrilateral is not convex: {_describe(ordered)}",
            user_message="The corner markers do not form a page-shaped rectangle.",
        )

    if not is_simple_quadrilateral(ordered):
        raise InvalidPageGeometryError(
            f"Marker quadrilateral is self-intersecting: {_describe(ordered)}",
            user_message="The corner markers do not form a page-shaped rectangle.",
        )

    area = polygon_area(ordered)
    if image_area_px <= 0.0:
        raise InvalidPageGeometryError(
            "Image area must be positive to validate the marker quadrilateral",
            user_message="The scan could not be measured.",
        )
    area_ratio = area / image_area_px
    if area_ratio < config.min_quadrilateral_area_ratio:
        raise InvalidPageGeometryError(
            f"Marker quadrilateral covers {area_ratio:.4f} of the scan, below the "
            f"minimum {config.min_quadrilateral_area_ratio:.4f}",
            user_message="The detected sheet is far too small a part of the scan.",
        )

    distances = pairwise_distances(ordered)
    longest = max(distances)
    shortest = min(distances)
    if longest <= 0.0:
        raise InvalidPageGeometryError(
            "Marker quadrilateral is degenerate: all markers coincide",
            user_message="The corner markers do not form a page-shaped rectangle.",
        )
    separation_ratio = shortest / longest
    if separation_ratio < config.min_marker_separation_ratio:
        raise InvalidPageGeometryError(
            f"Two markers are {separation_ratio:.3f} of the page diagonal apart, below "
            f"the minimum {config.min_marker_separation_ratio:.3f}",
            user_message="Two corner markers are implausibly close together.",
        )

    measured_aspect = quadrilateral_aspect_ratio(ordered)
    deviation = measured_aspect / expected_aspect_ratio - 1.0
    if abs(deviation) > config.max_aspect_ratio_deviation:
        raise InvalidPageGeometryError(
            f"Marker quadrilateral aspect ratio {measured_aspect:.3f} deviates "
            f"{deviation:+.1%} from the expected {expected_aspect_ratio:.3f}, beyond "
            f"the permitted {config.max_aspect_ratio_deviation:.1%}",
            user_message="The detected sheet has the wrong proportions for this template.",
        )
    return deviation


def perspective_transform(
    source: Sequence[Point], target: Sequence[Point]
) -> NDArray[np.float64]:
    """Return the 3x3 homography mapping ``source`` onto ``target``.

    Four correspondences determine a homography exactly, so no least-squares fit
    is involved and the four points map onto their targets to numerical
    precision. Accuracy away from those four points is what the interior control
    points in the test suite measure.

    Raises:
        AlignmentTransformError: The point sets are not four points each, or the
            solve produced a non-finite matrix.
    """
    if len(source) != QUADRILATERAL_VERTICES or len(target) != QUADRILATERAL_VERTICES:
        raise AlignmentTransformError(
            f"A homography needs {QUADRILATERAL_VERTICES} correspondences; "
            f"received {len(source)} source and {len(target)} target points",
            user_message="The sheet could not be aligned.",
        )
    source_array = np.array([point.as_tuple() for point in source], dtype=np.float32)
    target_array = np.array([point.as_tuple() for point in target], dtype=np.float32)
    matrix = np.asarray(
        cv2.getPerspectiveTransform(source_array, target_array), dtype=np.float64
    )
    if not np.all(np.isfinite(matrix)):
        raise AlignmentTransformError(
            f"Perspective transform is not finite for source {_describe(source)}",
            user_message="The sheet could not be aligned.",
        )
    return matrix


def invert_transform(matrix: NDArray[np.float64]) -> NDArray[np.float64]:
    """Return the inverse homography, for mapping canonical points back to the scan.

    Raises:
        AlignmentTransformError: The matrix is singular.
    """
    try:
        inverse = np.linalg.inv(matrix)
    except np.linalg.LinAlgError as exc:
        raise AlignmentTransformError(
            f"Perspective transform is singular and cannot be inverted: {exc}",
            user_message="The sheet could not be aligned.",
        ) from exc
    if not np.all(np.isfinite(inverse)):
        raise AlignmentTransformError(
            "Inverse perspective transform is not finite",
            user_message="The sheet could not be aligned.",
        )
    return np.asarray(inverse, dtype=np.float64)


def apply_transform(
    matrix: NDArray[np.float64], points: Sequence[Point]
) -> tuple[Point, ...]:
    """Map ``points`` through a homography.

    Args:
        matrix: A 3x3 homography.
        points: Points in the matrix's source space.

    Returns:
        The mapped points. A point on the horizon (zero homogeneous weight)
        would be infinitely far away; that cannot arise for a validated page
        quadrilateral, and is reported rather than silently producing ``inf``.

    Raises:
        AlignmentTransformError: A point mapped to a zero homogeneous weight.
    """
    if not points:
        return ()
    homogeneous = np.array([[point.x, point.y, 1.0] for point in points], dtype=np.float64)
    mapped = homogeneous @ matrix.T
    weights = mapped[:, 2]
    if np.any(weights == 0.0):
        raise AlignmentTransformError(
            "A point mapped to the horizon of the perspective transform",
            user_message="The sheet could not be aligned.",
        )
    return tuple(
        Point(x=float(row[0] / row[2]), y=float(row[1] / row[2])) for row in mapped
    )


def reprojection_errors(
    matrix: NDArray[np.float64], source: Sequence[Point], target: Sequence[Point]
) -> tuple[float, ...]:
    """Return the distance, per correspondence, between ``source`` mapped and ``target``."""
    if len(source) != len(target):
        raise ValueError("Source and target point counts differ")
    mapped = apply_transform(matrix, source)
    return tuple(a.distance_to(b) for a, b in zip(mapped, target, strict=True))


def _angular_distance(angle: float, reference: float) -> float:
    """Return the absolute angular difference, wrapped into ``[0, pi]``."""
    difference = (angle - reference + math.pi) % (2.0 * math.pi) - math.pi
    return abs(difference)


def _orientation_sign(a: Point, b: Point, c: Point) -> float:
    """Return the sign of the cross product of ``ab`` and ``ac``."""
    cross = (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)
    if cross == 0.0:
        return 0.0
    return math.copysign(1.0, cross)


def _segments_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Return whether segment ``ab`` properly crosses segment ``cd``."""
    d1 = _orientation_sign(c, d, a)
    d2 = _orientation_sign(c, d, b)
    d3 = _orientation_sign(a, b, c)
    d4 = _orientation_sign(a, b, d)
    return d1 * d2 < 0.0 and d3 * d4 < 0.0


def _describe(points: Sequence[Point]) -> str:
    """Return a compact textual form of a point sequence, for error messages."""
    return "[" + ", ".join(f"({point.x:.1f},{point.y:.1f})" for point in points) + "]"

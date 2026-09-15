"""Finding and choosing the four printed registration markers.

Purpose:
    Locate the dark contours on a scanned page that could be registration
    markers, measure them, and choose exactly one for each corner of the scan.

Responsibilities:
    * Measure every external dark contour into a
      :class:`~omr_scanner.imaging.models.MarkerCandidate`.
    * Apply the shape filters, recording why each rejected contour failed.
    * Score the survivors against each corner search region and choose a
      one-to-one assignment of candidates to the four corners.

What does NOT belong here:
    * Deciding which corner of the scan is the sheet's top-left. Detection works
      in :class:`~omr_scanner.imaging.models.ImageCorner` terms only;
      :mod:`omr_scanner.imaging.orientation` supplies the canonical roles.
    * Any warping.

Why the filters are combined rather than applied one at a time:
    A single geometric test always has a counter-example on a real sheet. A
    four-vertex polygon approximation also matches table cells and answer-box
    outlines; a size test also matches a bold letter; a darkness test also
    matches a printed logo. Requiring size, aspect ratio, rectangularity,
    solidity and *interior ink* together, and then ranking what survives by
    proximity to the corner it is claimed for, is what separates a marker from
    the rest of the page.
"""

from __future__ import annotations

import itertools
import math
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from omr_scanner.errors import AmbiguousMarkerError, InsufficientMarkersError
from omr_scanner.imaging.models import (
    IMAGE_CORNER_ORDER,
    BoundingBox,
    ImageCorner,
    MarkerCandidate,
    Point,
    RejectedCandidate,
    ScoredCandidate,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from omr_scanner.imaging.config import AlignmentConfig

_MIN_CONTOUR_POINTS = 3
"""Fewer points than this cannot enclose an area."""


def measure_candidate(
    contour: NDArray[Any], *, binary: NDArray[np.uint8], image_area_px: float
) -> MarkerCandidate | None:
    """Measure one contour into a candidate, or return ``None`` if it has no area.

    Args:
        contour: An OpenCV contour from the working-resolution binary image.
            Typed loosely because OpenCV's own stubs declare a contour as either
            integer or floating point; narrowing it here would only add a cast.
        binary: The image the contour came from, used to measure interior ink.
        image_area_px: Area of that image, for the scale-invariant area ratio.

    Returns:
        The measured candidate, or ``None`` when the contour is degenerate
        (fewer than three points, or zero enclosed area) and therefore carries
        no usable shape information.
    """
    if len(contour) < _MIN_CONTOUR_POINTS:
        return None
    area = float(cv2.contourArea(contour))
    if area <= 0.0:
        return None

    x, y, width, height = cv2.boundingRect(contour)
    if width <= 0 or height <= 0:
        return None
    box = BoundingBox(x=float(x), y=float(y), width=float(width), height=float(height))

    center = _contour_centroid(contour, fallback=box.center)

    (_, _), (rect_width, rect_height), _ = cv2.minAreaRect(contour)
    longer = max(float(rect_width), float(rect_height))
    shorter = min(float(rect_width), float(rect_height))
    if shorter <= 0.0:
        return None
    aspect_ratio = longer / shorter
    rectangularity = area / (longer * shorter)

    hull_area = float(cv2.contourArea(cv2.convexHull(contour)))
    solidity = area / hull_area if hull_area > 0.0 else 0.0

    return MarkerCandidate(
        center=center,
        bounding_box=box,
        area_px=area,
        area_ratio=area / image_area_px if image_area_px > 0.0 else 0.0,
        aspect_ratio=aspect_ratio,
        rectangularity=rectangularity,
        solidity=solidity,
        fill_ratio=_interior_ink_ratio(contour, binary=binary, box=box),
        extent=area / box.area,
    )


def detect_marker_candidates(
    binary: NDArray[np.uint8], *, config: AlignmentConfig
) -> tuple[tuple[MarkerCandidate, ...], tuple[RejectedCandidate, ...]]:
    """Find and filter the marker-like contours in a binary image.

    Args:
        binary: Working-resolution binary image; ink is 255.
        config: Supplies the accepted size, shape and darkness ranges.

    Returns:
        ``(accepted, rejected)``. Rejected candidates name the property that
        failed, which is what makes a miscalibrated template diagnosable rather
        than merely broken.
    """
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    image_area = float(binary.shape[0] * binary.shape[1])

    accepted: list[MarkerCandidate] = []
    rejected: list[RejectedCandidate] = []
    min_area_ratio, max_area_ratio = config.marker_area_ratio_bounds
    min_aspect, max_aspect = config.marker_aspect_ratio_bounds
    detection = config.marker_detection

    for contour in contours:
        candidate = measure_candidate(contour, binary=binary, image_area_px=image_area)
        if candidate is None:
            continue
        reason = _rejection_reason(
            candidate,
            min_area_px=detection.min_marker_area_px,
            min_area_ratio=min_area_ratio,
            max_area_ratio=max_area_ratio,
            min_aspect=min_aspect,
            max_aspect=max_aspect,
            min_rectangularity=detection.min_rectangularity,
            min_solidity=detection.min_solidity,
            min_fill_ratio=detection.min_fill_ratio,
        )
        if reason is None:
            accepted.append(candidate)
        else:
            rejected.append(RejectedCandidate(candidate=candidate, reason=reason))

    return tuple(accepted), tuple(rejected)


def corner_search_region(
    corner: ImageCorner, *, image_width: int, image_height: int, config: AlignmentConfig
) -> BoundingBox:
    """Return the region of the scan in which ``corner``'s marker may be found.

    The regions are fractions of the image rather than fixed pixel boxes, so the
    same configuration serves a 150 dpi and a 300 dpi scan. They are generous by
    design: a rotated page grows its own bounding canvas, which pulls every
    marker away from the image corner it belongs to.
    """
    width = config.marker_detection.corner_search_width * image_width
    height = config.marker_detection.corner_search_height * image_height
    left = 0.0 if corner in (ImageCorner.TOP_LEFT, ImageCorner.BOTTOM_LEFT) else image_width - width
    top = 0.0 if corner in (ImageCorner.TOP_LEFT, ImageCorner.TOP_RIGHT) else image_height - height
    return BoundingBox(x=left, y=top, width=width, height=height)


def corner_reference_point(
    corner: ImageCorner, *, image_width: int, image_height: int
) -> Point:
    """Return the pixel corner of the image that ``corner`` names."""
    x = 0.0 if corner in (ImageCorner.TOP_LEFT, ImageCorner.BOTTOM_LEFT) else float(image_width)
    y = 0.0 if corner in (ImageCorner.TOP_LEFT, ImageCorner.TOP_RIGHT) else float(image_height)
    return Point(x=x, y=y)


def score_candidate(
    candidate: MarkerCandidate,
    *,
    corner: ImageCorner,
    image_width: int,
    image_height: int,
    config: AlignmentConfig,
) -> ScoredCandidate:
    """Score how well ``candidate`` serves as the marker for ``corner``.

    The score is the weighted mean of two interpretable halves:

    * **shape** - the mean of five component scores (area, aspect ratio,
      rectangularity, solidity, interior ink), each 1.0 at the expected value
      and 0.0 at the configured acceptance limit;
    * **proximity** - 1.0 at the image corner itself, falling linearly to 0.0 at
      the far side of the corner search region.

    Position is scored rather than merely filtered because the four largest dark
    shapes on a page are not reliably its markers: a printed logo, a barcode or a
    heavily filled answer block can all be larger.
    """
    detection = config.marker_detection
    min_area_ratio, max_area_ratio = config.marker_area_ratio_bounds
    min_aspect, max_aspect = config.marker_aspect_ratio_bounds

    breakdown = {
        "area": _band_score(
            candidate.area_ratio, config.expected_marker_area_ratio, min_area_ratio, max_area_ratio
        ),
        "aspect": _band_score(
            candidate.aspect_ratio,
            config.expected_marker_aspect_ratio,
            min_aspect,
            max_aspect,
        ),
        "rectangularity": _floor_score(candidate.rectangularity, detection.min_rectangularity),
        "solidity": _floor_score(candidate.solidity, detection.min_solidity),
        "fill": _floor_score(candidate.fill_ratio, detection.min_fill_ratio),
    }
    shape_score = sum(breakdown.values()) / len(breakdown)

    region = corner_search_region(
        corner, image_width=image_width, image_height=image_height, config=config
    )
    reference = corner_reference_point(
        corner, image_width=image_width, image_height=image_height
    )
    reach = math.hypot(region.width, region.height)
    distance = candidate.center.distance_to(reference)
    proximity_score = _clip(1.0 - distance / reach) if reach > 0.0 else 0.0

    weights = detection.shape_weight + detection.proximity_weight
    total = (
        detection.shape_weight * shape_score + detection.proximity_weight * proximity_score
    ) / weights

    breakdown["shape"] = shape_score
    breakdown["proximity"] = proximity_score
    return ScoredCandidate(
        candidate=candidate, corner=corner, score=total, breakdown=breakdown
    )


def select_corner_markers(
    candidates: Sequence[MarkerCandidate],
    *,
    image_width: int,
    image_height: int,
    config: AlignmentConfig,
) -> tuple[tuple[ScoredCandidate, ...], tuple[int, ...]]:
    """Choose one distinct marker for each corner of the scan.

    Args:
        candidates: Contours that passed the shape filters.
        image_width: Width of the image the candidates were measured in.
        image_height: Height of that image.
        config: Supplies the search regions, the acceptance score and the search
            breadth.

    Returns:
        ``(selected, alternative_counts)``, both in
        :data:`~omr_scanner.imaging.models.IMAGE_CORNER_ORDER`.
        ``alternative_counts`` records how many other candidates competed for
        each corner, which is what raises ``MULTIPLE_CORNER_CANDIDATES``.

    Raises:
        InsufficientMarkersError: A corner has no candidate at or above the
            acceptance score. Phase 1 fails here rather than extrapolating the
            missing corner: a silently invented corner produces a plausible but
            wrong rectification, and every answer read from it is wrong in a way
            nobody notices.
        AmbiguousMarkerError: Candidates exist for every corner, but no
            assignment uses four distinct contours.
    """
    ranked: list[list[tuple[int, ScoredCandidate]]] = []
    empty_corners: list[ImageCorner] = []

    for corner in IMAGE_CORNER_ORDER:
        region = corner_search_region(
            corner, image_width=image_width, image_height=image_height, config=config
        )
        scored = [
            (index, score_candidate(
                candidate,
                corner=corner,
                image_width=image_width,
                image_height=image_height,
                config=config,
            ))
            for index, candidate in enumerate(candidates)
            if _within(candidate.center, region)
        ]
        accepted = [
            item for item in scored if item[1].score >= config.marker_detection.min_candidate_score
        ]
        accepted.sort(key=lambda item: item[1].score, reverse=True)
        if not accepted:
            empty_corners.append(corner)
        ranked.append(accepted[: config.marker_detection.max_candidates_per_corner])

    if empty_corners:
        names = ", ".join(corner.value for corner in empty_corners)
        raise InsufficientMarkersError(
            f"No registration marker found for corner(s): {names}. "
            f"{len(candidates)} marker-like contour(s) were available.",
            user_message=(
                "This sheet could not be aligned: one or more corner markers were not "
                "found. The scan may be cropped, skewed or damaged at a corner."
            ),
        )

    best: tuple[float, tuple[tuple[int, ScoredCandidate], ...]] | None = None
    for combination in itertools.product(*ranked):
        indices = {index for index, _ in combination}
        if len(indices) != len(IMAGE_CORNER_ORDER):
            continue
        total = sum(scored.score for _, scored in combination)
        if best is None or total > best[0]:
            best = (total, combination)

    if best is None:
        raise AmbiguousMarkerError(
            "Every corner has candidates, but no assignment uses four distinct contours; "
            "the same contour is the only option for more than one corner.",
            user_message=(
                "This sheet could not be aligned: the corner markers could not be told "
                "apart."
            ),
        )

    selected = tuple(scored for _, scored in best[1])
    alternatives = tuple(max(0, len(corner_ranked) - 1) for corner_ranked in ranked)
    return selected, alternatives


def _contour_centroid(contour: NDArray[Any], *, fallback: Point) -> Point:
    """Return the contour's centroid from image moments.

    The centroid is used rather than the bounding-rectangle centre because it is
    an area-weighted average: a nick or a blot on one edge moves it in
    proportion to the area affected, whereas a bounding rectangle is defined by
    its extreme pixels and jumps by the full amount of a single stray one.
    """
    moments = cv2.moments(contour)
    m00 = float(moments["m00"])
    if m00 == 0.0:
        return fallback
    return Point(x=float(moments["m10"]) / m00, y=float(moments["m01"]) / m00)


def _interior_ink_ratio(
    contour: NDArray[Any], *, binary: NDArray[np.uint8], box: BoundingBox
) -> float:
    """Return the fraction of the contour's interior that is ink.

    Measured on a filled mask of the contour rather than on its bounding box, so
    the value does not fall simply because the marker is rotated.
    """
    left, top = int(box.x), int(box.y)
    right, bottom = left + int(box.width), top + int(box.height)
    crop = binary[top:bottom, left:right]
    if crop.size == 0:
        return 0.0
    mask = np.zeros(crop.shape, dtype=np.uint8)
    shifted = contour - np.array([[left, top]], dtype=contour.dtype)
    cv2.drawContours(mask, [shifted], -1, 255, thickness=cv2.FILLED)
    interior = int(cv2.countNonZero(mask))
    if interior == 0:
        return 0.0
    ink = int(cv2.countNonZero(cv2.bitwise_and(crop, mask)))
    return ink / interior


def _rejection_reason(
    candidate: MarkerCandidate,
    *,
    min_area_px: float,
    min_area_ratio: float,
    max_area_ratio: float,
    min_aspect: float,
    max_aspect: float,
    min_rectangularity: float,
    min_solidity: float,
    min_fill_ratio: float,
) -> str | None:
    """Return the name of the first failing shape filter, or ``None`` if all pass."""
    if candidate.area_px < min_area_px:
        return "area_px_below_minimum"
    if candidate.area_ratio < min_area_ratio:
        return "area_ratio_too_small"
    if candidate.area_ratio > max_area_ratio:
        return "area_ratio_too_large"
    if candidate.aspect_ratio < min_aspect:
        return "aspect_ratio_too_small"
    if candidate.aspect_ratio > max_aspect:
        return "aspect_ratio_too_large"
    if candidate.rectangularity < min_rectangularity:
        return "rectangularity_too_low"
    if candidate.solidity < min_solidity:
        return "solidity_too_low"
    if candidate.fill_ratio < min_fill_ratio:
        return "fill_ratio_too_low"
    return None


def _band_score(value: float, expected: float, minimum: float, maximum: float) -> float:
    """Score a value that has an expected magnitude and a two-sided limit.

    Scored on a logarithmic scale, because the limits are multiplicative: being
    twice too large and half too large are equally wrong, and a linear scale
    would treat the first as far worse.
    """
    if value <= 0.0 or expected <= 0.0 or minimum <= 0.0 or maximum <= 0.0:
        return 0.0
    if value < minimum or value > maximum:
        return 0.0
    reach = max(math.log(expected / minimum), math.log(maximum / expected))
    if reach <= 0.0:
        return 1.0
    return _clip(1.0 - abs(math.log(value / expected)) / reach)


def _floor_score(value: float, minimum: float) -> float:
    """Score a value whose ideal is 1.0 and whose acceptance limit is ``minimum``."""
    if minimum >= 1.0:
        return 1.0 if value >= minimum else 0.0
    return _clip((value - minimum) / (1.0 - minimum))


def _clip(value: float) -> float:
    """Clamp ``value`` into ``[0, 1]``."""
    return min(1.0, max(0.0, value))


def _within(point: Point, box: BoundingBox) -> bool:
    """Return whether ``point`` lies inside ``box``, edges included."""
    return box.x <= point.x <= box.right and box.y <= point.y <= box.bottom

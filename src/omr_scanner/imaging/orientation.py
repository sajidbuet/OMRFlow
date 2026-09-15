"""Deciding which corner of a scan is the sheet's top-left.

Purpose:
    Resolve the rotational ambiguity that four corner markers cannot. A
    rectangle of four identical squares looks the same after a half turn, and on
    a near-square page after a quarter turn too, so the corner pattern alone
    cannot tell an upright sheet from an inverted one.

Responsibilities:
    * Evaluate the four possible assignments of scan corners to canonical roles.
    * Measure, for each, whether the orientation mark appears where the template
      says it should.
    * Return the winning assignment with its confidence and its margin over the
      runner-up, or fail explicitly.

What does NOT belong here:
    * Registration-marker detection, and the final warp.

Strategy, and why this one:
    Each of the four hypotheses is turned into a candidate homography onto the
    canonical page, and the *expected* location of the orientation mark is
    rectified out of the scan through it. The hypothesis under which that window
    actually contains ink is the right one.

    This tests the template's own statement about the sheet, so it needs no
    separate assumption about where a mark might be, and it works for a page
    rotated by a quarter turn and 3 degrees of skew just as well as for one
    rotated by exactly 180 degrees - the homography absorbs everything else.

    Before the mark is consulted, hypotheses whose quadrilateral is not a
    plausible page are discarded. On A4 that alone eliminates the two quarter
    turns, because a portrait marker rectangle read as landscape has an aspect
    ratio nowhere near the template's. It is a free, purely geometric
    constraint; the mark then only has to separate upright from inverted.

Failure:
    When no hypothesis produces a confident, unambiguous reading, the sheet is
    rejected with :class:`~omr_scanner.errors.OrientationDetectionError`
    (code ``ORIENTATION_NOT_FOUND``). Guessing is only possible when
    ``OrientationConfig.allow_fallback`` is explicitly enabled, and is then
    reported through the ``ORIENTATION_ASSUMED`` warning - never silently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np

from omr_scanner.errors import ImagingError, OrientationDetectionError
from omr_scanner.imaging.geometry import (
    perspective_transform,
    rotate_corner_order,
    validate_page_quadrilateral,
)
from omr_scanner.imaging.models import BoundingBox, OrientationHypothesis, OrientationResult, Point

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from omr_scanner.imaging.config import AlignmentConfig

QUARTER_TURNS = (0, 1, 2, 3)
"""The four possible assignments of scan corners to canonical corner roles."""


def orientation_windows(config: AlignmentConfig) -> tuple[BoundingBox, BoundingBox]:
    """Return the canonical evidence window and the wider localisation window.

    The two are deliberately different sizes. The *evidence* window is the
    printed mark scaled by ``window_margin``: it must be tight, because a large
    window dilutes the mark's ink with paper and makes every hypothesis look
    equally weak. The *localisation* window spans ``search_radius`` and is only
    used, after the decision is made, to report where the mark actually is.
    """
    orientation = config.orientation
    center_x = orientation.marker_center_x * config.canonical_width
    center_y = orientation.marker_center_y * config.canonical_height
    mark_width = orientation.marker_width * config.canonical_width
    mark_height = orientation.marker_height * config.canonical_height

    evidence_width = mark_width * orientation.window_margin
    evidence_height = mark_height * orientation.window_margin
    evidence = BoundingBox(
        x=center_x - evidence_width / 2.0,
        y=center_y - evidence_height / 2.0,
        width=evidence_width,
        height=evidence_height,
    )

    search_width = orientation.search_radius * config.canonical_width * 2.0
    search_height = orientation.search_radius * config.canonical_height * 2.0
    localisation = BoundingBox(
        x=center_x - search_width / 2.0,
        y=center_y - search_height / 2.0,
        width=search_width,
        height=search_height,
    )
    return evidence, localisation


def determine_orientation(
    binary: NDArray[np.uint8],
    *,
    image_corner_points: Sequence[Point],
    config: AlignmentConfig,
    to_source_factor: float = 1.0,
) -> OrientationResult:
    """Decide how the page was oriented in the scan.

    Args:
        binary: Working-resolution binary image; ink is 255.
        image_corner_points: The four detected marker centres in clockwise
            image-corner order, in the same coordinate frame as ``binary``.
        config: Canonical geometry, orientation expectations and thresholds.
        to_source_factor: Factor converting working-resolution coordinates back
            to source-image pixels, applied to every reported position.

    Returns:
        The winning orientation with its confidence, margin and located mark.

    Raises:
        OrientationDetectionError: No hypothesis reached ``min_confidence`` with
            at least ``min_margin`` over the runner-up, and the fallback is
            disabled.
    """
    evidence_window, localisation_window = orientation_windows(config)
    canonical_targets = config.canonical_marker_points()
    image_area = float(binary.shape[0] * binary.shape[1])
    expected_fill = config.orientation.expected_window_fill

    hypotheses: list[OrientationHypothesis] = []
    matrices: dict[int, NDArray[np.float64]] = {}

    for turns in QUARTER_TURNS:
        ordered = rotate_corner_order(image_corner_points, turns)
        geometry_valid = _geometry_is_plausible(
            ordered, image_area_px=image_area, config=config
        )
        try:
            matrix = perspective_transform(ordered, canonical_targets)
        except ImagingError:
            hypotheses.append(
                OrientationHypothesis(
                    quarter_turns=turns,
                    geometry_valid=False,
                    confidence=0.0,
                    sample_window=evidence_window,
                )
            )
            continue
        matrices[turns] = matrix
        fill = _window_ink_ratio(binary, matrix=matrix, window=evidence_window)
        confidence = min(1.0, fill / expected_fill) if expected_fill > 0.0 else 0.0
        hypotheses.append(
            OrientationHypothesis(
                quarter_turns=turns,
                geometry_valid=geometry_valid,
                confidence=confidence,
                sample_window=_window_in_source(
                    matrix, window=evidence_window, to_source_factor=to_source_factor
                ),
            )
        )

    considered = [item for item in hypotheses if item.geometry_valid] or list(hypotheses)
    considered.sort(key=lambda item: item.confidence, reverse=True)
    best = considered[0]
    runner_up = considered[1].confidence if len(considered) > 1 else 0.0
    margin = best.confidence - runner_up

    orientation = config.orientation
    if best.confidence < orientation.min_confidence or margin < orientation.min_margin:
        if not orientation.allow_fallback:
            raise OrientationDetectionError(
                f"Orientation mark not identified: best confidence {best.confidence:.2f} "
                f"(minimum {orientation.min_confidence:.2f}) with margin {margin:.2f} "
                f"(minimum {orientation.min_margin:.2f}) over the next hypothesis. "
                f"Confidences by quarter turn: "
                + ", ".join(f"{item.quarter_turns}:{item.confidence:.2f}" for item in hypotheses),
                user_message=(
                    "This sheet could not be aligned: the orientation mark that tells "
                    "OMRFlow which way up the page is was not found."
                ),
            )
        return OrientationResult(
            quarter_turns=orientation.fallback_quarter_turns,
            confidence=best.confidence,
            margin=margin,
            marker_center=None,
            marker_box=None,
            assumed=True,
            hypotheses=tuple(hypotheses),
        )

    center, box = _locate_mark(
        binary,
        matrix=matrices[best.quarter_turns],
        window=localisation_window,
        to_source_factor=to_source_factor,
    )
    return OrientationResult(
        quarter_turns=best.quarter_turns,
        confidence=best.confidence,
        margin=margin,
        marker_center=center,
        marker_box=box,
        assumed=False,
        hypotheses=tuple(hypotheses),
    )


def _geometry_is_plausible(
    ordered: Sequence[Point], *, image_area_px: float, config: AlignmentConfig
) -> bool:
    """Return whether an ordered quadrilateral could be this template's page."""
    try:
        validate_page_quadrilateral(
            ordered,
            image_area_px=image_area_px,
            expected_aspect_ratio=config.expected_quadrilateral_aspect_ratio,
            config=config.geometry,
        )
    except ImagingError:
        return False
    return True


def _window_matrix(
    matrix: NDArray[np.float64], *, window: BoundingBox
) -> NDArray[np.float64]:
    """Compose ``matrix`` with the translation that puts ``window`` at the origin."""
    translation = np.array(
        [[1.0, 0.0, -window.x], [0.0, 1.0, -window.y], [0.0, 0.0, 1.0]], dtype=np.float64
    )
    return translation @ matrix


def _warp_window(
    binary: NDArray[np.uint8], *, matrix: NDArray[np.float64], window: BoundingBox
) -> NDArray[np.uint8]:
    """Rectify just ``window`` of the canonical page out of ``binary``.

    Only the window is warped, not the whole page: four small patches per sheet
    cost a fraction of four full rectifications, and orientation is decided
    before the real warp is ever computed.
    """
    size = (max(1, round(window.width)), max(1, round(window.height)))
    patch = cv2.warpPerspective(
        binary,
        _window_matrix(matrix, window=window),
        size,
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return np.asarray(patch, dtype=np.uint8)


def _window_ink_ratio(
    binary: NDArray[np.uint8], *, matrix: NDArray[np.float64], window: BoundingBox
) -> float:
    """Return the fraction of the rectified window that is ink."""
    patch = _warp_window(binary, matrix=matrix, window=window)
    if patch.size == 0:
        return 0.0
    return float(cv2.countNonZero(patch)) / float(patch.size)


def _window_in_source(
    matrix: NDArray[np.float64], *, window: BoundingBox, to_source_factor: float
) -> BoundingBox:
    """Return the source-image bounding box of a canonical window.

    Reported for diagnostics: it shows where on the original scan the engine
    looked for the mark.
    """
    inverse = np.linalg.inv(matrix)
    corners = np.array(
        [
            [window.x, window.y, 1.0],
            [window.right, window.y, 1.0],
            [window.right, window.bottom, 1.0],
            [window.x, window.bottom, 1.0],
        ],
        dtype=np.float64,
    )
    mapped = corners @ inverse.T
    weights = mapped[:, 2]
    if np.any(weights == 0.0):
        return window
    xs = mapped[:, 0] / weights * to_source_factor
    ys = mapped[:, 1] / weights * to_source_factor
    return BoundingBox(
        x=float(xs.min()),
        y=float(ys.min()),
        width=float(xs.max() - xs.min()),
        height=float(ys.max() - ys.min()),
    )


def _locate_mark(
    binary: NDArray[np.uint8],
    *,
    matrix: NDArray[np.float64],
    window: BoundingBox,
    to_source_factor: float,
) -> tuple[Point | None, BoundingBox | None]:
    """Find the orientation mark inside the localisation window.

    Returns the mark's centroid and bounding box in source-image pixels, or
    ``(None, None)`` when the wider window holds no contour - which can happen
    when the mark sits at the very edge of the search radius even though the
    evidence window saw enough ink to decide.
    """
    patch = _warp_window(binary, matrix=matrix, window=window)
    contours, _ = cv2.findContours(patch, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, None
    largest = max(contours, key=cv2.contourArea)
    if float(cv2.contourArea(largest)) <= 0.0:
        return None, None

    moments = cv2.moments(largest)
    m00 = float(moments["m00"])
    if m00 == 0.0:
        return None, None
    canonical_center = Point(
        x=window.x + float(moments["m10"]) / m00, y=window.y + float(moments["m01"]) / m00
    )
    x, y, width, height = cv2.boundingRect(largest)
    canonical_box = BoundingBox(
        x=window.x + float(x), y=window.y + float(y), width=float(width), height=float(height)
    )

    inverse = np.linalg.inv(matrix)
    homogeneous = np.array(
        [canonical_center.x, canonical_center.y, 1.0], dtype=np.float64
    ) @ inverse.T
    if homogeneous[2] == 0.0:
        return None, None
    source_center = Point(
        x=float(homogeneous[0] / homogeneous[2]) * to_source_factor,
        y=float(homogeneous[1] / homogeneous[2]) * to_source_factor,
    )
    source_box = _window_in_source(
        matrix, window=canonical_box, to_source_factor=to_source_factor
    )
    return source_center, source_box

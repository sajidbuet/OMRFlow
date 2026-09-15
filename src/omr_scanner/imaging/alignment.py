"""The public entry point of the geometric normalisation engine.

Purpose:
    Turn one arbitrary scan of an OMR sheet into the canonical page the template
    describes, or fail with a reason.

Responsibilities:
    * Orchestrate the pipeline: validate, preprocess, detect candidates, select
      corner markers, resolve orientation, validate the page quadrilateral,
      compute the homography, warp, and measure the result.
    * Collect the non-fatal observations that become
      :class:`~omr_scanner.imaging.models.AlignmentWarning` values.

What does NOT belong here:
    * The algorithms themselves. Each stage lives in its own module so that it
      can be tested in isolation; this module only sequences them.
    * File or project knowledge. The engine takes an array and a configuration.
      Loading a scan and building a configuration from a template is
      :mod:`omr_scanner.services.alignment_service`.

Pipeline::

    raw scan
      -> validate_image                 shape, dtype, channels, minimum size
      -> prepare_for_detection          grayscale, downscale, denoise, threshold
      -> detect_marker_candidates       measure and filter every dark contour
      -> select_corner_markers          one distinct marker per scan corner
      -> determine_orientation          which scan corner is the canonical TL
      -> rotate_corner_order            re-anchor to canonical corner order
      -> validate_page_quadrilateral    convex, sized, proportioned
      -> perspective_transform          marker centres -> canonical targets
      -> warpPerspective                rectify at full source resolution
      -> AlignmentResult                image, transforms, metrics, warnings

Invariants:
    * The supplied array is never modified.
    * On success, ``normalized_image`` has exactly the configured canonical size
      and the same channel layout as the input.
    * Every expected failure raises an
      :class:`~omr_scanner.errors.ImagingError` subclass carrying a stable
      ``code``; nothing returns ``None`` to mean "it did not work".
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import cv2
import numpy as np

from omr_scanner.imaging.config import AlignmentConfig
from omr_scanner.imaging.geometry import (
    invert_transform,
    pairwise_distances,
    perspective_transform,
    polygon_area,
    quadrilateral_aspect_ratio,
    reprojection_errors,
    validate_page_quadrilateral,
)
from omr_scanner.imaging.marker_detection import (
    detect_marker_candidates,
    score_candidate,
    select_corner_markers,
)
from omr_scanner.imaging.models import (
    CANONICAL_CORNER_ORDER,
    IMAGE_CORNER_ORDER,
    AlignmentDiagnostics,
    AlignmentMetrics,
    AlignmentResult,
    AlignmentWarning,
    MarkerCandidate,
    Point,
    RegistrationMarkerDetection,
    RejectedCandidate,
    ScoredCandidate,
)
from omr_scanner.imaging.orientation import determine_orientation
from omr_scanner.imaging.preprocessing import prepare_for_detection, validate_image

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from omr_scanner.imaging.preprocessing import PreparedImage

_LOGGER = logging.getLogger(__name__)

_PAPER_VALUE = 255
"""Border fill for areas of the canonical page the scan does not cover.

White rather than black, so that a slightly cropped scan produces blank paper
where data is missing instead of a black band that later reads as heavy ink.
"""


def align_sheet(
    image: NDArray[np.uint8], *, config: AlignmentConfig | None = None
) -> AlignmentResult:
    """Normalise one raw OMR scan into canonical template coordinates.

    Args:
        image: The scan, as an 8-bit grayscale, BGR or BGRA array. It is read
            only; the caller's array is never modified.
        config: Geometry and thresholds. Defaults to
            :class:`~omr_scanner.imaging.config.AlignmentConfig`'s own defaults,
            which describe the example sheet shipped in ``resources/templates``.

    Returns:
        The rectified page together with the transforms, the measured quality
        and any non-fatal warnings.

    Raises:
        ImageValidationError: ``image`` is not a usable 8-bit image.
        InsufficientMarkersError: A corner produced no acceptable marker.
        AmbiguousMarkerError: No one-to-one assignment of candidates to corners
            exists.
        OrientationDetectionError: The orientation mark was not identified and
            the fallback is disabled.
        InvalidPageGeometryError: The selected markers cannot bound a page.
        AlignmentTransformError: The homography could not be computed.
    """
    started = time.perf_counter()
    active = config if config is not None else AlignmentConfig()

    validated = validate_image(image, config=active.preprocessing)
    prepared = prepare_for_detection(validated, config=active.preprocessing)

    candidates, rejected = detect_marker_candidates(prepared.binary, config=active)
    selected, alternatives = select_corner_markers(
        candidates,
        image_width=prepared.working_width,
        image_height=prepared.working_height,
        config=active,
    )

    working_points = tuple(item.candidate.center for item in selected)
    orientation = determine_orientation(
        prepared.binary,
        image_corner_points=working_points,
        config=active,
        to_source_factor=prepared.to_source_factor,
    )

    factor = prepared.to_source_factor
    source_selected = tuple(_scaled_selection(item, factor) for item in selected)
    ordered_selected = rotate_to_canonical_order(source_selected, orientation.quarter_turns)
    ordered_alternatives = rotate_to_canonical_order(alternatives, orientation.quarter_turns)
    ordered_points = tuple(item.candidate.center for item in ordered_selected)

    source_area = float(prepared.source_width * prepared.source_height)
    aspect_deviation = validate_page_quadrilateral(
        ordered_points,
        image_area_px=source_area,
        expected_aspect_ratio=active.expected_quadrilateral_aspect_ratio,
        config=active.geometry,
    )

    targets = active.canonical_marker_points()
    transform = perspective_transform(ordered_points, targets)
    inverse = invert_transform(transform)
    normalized = _warp(validated, transform=transform, config=active)

    markers = tuple(
        RegistrationMarkerDetection(
            role=role,
            candidate=item.candidate,
            score=item.score,
            breakdown=item.breakdown,
            alternative_count=alternative_count,
        )
        for role, item, alternative_count in zip(
            CANONICAL_CORNER_ORDER, ordered_selected, ordered_alternatives, strict=True
        )
    )

    errors = reprojection_errors(transform, ordered_points, targets)
    metrics = AlignmentMetrics(
        candidate_count=len(candidates),
        rejected_count=len(rejected),
        marker_scores={marker.role: marker.score for marker in markers},
        min_marker_score=min(marker.score for marker in markers),
        quadrilateral_area_ratio=polygon_area(ordered_points) / source_area,
        source_aspect_ratio=quadrilateral_aspect_ratio(ordered_points),
        expected_aspect_ratio=active.expected_quadrilateral_aspect_ratio,
        aspect_ratio_deviation=aspect_deviation,
        min_marker_separation_px=min(pairwise_distances(ordered_points)),
        orientation_confidence=orientation.confidence,
        orientation_margin=orientation.margin,
        mean_reprojection_error_px=float(np.mean(errors)),
        max_reprojection_error_px=float(np.max(errors)),
        working_scale=prepared.scale,
        elapsed_seconds=time.perf_counter() - started,
    )

    warnings = _collect_warnings(
        markers=markers,
        orientation_confidence=orientation.confidence,
        orientation_assumed=orientation.assumed,
        aspect_deviation=aspect_deviation,
        max_reprojection_error_px=metrics.max_reprojection_error_px,
        source_width=prepared.source_width,
        source_height=prepared.source_height,
        config=active,
    )

    diagnostics = (
        AlignmentDiagnostics(
            working_scale=prepared.scale,
            grayscale=prepared.grayscale,
            binary=prepared.binary,
            candidates=tuple(candidate.scaled(factor) for candidate in candidates),
            rejected=tuple(
                RejectedCandidate(candidate=item.candidate.scaled(factor), reason=item.reason)
                for item in rejected
            ),
            scored=_score_every_candidate(candidates, prepared=prepared, config=active),
            source_quadrilateral=ordered_points,
        )
        if active.diagnostics
        else None
    )

    _LOGGER.debug(
        "Aligned a %dx%d scan in %.3f s: %d candidates, quarter turns %d, "
        "orientation confidence %.2f, minimum marker score %.2f",
        prepared.source_width,
        prepared.source_height,
        metrics.elapsed_seconds,
        metrics.candidate_count,
        orientation.quarter_turns,
        orientation.confidence,
        metrics.min_marker_score,
    )

    return AlignmentResult(
        normalized_image=normalized,
        original_width=prepared.source_width,
        original_height=prepared.source_height,
        canonical_width=active.canonical_width,
        canonical_height=active.canonical_height,
        corner_markers=markers,
        orientation=orientation,
        transform_matrix=transform,
        inverse_transform_matrix=inverse,
        metrics=metrics,
        warnings=warnings,
        diagnostics=diagnostics,
    )


def rotate_to_canonical_order[T](values: Sequence[T], quarter_turns: int) -> tuple[T, ...]:
    """Re-anchor a clockwise four-element sequence by ``quarter_turns`` steps.

    The sequence arrives in scan-corner order; ``quarter_turns`` says which of
    those corners is the canonical top-left, so rotating by it produces
    :data:`~omr_scanner.imaging.models.CANONICAL_CORNER_ORDER`. The point-level
    equivalent is :func:`omr_scanner.imaging.geometry.rotate_corner_order`; this
    generic form keeps scores and counts attached to their markers.

    Raises:
        ValueError: The sequence is not four elements, or ``quarter_turns`` is
            not 0, 1, 2 or 3.
    """
    if len(values) != len(CANONICAL_CORNER_ORDER):
        raise ValueError(f"Expected four elements, received {len(values)}")
    if quarter_turns not in (0, 1, 2, 3):
        raise ValueError("quarter_turns must be 0, 1, 2 or 3")
    return tuple(values[(index + quarter_turns) % 4] for index in range(4))


def canonical_target_for(
    config: AlignmentConfig, normalized_x: float, normalized_y: float
) -> Point:
    """Convert a normalised page coordinate into canonical pixels.

    A thin convenience over
    :meth:`~omr_scanner.imaging.config.AlignmentConfig.normalized_to_canonical`
    for callers that hold plain floats rather than a
    :class:`~omr_scanner.domain.geometry.NormalizedPoint`.
    """
    return Point(x=normalized_x * config.canonical_width, y=normalized_y * config.canonical_height)


def _scaled_selection(item: ScoredCandidate, factor: float) -> ScoredCandidate:
    """Return ``item`` with its pixel measurements mapped to source resolution."""
    if factor == 1.0:
        return item
    return ScoredCandidate(
        candidate=item.candidate.scaled(factor),
        corner=item.corner,
        score=item.score,
        breakdown=item.breakdown,
    )


def _score_every_candidate(
    candidates: Sequence[MarkerCandidate], *, prepared: PreparedImage, config: AlignmentConfig
) -> tuple[ScoredCandidate, ...]:
    """Score every accepted candidate against every scan corner, for diagnostics.

    Only computed when diagnostics are enabled: it answers "why was *that* shape
    chosen and not this one", which is the question a miscalibrated template
    raises.
    """
    factor = prepared.to_source_factor
    return tuple(
        _scaled_selection(
            score_candidate(
                candidate,
                corner=corner,
                image_width=prepared.working_width,
                image_height=prepared.working_height,
                config=config,
            ),
            factor,
        )
        for corner in IMAGE_CORNER_ORDER
        for candidate in candidates
    )


def _warp(
    image: NDArray[np.uint8], *, transform: NDArray[np.float64], config: AlignmentConfig
) -> NDArray[np.uint8]:
    """Rectify the full-resolution scan onto the canonical page."""
    border = (_PAPER_VALUE, _PAPER_VALUE, _PAPER_VALUE, _PAPER_VALUE)
    warped = cv2.warpPerspective(
        image,
        transform,
        config.canonical_size,
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border,
    )
    return np.asarray(warped, dtype=np.uint8)


def _collect_warnings(
    *,
    markers: Sequence[RegistrationMarkerDetection],
    orientation_confidence: float,
    orientation_assumed: bool,
    aspect_deviation: float,
    max_reprojection_error_px: float,
    source_width: int,
    source_height: int,
    config: AlignmentConfig,
) -> tuple[AlignmentWarning, ...]:
    """Collect the non-fatal observations about an otherwise successful alignment."""
    detection = config.marker_detection
    warnings: list[AlignmentWarning] = []

    score_floor = detection.min_candidate_score * detection.low_score_warning_ratio
    if any(marker.score < score_floor for marker in markers):
        warnings.append(AlignmentWarning.LOW_MARKER_SCORE)
    if any(
        marker.candidate.rectangularity < detection.low_rectangularity_warning
        for marker in markers
    ):
        warnings.append(AlignmentWarning.LOW_MARKER_RECTANGULARITY)
    if any(marker.alternative_count > 0 for marker in markers):
        warnings.append(AlignmentWarning.MULTIPLE_CORNER_CANDIDATES)
    if any(
        _touches_border(marker.candidate, source_width, source_height, detection.edge_margin_ratio)
        for marker in markers
    ):
        warnings.append(AlignmentWarning.MARKER_NEAR_IMAGE_EDGE)

    if orientation_assumed:
        warnings.append(AlignmentWarning.ORIENTATION_ASSUMED)
    elif orientation_confidence < config.orientation.low_confidence_warning:
        warnings.append(AlignmentWarning.LOW_ORIENTATION_CONFIDENCE)

    if abs(aspect_deviation) > config.geometry.aspect_ratio_warning_deviation:
        warnings.append(AlignmentWarning.ASPECT_RATIO_DEVIATION)
    if max_reprojection_error_px > config.geometry.max_reprojection_error_px:
        warnings.append(AlignmentWarning.LARGE_REPROJECTION_ERROR)

    return tuple(warnings)


def _touches_border(
    candidate: MarkerCandidate, width: int, height: int, margin_ratio: float
) -> bool:
    """Return whether a marker sits within ``margin_ratio`` of the image border."""
    margin = margin_ratio * min(width, height)
    box = candidate.bounding_box
    return (
        box.x <= margin
        or box.y <= margin
        or box.right >= width - margin
        or box.bottom >= height - margin
    )

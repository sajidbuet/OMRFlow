"""The seam between the template designer GUI and Phase 1's pixel algorithms.

Purpose:
    Let the template designer show a reference image and locate its four
    registration markers *without* the GUI layer ever importing ``cv2`` or
    ``numpy`` - both are forbidden imports for ``omr_scanner.gui`` per
    ``docs/ARCHITECTURE.md`` and ``tests/unit/test_architecture.py``.

Responsibilities:
    * Decode an image file into plain bytes a `QImage` can be built from
      directly, with no array type crossing the layer boundary.
    * Run marker *candidate detection* (:mod:`omr_scanner.imaging.marker_detection`)
      against a raw reference image and report one best candidate per corner,
      independently - never raising when a corner is missing, because the
      designer always has a human present to place that corner by hand.

What does NOT belong here:
    * Perspective correction or orientation resolution. The designer works
      directly in the reference image's own pixel coordinates; there is no
      "canonical page" to rectify onto yet; the sheet *is* the reference.
    * Anything about `.omrt` documents; that is `omr_scanner.domain` and
      `omr_scanner.services.template_service`.

Why detection here differs from Phase 1's `align_sheet`:
    :func:`omr_scanner.imaging.marker_detection.select_corner_markers` demands
    a mathematically exact one-to-one assignment of candidates to all four
    corners, because Phase 1 has no human to ask when a corner is ambiguous and
    must fail rather than guess. A designer session is the opposite case: a
    person is looking at the canvas and will confirm, drag or discard whatever
    is suggested. So this module scores each corner independently with the same
    :func:`~omr_scanner.imaging.marker_detection.score_candidate` Phase 1 uses,
    and reports a per-corner "found" or "not found" outcome rather than an
    all-or-nothing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from omr_scanner.errors import ImagingError
from omr_scanner.imaging.config import (
    AlignmentConfig,
    MarkerDetectionConfig,
    PreprocessingConfig,
)
from omr_scanner.imaging.marker_detection import (
    corner_search_region,
    detect_marker_candidates,
    score_candidate,
)
from omr_scanner.imaging.models import IMAGE_CORNER_ORDER, BoundingBox
from omr_scanner.imaging.orientation_marker import (
    OrientationMarkerConfig,
    detect_orientation_marker,
)
from omr_scanner.imaging.preprocessing import prepare_for_detection
from omr_scanner.services.alignment_service import load_scan_image

DEFAULT_EXPECTED_MARKER_WIDTH = 0.03
"""Printed marker width as a fraction of the reference image width."""

DEFAULT_EXPECTED_MARKER_HEIGHT = 0.021
"""Printed marker height as a fraction of the reference image height."""

DEFAULT_CORNER_SEARCH_FRACTION = 0.32
"""Default corner search region size, as a fraction of the image, each axis."""

DEFAULT_MIN_CANDIDATE_SCORE = 0.45
"""Default acceptance floor for a corner candidate; matches Phase 1's default."""

ORIENTATION_DEBUG_IMAGE_NAME = "orientation_detection_latest.png"
"""File name of the orientation detector's diagnostic overlay, written inside
whatever directory the caller passes as ``debug_dir``. A fixed name, overwritten
each run: the useful artefact is "what did the last attempt see", and a growing
pile of timestamped overlays is a directory nobody reads."""


@dataclass(frozen=True, slots=True)
class DecodedImage:
    """An image decoded to plain bytes, ready for `QImage` construction.

    Attributes:
        width: Image width in pixels.
        height: Image height in pixels.
        channels: 1 (grayscale) or 3 (BGR - matches Qt's ``Format_BGR888``, so
            the GUI never needs to swap channel order).
        stride: Bytes per row (``width * channels`` for a contiguous buffer).
        data: Raw pixel bytes, row-major, top-to-bottom.
    """

    width: int
    height: int
    channels: int
    stride: int
    data: bytes


@dataclass(frozen=True, slots=True)
class MarkerSearchConfig:
    """Plain-value tuning for :func:`detect_registration_markers`.

    A deliberately small subset of `imaging.config.MarkerDetectionConfig` -
    the fields a designer user might reasonably need to widen (the sheet's
    markers are larger/smaller than the default, or sit further from the
    corner than usual) - expressed without any dependency on the imaging
    layer's types, so this dataclass can be constructed and passed around
    inside `gui` freely.

    Attributes:
        expected_marker_width: Printed marker width as a fraction of the image.
        expected_marker_height: Printed marker height as a fraction of the image.
        corner_search_fraction: Corner search region size, as a fraction of the
            image, applied to both width and height.
        min_candidate_score: Acceptance floor in ``[0, 1]``.
    """

    expected_marker_width: float = DEFAULT_EXPECTED_MARKER_WIDTH
    expected_marker_height: float = DEFAULT_EXPECTED_MARKER_HEIGHT
    corner_search_fraction: float = DEFAULT_CORNER_SEARCH_FRACTION
    min_candidate_score: float = DEFAULT_MIN_CANDIDATE_SCORE


@dataclass(frozen=True, slots=True)
class DetectedMarker:
    """One corner's detection outcome.

    Attributes:
        corner: Which corner of the reference image this is, as the string
            value of :class:`~omr_scanner.imaging.models.ImageCorner`
            (``"top_left"`` etc) - deliberately a plain string so the GUI layer
            never has to import the imaging enum.
        found: Whether an acceptable candidate was located.
        x, y: Centre of the candidate, in reference-image pixels. ``0.0`` when
            not found.
        width, height: Bounding box size, in reference-image pixels.
        score: Combined score in ``[0, 1]``. ``0.0`` when not found.
        reason: Empty when found; otherwise a short, user-facing reason such as
            ``"No candidate scored above the acceptance threshold"``.
    """

    corner: str
    found: bool
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    score: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class OrientationSearchConfig:
    """Plain-value tuning for :func:`detect_orientation_marker_in_region`.

    The subset of
    :class:`~omr_scanner.imaging.orientation_marker.OrientationMarkerConfig` a
    designer user might reasonably need - the shape of the mark their sheet
    actually prints - expressed without any dependency on the imaging layer's
    types, so this dataclass can be constructed inside ``gui`` freely (the same
    reason :class:`MarkerSearchConfig` exists).

    Attributes:
        expected_aspect_ratio: The mark's long side divided by its short side.
            ``2.0`` describes the conventional dash.
        min_aspect_ratio: Narrowest accepted ratio; ``1.2`` still admits a nearly
            square mark.
        max_aspect_ratio: Widest accepted ratio.
        min_score: Acceptance floor in ``[0, 1]``.
    """

    expected_aspect_ratio: float = 2.0
    min_aspect_ratio: float = 1.2
    max_aspect_ratio: float = 6.0
    min_score: float = 0.45


@dataclass(frozen=True, slots=True)
class OrientationDetectionOutcome:
    """The result of one orientation-mark search inside a user-drawn rectangle.

    Attributes:
        found: Whether an acceptable mark was located.
        x, y: Top-left of the mark's bounding box, in reference-image pixels.
            ``0.0`` when not found.
        width, height: Bounding box size, in reference-image pixels.
        center_x, center_y: The mark's centroid, in reference-image pixels - what
            the template's
            :attr:`~omr_scanner.domain.template.OrientationMarker.center` stores,
            supplied separately because a centroid is not the centre of a
            bounding box for anything but a perfectly symmetric shape.
        score: Combined score in ``[0, 1]`` - of the accepted mark, or of the best
            rejected candidate, or ``0.0`` when the rectangle held nothing.
        reason: Empty when found; otherwise why nothing was accepted.
        candidate_count: How many dark shapes were measured inside the rectangle,
            for display ("6 shapes found, none dash-like enough").
        debug_image_path: Where the diagnostic overlay was written, when one was
            requested.
    """

    found: bool
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    center_x: float = 0.0
    center_y: float = 0.0
    score: float = 0.0
    reason: str = ""
    candidate_count: int = 0
    debug_image_path: Path | None = None


@dataclass(frozen=True, slots=True)
class MarkerDetectionOutcome:
    """The result of one detection pass over a reference image.

    Attributes:
        markers: One :class:`DetectedMarker` per corner, keyed by the same
            string values as :attr:`DetectedMarker.corner`.
        candidate_count: How many dark contours passed the shape filters, for
            display ("14 marker-like shapes found on this page").
    """

    markers: dict[str, DetectedMarker]
    candidate_count: int


def decode_image_file(path: Path, *, color: bool = True) -> DecodedImage:
    """Decode an image file into plain bytes for display.

    Args:
        path: Image file to read.
        color: Decode as BGR (3 channels) rather than grayscale.

    Returns:
        A :class:`DecodedImage` whose ``data`` a `QImage` can be built from
        directly with ``QImage.Format_BGR888`` (colour) or
        ``QImage.Format_Grayscale8`` (grayscale).

    Raises:
        ImageValidationError: The file is missing, unreadable or not a
            decodable image (see
            :func:`omr_scanner.services.alignment_service.load_scan_image`).
    """
    image = load_scan_image(path, color=color)
    contiguous = np.ascontiguousarray(image)
    height, width = int(contiguous.shape[0]), int(contiguous.shape[1])
    channels = int(contiguous.shape[2]) if contiguous.ndim == 3 else 1
    return DecodedImage(
        width=width,
        height=height,
        channels=channels,
        stride=width * channels,
        data=contiguous.tobytes(),
    )


def detect_orientation_marker_in_region(
    path: Path,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    config: OrientationSearchConfig | None = None,
    debug_dir: Path | None = None,
) -> OrientationDetectionOutcome:
    """Locate the printed orientation mark inside a user-drawn search rectangle.

    The designer's counterpart to :func:`detect_registration_markers`: the user
    draws a rectangle around the orientation dash and this finds exactly where it
    is, so the template records the printed mark rather than an approximation
    dragged into place by eye.

    The rectangle is taken in **reference-image pixels** and every returned
    coordinate is in reference-image pixels; the ROI-local frame the detector
    works in never escapes :mod:`omr_scanner.imaging.orientation_marker`.

    A mark lying wholly inside the rectangle is the expected result and is never
    rejected for that - the rectangle's whole purpose is to say "search here".

    Args:
        path: Reference image file.
        x: Left edge of the search rectangle, in reference-image pixels.
        y: Top edge, in reference-image pixels.
        width: Rectangle width, in reference-image pixels.
        height: Rectangle height, in reference-image pixels.
        config: Shape and acceptance tuning; dash-shaped defaults when omitted.
        debug_dir: When given, an annotated overlay is written there as
            :data:`ORIENTATION_DEBUG_IMAGE_NAME`, showing the ROI, every
            candidate and why each was rejected.

    Returns:
        The outcome, with ``found=False`` and a readable ``reason`` rather than an
        exception when nothing qualified - a designer session always has a person
        present who can place the mark by hand.

    Raises:
        ImageValidationError: The file could not be decoded.
        ValueError: The rectangle does not overlap the image at all.
    """
    active = config if config is not None else OrientationSearchConfig()
    image = load_scan_image(path, color=False)
    detection = detect_orientation_marker(
        image,
        roi=BoundingBox(x=x, y=y, width=width, height=height),
        config=OrientationMarkerConfig(
            expected_aspect_ratio=active.expected_aspect_ratio,
            min_aspect_ratio=active.min_aspect_ratio,
            max_aspect_ratio=active.max_aspect_ratio,
            min_score=active.min_score,
        ),
        debug_path=(
            debug_dir / ORIENTATION_DEBUG_IMAGE_NAME if debug_dir is not None else None
        ),
    )
    box = detection.box
    center = detection.center
    return OrientationDetectionOutcome(
        found=detection.found,
        x=box.x if box is not None else 0.0,
        y=box.y if box is not None else 0.0,
        width=box.width if box is not None else 0.0,
        height=box.height if box is not None else 0.0,
        center_x=center.x if center is not None else 0.0,
        center_y=center.y if center is not None else 0.0,
        score=detection.score,
        reason=detection.reason,
        candidate_count=len(detection.candidates),
        debug_image_path=detection.debug_image_path,
    )


def detect_registration_markers(
    path: Path, *, config: MarkerSearchConfig | None = None
) -> MarkerDetectionOutcome:
    """Locate a registration-marker candidate for each corner of a reference image.

    Every corner is scored independently (see the module docstring for why),
    so a page with three good corners and one damaged one returns three
    ``found`` results and one ``found=False`` result naming the reason, rather
    than failing the whole call.

    Args:
        path: Reference image file.
        config: Marker size and search-region tuning. Defaults describe a
            typical printed square roughly 3 per cent of the page wide.

    Returns:
        One outcome per corner, plus the number of accepted candidates overall.

    Raises:
        ImageValidationError: The file could not be decoded.
    """
    active = config if config is not None else MarkerSearchConfig()
    image = load_scan_image(path, color=False)
    height, width = int(image.shape[0]), int(image.shape[1])

    alignment_config = AlignmentConfig(
        canonical_width=width,
        canonical_height=height,
        preprocessing=PreprocessingConfig(),
        marker_detection=MarkerDetectionConfig(
            expected_marker_width=active.expected_marker_width,
            expected_marker_height=active.expected_marker_height,
            corner_search_width=active.corner_search_fraction,
            corner_search_height=active.corner_search_fraction,
            min_candidate_score=active.min_candidate_score,
        ),
    )

    prepared = prepare_for_detection(image, config=alignment_config.preprocessing)
    try:
        candidates, _rejected = detect_marker_candidates(
            prepared.binary, config=alignment_config
        )
    except ImagingError:
        # Preprocessing produced no usable binary image (e.g. a solid-colour
        # scan): every corner is reported missing rather than raising, so the
        # designer can still place markers by hand.
        return MarkerDetectionOutcome(
            markers={
                corner.value: DetectedMarker(
                    corner=corner.value, found=False, reason="Detection failed"
                )
                for corner in IMAGE_CORNER_ORDER
            },
            candidate_count=0,
        )

    scale_factor = prepared.to_source_factor
    outcomes: dict[str, DetectedMarker] = {}

    for corner in IMAGE_CORNER_ORDER:
        region = corner_search_region(
            corner,
            image_width=prepared.working_width,
            image_height=prepared.working_height,
            config=alignment_config,
        )
        in_region = [
            candidate
            for candidate in candidates
            if region.x <= candidate.center.x <= region.right
            and region.y <= candidate.center.y <= region.bottom
        ]
        if not in_region:
            outcomes[corner.value] = DetectedMarker(
                corner=corner.value,
                found=False,
                reason="No marker-like shape found in this corner's search area",
            )
            continue

        scored = [
            score_candidate(
                candidate,
                corner=corner,
                image_width=prepared.working_width,
                image_height=prepared.working_height,
                config=alignment_config,
            )
            for candidate in in_region
        ]
        best = max(scored, key=lambda item: item.score)
        if best.score < active.min_candidate_score:
            outcomes[corner.value] = DetectedMarker(
                corner=corner.value,
                found=False,
                reason=(
                    f"Best candidate scored {best.score:.2f}, below the "
                    f"acceptance threshold {active.min_candidate_score:.2f}"
                ),
            )
            continue

        box = best.candidate.bounding_box.scaled(scale_factor)
        center = best.candidate.center.scaled(scale_factor)
        outcomes[corner.value] = DetectedMarker(
            corner=corner.value,
            found=True,
            x=center.x,
            y=center.y,
            width=box.width,
            height=box.height,
            score=best.score,
        )

    return MarkerDetectionOutcome(markers=outcomes, candidate_count=len(candidates))


__all__ = [
    "DEFAULT_CORNER_SEARCH_FRACTION",
    "DEFAULT_EXPECTED_MARKER_HEIGHT",
    "DEFAULT_EXPECTED_MARKER_WIDTH",
    "DEFAULT_MIN_CANDIDATE_SCORE",
    "ORIENTATION_DEBUG_IMAGE_NAME",
    "DecodedImage",
    "DetectedMarker",
    "MarkerDetectionOutcome",
    "MarkerSearchConfig",
    "OrientationDetectionOutcome",
    "OrientationSearchConfig",
    "decode_image_file",
    "detect_orientation_marker_in_region",
    "detect_registration_markers",
]

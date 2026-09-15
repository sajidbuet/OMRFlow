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
from omr_scanner.imaging.models import IMAGE_CORNER_ORDER
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
    "DecodedImage",
    "DetectedMarker",
    "MarkerDetectionOutcome",
    "MarkerSearchConfig",
    "decode_image_file",
    "detect_registration_markers",
]

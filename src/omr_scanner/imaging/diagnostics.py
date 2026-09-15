"""Optional visual and textual explanations of an alignment.

Purpose:
    Make the engine's decisions inspectable. When a sheet aligns wrongly, the
    useful question is not "did it fail" but "which shape did it choose, and
    what did it reject" - these renderings answer that.

Responsibilities:
    * Draw the candidates, the rejections, the chosen corner markers with their
      canonical labels, the orientation mark and the source quadrilateral onto a
      copy of the scan.
    * Draw the expected canonical marker positions onto the rectified page, so a
      residual error is visible rather than inferred.
    * Produce a compact textual summary of the measured metrics.

What does NOT belong here:
    * Writing files. These functions return arrays and strings; the developer
      tool in :mod:`omr_scanner.tools.align_image` decides where they go, and
      tests write them into a temporary directory.
    * Any influence on the outcome. Diagnostics are produced *from* a finished
      :class:`~omr_scanner.imaging.models.AlignmentResult` and cannot change it.
      Nothing in the pipeline calls into this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import cv2
import numpy as np

from omr_scanner.imaging.models import CANONICAL_CORNER_ORDER, AlignmentResult, BoundingBox, Point

if TYPE_CHECKING:  # pragma: no cover - typing only
    from numpy.typing import NDArray

    from omr_scanner.imaging.config import AlignmentConfig

_SELECTED_COLOR: Final = (40, 200, 40)
"""BGR green: a marker chosen for the transform."""

_CANDIDATE_COLOR: Final = (220, 160, 40)
"""BGR blue-ish: a contour that passed the shape filters but was not chosen."""

_REJECTED_COLOR: Final = (120, 120, 120)
"""BGR grey: a contour discarded by a shape filter."""

_ORIENTATION_COLOR: Final = (40, 120, 240)
"""BGR orange: the orientation mark."""

_QUAD_COLOR: Final = (200, 60, 200)
"""BGR magenta: the quadrilateral fed to the homography."""

_CORNER_LABELS: Final = ("TL", "TR", "BR", "BL")
"""Short labels drawn beside the selected markers, in canonical corner order."""

_FONT: Final = cv2.FONT_HERSHEY_SIMPLEX


def render_detection_overlay(
    image: NDArray[np.uint8], result: AlignmentResult, *, include_rejected: bool = True
) -> NDArray[np.uint8]:
    """Draw what the engine saw onto a colour copy of the scan.

    Args:
        image: The original scan that produced ``result``. It is not modified.
        result: A finished alignment. Candidate and rejection layers are drawn
            only when the result carries diagnostics.
        include_rejected: Draw the discarded contours too. Useful when a marker
            was missed and the question is which filter removed it.

    Returns:
        A new BGR image the same size as ``image``.
    """
    canvas = _as_color(image)
    scale = _annotation_scale(canvas)

    if result.diagnostics is not None:
        if include_rejected:
            for rejected in result.diagnostics.rejected:
                _draw_box(canvas, rejected.candidate.bounding_box, _REJECTED_COLOR, scale)
        for candidate in result.diagnostics.candidates:
            _draw_box(canvas, candidate.bounding_box, _CANDIDATE_COLOR, scale)

    quad = [
        (round(point.x), round(point.y)) for point in result.source_quadrilateral
    ]
    cv2.polylines(
        canvas, [np.array(quad, dtype=np.int32)], True, _QUAD_COLOR, max(1, scale)
    )

    for label, role in zip(_CORNER_LABELS, CANONICAL_CORNER_ORDER, strict=True):
        marker = result.marker(role)
        _draw_box(canvas, marker.candidate.bounding_box, _SELECTED_COLOR, scale * 2)
        _draw_label(
            canvas,
            f"{label} {marker.score:.2f}",
            marker.center,
            _SELECTED_COLOR,
            scale,
        )

    orientation = result.orientation
    if orientation.marker_box is not None:
        _draw_box(canvas, orientation.marker_box, _ORIENTATION_COLOR, scale * 2)
    if orientation.marker_center is not None:
        _draw_label(
            canvas,
            f"ORIENT {orientation.confidence:.2f}",
            orientation.marker_center,
            _ORIENTATION_COLOR,
            scale,
        )
    return canvas


def render_normalized_preview(
    result: AlignmentResult, *, config: AlignmentConfig
) -> NDArray[np.uint8]:
    """Draw the expected canonical marker centres onto the rectified page.

    Each target is drawn as a cross at the position the template declares. On a
    correct alignment every cross sits in the middle of a printed marker; a
    consistent offset is what a miscalibrated template looks like.
    """
    canvas = _as_color(result.normalized_image)
    scale = _annotation_scale(canvas)
    for label, target in zip(_CORNER_LABELS, config.canonical_marker_points(), strict=True):
        _draw_cross(canvas, target, _SELECTED_COLOR, scale)
        _draw_label(canvas, label, target, _SELECTED_COLOR, scale)
    return canvas


def summarize(result: AlignmentResult) -> str:
    """Return a human-readable report of one alignment's measured quality."""
    metrics = result.metrics
    lines = [
        f"source            {result.original_width} x {result.original_height} px",
        f"canonical         {result.canonical_width} x {result.canonical_height} px",
        f"working scale     {metrics.working_scale:.3f}",
        f"candidates        {metrics.candidate_count} accepted, "
        f"{metrics.rejected_count} rejected",
        f"quarter turns     {result.orientation.quarter_turns}"
        + (" (assumed)" if result.orientation.assumed else ""),
        f"orientation       confidence {metrics.orientation_confidence:.2f}, "
        f"margin {metrics.orientation_margin:.2f}",
        "marker scores     "
        + ", ".join(
            f"{label}={metrics.marker_scores[role]:.2f}"
            for label, role in zip(_CORNER_LABELS, CANONICAL_CORNER_ORDER, strict=True)
        ),
        f"quad area ratio   {metrics.quadrilateral_area_ratio:.3f}",
        f"aspect ratio      {metrics.source_aspect_ratio:.3f} vs expected "
        f"{metrics.expected_aspect_ratio:.3f} ({metrics.aspect_ratio_deviation:+.1%})",
        f"marker separation {metrics.min_marker_separation_px:.1f} px",
        f"reprojection      mean {metrics.mean_reprojection_error_px:.4f} px, "
        f"max {metrics.max_reprojection_error_px:.4f} px",
        f"elapsed           {metrics.elapsed_seconds * 1000.0:.1f} ms",
        "warnings          "
        + (", ".join(warning.value for warning in result.warnings) or "none"),
    ]
    for label, role in zip(_CORNER_LABELS, CANONICAL_CORNER_ORDER, strict=True):
        marker = result.marker(role)
        lines.append(
            f"  {label} centre ({marker.center.x:.1f}, {marker.center.y:.1f})  "
            f"area_ratio={marker.candidate.area_ratio:.5f} "
            f"aspect={marker.candidate.aspect_ratio:.2f} "
            f"rect={marker.candidate.rectangularity:.2f} "
            f"solidity={marker.candidate.solidity:.2f} "
            f"fill={marker.candidate.fill_ratio:.2f} "
            f"alternatives={marker.alternative_count}"
        )
    return "\n".join(lines)


def _as_color(image: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Return a BGR copy of ``image``, whatever its channel layout."""
    if image.ndim == 2:
        return np.asarray(cv2.cvtColor(image, cv2.COLOR_GRAY2BGR), dtype=np.uint8)
    channels = int(image.shape[2])
    if channels == 1:
        return np.asarray(cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2BGR), dtype=np.uint8)
    if channels == 4:
        return np.asarray(cv2.cvtColor(image, cv2.COLOR_BGRA2BGR), dtype=np.uint8)
    return np.array(image, dtype=np.uint8, copy=True)


def _annotation_scale(image: NDArray[np.uint8]) -> int:
    """Return a line thickness that stays visible at any scan resolution."""
    shorter = min(int(image.shape[0]), int(image.shape[1]))
    return max(1, round(shorter / 600.0))


def _draw_box(
    canvas: NDArray[np.uint8], box: BoundingBox, color: tuple[int, int, int], thickness: int
) -> None:
    """Outline a bounding box."""
    cv2.rectangle(
        canvas,
        (round(box.x), round(box.y)),
        (round(box.right), round(box.bottom)),
        color,
        max(1, thickness),
    )


def _draw_cross(
    canvas: NDArray[np.uint8], point: Point, color: tuple[int, int, int], scale: int
) -> None:
    """Draw a small cross centred on ``point``."""
    arm = max(4, scale * 6)
    x, y = round(point.x), round(point.y)
    cv2.line(canvas, (x - arm, y), (x + arm, y), color, max(1, scale))
    cv2.line(canvas, (x, y - arm), (x, y + arm), color, max(1, scale))


def _draw_label(
    canvas: NDArray[np.uint8], text: str, point: Point, color: tuple[int, int, int], scale: int
) -> None:
    """Draw ``text`` beside ``point``."""
    cv2.putText(
        canvas,
        text,
        (round(point.x) + scale * 8, round(point.y) - scale * 8),
        _FONT,
        0.4 * scale,
        color,
        max(1, scale),
        cv2.LINE_AA,
    )

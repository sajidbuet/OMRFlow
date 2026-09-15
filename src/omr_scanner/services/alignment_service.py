"""The boundary between a template document and the alignment engine.

Purpose:
    Supply the imaging layer with the geometry a template declares, and read
    scans from disk, so that :mod:`omr_scanner.imaging` never needs to know that
    ``.omrt`` files or file systems exist.

Responsibilities:
    * Convert an :class:`~omr_scanner.domain.template.OmrTemplate` into an
      :class:`~omr_scanner.imaging.config.AlignmentConfig`.
    * Load an image file into a NumPy array, reporting unreadable files as
      :class:`~omr_scanner.errors.ImageValidationError`.
    * Save a derived image without overwriting anything silently.

What does NOT belong here:
    * Any geometry or pixel algorithm; this module only moves values across a
      boundary.
    * Project layout decisions and persistence of results. Writing aligned
      sheets into ``<project>/scans_aligned`` and recording them is the scan
      service's job in Phase 5.

Why the conversion lives here rather than in ``imaging``:
    ``docs/ARCHITECTURE.md`` puts the template in ``domain`` and pixel work in
    ``imaging``, and keeps the two from depending on each other. Alignment
    functions therefore take plain geometry, which is also what makes them
    testable with synthetic sheets that have no template at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np

from omr_scanner.errors import ImageValidationError
from omr_scanner.imaging.config import (
    AlignmentConfig,
    GeometryConfig,
    MarkerDetectionConfig,
    OrientationConfig,
    PreprocessingConfig,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from numpy.typing import NDArray

    from omr_scanner.domain.template import OmrTemplate


def alignment_config_from_template(
    template: OmrTemplate,
    *,
    preprocessing: PreprocessingConfig | None = None,
    marker_detection: MarkerDetectionConfig | None = None,
    orientation: OrientationConfig | None = None,
    geometry: GeometryConfig | None = None,
    diagnostics: bool = False,
) -> AlignmentConfig:
    """Build an alignment configuration from a template document.

    The canonical page size, the four expected marker centres, the printed
    marker size and the orientation mark's position and size all come from the
    template. The remaining parameters - thresholds, tolerances, score weights -
    are engine tuning rather than sheet design, so they keep their defaults
    unless a caller overrides them.

    Args:
        template: The validated template describing the sheet.
        preprocessing: Override for the preprocessing parameters.
        marker_detection: Override for the detection parameters. The expected
            marker size is taken from the template even when this is supplied,
            unless the override already differs from the default.
        orientation: Override for the orientation parameters, used in full when
            given.
        geometry: Override for the geometry validation bounds.
        diagnostics: Retain intermediate state on the result.

    Returns:
        A configuration equivalent to what the template declares.
    """
    page = template.page
    targets = {
        marker.role: marker.center for marker in template.registration_markers
    }
    first_marker = template.registration_markers[0]

    detection = marker_detection or MarkerDetectionConfig(
        expected_marker_width=first_marker.size.width,
        expected_marker_height=first_marker.size.height,
    )

    mark = template.orientation_marker
    orientation_config = orientation or OrientationConfig(
        marker_center_x=mark.center.x,
        marker_center_y=mark.center.y,
        marker_width=mark.size.width,
        marker_height=mark.size.height,
        search_radius=mark.search_radius,
    )

    return AlignmentConfig(
        canonical_width=page.canonical_width_px,
        canonical_height=page.canonical_height_px,
        marker_targets=targets,
        preprocessing=preprocessing or PreprocessingConfig(),
        marker_detection=detection,
        orientation=orientation_config,
        geometry=geometry or GeometryConfig(),
        diagnostics=diagnostics,
    )


def load_scan_image(path: Path, *, color: bool = False) -> NDArray[np.uint8]:
    """Read an image file into an array.

    Decoded through :func:`numpy.fromfile` and ``cv2.imdecode`` rather than
    ``cv2.imread`` because ``imread`` cannot open a path containing non-ASCII
    characters on Windows, and examination folders are frequently named in the
    local language.

    Args:
        path: The image file.
        color: Return BGR instead of grayscale. Alignment does not need colour;
            it is useful when the rectified page will be shown to a human.

    Returns:
        The decoded image.

    Raises:
        ImageValidationError: The file is missing, unreadable or not a decodable
            image.
    """
    try:
        raw = np.fromfile(path, dtype=np.uint8)
    except OSError as exc:
        raise ImageValidationError(
            f"Could not read image file '{path}': {exc}",
            user_message=f"The file '{path.name}' could not be read.",
        ) from exc
    if raw.size == 0:
        raise ImageValidationError(
            f"Image file '{path}' is empty",
            user_message=f"The file '{path.name}' is empty.",
        )

    flag = cv2.IMREAD_COLOR if color else cv2.IMREAD_GRAYSCALE
    decoded = cv2.imdecode(raw, flag)
    if decoded is None:
        raise ImageValidationError(
            f"Image file '{path}' could not be decoded; it may not be an image",
            user_message=f"The file '{path.name}' is not an image OMRFlow can read.",
        )
    return np.asarray(decoded, dtype=np.uint8)


def save_image(image: NDArray[np.uint8], path: Path, *, overwrite: bool = False) -> None:
    """Write an image, refusing to replace an existing file unless asked.

    Encoded in memory and written with :meth:`pathlib.Path.write_bytes` for the
    same non-ASCII path reason as :func:`load_scan_image`.

    Raises:
        ImageValidationError: The target exists and ``overwrite`` is false, the
            suffix names no encoder, or encoding failed.
    """
    if path.exists() and not overwrite:
        raise ImageValidationError(
            f"Refusing to overwrite the existing file '{path}'",
            user_message=f"'{path.name}' already exists.",
        )
    try:
        success, buffer = cv2.imencode(path.suffix, image)
    except cv2.error as exc:
        # OpenCV raises rather than returning False for an unknown extension;
        # that assertion must not become the application's error interface.
        raise ImageValidationError(
            f"Could not encode an image as '{path.suffix}': {exc}",
            user_message=f"OMRFlow cannot write '{path.suffix}' images.",
        ) from exc
    if not success:
        raise ImageValidationError(
            f"Could not encode an image as '{path.suffix}'",
            user_message=f"OMRFlow cannot write '{path.suffix}' images.",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.tobytes())

"""Image validation, grayscale conversion and binarisation.

Purpose:
    Turn whatever array a caller supplies into the two things marker detection
    needs - a grayscale image and a binary image in which ink is white - while
    refusing malformed input with an OMRFlow error rather than an OpenCV
    assertion.

Responsibilities:
    * Validate the input array's dtype, dimensionality, channel count and size.
    * Convert to grayscale for any supported channel layout.
    * Produce the working-resolution copy detection runs on, and report the
      scale factor so that measurements can be mapped back to the original.
    * Apply the configured denoising and thresholding strategy.

What does NOT belong here:
    * Any decision about which contour is a marker. This module produces pixels;
      :mod:`omr_scanner.imaging.marker_detection` interprets them.
    * The final warp. Rectification always reads the full-resolution original,
      never the downscaled working copy, so no detail is lost to detection's
      convenience.

Invariants:
    * The supplied array is never written to. Every stage returns a new array.
    * The scale factor is exact for both axes: the working copy preserves the
      aspect ratio, so one number suffices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import cv2
import numpy as np

from omr_scanner.errors import ImageValidationError
from omr_scanner.imaging.config import ThresholdStrategy

if TYPE_CHECKING:  # pragma: no cover - typing only
    from numpy.typing import NDArray

    from omr_scanner.imaging.config import PreprocessingConfig

SUPPORTED_CHANNEL_COUNTS: Final = (1, 3, 4)
"""Grayscale, BGR and BGRA. Anything else is rejected as malformed."""

_MIN_ADAPTIVE_BLOCK_PX: Final = 3
"""Smallest neighbourhood OpenCV accepts for an adaptive threshold."""

_INK_VALUE: Final = 255
"""Value written for ink in the binary image; contours are found on ink."""


@dataclass(frozen=True, slots=True)
class PreparedImage:
    """The working-resolution products of preprocessing.

    Attributes:
        grayscale: Working-resolution grayscale image.
        binary: Working-resolution binary image; ink is 255, paper is 0.
        scale: ``working_dimension / original_dimension``, at most 1.0.
        source_width: Width of the original image in pixels.
        source_height: Height of the original image in pixels.
    """

    grayscale: NDArray[np.uint8]
    binary: NDArray[np.uint8]
    scale: float
    source_width: int
    source_height: int

    @property
    def to_source_factor(self) -> float:
        """Factor converting a working-resolution length into a source-image length."""
        return 1.0 / self.scale

    @property
    def working_width(self) -> int:
        """Width of the working-resolution images."""
        return int(self.grayscale.shape[1])

    @property
    def working_height(self) -> int:
        """Height of the working-resolution images."""
        return int(self.grayscale.shape[0])


def validate_image(image: object, *, config: PreprocessingConfig) -> NDArray[np.uint8]:
    """Check that ``image`` is an array this pipeline can process.

    Args:
        image: The candidate image. Typed as ``object`` deliberately: this
            function is the boundary where an untrusted value becomes a known
            array, and it must be callable with anything.
        config: Supplies the minimum accepted dimension.

    Returns:
        The same array, unmodified and unconverted.

    Raises:
        ImageValidationError: The value is not a NumPy array, is not 8-bit, has
            the wrong number of dimensions or channels, or is smaller than
            ``config.min_image_dimension_px`` on either axis.
    """
    if not isinstance(image, np.ndarray):
        raise ImageValidationError(
            f"Expected a NumPy array, received {type(image).__name__}",
            user_message="That file could not be read as an image.",
        )
    if image.dtype != np.uint8:
        raise ImageValidationError(
            f"Expected an 8-bit image, received dtype {image.dtype}",
            user_message="That image uses an unsupported pixel format.",
        )
    if image.ndim not in (2, 3):
        raise ImageValidationError(
            f"Expected a 2- or 3-dimensional array, received shape {image.shape}",
            user_message="That file could not be read as an image.",
        )
    if image.ndim == 3 and image.shape[2] not in SUPPORTED_CHANNEL_COUNTS:
        raise ImageValidationError(
            f"Expected {SUPPORTED_CHANNEL_COUNTS} channels, received {image.shape[2]}",
            user_message="That image uses an unsupported colour format.",
        )
    if image.size == 0:
        raise ImageValidationError(
            f"Image is empty (shape {image.shape})",
            user_message="That image is empty.",
        )
    height, width = int(image.shape[0]), int(image.shape[1])
    if min(height, width) < config.min_image_dimension_px:
        raise ImageValidationError(
            f"Image is {width}x{height}; the shortest side must be at least "
            f"{config.min_image_dimension_px} pixels to resolve a registration marker",
            user_message="That scan is too small to contain a readable answer sheet.",
        )
    return image


def to_grayscale(image: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Return a single-channel 8-bit copy of ``image``.

    A copy is always returned, even when the input is already grayscale, so that
    a caller holding the result can never write through it into the original
    scan.
    """
    if image.ndim == 2:
        return np.array(image, dtype=np.uint8, copy=True)
    channels = int(image.shape[2])
    if channels == 1:
        return np.array(image[:, :, 0], dtype=np.uint8, copy=True)
    code = cv2.COLOR_BGRA2GRAY if channels == 4 else cv2.COLOR_BGR2GRAY
    return np.asarray(cv2.cvtColor(image, code), dtype=np.uint8)


def working_scale(width: int, height: int, *, max_dimension_px: int) -> float:
    """Return the factor that fits ``width`` x ``height`` inside ``max_dimension_px``.

    Never enlarges: an image already small enough is used at its own size, so a
    low-resolution scan is not given false detail.
    """
    longest = max(width, height)
    if longest <= max_dimension_px:
        return 1.0
    return max_dimension_px / longest


def binarize(image: NDArray[np.uint8], *, config: PreprocessingConfig) -> NDArray[np.uint8]:
    """Return a binary image in which ink is 255 and paper is 0.

    Args:
        image: Grayscale working-resolution image.
        config: Selects the strategy and its parameters.

    Returns:
        A new 8-bit single-channel array the same size as ``image``.
    """
    if config.threshold_strategy is ThresholdStrategy.OTSU:
        _, binary = cv2.threshold(
            image, 0, _INK_VALUE, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )
        return np.asarray(binary, dtype=np.uint8)

    block = _adaptive_block_size(image, config=config)
    method = (
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C
        if config.threshold_strategy is ThresholdStrategy.ADAPTIVE_GAUSSIAN
        else cv2.ADAPTIVE_THRESH_MEAN_C
    )
    binary = cv2.adaptiveThreshold(
        image, _INK_VALUE, method, cv2.THRESH_BINARY_INV, block, config.adaptive_offset
    )
    return np.asarray(binary, dtype=np.uint8)


def prepare_for_detection(
    image: NDArray[np.uint8], *, config: PreprocessingConfig
) -> PreparedImage:
    """Produce the grayscale and binary working images marker detection needs.

    Args:
        image: A validated image (see :func:`validate_image`).
        config: Denoising, downscaling and thresholding parameters.

    Returns:
        The working-resolution grayscale and binary images together with the
        scale factor needed to map measurements back to source coordinates.
    """
    source_height, source_width = int(image.shape[0]), int(image.shape[1])
    grayscale = to_grayscale(image)

    scale = working_scale(
        source_width, source_height, max_dimension_px=config.working_max_dimension_px
    )
    if scale < 1.0:
        target = (max(1, round(source_width * scale)), max(1, round(source_height * scale)))
        grayscale = np.asarray(
            cv2.resize(grayscale, target, interpolation=cv2.INTER_AREA), dtype=np.uint8
        )
        # Recompute from the realised size: rounding to whole pixels means the
        # achieved scale differs slightly from the requested one, and a marker
        # centre mapped back with the wrong factor would be systematically off.
        scale = grayscale.shape[1] / source_width

    denoised = grayscale
    if config.blur_kernel_px:
        kernel = (config.blur_kernel_px, config.blur_kernel_px)
        denoised = np.asarray(cv2.GaussianBlur(grayscale, kernel, 0), dtype=np.uint8)

    return PreparedImage(
        grayscale=grayscale,
        binary=binarize(denoised, config=config),
        scale=scale,
        source_width=source_width,
        source_height=source_height,
    )


def _adaptive_block_size(image: NDArray[np.uint8], *, config: PreprocessingConfig) -> int:
    """Return an odd neighbourhood size for the adaptive threshold strategies."""
    shorter = min(int(image.shape[0]), int(image.shape[1]))
    block = round(shorter * config.adaptive_block_ratio)
    if block % 2 == 0:
        block += 1
    return max(_MIN_ADAPTIVE_BLOCK_PX, block)

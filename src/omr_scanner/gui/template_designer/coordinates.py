"""One place every pixel/normalised/scene coordinate conversion goes through.

Purpose:
    The designer juggles three coordinate systems at once - where something is
    drawn on screen, where it is in the reference image's own pixels, and where
    it is as a template-normalised fraction - and scattering the arithmetic
    between them across canvas, item and dialog code is exactly how an
    off-by-one zoom bug becomes unfindable. This module is the only place that
    arithmetic exists.

Responsibilities:
    * :class:`CoordinateMapper` - a pure, Qt-free value type converting between
      **image pixels** (the reference image's own coordinate system - stable
      regardless of zoom) and **normalised template coordinates** (``[0, 1]``,
      what the ``.omrt`` document actually stores).
    * Scene-vs-image conversion is deliberately *not* handled here: with
      `QGraphicsView`, placing overlay items directly in image-pixel scene
      coordinates and letting the view itself apply the zoom transform (via
      `QGraphicsView.scale`) means the view's own matrix - not a second
      hand-written one - is the only place zoom is ever applied. This module
      supplies the other half: image pixels <-> normalised coordinates, which
      the view has no reason to know about.

What does NOT belong here:
    * Qt imports. Every function here takes and returns plain floats, so it is
      testable without a `QApplication`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CoordinateMapper:
    """Converts between reference-image pixels and normalised template coordinates.

    Attributes:
        image_width: Width of the reference image in pixels.
        image_height: Height of the reference image in pixels.

    Raises:
        ValueError: Either dimension is not positive - a mapper for an image
            that has no size cannot convert anything meaningfully.
    """

    image_width: float
    image_height: float

    def __post_init__(self) -> None:
        """Reject a mapper that could not convert anything."""
        if self.image_width <= 0.0 or self.image_height <= 0.0:
            raise ValueError("image_width and image_height must be positive")

    def to_normalized(self, pixel_x: float, pixel_y: float) -> tuple[float, float]:
        """Convert an image-pixel position to a normalised ``(x, y)`` pair."""
        return (pixel_x / self.image_width, pixel_y / self.image_height)

    def to_pixels(self, normalized_x: float, normalized_y: float) -> tuple[float, float]:
        """Convert a normalised ``(x, y)`` pair to image pixels."""
        return (normalized_x * self.image_width, normalized_y * self.image_height)

    def size_to_normalized(self, width_px: float, height_px: float) -> tuple[float, float]:
        """Convert a pixel size to a normalised ``(width, height)`` pair.

        Split from :meth:`to_normalized` because a size is not a position: it
        must not be affected by choosing a different image, only by scale, and
        keeping the two operations separate is what prevents a future refactor
        from quietly subtracting an origin from a size.
        """
        return (width_px / self.image_width, height_px / self.image_height)

    def size_to_pixels(
        self, normalized_width: float, normalized_height: float
    ) -> tuple[float, float]:
        """Convert a normalised size to a pixel ``(width, height)`` pair."""
        return (normalized_width * self.image_width, normalized_height * self.image_height)

    def rect_to_normalized(
        self, x_px: float, y_px: float, width_px: float, height_px: float
    ) -> tuple[float, float, float, float]:
        """Convert a top-left-anchored pixel rectangle to normalised ``(x, y, w, h)``."""
        x, y = self.to_normalized(x_px, y_px)
        width, height = self.size_to_normalized(width_px, height_px)
        return (x, y, width, height)

    def rect_to_pixels(
        self, x: float, y: float, width: float, height: float
    ) -> tuple[float, float, float, float]:
        """Convert a normalised top-left-anchored rectangle to pixel ``(x, y, w, h)``."""
        x_px, y_px = self.to_pixels(x, y)
        width_px, height_px = self.size_to_pixels(width, height)
        return (x_px, y_px, width_px, height_px)

    def clamp_normalized(self, value: float) -> float:
        """Clamp a single normalised coordinate into ``[0, 1]``.

        Dragging a handle a pixel past the edge of the reference image is a
        routine mouse-tracking overshoot, not a meaningful attempt to place
        geometry off the page; clamping here means every caller gets a value
        the domain model will always accept, instead of each call site needing
        its own bounds check before constructing a `NormalizedPoint`.
        """
        return min(1.0, max(0.0, value))


def snap(value: float, *, grid_size: float) -> float:
    """Round ``value`` to the nearest multiple of ``grid_size``.

    Args:
        value: A normalised coordinate.
        grid_size: Spacing of the snap grid, in normalised units. A
            non-positive value disables snapping (the value is returned
            unchanged), so callers can wire a "snap enabled" checkbox straight
            to this parameter without a separate branch.

    Returns:
        The snapped value, not re-clamped to ``[0, 1]`` - callers that also
        need clamping call :meth:`CoordinateMapper.clamp_normalized` afterwards,
        since snapping and clamping are independent concerns.
    """
    if grid_size <= 0.0:
        return value
    return round(value / grid_size) * grid_size

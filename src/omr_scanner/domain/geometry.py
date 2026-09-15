"""Normalised geometry primitives shared by templates and (later) imaging.

Purpose:
    Express positions and sizes on an OMR sheet independently of scan
    resolution, so that one template works for 150 dpi and 300 dpi scans alike.

Coordinate convention (used everywhere in OMRFlow):
    * Origin ``(0.0, 0.0)`` is the **top-left** corner of the canonical page.
    * ``x`` grows to the right, ``y`` grows downward - the same convention as
      OpenCV and Qt, so no axis flipping is ever needed at a layer boundary.
    * Values are fractions of the canonical page::

          x_normalized = pixel_x / image_width
          y_normalized = pixel_y / image_height

    * The valid range is ``[0.0, 1.0]`` inclusive. A value outside it means the
      feature is off the page and is rejected at validation time.
    * Because ``x`` and ``y`` are normalised by *different* denominators, a
      "radius" is not meaningful; bubbles therefore carry a width and a height
      (:class:`NormalizedSize`) rather than a single radius.

What does NOT belong here:
    * Pixel conversion helpers that need an actual image; those take an image
      size and belong to ``imaging`` in Phase 1. The only conversion offered
      here is the trivially pure :meth:`NormalizedPoint.to_pixels`.
    * Any dependency on NumPy or OpenCV.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

NORMALIZED_MIN = 0.0
NORMALIZED_MAX = 1.0


class NormalizedPoint(BaseModel):
    """A point on the canonical page, in normalised page coordinates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: float = Field(ge=NORMALIZED_MIN, le=NORMALIZED_MAX)
    y: float = Field(ge=NORMALIZED_MIN, le=NORMALIZED_MAX)

    def to_pixels(self, image_width: int, image_height: int) -> tuple[float, float]:
        """Project this point onto an image of the given pixel size.

        Args:
            image_width: Width of the target image in pixels.
            image_height: Height of the target image in pixels.

        Returns:
            ``(x_pixels, y_pixels)`` as floats; callers decide how to round,
            because marker refinement works at sub-pixel precision.
        """
        return self.x * image_width, self.y * image_height


class NormalizedSize(BaseModel):
    """A width and height in normalised page coordinates.

    Sizes may be zero-width in neither axis: a zone or bubble with no area
    cannot be measured, so both values must be greater than zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    width: float = Field(gt=0.0, le=NORMALIZED_MAX)
    height: float = Field(gt=0.0, le=NORMALIZED_MAX)


class NormalizedRect(BaseModel):
    """An axis-aligned rectangle in normalised page coordinates.

    The rectangle is defined by its top-left corner plus a size, matching how
    zones are drawn in a designer canvas. Rotated regions are not representable
    by design: recognition happens *after* the sheet has been rectified, so
    every zone is axis aligned in the canonical frame.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: float = Field(ge=NORMALIZED_MIN, le=NORMALIZED_MAX)
    y: float = Field(ge=NORMALIZED_MIN, le=NORMALIZED_MAX)
    width: float = Field(gt=0.0, le=NORMALIZED_MAX)
    height: float = Field(gt=0.0, le=NORMALIZED_MAX)

    @property
    def right(self) -> float:
        """Normalised x coordinate of the right edge."""
        return self.x + self.width

    @property
    def bottom(self) -> float:
        """Normalised y coordinate of the bottom edge."""
        return self.y + self.height

    @property
    def center(self) -> NormalizedPoint:
        """Centre of the rectangle."""
        return NormalizedPoint(x=self.x + self.width / 2, y=self.y + self.height / 2)

    def contains_point(self, point: NormalizedPoint) -> bool:
        """Return whether ``point`` lies inside the rectangle (edges included)."""
        return self.x <= point.x <= self.right and self.y <= point.y <= self.bottom

    def is_within_page(self) -> bool:
        """Return whether the rectangle lies entirely on the page.

        Field validation already constrains ``x``/``y``/``width``/``height``
        individually; this additionally rejects rectangles that start on the page
        but extend past its right or bottom edge.
        """
        return self.right <= NORMALIZED_MAX and self.bottom <= NORMALIZED_MAX

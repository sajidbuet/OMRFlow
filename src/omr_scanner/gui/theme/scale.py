"""The interface scale, as a plain value: one percentage and its arithmetic.

Purpose:
    Turn a canonical 100% design value - a token from :mod:`.tokens`, or a
    pixel literal a page uses for a control - into the value to apply at the
    operator's chosen interface zoom, in exactly one way everywhere.

Responsibilities:
    * :class:`UiScale` - an immutable percentage with the rounding rules for
      lengths, strokes and font sizes.

What does NOT belong here:
    * Any Qt import, and any state. Which scale is *current* is the
      application's business - see :mod:`omr_scanner.gui.ui_scale`. This
      module only answers "what is 16 pixels at 130%?", which is what lets a
      test check the arithmetic without a display.
    * Document coordinates. A bubble radius on a scanned sheet, a template's
      normalised rectangle or an image-processing kernel is data, not
      interface, and is never passed through a :class:`UiScale`.

Why every value is computed from the canonical one:
    The tokens stay the 100% design. A scale is applied to them on the way
    out, never stored back into them, so 100% -> 150% -> 100% returns exactly
    the original value and 110% followed by 120% means 120%, not
    ``1.10 x 1.20``. Nothing here can compound, because nothing here
    remembers the previous scale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cache
from typing import ClassVar

from omr_scanner.config.app_config import (
    DEFAULT_UI_ZOOM_PERCENT,
    MAX_UI_ZOOM_PERCENT,
    MIN_UI_ZOOM_PERCENT,
)
from omr_scanner.gui.theme.tokens import FontSize


def _round_half_up(value: float) -> int:
    """Round to the nearest integer, halves away from zero.

    Not the built-in ``round``, which rounds halves to even: 2.5 and 3.5
    would become 2 and 4, so two tokens a pixel apart could scale to sizes
    two pixels apart, and the steps of the scale would be uneven.
    """
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


@dataclass(frozen=True)
class UiScale:
    """One interface zoom level.

    Attributes:
        percent: The zoom, where 100 is the canonical design. Clamped to
            :data:`~omr_scanner.config.app_config.MIN_UI_ZOOM_PERCENT` ..
            :data:`~omr_scanner.config.app_config.MAX_UI_ZOOM_PERCENT` by
            :meth:`of`; a directly constructed instance is taken as given, so a
            test can probe the arithmetic outside the operator's range.
    """

    percent: int = DEFAULT_UI_ZOOM_PERCENT

    IDENTITY: ClassVar[UiScale]

    @staticmethod
    @cache
    def of(percent: int) -> UiScale:
        """The scale for ``percent``, clamped to the supported range."""
        return UiScale(max(MIN_UI_ZOOM_PERCENT, min(MAX_UI_ZOOM_PERCENT, int(percent))))

    @property
    def factor(self) -> float:
        """The multiplier: 1.0 at 100%."""
        return self.percent / 100.0

    @property
    def is_identity(self) -> bool:
        """Whether this is the canonical, unscaled interface."""
        return self.percent == DEFAULT_UI_ZOOM_PERCENT

    def px(self, value: float) -> int:
        """A length - padding, margin, gap, control or icon size - in pixels.

        A non-zero length never rounds to zero: a 1-pixel gap at 80% is still
        a gap. Zero stays zero, because "no margin" is a decision, not a size.
        """
        if value == 0:
            return 0
        scaled = _round_half_up(value * self.factor)
        if scaled == 0:
            return 1 if value > 0 else -1
        return scaled

    def size(self, width: float, height: float) -> tuple[int, int]:
        """``(width, height)``, each through :meth:`px`."""
        return self.px(width), self.px(height)

    def stroke(self, width: float) -> int:
        """A border or rule width: never thinner than one pixel.

        Rounded *down*, so a hairline stays a hairline until the scale
        genuinely doubles it. A 1-pixel border drawn 2 pixels wide at 150%
        reads as a heavier control, not as the same control larger.
        """
        if width <= 0:
            return 0
        return max(1, math.floor(width * self.factor + 1e-9))

    def pen(self, width: float) -> float:
        """A painter pen width, unrounded - a `QPen` takes fractions."""
        return width * self.factor

    def point_size(
        self, base_points: float, delta: float = 0.0, *, floor: float | None = None
    ) -> float:
        """A font's point size: the semantic delta first, then the scale.

        Args:
            base_points: The platform's own 100% UI font size. Never an
                already-scaled size - see :mod:`omr_scanner.gui.ui_scale`,
                which keeps the original.
            delta: A :class:`~omr_scanner.gui.theme.tokens.FontSize` delta.
            floor: Minimum *unscaled* size. Defaults to
                :data:`FontSize.MIN_POINT_SIZE`.

        Returns:
            ``max(base + delta, floor) * factor`` - so a page title at 150% is
            one and a half times the page title, not one and a half times the
            body text plus a fixed six points.
        """
        minimum = FontSize.MIN_POINT_SIZE if floor is None else floor
        return max(base_points + delta, minimum) * self.factor


UiScale.IDENTITY = UiScale()


__all__ = ["UiScale"]

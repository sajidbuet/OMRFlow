"""Per-bubble ink measurement on a rectified canonical page.

Purpose:
    Turn "there should be a bubble centred here, this big" into numbers a
    decision layer can reason about - how much of the bubble is ink, how dark it
    is relative to the paper immediately around it, and how much of the sample
    was usable.

Responsibilities:
    * Sample the interior of one expected bubble and report its ink metrics
      (:func:`measure_bubble`).
    * Do the same for a whole group of bubbles in one call
      (:func:`measure_bubbles`).

What does NOT belong here:
    * Deciding what a measurement *means*. "Fill ratio 0.71 therefore the
      candidate chose B" is :mod:`omr_scanner.recognition`; this module has no
      notion of an answer, a symbol or a threshold for acceptance.
    * Knowing where the bubbles are. Centres arrive as canonical pixels; turning
      a template's normalised :class:`~omr_scanner.domain.template.BubbleGrid`
      into those pixels is the recognition service's job, which keeps ``.omrt``
      knowledge out of ``imaging`` exactly as ``docs/ARCHITECTURE.md`` requires.

Why the sample is smaller than the printed bubble:
    The printed ring is ink. Sampling the full bubble would score an *empty*
    bubble as partly filled simply because its outline is dark, and the
    proportion of ring to interior changes with print size and scan resolution -
    exactly the resolution dependence the canonical page exists to remove. The
    sample therefore covers the interior only
    (:attr:`BubbleMetricsConfig.sample_radius_ratio`), and the ring is used
    deliberately in the *other* direction: the annulus just outside it estimates
    the local paper level.

Why the paper level is measured per bubble:
    A scan is rarely evenly lit - a page lifted off the platen, the shadow of a
    bound spine, or simply a cheap scanner's lamp falloff all leave one part of
    the sheet darker than another. A single page-wide threshold then marks faint
    bubbles in the bright region as blank and empty bubbles in the dark region as
    filled. Comparing each bubble against the paper *immediately around it*
    removes the gradient without any global normalisation pass, and costs one
    extra annulus per bubble.

Why "ink" is defined relative to the page, not as a fixed grey level:
    Most OMR sheets print the option's own symbol *inside* its bubble - ⓐ ⓑ ⓒ ⓓ,
    or the digit in a roll-number column. An empty bubble therefore already
    contains ink, and a fixed "darker than paper by 40 levels" rule counts that
    printing as a mark: on the repository's real sample it reports an
    untouched ⓑ as 58 per cent filled. The printed glyph is light grey; a pencil
    or pen mark is close to the darkest ink on the page. The threshold is
    therefore placed *halfway between the local paper and the darkest ink the
    page actually contains* (:func:`estimate_ink_level`), which separates the two
    populations on any scanner, at any exposure, without a per-sheet constant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from omr_scanner.errors import ImageValidationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from numpy.typing import NDArray

MAX_GREY = 255.0
"""Full white in an 8-bit image; used to normalise darkness to ``[0, 1]``."""


@dataclass(frozen=True, slots=True)
class BubbleMetricsConfig:
    """Tunable geometry and thresholds for bubble sampling.

    Every value is a ratio or a grey-level difference rather than a pixel count,
    so one configuration serves a 150 dpi and a 300 dpi scan alike - the same
    resolution-independence rule the alignment engine follows.

    Attributes:
        sample_radius_ratio: Fraction of the bubble's half-axes that the
            interior sample covers. ``0.62`` keeps the sample clear of the
            printed ring while still covering most of the area a candidate
            actually shades.
        background_inner_ratio: Inner radius of the paper-estimating annulus, as
            a fraction of the bubble's half-axes. Must sit outside the printed
            ring, hence greater than ``1``.
        background_outer_ratio: Outer radius of the same annulus. The gap
            between the two ratios is what gives the estimate enough pixels to
            be stable without reaching into the neighbouring bubble.
        paper_percentile: Percentile of the annulus used as "paper". A high
            percentile rather than the mean, because the annulus frequently
            catches part of a neighbouring mark, a grid line or the printed
            symbol beside the bubble; the brightest pixels in a small
            neighbourhood are the ones that are reliably paper.
        ink_percentile: Percentile of the whole page taken as "solid ink" by
            :func:`estimate_ink_level`. Low, but not the minimum: the single
            darkest pixel of a scan is noise, while the darkest one per cent is
            reliably the printed text, the registration squares and the marks.
        ink_fraction: Where the ink threshold sits between the local paper level
            and the page's ink level. ``0.5`` - halfway - is the value the
            printed-glyph problem actually calls for: the glyph inside an empty
            bubble is light grey, well under halfway, and a pencil mark is well
            over it.
        min_ink_margin: Floor on the resulting threshold distance, in grey
            levels. Protects a sheet with almost nothing printed on it, where
            the "darkest ink" estimate would otherwise be paper-coloured and
            every speck would read as a mark.
        max_ink_margin: Ceiling on the same distance, so an unusually black
            scan cannot demand an impossibly dark mark.
        min_sample_pixels: Fewest interior pixels a measurement may be based on.
            A bubble whose sample falls off the page edge yields fewer; below
            this the measurement is reported as unusable rather than guessed.
    """

    sample_radius_ratio: float = 0.62
    background_inner_ratio: float = 1.25
    background_outer_ratio: float = 1.95
    paper_percentile: float = 80.0
    ink_percentile: float = 1.0
    ink_fraction: float = 0.5
    min_ink_margin: float = 35.0
    max_ink_margin: float = 130.0
    min_sample_pixels: int = 9

    def __post_init__(self) -> None:
        """Reject a configuration that could not produce a usable sample."""
        if not 0.0 < self.sample_radius_ratio <= 1.0:
            raise ValueError("sample_radius_ratio must be in (0, 1]")
        if self.background_inner_ratio <= 1.0:
            raise ValueError(
                "background_inner_ratio must be greater than 1: the annulus has to "
                "sit outside the printed ring to measure paper rather than ink"
            )
        if self.background_outer_ratio <= self.background_inner_ratio:
            raise ValueError("background_outer_ratio must exceed background_inner_ratio")
        if not 0.0 <= self.paper_percentile <= 100.0:
            raise ValueError("paper_percentile must be a percentile in [0, 100]")
        if not 0.0 <= self.ink_percentile <= 100.0:
            raise ValueError("ink_percentile must be a percentile in [0, 100]")
        if not 0.0 < self.ink_fraction < 1.0:
            raise ValueError("ink_fraction must be in (0, 1)")
        if self.min_ink_margin <= 0.0:
            raise ValueError("min_ink_margin must be positive")
        if self.max_ink_margin < self.min_ink_margin:
            raise ValueError("max_ink_margin must be at least min_ink_margin")
        if self.min_sample_pixels < 1:
            raise ValueError("min_sample_pixels must be at least 1")


@dataclass(frozen=True, slots=True)
class BubbleMeasurement:
    """What one expected bubble position actually contains.

    Deliberately several named numbers rather than a single "is it filled"
    verdict: a faint pencil mark and a printing smudge produce different
    combinations of them, and only the decision layer - which can also see the
    *other* bubbles in the same group - can tell those apart.

    Attributes:
        center_x: Sampled centre on the canonical page, in pixels.
        center_y: Sampled centre on the canonical page, in pixels.
        fill_ratio: Fraction of the interior sample classified as ink, in
            ``[0, 1]``. This is the quantity
            :class:`~omr_scanner.domain.template.RecognitionSettings` thresholds
            are expressed against.
        mean_darkness: ``1 - mean(sample) / 255``, in ``[0, 1]``. Unlike
            ``fill_ratio`` this is not thresholded, so it still separates two
            bubbles that are both fully above or both fully below the ink
            margin.
        paper_level: Estimated local paper brightness in grey levels, from the
            annulus around the bubble.
        contrast: ``(paper_level - mean(sample)) / 255``, in ``[-1, 1]``. How
            much darker the bubble's interior is than the paper beside it;
            negative when the interior is brighter than its surroundings.
        sample_pixels: Number of pixels the interior sample covered.
        usable: Whether the sample met
            :attr:`BubbleMetricsConfig.min_sample_pixels`. A measurement that is
            not usable has zeroed metrics and must never be read as "blank".
    """

    center_x: float
    center_y: float
    fill_ratio: float
    mean_darkness: float
    paper_level: float
    contrast: float
    sample_pixels: int
    usable: bool


def estimate_ink_level(
    image: NDArray[np.uint8], *, config: BubbleMetricsConfig | None = None
) -> float:
    """Estimate the grey level of solid ink on one page.

    Args:
        image: Grayscale canonical page. Read only.
        config: Supplies :attr:`BubbleMetricsConfig.ink_percentile`.

    Returns:
        The page's ``ink_percentile``-th darkest grey level. On a normal OMR
        sheet this lands on the printed text, the registration squares and the
        candidate's own marks - everything that is unambiguously ink - rather
        than on the light grey of the symbol printed inside each bubble.

    Raises:
        ImageValidationError: ``image`` is not a 2-D 8-bit array.
    """
    settings = config if config is not None else BubbleMetricsConfig()
    _validate_page(image)
    return float(np.percentile(image, settings.ink_percentile))


def ink_threshold(paper_level: float, ink_level: float, config: BubbleMetricsConfig) -> float:
    """Return the grey level below which a pixel counts as ink.

    Args:
        paper_level: The paper brightness measured beside this bubble.
        ink_level: The page's solid-ink level, from :func:`estimate_ink_level`.
        config: Supplies the fraction and its bounds.

    Returns:
        ``paper_level`` minus a distance that is ``ink_fraction`` of the way to
        ``ink_level``, clamped into
        ``[min_ink_margin, max_ink_margin]``. Clamping is what keeps the rule
        honest at both extremes: a page with no ink on it cannot lower the bar
        to nothing, and a very black page cannot raise it out of reach.
    """
    span = max(paper_level - ink_level, 0.0)
    margin = min(max(config.ink_fraction * span, config.min_ink_margin), config.max_ink_margin)
    return paper_level - margin


def _validate_page(image: NDArray[np.uint8]) -> None:
    """Reject an array that is not a single-channel 8-bit page."""
    if image.ndim != 2:
        raise ImageValidationError(
            f"Bubble metrics need a single-channel image, got shape {image.shape}",
            user_message="The rectified page could not be measured.",
        )
    if image.dtype != np.uint8:
        raise ImageValidationError(
            f"Bubble metrics need an 8-bit image, got dtype {image.dtype}",
            user_message="The rectified page could not be measured.",
        )


def _elliptical_mask(
    height: int, width: int, cx: float, cy: float, rx: float, ry: float
) -> NDArray[np.bool_]:
    """Return a boolean mask of the ellipse ``(cx, cy, rx, ry)`` inside a patch.

    The patch's own origin is ``(0, 0)``; ``cx``/``cy`` are already expressed
    relative to it.
    """
    yy, xx = np.ogrid[:height, :width]
    norm_x = (xx - cx) / max(rx, 1e-6)
    norm_y = (yy - cy) / max(ry, 1e-6)
    return np.asarray(norm_x * norm_x + norm_y * norm_y <= 1.0)


def measure_bubble(
    image: NDArray[np.uint8],
    *,
    center_x: float,
    center_y: float,
    width_px: float,
    height_px: float,
    ink_level: float | None = None,
    config: BubbleMetricsConfig | None = None,
) -> BubbleMeasurement:
    """Measure the ink at one expected bubble position.

    Args:
        image: Grayscale canonical page. Read only.
        center_x: Expected bubble centre, in canonical pixels.
        center_y: Expected bubble centre, in canonical pixels.
        width_px: Printed bubble width in canonical pixels (the full extent, not
            the half-axis).
        height_px: Printed bubble height in canonical pixels.
        ink_level: The page's solid-ink grey level, from
            :func:`estimate_ink_level`. ``None`` assumes pure black, which is
            correct for a well-exposed scan and conservative otherwise;
            :func:`measure_bubbles` measures it from the page instead, and that
            is the entry point recognition uses.
        config: Sampling geometry and thresholds; defaults apply when omitted.

    Returns:
        The measurement. A bubble whose sample is mostly outside the page
        returns ``usable=False`` rather than a misleading zero fill ratio.

    Raises:
        ImageValidationError: ``image`` is not a 2-D 8-bit array.
        ValueError: ``width_px`` or ``height_px`` is not positive.
    """
    settings = config if config is not None else BubbleMetricsConfig()
    _validate_page(image)
    if width_px <= 0.0 or height_px <= 0.0:
        raise ValueError("Bubble width and height must be positive")
    page_ink = 0.0 if ink_level is None else ink_level

    page_height, page_width = image.shape
    half_x = width_px / 2.0
    half_y = height_px / 2.0
    outer_x = half_x * settings.background_outer_ratio
    outer_y = half_y * settings.background_outer_ratio

    # Patch bounds are clamped to the page; the masks below are built in patch
    # coordinates so a clipped patch still measures the part that does exist.
    x0 = max(math.floor(center_x - outer_x) - 1, 0)
    x1 = min(math.ceil(center_x + outer_x) + 2, page_width)
    y0 = max(math.floor(center_y - outer_y) - 1, 0)
    y1 = min(math.ceil(center_y + outer_y) + 2, page_height)
    if x1 <= x0 or y1 <= y0:
        return BubbleMeasurement(
            center_x=center_x,
            center_y=center_y,
            fill_ratio=0.0,
            mean_darkness=0.0,
            paper_level=MAX_GREY,
            contrast=0.0,
            sample_pixels=0,
            usable=False,
        )

    patch = image[y0:y1, x0:x1].astype(np.float32)
    local_cx = center_x - x0
    local_cy = center_y - y0
    patch_h, patch_w = patch.shape

    interior = _elliptical_mask(
        patch_h,
        patch_w,
        local_cx,
        local_cy,
        half_x * settings.sample_radius_ratio,
        half_y * settings.sample_radius_ratio,
    )
    sample_pixels = int(interior.sum())
    if sample_pixels < settings.min_sample_pixels:
        return BubbleMeasurement(
            center_x=center_x,
            center_y=center_y,
            fill_ratio=0.0,
            mean_darkness=0.0,
            paper_level=MAX_GREY,
            contrast=0.0,
            sample_pixels=sample_pixels,
            usable=False,
        )

    outer = _elliptical_mask(patch_h, patch_w, local_cx, local_cy, outer_x, outer_y)
    inner = _elliptical_mask(
        patch_h,
        patch_w,
        local_cx,
        local_cy,
        half_x * settings.background_inner_ratio,
        half_y * settings.background_inner_ratio,
    )
    annulus = outer & ~inner

    values = patch[interior]
    if annulus.any():
        paper_level = float(np.percentile(patch[annulus], settings.paper_percentile))
    else:
        # A bubble at the very edge of the page may have no annulus left. Fall
        # back to the brightest interior pixels, which is the best available
        # estimate of what the paper looks like there.
        paper_level = float(np.percentile(values, settings.paper_percentile))

    threshold = ink_threshold(paper_level, page_ink, settings)
    fill_ratio = float(np.count_nonzero(values < threshold) / values.size)
    mean_value = float(values.mean())

    return BubbleMeasurement(
        center_x=center_x,
        center_y=center_y,
        fill_ratio=fill_ratio,
        mean_darkness=1.0 - mean_value / MAX_GREY,
        paper_level=paper_level,
        contrast=(paper_level - mean_value) / MAX_GREY,
        sample_pixels=sample_pixels,
        usable=True,
    )


def measure_bubbles(
    image: NDArray[np.uint8],
    centers: Sequence[tuple[float, float]] | Iterable[tuple[float, float]],
    *,
    width_px: float,
    height_px: float,
    ink_level: float | None = None,
    config: BubbleMetricsConfig | None = None,
) -> tuple[BubbleMeasurement, ...]:
    """Measure a group of bubbles that share one printed size.

    Args:
        image: Grayscale canonical page. Read only.
        centers: ``(x, y)`` centres in canonical pixels, in the caller's own
            order - the returned tuple preserves it, so index ``i`` of the
            result always describes ``centers[i]``.
        width_px: Printed bubble width in canonical pixels.
        height_px: Printed bubble height in canonical pixels.
        ink_level: The page's solid-ink grey level. Measured from ``image`` when
            omitted; a caller measuring many zones of the same page should
            compute it once with :func:`estimate_ink_level` and pass it in,
            since the estimate is a property of the page rather than of a zone.
        config: Sampling geometry and thresholds.

    Returns:
        One measurement per centre, in the same order.
    """
    settings = config if config is not None else BubbleMetricsConfig()
    page_ink = estimate_ink_level(image, config=settings) if ink_level is None else ink_level
    return tuple(
        measure_bubble(
            image,
            center_x=x,
            center_y=y,
            width_px=width_px,
            height_px=height_px,
            ink_level=page_ink,
            config=settings,
        )
        for x, y in centers
    )


__all__ = [
    "MAX_GREY",
    "BubbleMeasurement",
    "BubbleMetricsConfig",
    "estimate_ink_level",
    "ink_threshold",
    "measure_bubble",
    "measure_bubbles",
]

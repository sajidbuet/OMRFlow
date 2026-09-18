"""Tests for per-bubble ink measurement.

These build tiny synthetic pages in memory rather than loading fixtures, so each
test states exactly the one optical condition it is about: an empty bubble with
a printed glyph in it, an uneven scan, a bubble half off the page.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from omr_scanner.errors import ImageValidationError
from omr_scanner.imaging.metrics import (
    BubbleMetricsConfig,
    estimate_ink_level,
    ink_threshold,
    measure_bubble,
    measure_bubbles,
)

PAPER = 245
"""Scanned white paper is never 255."""

GLYPH_GREY = 150
"""The light grey an option symbol is printed in inside an empty bubble."""

INK = 30
"""A pencil or pen mark."""

BUBBLE = 36
"""Printed bubble diameter in canonical pixels, matching the real sample sheet."""


def blank_page(width: int = 200, height: int = 200, level: int = PAPER) -> np.ndarray:
    return np.full((height, width), level, dtype=np.uint8)


def draw_bubble_outline(page: np.ndarray, cx: int, cy: int, *, diameter: int = BUBBLE) -> None:
    """Draw the printed ring that every bubble has, marked or not."""
    cv2.circle(page, (cx, cy), diameter // 2, int(INK), thickness=2)


def draw_mark(
    page: np.ndarray, cx: int, cy: int, *, coverage: float, diameter: int = BUBBLE
) -> None:
    """Shade ``coverage`` of the sampled interior solid.

    Area scales with the square of the radius, so covering a fraction of the
    sample disc needs a radius of ``sqrt(coverage)`` times it.
    """
    sample_radius = diameter / 2 * BubbleMetricsConfig().sample_radius_ratio
    radius = max(1, round(sample_radius * coverage**0.5))
    cv2.circle(page, (cx, cy), radius, int(INK), thickness=cv2.FILLED)


class TestValidation:
    def test_a_colour_image_is_refused(self):
        page = np.zeros((50, 50, 3), dtype=np.uint8)
        with pytest.raises(ImageValidationError, match="single-channel"):
            measure_bubble(page, center_x=25, center_y=25, width_px=20, height_px=20)

    def test_a_float_image_is_refused(self):
        page = np.zeros((50, 50), dtype=np.float32)
        with pytest.raises(ImageValidationError, match="8-bit"):
            measure_bubble(page, center_x=25, center_y=25, width_px=20, height_px=20)

    @pytest.mark.parametrize(("width", "height"), [(0, 20), (20, 0), (-5, 20)])
    def test_a_non_positive_bubble_size_is_refused(self, width, height):
        with pytest.raises(ValueError, match="must be positive"):
            measure_bubble(
                blank_page(), center_x=25, center_y=25, width_px=width, height_px=height
            )


class TestReportedSampleGeometry:
    """The sampled region must travel with the measurement.

    Anything that draws "what recognition measured" - the Phase 4 calibration
    overlay above all - has to read the ellipse that was actually sampled. It
    is deliberately smaller than the printed bubble, so drawing the printed
    size and calling it the sampling window would show an operator a region
    the engine never looked at while appearing entirely convincing.
    """

    def test_the_sampled_half_axes_are_the_configured_fraction_of_the_printed_ones(self):
        config = BubbleMetricsConfig()
        result = measure_bubble(
            blank_page(), center_x=100, center_y=100, width_px=BUBBLE, height_px=24,
            config=config,
        )
        assert result.sample_half_width == pytest.approx(
            BUBBLE / 2 * config.sample_radius_ratio
        )
        assert result.sample_half_height == pytest.approx(
            24 / 2 * config.sample_radius_ratio
        )

    def test_the_sampled_region_is_strictly_smaller_than_the_printed_bubble(self):
        result = measure_bubble(
            blank_page(), center_x=100, center_y=100, width_px=BUBBLE, height_px=BUBBLE
        )
        assert 0.0 < result.sample_half_width < BUBBLE / 2
        assert 0.0 < result.sample_half_height < BUBBLE / 2

    def test_a_custom_sample_ratio_is_reflected_in_what_is_reported(self):
        config = BubbleMetricsConfig(sample_radius_ratio=0.5)
        result = measure_bubble(
            blank_page(), center_x=100, center_y=100, width_px=BUBBLE, height_px=BUBBLE,
            config=config,
        )
        assert result.sample_half_width == pytest.approx(BUBBLE / 2 * 0.5)

    def test_an_unusable_measurement_still_reports_where_it_tried_to_sample(self):
        # "The window that could not be measured" is precisely what a
        # calibration overlay needs to show for an off-page bubble.
        page = blank_page(width=200, height=200)
        result = measure_bubble(
            page, center_x=-400.0, center_y=-400.0, width_px=BUBBLE, height_px=BUBBLE
        )
        assert result.usable is False
        assert result.sample_half_width > 0.0
        assert result.sample_half_height > 0.0


class TestFillRatio:
    def test_a_bare_printed_ring_reads_as_empty(self):
        page = blank_page()
        draw_bubble_outline(page, 100, 100)
        result = measure_bubble(
            page, center_x=100, center_y=100, width_px=BUBBLE, height_px=BUBBLE,
            ink_level=float(INK),
        )
        assert result.usable
        assert result.fill_ratio == pytest.approx(0.0, abs=0.02)

    def test_a_printed_glyph_inside_an_empty_bubble_does_not_read_as_a_mark(self):
        # The defect this rule exists for: with a fixed grey-level margin the
        # light grey symbol printed inside an untouched bubble measured as more
        # than half filled.
        page = blank_page()
        draw_bubble_outline(page, 100, 100)
        cv2.putText(
            page, "B", (92, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.6, int(GLYPH_GREY), 2
        )
        # Ink level as the page would report it with a mark elsewhere on it.
        result = measure_bubble(
            page, center_x=100, center_y=100, width_px=BUBBLE, height_px=BUBBLE,
            ink_level=float(INK),
        )
        assert result.fill_ratio < 0.25, "a printed symbol must not look like a mark"

    def test_a_fully_shaded_bubble_reads_as_completely_filled(self):
        page = blank_page()
        draw_bubble_outline(page, 100, 100)
        draw_mark(page, 100, 100, coverage=1.0)
        result = measure_bubble(
            page, center_x=100, center_y=100, width_px=BUBBLE, height_px=BUBBLE,
            ink_level=float(INK),
        )
        assert result.fill_ratio > 0.95

    def test_fill_ratio_rises_with_the_shaded_area(self):
        ratios = []
        for coverage in (0.0, 0.25, 0.5, 0.75, 1.0):
            page = blank_page()
            draw_bubble_outline(page, 100, 100)
            if coverage:
                draw_mark(page, 100, 100, coverage=coverage)
            ratios.append(
                measure_bubble(
                    page, center_x=100, center_y=100, width_px=BUBBLE, height_px=BUBBLE,
                    ink_level=float(INK),
                ).fill_ratio
            )
        assert ratios == sorted(ratios)
        assert ratios[0] < 0.05
        assert ratios[-1] > 0.95

    def test_fill_ratio_never_leaves_the_unit_interval(self):
        page = blank_page(level=INK)  # an entirely black page
        result = measure_bubble(
            page, center_x=100, center_y=100, width_px=BUBBLE, height_px=BUBBLE,
            ink_level=float(INK),
        )
        assert 0.0 <= result.fill_ratio <= 1.0


class TestLocalPaperEstimate:
    def test_the_same_mark_measures_the_same_in_a_dark_and_a_bright_region(self):
        # A page with a lighting gradient: the left half is notably darker.
        page = blank_page(width=400)
        page[:, :200] = 190
        for cx in (100, 300):
            draw_bubble_outline(page, cx, 100)
            draw_mark(page, cx, 100, coverage=0.8)

        dark_side, bright_side = measure_bubbles(
            page, [(100, 100), (300, 100)], width_px=BUBBLE, height_px=BUBBLE,
            ink_level=float(INK),
        )
        assert dark_side.paper_level < bright_side.paper_level
        assert dark_side.fill_ratio == pytest.approx(bright_side.fill_ratio, abs=0.05)

    def test_an_empty_bubble_in_a_shaded_region_is_still_empty(self):
        page = blank_page()
        page[:, :] = 185  # a whole page scanned dark
        draw_bubble_outline(page, 100, 100)
        result = measure_bubble(
            page, center_x=100, center_y=100, width_px=BUBBLE, height_px=BUBBLE,
            ink_level=float(INK),
        )
        assert result.fill_ratio < 0.05


class TestUsability:
    def test_a_bubble_well_off_the_page_is_unusable(self):
        page = blank_page()
        result = measure_bubble(
            page, center_x=-80, center_y=-80, width_px=BUBBLE, height_px=BUBBLE
        )
        assert result.usable is False
        assert result.fill_ratio == 0.0

    def test_a_bubble_partly_off_the_page_measures_what_exists(self):
        page = blank_page()
        draw_mark(page, 4, 100, coverage=1.0)
        result = measure_bubble(
            page, center_x=4, center_y=100, width_px=BUBBLE, height_px=BUBBLE,
            ink_level=float(INK),
        )
        assert result.usable is True
        assert result.sample_pixels >= BubbleMetricsConfig().min_sample_pixels

    def test_an_unusable_measurement_reports_zeroes_that_must_not_read_as_blank(self):
        page = blank_page()
        result = measure_bubble(
            page, center_x=1000, center_y=1000, width_px=BUBBLE, height_px=BUBBLE
        )
        assert result.usable is False
        assert (result.fill_ratio, result.mean_darkness, result.contrast) == (0.0, 0.0, 0.0)


class TestDerivedMetrics:
    def test_mean_darkness_separates_two_bubbles_that_are_both_fully_filled(self):
        # `fill_ratio` saturates at 1.0 for both; `mean_darkness` does not.
        results = []
        for level in (120, 20):
            page = blank_page()
            cv2.circle(page, (100, 100), BUBBLE // 2, int(level), thickness=cv2.FILLED)
            results.append(
                measure_bubble(
                    page, center_x=100, center_y=100, width_px=BUBBLE, height_px=BUBBLE,
                    ink_level=20.0,
                )
            )
        assert results[1].mean_darkness > results[0].mean_darkness

    def test_contrast_is_negative_when_the_bubble_is_brighter_than_its_surroundings(self):
        page = blank_page(level=120)
        cv2.circle(page, (100, 100), BUBBLE, 250, thickness=cv2.FILLED)
        result = measure_bubble(
            page, center_x=100, center_y=100, width_px=BUBBLE, height_px=BUBBLE,
            ink_level=20.0,
        )
        assert result.contrast <= 0.0

    def test_the_sampled_centre_is_reported_back_unchanged(self):
        result = measure_bubble(
            blank_page(), center_x=71.5, center_y=33.25, width_px=BUBBLE, height_px=BUBBLE
        )
        assert (result.center_x, result.center_y) == (71.5, 33.25)


class TestInkLevelEstimate:
    def test_a_page_with_ink_on_it_reports_a_dark_ink_level(self):
        page = blank_page()
        cv2.rectangle(page, (10, 10), (60, 60), int(INK), thickness=cv2.FILLED)
        assert estimate_ink_level(page) < 100

    def test_an_utterly_blank_page_reports_paper_as_its_darkest_ink(self):
        assert estimate_ink_level(blank_page()) == pytest.approx(PAPER)

    def test_the_threshold_is_clamped_so_a_blank_page_cannot_lower_the_bar(self):
        config = BubbleMetricsConfig()
        # Paper and "ink" identical: without the floor the threshold would sit
        # at the paper level itself and every speck of noise would be a mark.
        assert ink_threshold(PAPER, PAPER, config) == PAPER - config.min_ink_margin

    def test_the_threshold_is_clamped_so_a_very_black_scan_cannot_raise_it(self):
        # The ceiling needs a fraction high enough to reach it: at the default
        # 0.5 the largest possible margin is half of 255, already under 130.
        config = BubbleMetricsConfig(ink_fraction=0.9)
        assert ink_threshold(255.0, 0.0, config) == 255.0 - config.max_ink_margin

    def test_at_the_default_fraction_the_ceiling_is_out_of_reach(self):
        config = BubbleMetricsConfig()
        assert config.max_ink_margin > 0.5 * 255.0

    def test_between_the_clamps_the_threshold_sits_at_the_configured_fraction(self):
        config = BubbleMetricsConfig(ink_fraction=0.5)
        # Span 200, half of it is 100, which lies inside [35, 130].
        assert ink_threshold(240.0, 40.0, config) == pytest.approx(140.0)

    def test_measure_bubbles_estimates_the_ink_level_when_not_given_one(self):
        page = blank_page()
        cv2.rectangle(page, (5, 5), (40, 40), int(INK), thickness=cv2.FILLED)
        draw_bubble_outline(page, 100, 100)
        draw_mark(page, 100, 100, coverage=1.0)
        draw_bubble_outline(page, 150, 100)

        marked, empty = measure_bubbles(
            page, [(100, 100), (150, 100)], width_px=BUBBLE, height_px=BUBBLE
        )
        assert marked.fill_ratio > 0.95
        assert empty.fill_ratio < 0.05


class TestConfigValidation:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"sample_radius_ratio": 0.0}, "sample_radius_ratio"),
            ({"sample_radius_ratio": 1.5}, "sample_radius_ratio"),
            ({"background_inner_ratio": 0.9}, "greater than 1"),
            ({"background_outer_ratio": 1.0}, "must exceed"),
            ({"paper_percentile": 101.0}, "paper_percentile"),
            ({"ink_percentile": -1.0}, "ink_percentile"),
            ({"ink_fraction": 0.0}, "ink_fraction"),
            ({"ink_fraction": 1.0}, "ink_fraction"),
            ({"min_ink_margin": 0.0}, "min_ink_margin"),
            ({"max_ink_margin": 1.0}, "max_ink_margin"),
            ({"min_sample_pixels": 0}, "min_sample_pixels"),
        ],
    )
    def test_an_unusable_configuration_is_refused_at_construction(self, kwargs, message):
        with pytest.raises(ValueError, match=message):
            BubbleMetricsConfig(**kwargs)


class TestMeasureBubbles:
    def test_results_are_returned_in_the_caller_s_order(self):
        page = blank_page(width=400)
        for index, cx in enumerate((300, 100, 200)):
            draw_bubble_outline(page, cx, 100)
            if index == 0:
                draw_mark(page, cx, 100, coverage=1.0)

        results = measure_bubbles(
            page, [(300, 100), (100, 100), (200, 100)],
            width_px=BUBBLE, height_px=BUBBLE, ink_level=float(INK),
        )
        assert [round(r.center_x) for r in results] == [300, 100, 200]
        assert results[0].fill_ratio > 0.95
        assert results[1].fill_ratio < 0.05

    def test_no_centres_measures_nothing_without_erroring(self):
        assert measure_bubbles(blank_page(), [], width_px=BUBBLE, height_px=BUBBLE) == ()

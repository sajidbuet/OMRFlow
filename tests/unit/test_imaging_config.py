"""Alignment configuration: validation and the quantities derived from it.

The configuration is where every threshold in the engine lives, so these tests
cover two things: that an impossible configuration is refused at construction
rather than halfway through a batch, and that the derived bounds the detector
actually compares against are the ones the documented formulae produce.
"""

from __future__ import annotations

import pytest

from omr_scanner.domain.geometry import NormalizedPoint
from omr_scanner.domain.template import MarkerRole
from omr_scanner.imaging.config import (
    DEFAULT_MARKER_TARGETS,
    AlignmentConfig,
    GeometryConfig,
    MarkerDetectionConfig,
    OrientationConfig,
    PreprocessingConfig,
    ThresholdStrategy,
)
from omr_scanner.imaging.models import CANONICAL_CORNER_ORDER


class TestPreprocessingConfig:
    def test_defaults_are_valid(self):
        assert PreprocessingConfig().threshold_strategy is ThresholdStrategy.OTSU

    def test_an_even_blur_kernel_is_rejected(self):
        with pytest.raises(ValueError, match="must be odd"):
            PreprocessingConfig(blur_kernel_px=4)

    def test_blurring_can_be_disabled(self):
        assert PreprocessingConfig(blur_kernel_px=0).blur_kernel_px == 0

    def test_a_negative_blur_kernel_is_rejected(self):
        with pytest.raises(ValueError, match="zero or positive"):
            PreprocessingConfig(blur_kernel_px=-3)

    def test_a_working_size_below_the_minimum_image_size_is_rejected(self):
        with pytest.raises(ValueError, match="working_max_dimension_px"):
            PreprocessingConfig(min_image_dimension_px=200, working_max_dimension_px=100)

    def test_an_out_of_range_adaptive_block_ratio_is_rejected(self):
        with pytest.raises(ValueError, match="adaptive_block_ratio"):
            PreprocessingConfig(adaptive_block_ratio=1.5)


class TestMarkerDetectionConfig:
    def test_a_tolerance_of_one_is_rejected(self):
        # A tolerance of exactly 1 would accept only the exact expected area,
        # which no measured contour ever has.
        with pytest.raises(ValueError, match="marker_area_tolerance"):
            MarkerDetectionConfig(marker_area_tolerance=1.0)

    def test_an_aspect_tolerance_of_one_is_rejected(self):
        with pytest.raises(ValueError, match="marker_aspect_tolerance"):
            MarkerDetectionConfig(marker_aspect_tolerance=0.9)

    def test_an_impossible_marker_size_is_rejected(self):
        with pytest.raises(ValueError, match="expected_marker_width"):
            MarkerDetectionConfig(expected_marker_width=0.0)

    def test_an_oversized_search_region_is_rejected(self):
        with pytest.raises(ValueError, match="corner_search_height"):
            MarkerDetectionConfig(corner_search_height=1.4)

    def test_zero_weights_are_rejected(self):
        with pytest.raises(ValueError, match="At least one score weight"):
            MarkerDetectionConfig(shape_weight=0.0, proximity_weight=0.0)

    def test_a_negative_weight_is_rejected(self):
        with pytest.raises(ValueError, match="must not be negative"):
            MarkerDetectionConfig(proximity_weight=-1.0)

    def test_no_candidates_per_corner_is_rejected(self):
        with pytest.raises(ValueError, match="max_candidates_per_corner"):
            MarkerDetectionConfig(max_candidates_per_corner=0)


class TestOrientationConfig:
    def test_a_window_margin_below_one_is_rejected(self):
        with pytest.raises(ValueError, match="window_margin"):
            OrientationConfig(window_margin=0.8)

    def test_a_mark_off_the_page_is_rejected(self):
        with pytest.raises(ValueError, match="must lie on the page"):
            OrientationConfig(marker_center_x=1.4)

    def test_an_impossible_search_radius_is_rejected(self):
        with pytest.raises(ValueError, match="search_radius"):
            OrientationConfig(search_radius=0.8)

    def test_an_impossible_fallback_is_rejected(self):
        with pytest.raises(ValueError, match="fallback_quarter_turns"):
            OrientationConfig(fallback_quarter_turns=5)

    def test_expected_window_fill_is_the_inverse_square_of_the_margin(self):
        assert OrientationConfig(window_margin=2.0).expected_window_fill == pytest.approx(0.25)

    def test_the_fallback_is_off_by_default(self):
        # An undetected upside-down sheet produces a complete, confident and
        # wrong set of answers; guessing must be an explicit choice.
        assert OrientationConfig().allow_fallback is False


class TestGeometryConfig:
    def test_an_impossible_area_ratio_is_rejected(self):
        with pytest.raises(ValueError, match="min_quadrilateral_area_ratio"):
            GeometryConfig(min_quadrilateral_area_ratio=0.0)

    def test_an_impossible_separation_ratio_is_rejected(self):
        with pytest.raises(ValueError, match="min_marker_separation_ratio"):
            GeometryConfig(min_marker_separation_ratio=1.0)

    def test_a_non_positive_aspect_tolerance_is_rejected(self):
        with pytest.raises(ValueError, match="max_aspect_ratio_deviation"):
            GeometryConfig(max_aspect_ratio_deviation=0.0)

    def test_a_non_positive_reprojection_limit_is_rejected(self):
        with pytest.raises(ValueError, match="max_reprojection_error_px"):
            GeometryConfig(max_reprojection_error_px=0.0)


class TestAlignmentConfig:
    def test_defaults_describe_the_shipped_example_sheet(self):
        config = AlignmentConfig()
        assert config.canonical_size == (1240, 1754)
        assert config.marker_targets == DEFAULT_MARKER_TARGETS

    def test_a_zero_sized_canonical_page_is_rejected(self):
        with pytest.raises(ValueError, match="Canonical page dimensions"):
            AlignmentConfig(canonical_width=0)

    def test_a_missing_corner_role_is_rejected(self):
        partial = {
            role: DEFAULT_MARKER_TARGETS[role] for role in CANONICAL_CORNER_ORDER[:3]
        }
        with pytest.raises(ValueError, match="missing corner role"):
            AlignmentConfig(marker_targets=partial)

    def test_normalized_coordinates_convert_to_canonical_pixels(self):
        config = AlignmentConfig(canonical_width=1240, canonical_height=1754)
        point = config.normalized_to_canonical(NormalizedPoint(x=0.035, y=0.025))
        assert point.x == pytest.approx(0.035 * 1240)
        assert point.y == pytest.approx(0.025 * 1754)

    def test_canonical_marker_points_follow_the_canonical_corner_order(self):
        config = AlignmentConfig()
        points = config.canonical_marker_points()
        assert len(points) == 4
        assert points[0].x < points[1].x  # top-left is left of top-right
        assert points[0].y < points[3].y  # top-left is above bottom-left
        assert points[2].x == pytest.approx(points[1].x)  # bottom-right below top-right

    def test_canonical_marker_points_scale_with_the_page(self):
        small = AlignmentConfig(canonical_width=620, canonical_height=877)
        large = AlignmentConfig(canonical_width=2480, canonical_height=3508)
        for a, b in zip(
            small.canonical_marker_points(), large.canonical_marker_points(), strict=True
        ):
            assert b.x == pytest.approx(a.x * 4.0)
            assert b.y == pytest.approx(a.y * 4.0)

    def test_expected_marker_area_ratio_is_the_product_of_the_normalised_sides(self):
        config = AlignmentConfig()
        assert config.expected_marker_area_ratio == pytest.approx(0.03 * 0.021)

    def test_marker_area_bounds_bracket_the_expected_ratio(self):
        config = AlignmentConfig()
        minimum, maximum = config.marker_area_ratio_bounds
        expected = config.expected_marker_area_ratio
        tolerance = config.marker_detection.marker_area_tolerance
        assert minimum == pytest.approx(expected / tolerance)
        assert maximum == pytest.approx(expected * tolerance)

    def test_marker_aspect_ratio_is_the_longer_side_over_the_shorter(self):
        config = AlignmentConfig(canonical_width=1000, canonical_height=1000)
        wide = AlignmentConfig(
            canonical_width=1000,
            canonical_height=1000,
            marker_detection=MarkerDetectionConfig(
                expected_marker_width=0.04, expected_marker_height=0.01
            ),
        )
        assert config.expected_marker_aspect_ratio == pytest.approx(0.03 / 0.021)
        assert wide.expected_marker_aspect_ratio == pytest.approx(4.0)

    def test_the_marker_aspect_lower_bound_never_falls_below_one(self):
        # A measured aspect ratio is longer-over-shorter, so it cannot be < 1;
        # a lower bound below 1 would be unreachable and therefore meaningless.
        minimum, _ = AlignmentConfig().marker_aspect_ratio_bounds
        assert minimum >= 1.0

    def test_the_expected_quadrilateral_aspect_uses_the_marker_rectangle(self):
        config = AlignmentConfig()
        markers = config.canonical_marker_points()
        width = markers[1].x - markers[0].x
        height = markers[3].y - markers[0].y
        assert config.expected_quadrilateral_aspect_ratio == pytest.approx(width / height)
        # Not the page aspect ratio: the markers sit inside the printed margins.
        page_aspect = config.canonical_width / config.canonical_height
        assert config.expected_quadrilateral_aspect_ratio != pytest.approx(page_aspect)

    def test_a_template_style_marker_layout_is_accepted(self):
        targets = {
            MarkerRole.TOP_LEFT: NormalizedPoint(x=0.03, y=0.03),
            MarkerRole.TOP_RIGHT: NormalizedPoint(x=0.97, y=0.03),
            MarkerRole.BOTTOM_RIGHT: NormalizedPoint(x=0.97, y=0.97),
            MarkerRole.BOTTOM_LEFT: NormalizedPoint(x=0.03, y=0.97),
        }
        config = AlignmentConfig(marker_targets=targets, canonical_width=1000,
                                 canonical_height=1000)
        assert config.expected_quadrilateral_aspect_ratio == pytest.approx(1.0)

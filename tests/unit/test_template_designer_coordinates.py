"""Tests for `CoordinateMapper` and `snap`.

The one place pixel/normalised arithmetic happens for the template designer.
"""

from __future__ import annotations

import pytest

from omr_scanner.gui.template_designer.coordinates import CoordinateMapper, snap


class TestCoordinateMapper:
    def test_construction_rejects_non_positive_dimensions(self):
        with pytest.raises(ValueError, match="positive"):
            CoordinateMapper(image_width=0, image_height=100)
        with pytest.raises(ValueError, match="positive"):
            CoordinateMapper(image_width=100, image_height=-1)

    def test_to_normalized_divides_by_image_size(self):
        mapper = CoordinateMapper(image_width=1000, image_height=2000)
        assert mapper.to_normalized(500, 1000) == pytest.approx((0.5, 0.5))

    def test_to_pixels_multiplies_by_image_size(self):
        mapper = CoordinateMapper(image_width=1000, image_height=2000)
        assert mapper.to_pixels(0.25, 0.1) == pytest.approx((250.0, 200.0))

    def test_to_pixels_and_to_normalized_are_inverses(self):
        mapper = CoordinateMapper(image_width=1240, image_height=1754)
        original = (0.3456, 0.7891)
        pixels = mapper.to_pixels(*original)
        back = mapper.to_normalized(*pixels)
        assert back == pytest.approx(original)

    def test_size_conversions_do_not_use_an_origin(self):
        mapper = CoordinateMapper(image_width=1000, image_height=1000)
        # A size at any position converts identically - it is not a point.
        assert mapper.size_to_normalized(100, 50) == mapper.size_to_normalized(100, 50)
        assert mapper.size_to_pixels(0.1, 0.05) == pytest.approx((100.0, 50.0))

    def test_rect_conversions_round_trip(self):
        mapper = CoordinateMapper(image_width=1240, image_height=1754)
        original = (0.1, 0.2, 0.3, 0.4)
        pixels = mapper.rect_to_pixels(*original)
        back = mapper.rect_to_normalized(*pixels)
        assert back == pytest.approx(original)

    @pytest.mark.parametrize(
        ("value", "expected"), [(-0.5, 0.0), (0.0, 0.0), (0.5, 0.5), (1.0, 1.0), (1.5, 1.0)]
    )
    def test_clamp_normalized_bounds_to_the_unit_range(self, value, expected):
        mapper = CoordinateMapper(image_width=100, image_height=100)
        assert mapper.clamp_normalized(value) == expected


class TestSnap:
    def test_snapping_rounds_to_the_nearest_grid_line(self):
        assert snap(0.123, grid_size=0.05) == pytest.approx(0.10)
        assert snap(0.137, grid_size=0.05) == pytest.approx(0.15)

    def test_a_value_already_on_the_grid_is_unchanged(self):
        assert snap(0.20, grid_size=0.05) == pytest.approx(0.20)

    def test_a_non_positive_grid_size_disables_snapping(self):
        assert snap(0.1234, grid_size=0.0) == pytest.approx(0.1234)
        assert snap(0.1234, grid_size=-1.0) == pytest.approx(0.1234)

    def test_snapping_does_not_clamp(self):
        # Snapping and clamping are independent; a value near 1.0 can snap
        # to a grid line past it, and it is the caller's job to clamp after.
        assert snap(0.98, grid_size=0.05) == pytest.approx(1.0)

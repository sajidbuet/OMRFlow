"""Point ordering, quadrilateral validation and homography arithmetic.

These are the pieces of the alignment engine that can be tested exactly, with
coordinates written by hand rather than measured from pixels. Every rotated,
sheared and perspective-distorted case here is constructed analytically, so a
failure names a geometric mistake rather than a detection one.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from omr_scanner.errors import AlignmentTransformError, InvalidPageGeometryError
from omr_scanner.imaging.config import GeometryConfig
from omr_scanner.imaging.geometry import (
    apply_transform,
    centroid,
    invert_transform,
    is_convex,
    is_simple_quadrilateral,
    order_clockwise,
    pairwise_distances,
    perspective_transform,
    polygon_area,
    quadrilateral_aspect_ratio,
    reprojection_errors,
    rotate_corner_order,
    signed_area,
    validate_page_quadrilateral,
)
from omr_scanner.imaging.models import Point

UPRIGHT = (
    Point(x=100.0, y=200.0),
    Point(x=900.0, y=200.0),
    Point(x=900.0, y=1300.0),
    Point(x=100.0, y=1300.0),
)
"""A plain upright rectangle in canonical corner order: TL, TR, BR, BL."""

EXACT_MAP_TOLERANCE_PX = 1e-3
"""How closely a four-point homography reproduces its own correspondences.

Not zero: ``cv2.getPerspectiveTransform`` takes single-precision corner arrays,
so on coordinates of order 1000 px the residual is a few parts in 10^6. A
thousandth of a pixel is four orders of magnitude below anything that could
affect a bubble measurement.
"""


def rotated(points, degrees: float, about: Point | None = None):
    """Rotate points clockwise about ``about`` (their centroid by default)."""
    pivot = about if about is not None else centroid(points)
    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    return tuple(
        Point(
            x=pivot.x + (point.x - pivot.x) * cos - (point.y - pivot.y) * sin,
            y=pivot.y + (point.x - pivot.x) * sin + (point.y - pivot.y) * cos,
        )
        for point in points
    )


class TestOrderClockwise:
    def test_an_upright_rectangle_keeps_its_order(self):
        assert order_clockwise(UPRIGHT) == UPRIGHT

    @pytest.mark.parametrize("shuffle", [(1, 2, 3, 0), (2, 0, 3, 1), (3, 2, 1, 0)])
    def test_order_is_recovered_whatever_the_input_order(self, shuffle):
        scrambled = tuple(UPRIGHT[index] for index in shuffle)
        assert order_clockwise(scrambled) == UPRIGHT

    @pytest.mark.parametrize("degrees", [-30.0, -15.0, -5.0, 0.0, 5.0, 15.0, 30.0])
    def test_a_rotated_rectangle_is_ordered_consistently(self, degrees):
        turned = rotated(UPRIGHT, degrees)
        scrambled = (turned[2], turned[0], turned[3], turned[1])
        assert order_clockwise(scrambled) == turned

    def test_a_translated_rectangle_is_ordered_the_same_way(self):
        moved = tuple(Point(x=p.x + 4000.0, y=p.y - 150.0) for p in UPRIGHT)
        assert order_clockwise((moved[3], moved[1], moved[2], moved[0])) == moved

    def test_a_perspective_distorted_quadrilateral_is_ordered_correctly(self):
        # The top edge is shorter than the bottom edge, as when a page leans away
        # from the platen. Sorting by raw coordinates is unreliable here; sorting
        # by angle about the centroid is not.
        distorted = (
            Point(x=260.0, y=190.0),
            Point(x=760.0, y=205.0),
            Point(x=930.0, y=1320.0),
            Point(x=70.0, y=1290.0),
        )
        scrambled = (distorted[1], distorted[3], distorted[0], distorted[2])
        assert order_clockwise(scrambled) == distorted

    def test_a_narrow_quadrilateral_is_ordered_correctly(self):
        narrow = (
            Point(x=500.0, y=100.0),
            Point(x=560.0, y=100.0),
            Point(x=560.0, y=1600.0),
            Point(x=500.0, y=1600.0),
        )
        assert order_clockwise((narrow[2], narrow[3], narrow[0], narrow[1])) == narrow

    def test_wrong_point_count_is_rejected(self):
        with pytest.raises(ValueError, match="Expected 4 points"):
            order_clockwise(UPRIGHT[:3])

    def test_a_vertex_on_the_centroid_is_rejected(self):
        degenerate = (
            Point(x=0.0, y=0.0),
            Point(x=2.0, y=0.0),
            Point(x=1.0, y=0.0),
            Point(x=1.0, y=0.0),
        )
        with pytest.raises(ValueError, match="coincides with the centroid"):
            order_clockwise(degenerate)


class TestRotateCornerOrder:
    @pytest.mark.parametrize(
        ("turns", "expected_first"), [(0, 0), (1, 1), (2, 2), (3, 3)]
    )
    def test_the_sequence_starts_at_the_requested_index(self, turns, expected_first):
        assert rotate_corner_order(UPRIGHT, turns)[0] == UPRIGHT[expected_first]

    def test_the_cyclic_order_is_preserved(self):
        turned = rotate_corner_order(UPRIGHT, 3)
        assert turned == (UPRIGHT[3], UPRIGHT[0], UPRIGHT[1], UPRIGHT[2])

    def test_an_impossible_number_of_turns_is_rejected(self):
        with pytest.raises(ValueError, match="quarter_turns"):
            rotate_corner_order(UPRIGHT, 4)


class TestMeasurement:
    def test_clockwise_order_has_positive_signed_area(self):
        # y grows downward, so the visually clockwise order is the positive one.
        assert signed_area(UPRIGHT) > 0.0

    def test_area_matches_width_times_height(self):
        assert polygon_area(UPRIGHT) == pytest.approx(800.0 * 1100.0)

    def test_area_is_unchanged_by_rotation(self):
        assert polygon_area(rotated(UPRIGHT, 23.0)) == pytest.approx(800.0 * 1100.0)

    def test_aspect_ratio_is_width_over_height(self):
        assert quadrilateral_aspect_ratio(UPRIGHT) == pytest.approx(800.0 / 1100.0)

    def test_aspect_ratio_survives_rotation(self):
        assert quadrilateral_aspect_ratio(rotated(UPRIGHT, 12.0)) == pytest.approx(
            800.0 / 1100.0
        )

    def test_pairwise_distances_covers_every_pair(self):
        assert len(pairwise_distances(UPRIGHT)) == 6

    def test_centroid_of_a_rectangle_is_its_middle(self):
        assert centroid(UPRIGHT) == Point(x=500.0, y=750.0)

    def test_centroid_of_nothing_is_rejected(self):
        with pytest.raises(ValueError, match="empty point set"):
            centroid([])


class TestConvexityAndSimplicity:
    def test_a_rectangle_is_convex(self):
        assert is_convex(UPRIGHT)

    def test_a_dented_quadrilateral_is_not_convex(self):
        dented = (UPRIGHT[0], UPRIGHT[1], Point(x=400.0, y=700.0), UPRIGHT[3])
        assert not is_convex(dented)

    def test_collinear_vertices_are_not_convex(self):
        collinear = (
            Point(x=0.0, y=0.0),
            Point(x=100.0, y=0.0),
            Point(x=200.0, y=0.0),
            Point(x=100.0, y=100.0),
        )
        assert not is_convex(collinear)

    def test_a_rectangle_is_simple(self):
        assert is_simple_quadrilateral(UPRIGHT)

    def test_a_bow_tie_is_not_simple(self):
        bow_tie = (UPRIGHT[0], UPRIGHT[1], UPRIGHT[3], UPRIGHT[2])
        assert not is_simple_quadrilateral(bow_tie)

    def test_a_three_point_sequence_is_not_a_quadrilateral(self):
        assert not is_simple_quadrilateral(UPRIGHT[:3])


class TestValidatePageQuadrilateral:
    config = GeometryConfig()
    image_area = 1200.0 * 1600.0
    expected_aspect = 800.0 / 1100.0

    def validate(self, points, **overrides: object):
        return validate_page_quadrilateral(
            points,
            image_area_px=overrides.pop("image_area_px", self.image_area),
            expected_aspect_ratio=overrides.pop("expected_aspect_ratio", self.expected_aspect),
            config=overrides.pop("config", self.config),
        )

    def test_a_plausible_page_is_accepted_and_reports_no_deviation(self):
        assert self.validate(UPRIGHT) == pytest.approx(0.0)

    def test_a_slightly_skewed_page_is_accepted(self):
        skewed = (
            Point(x=110.0, y=195.0),
            Point(x=905.0, y=215.0),
            Point(x=895.0, y=1305.0),
            Point(x=95.0, y=1290.0),
        )
        assert abs(self.validate(skewed)) < self.config.max_aspect_ratio_deviation

    def test_wrong_point_count_is_rejected(self):
        with pytest.raises(InvalidPageGeometryError, match="4 marker centres"):
            self.validate(UPRIGHT[:3])

    def test_a_non_convex_quadrilateral_is_rejected(self):
        dented = (UPRIGHT[0], UPRIGHT[1], Point(x=400.0, y=700.0), UPRIGHT[3])
        with pytest.raises(InvalidPageGeometryError, match="not convex"):
            self.validate(dented)

    def test_a_bow_tie_is_rejected(self):
        # Ordered so that convexity passes but the outline crosses itself.
        bow_tie = (
            Point(x=100.0, y=200.0),
            Point(x=900.0, y=1300.0),
            Point(x=900.0, y=200.0),
            Point(x=100.0, y=1300.0),
        )
        with pytest.raises(InvalidPageGeometryError, match=r"convex|self-intersecting"):
            self.validate(bow_tie)

    def test_a_quadrilateral_covering_too_little_of_the_scan_is_rejected(self):
        tiny = tuple(Point(x=p.x / 12.0, y=p.y / 12.0) for p in UPRIGHT)
        with pytest.raises(InvalidPageGeometryError, match=r"below the\s+minimum"):
            self.validate(tiny)

    def test_a_nearly_degenerate_quadrilateral_is_rejected(self):
        sliver = (
            Point(x=100.0, y=200.0),
            Point(x=1100.0, y=200.0),
            Point(x=1100.0, y=215.0),
            Point(x=100.0, y=215.0),
        )
        # A small enough image so the sliver passes the relative-area test and
        # the separation test is the one that has to catch it.
        with pytest.raises(InvalidPageGeometryError, match="apart"):
            self.validate(sliver, image_area_px=100_000.0)

    def test_a_page_of_the_wrong_proportions_is_rejected(self):
        # The same rectangle read as landscape: this is what a quarter-turn
        # orientation hypothesis looks like, and why the check prunes them.
        landscape = (
            Point(x=100.0, y=200.0),
            Point(x=1200.0, y=200.0),
            Point(x=1200.0, y=1000.0),
            Point(x=100.0, y=1000.0),
        )
        with pytest.raises(InvalidPageGeometryError, match="aspect ratio"):
            self.validate(landscape)

    def test_zero_image_area_is_rejected(self):
        with pytest.raises(InvalidPageGeometryError, match="Image area"):
            self.validate(UPRIGHT, image_area_px=0.0)


class TestPerspectiveTransform:
    def test_the_four_correspondences_map_exactly(self):
        target = (
            Point(x=0.0, y=0.0),
            Point(x=1240.0, y=0.0),
            Point(x=1240.0, y=1754.0),
            Point(x=0.0, y=1754.0),
        )
        matrix = perspective_transform(UPRIGHT, target)
        for mapped, expected in zip(apply_transform(matrix, UPRIGHT), target, strict=True):
            assert mapped.x == pytest.approx(expected.x, abs=EXACT_MAP_TOLERANCE_PX)
            assert mapped.y == pytest.approx(expected.y, abs=EXACT_MAP_TOLERANCE_PX)

    def test_reprojection_error_of_the_fitted_points_is_negligible(self):
        target = (
            Point(x=40.0, y=60.0),
            Point(x=1200.0, y=60.0),
            Point(x=1200.0, y=1690.0),
            Point(x=40.0, y=1690.0),
        )
        matrix = perspective_transform(UPRIGHT, target)
        assert max(reprojection_errors(matrix, UPRIGHT, target)) < EXACT_MAP_TOLERANCE_PX

    def test_the_inverse_returns_the_original_points(self):
        target = (
            Point(x=0.0, y=0.0),
            Point(x=1240.0, y=0.0),
            Point(x=1240.0, y=1754.0),
            Point(x=0.0, y=1754.0),
        )
        matrix = perspective_transform(UPRIGHT, target)
        inverse = invert_transform(matrix)
        interior = [Point(x=310.0, y=640.0), Point(x=812.5, y=1002.0)]
        forward = apply_transform(matrix, interior)
        back = apply_transform(inverse, forward)
        for original, recovered in zip(interior, back, strict=True):
            assert recovered.x == pytest.approx(original.x, abs=EXACT_MAP_TOLERANCE_PX)
            assert recovered.y == pytest.approx(original.y, abs=EXACT_MAP_TOLERANCE_PX)

    def test_a_rotation_is_inverted_by_the_transform(self):
        turned = rotated(UPRIGHT, 17.0)
        matrix = perspective_transform(turned, UPRIGHT)
        interior_before = Point(x=500.0, y=750.0)
        turned_interior = rotated([interior_before], 17.0, about=centroid(UPRIGHT))[0]
        recovered = apply_transform(matrix, [turned_interior])[0]
        assert recovered.x == pytest.approx(interior_before.x, abs=EXACT_MAP_TOLERANCE_PX)
        assert recovered.y == pytest.approx(interior_before.y, abs=EXACT_MAP_TOLERANCE_PX)

    def test_the_wrong_number_of_correspondences_is_rejected(self):
        with pytest.raises(AlignmentTransformError, match="4 correspondences"):
            perspective_transform(UPRIGHT[:3], UPRIGHT)

    def test_a_singular_matrix_cannot_be_inverted(self):
        with pytest.raises(AlignmentTransformError, match="singular"):
            invert_transform(np.zeros((3, 3), dtype=np.float64))

    def test_transforming_no_points_returns_nothing(self):
        assert apply_transform(np.eye(3, dtype=np.float64), []) == ()

    def test_mismatched_point_counts_are_rejected(self):
        with pytest.raises(ValueError, match="counts differ"):
            reprojection_errors(np.eye(3, dtype=np.float64), UPRIGHT, UPRIGHT[:2])

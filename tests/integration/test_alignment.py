"""End-to-end geometric normalisation, measured against known ground truth.

The central assertion of Phase 1 lives here. Each case renders a canonical page,
distorts it by a *known* homography, feeds the result to the engine and then
measures where nine interior control points ended up. Those points take no part
in fitting the transform, so the distance between where one lands and where it
was drawn is an honest measure of the recovered geometry - unlike reprojection
over the four fitted markers, which four correspondences make exact by
construction.

Tolerances are stated once, at the top, with the reason for their magnitude.
"""

from __future__ import annotations

import numpy as np
import pytest

from omr_scanner.domain.geometry import NormalizedPoint
from omr_scanner.imaging import (
    CANONICAL_CORNER_ORDER,
    AlignmentConfig,
    AlignmentWarning,
    GeometryConfig,
    align_sheet,
)
from omr_scanner.imaging.geometry import apply_transform
from omr_scanner.imaging.models import Point
from omr_scanner.imaging.synthetic import (
    DistortionSpec,
    SyntheticSheetSpec,
    apply_distortion,
    control_point_errors,
    render_sheet,
)

CONTROL_POINT_TOLERANCE_PX = 1.5
"""Largest accepted control-point error, in canonical pixels.

Derived from measurement, not chosen for convenience. Over the whole suite below
- 40 cases x 9 control points - the measured error is 0.06 px mean, 0.15 px at
the 95th percentile and 0.39 px at worst. The dominant error source is that a
marker centroid is measured on a thresholded image and is therefore quantised to
a fraction of a working-resolution pixel.

1.5 px is roughly a tenth of a printed marker's width and two orders of
magnitude below a bubble pitch, so an error at this scale cannot move a
measurement window onto the wrong bubble. The headroom above the measured
maximum is deliberately modest: a regression that tripled the error would fail
this suite rather than pass quietly.
"""

IMAGE_RECOVERY_TOLERANCE_PX = 3.0
"""Largest accepted error when a control point is re-detected in the output.

Looser than the analytic tolerance because it also absorbs the bilinear
interpolation of the warp and the centroid of a re-thresholded blob, neither of
which is part of the geometry being tested.
"""

DISTORTION_SUITE = [
    pytest.param(DistortionSpec(), id="no distortion"),
    pytest.param(DistortionSpec(rotation_degrees=1.0), id="rotate +1"),
    pytest.param(DistortionSpec(rotation_degrees=-1.0), id="rotate -1"),
    pytest.param(DistortionSpec(rotation_degrees=3.0), id="rotate +3"),
    pytest.param(DistortionSpec(rotation_degrees=-3.0), id="rotate -3"),
    pytest.param(DistortionSpec(rotation_degrees=5.0), id="rotate +5"),
    pytest.param(DistortionSpec(rotation_degrees=-5.0), id="rotate -5"),
    pytest.param(DistortionSpec(rotation_degrees=10.0), id="rotate +10"),
    pytest.param(DistortionSpec(rotation_degrees=-10.0), id="rotate -10"),
    pytest.param(DistortionSpec(rotation_degrees=15.0), id="rotate +15"),
    pytest.param(DistortionSpec(rotation_degrees=90.0), id="rotate 90"),
    pytest.param(DistortionSpec(rotation_degrees=180.0), id="rotate 180"),
    pytest.param(DistortionSpec(rotation_degrees=270.0), id="rotate 270"),
    pytest.param(DistortionSpec(scale_x=0.5, scale_y=0.5), id="scale 0.5"),
    pytest.param(DistortionSpec(scale_x=0.75, scale_y=0.75), id="scale 0.75"),
    pytest.param(DistortionSpec(scale_x=1.5, scale_y=1.5), id="scale 1.5"),
    pytest.param(DistortionSpec(scale_x=1.05, scale_y=0.95), id="non-uniform scale"),
    pytest.param(
        DistortionSpec(translate_x_px=80.0, translate_y_px=-40.0, margin_px=120),
        id="translate",
    ),
    pytest.param(DistortionSpec(perspective_strength=0.01, seed=1), id="perspective 1%"),
    pytest.param(DistortionSpec(perspective_strength=0.03, seed=2), id="perspective 3%"),
    pytest.param(DistortionSpec(perspective_strength=0.06, seed=3), id="perspective 6%"),
    pytest.param(DistortionSpec(perspective_strength=0.10, seed=4), id="perspective 10%"),
    pytest.param(DistortionSpec(brightness_gain=0.5), id="dark scan"),
    pytest.param(DistortionSpec(brightness_offset=80.0), id="bright scan"),
    pytest.param(
        DistortionSpec(brightness_gain=0.45, brightness_offset=120.0), id="low contrast"
    ),
    pytest.param(DistortionSpec(illumination_gradient=0.35), id="illumination gradient"),
    pytest.param(DistortionSpec(blur_kernel_px=3), id="blur 3"),
    pytest.param(DistortionSpec(blur_kernel_px=7), id="blur 7"),
    pytest.param(DistortionSpec(blur_kernel_px=13), id="blur 13"),
    pytest.param(DistortionSpec(noise_sigma=5.0, seed=5), id="noise 5"),
    pytest.param(DistortionSpec(noise_sigma=15.0, seed=6), id="noise 15"),
    pytest.param(DistortionSpec(noise_sigma=25.0, seed=7), id="noise 25"),
    pytest.param(DistortionSpec(jpeg_quality=60), id="jpeg 60"),
    pytest.param(DistortionSpec(jpeg_quality=25), id="jpeg 25"),
    pytest.param(DistortionSpec(margin_px=0), id="no margin"),
    pytest.param(DistortionSpec(margin_px=-25), id="slightly cropped"),
    pytest.param(
        DistortionSpec(
            rotation_degrees=4.0,
            scale_x=0.85,
            scale_y=0.85,
            perspective_strength=0.02,
            blur_kernel_px=3,
            noise_sigma=4.0,
            brightness_gain=0.8,
            seed=21,
        ),
        id="combined moderate",
    ),
    pytest.param(
        DistortionSpec(
            rotation_degrees=-7.0,
            scale_x=1.25,
            scale_y=1.25,
            perspective_strength=0.04,
            blur_kernel_px=5,
            noise_sigma=8.0,
            brightness_offset=40.0,
            jpeg_quality=70,
            seed=22,
        ),
        id="combined heavy",
    ),
    pytest.param(
        DistortionSpec(
            rotation_degrees=183.0,
            scale_x=0.9,
            scale_y=0.9,
            perspective_strength=0.02,
            noise_sigma=6.0,
            seed=23,
        ),
        id="combined upside down",
    ),
    pytest.param(
        DistortionSpec(
            rotation_degrees=92.0, perspective_strength=0.02, blur_kernel_px=3, seed=24
        ),
        id="combined quarter turn",
    ),
]
"""The Phase 1 accuracy suite: every distortion class, singly and combined."""


class TestGeometricAccuracy:
    @pytest.mark.parametrize("spec", DISTORTION_SUITE)
    def test_interior_control_points_are_recovered(
        self, canonical_sheet, canonical_config, spec
    ):
        distorted = apply_distortion(canonical_sheet, spec)
        result = align_sheet(distorted.image, config=canonical_config)
        errors = control_point_errors(result.transform_matrix, distorted)
        assert max(errors) < CONTROL_POINT_TOLERANCE_PX

    @pytest.mark.parametrize("spec", DISTORTION_SUITE)
    def test_the_output_is_exactly_the_canonical_page_size(
        self, canonical_sheet, canonical_config, spec
    ):
        distorted = apply_distortion(canonical_sheet, spec)
        result = align_sheet(distorted.image, config=canonical_config)
        assert result.normalized_image.shape[:2] == (
            canonical_config.canonical_height,
            canonical_config.canonical_width,
        )

    def test_the_whole_suite_is_deterministic(self, canonical_sheet, canonical_config):
        spec = DistortionSpec(rotation_degrees=6.0, noise_sigma=9.0, seed=31)
        first = align_sheet(
            apply_distortion(canonical_sheet, spec).image, config=canonical_config
        )
        second = align_sheet(
            apply_distortion(canonical_sheet, spec).image, config=canonical_config
        )
        assert np.allclose(first.transform_matrix, second.transform_matrix)
        assert np.array_equal(first.normalized_image, second.normalized_image)

    @pytest.mark.parametrize(("width", "height"), [(620, 877), (1240, 1754), (2480, 3508)])
    def test_accuracy_holds_at_every_resolution(self, width, height):
        sheet = render_sheet(SyntheticSheetSpec(width=width, height=height))
        config = AlignmentConfig(canonical_width=width, canonical_height=height)
        distorted = apply_distortion(
            sheet, DistortionSpec(rotation_degrees=4.0, perspective_strength=0.02, seed=9)
        )
        result = align_sheet(distorted.image, config=config)
        assert max(control_point_errors(result.transform_matrix, distorted)) < (
            CONTROL_POINT_TOLERANCE_PX
        )


class TestRecoveredImage:
    def test_control_points_land_where_they_were_drawn(
        self, canonical_sheet, canonical_config
    ):
        # The analytic test measures the transform; this one measures the actual
        # pixels, so a warp using the right matrix with the wrong output size or
        # a flipped axis cannot pass unnoticed.
        distorted = apply_distortion(
            canonical_sheet,
            DistortionSpec(rotation_degrees=6.0, scale_x=1.1, scale_y=1.1,
                           perspective_strength=0.02, seed=15),
        )
        result = align_sheet(distorted.image, config=canonical_config)
        for expected in canonical_sheet.control_points:
            found = _blob_centroid_near(result.normalized_image, expected, radius=15)
            assert found is not None
            assert found.distance_to(expected) < IMAGE_RECOVERY_TOLERANCE_PX

    def test_the_markers_land_on_their_canonical_targets(
        self, canonical_sheet, canonical_config
    ):
        distorted = apply_distortion(canonical_sheet, DistortionSpec(rotation_degrees=-8.0))
        result = align_sheet(distorted.image, config=canonical_config)
        for target in canonical_config.canonical_marker_points():
            assert result.normalized_image[round(target.y), round(target.x)] < 80

    def test_uncovered_area_is_rendered_as_paper(self, canonical_sheet, canonical_config):
        # A cropped scan must leave blank paper where data is missing, not a
        # black band that later reads as heavy ink.
        distorted = apply_distortion(canonical_sheet, DistortionSpec(margin_px=-30))
        result = align_sheet(distorted.image, config=canonical_config)
        assert result.normalized_image[2, 2] > 200

    def test_a_colour_scan_stays_colour(self, canonical_sheet, canonical_config):
        distorted = apply_distortion(canonical_sheet, DistortionSpec())
        colour = np.repeat(distorted.image[:, :, None], 3, axis=2)
        result = align_sheet(colour, config=canonical_config)
        assert result.normalized_image.shape == (
            canonical_config.canonical_height,
            canonical_config.canonical_width,
            3,
        )

    def test_a_bgra_scan_is_accepted(self, canonical_sheet, canonical_config):
        distorted = apply_distortion(canonical_sheet, DistortionSpec())
        bgra = np.dstack(
            [distorted.image] * 3 + [np.full_like(distorted.image, 255)]
        )
        result = align_sheet(bgra, config=canonical_config)
        assert result.normalized_image.shape[2] == 4

    def test_the_source_image_is_never_modified(self, canonical_sheet, canonical_config):
        distorted = apply_distortion(canonical_sheet, DistortionSpec(rotation_degrees=3.0))
        before = distorted.image.copy()
        align_sheet(distorted.image, config=canonical_config)
        assert np.array_equal(distorted.image, before)


class TestTransforms:
    def test_the_inverse_maps_canonical_coordinates_back_onto_the_scan(
        self, canonical_sheet, canonical_config
    ):
        # This is what a conflict-review overlay needs: point at the original
        # paper given a position on the rectified page.
        distorted = apply_distortion(
            canonical_sheet, DistortionSpec(rotation_degrees=9.0, scale_x=0.9, scale_y=0.9)
        )
        result = align_sheet(distorted.image, config=canonical_config)
        back = apply_transform(result.inverse_transform_matrix, canonical_sheet.control_points)
        for mapped, truth in zip(back, distorted.control_points, strict=True):
            assert mapped.distance_to(truth) < 2.0

    def test_the_transform_and_its_inverse_compose_to_the_identity(
        self, canonical_sheet, canonical_config
    ):
        result = align_sheet(
            apply_distortion(canonical_sheet, DistortionSpec()).image, config=canonical_config
        )
        product = result.transform_matrix @ result.inverse_transform_matrix
        assert np.allclose(product / product[2, 2], np.eye(3), atol=1e-8)

    def test_reprojection_over_the_fitted_markers_is_negligible(
        self, canonical_sheet, canonical_config
    ):
        # Four correspondences determine a homography exactly, so this number
        # measures numerical conditioning rather than geometric accuracy. It is
        # recorded because a large value means the solve went wrong, and
        # asserted here so that meaning stays true.
        result = align_sheet(
            apply_distortion(canonical_sheet, DistortionSpec(rotation_degrees=7.0)).image,
            config=canonical_config,
        )
        assert result.metrics.max_reprojection_error_px < 0.01
        assert result.metrics.mean_reprojection_error_px <= (
            result.metrics.max_reprojection_error_px
        )


class TestResultStructure:
    @pytest.fixture
    def result(self, canonical_sheet, canonical_config):
        distorted = apply_distortion(
            canonical_sheet, DistortionSpec(rotation_degrees=5.0, perspective_strength=0.02)
        )
        return align_sheet(distorted.image, config=canonical_config)

    def test_the_markers_are_returned_in_canonical_corner_order(self, result):
        assert tuple(marker.role for marker in result.corner_markers) == (
            CANONICAL_CORNER_ORDER
        )

    def test_a_marker_can_be_looked_up_by_role(self, result):
        for role in CANONICAL_CORNER_ORDER:
            assert result.marker(role).role is role

    def test_an_unknown_role_is_refused(self, result):
        with pytest.raises(KeyError):
            result.marker("not a role")

    def test_the_source_dimensions_are_reported(self, canonical_sheet, canonical_config):
        distorted = apply_distortion(canonical_sheet, DistortionSpec())
        result = align_sheet(distorted.image, config=canonical_config)
        assert result.original_width == distorted.image.shape[1]
        assert result.original_height == distorted.image.shape[0]

    def test_the_source_quadrilateral_matches_the_marker_centres(self, result):
        assert result.source_quadrilateral == tuple(
            marker.center for marker in result.corner_markers
        )

    def test_diagnostics_are_absent_by_default(self, result):
        assert result.diagnostics is None


class TestMetrics:
    @pytest.fixture
    def result(self, canonical_sheet, canonical_config):
        distorted = apply_distortion(canonical_sheet, DistortionSpec(rotation_degrees=5.0))
        return align_sheet(distorted.image, config=canonical_config)

    def test_candidate_counts_are_reported(self, result):
        assert result.metrics.candidate_count == 4
        assert result.metrics.rejected_count > 0

    def test_a_score_is_reported_for_every_corner(self, result):
        assert set(result.metrics.marker_scores) == set(CANONICAL_CORNER_ORDER)
        assert all(0.0 <= score <= 1.0 for score in result.metrics.marker_scores.values())
        assert result.metrics.min_marker_score == min(result.metrics.marker_scores.values())

    def test_the_quadrilateral_area_ratio_is_a_fraction_of_the_scan(self, result):
        assert 0.0 < result.metrics.quadrilateral_area_ratio < 1.0

    def test_the_aspect_ratio_deviation_is_small_for_a_square_on_scan(self, result):
        assert abs(result.metrics.aspect_ratio_deviation) < 0.02
        assert result.metrics.expected_aspect_ratio > 0.0

    def test_the_marker_separation_is_reported_in_source_pixels(self, result):
        assert result.metrics.min_marker_separation_px > 100.0

    def test_orientation_evidence_is_reported(self, result):
        assert 0.0 <= result.metrics.orientation_confidence <= 1.0
        assert result.metrics.orientation_margin >= 0.0

    def test_the_working_scale_is_reported(self, result):
        assert 0.0 < result.metrics.working_scale <= 1.0

    def test_the_elapsed_time_is_reported(self, result):
        assert result.metrics.elapsed_seconds > 0.0


class TestWarnings:
    def test_a_clean_scan_produces_no_warnings(self, canonical_sheet, canonical_config):
        result = align_sheet(
            apply_distortion(canonical_sheet, DistortionSpec()).image, config=canonical_config
        )
        assert result.warnings == ()

    def test_a_stretched_page_warns_about_its_proportions(
        self, canonical_sheet, canonical_config
    ):
        distorted = apply_distortion(
            canonical_sheet, DistortionSpec(scale_x=1.15, scale_y=0.9)
        )
        result = align_sheet(distorted.image, config=canonical_config)
        assert AlignmentWarning.ASPECT_RATIO_DEVIATION in result.warnings

    def test_a_cropped_scan_warns_that_a_marker_touches_the_border(
        self, canonical_sheet, canonical_config
    ):
        distorted = apply_distortion(canonical_sheet, DistortionSpec(margin_px=-45))
        result = align_sheet(distorted.image, config=canonical_config)
        assert AlignmentWarning.MARKER_NEAR_IMAGE_EDGE in result.warnings

    def test_competing_candidates_are_reported(self, canonical_config):
        decoys = (
            NormalizedPoint(x=0.22, y=0.16),
            NormalizedPoint(x=0.78, y=0.16),
        )
        sheet = render_sheet(SyntheticSheetSpec(decoy_markers=decoys))
        result = align_sheet(
            apply_distortion(sheet, DistortionSpec()).image, config=canonical_config
        )
        assert AlignmentWarning.MULTIPLE_CORNER_CANDIDATES in result.warnings
        assert sum(marker.alternative_count for marker in result.corner_markers) >= 2

    def test_a_warning_never_changes_the_produced_image(
        self, canonical_sheet, canonical_config
    ):
        # Warnings are observations recorded alongside a decision, never inputs
        # to it. The same scan under two configurations that differ only in a
        # warning threshold must therefore rectify to identical pixels.
        distorted = apply_distortion(
            canonical_sheet, DistortionSpec(scale_x=1.15, scale_y=0.9)
        )
        strict = align_sheet(distorted.image, config=canonical_config)
        lenient = align_sheet(
            distorted.image,
            config=AlignmentConfig(
                geometry=GeometryConfig(aspect_ratio_warning_deviation=0.9)
            ),
        )
        assert AlignmentWarning.ASPECT_RATIO_DEVIATION in strict.warnings
        assert AlignmentWarning.ASPECT_RATIO_DEVIATION not in lenient.warnings
        assert np.array_equal(strict.normalized_image, lenient.normalized_image)


class TestDefaultConfiguration:
    def test_align_sheet_works_without_an_explicit_configuration(self, canonical_sheet):
        result = align_sheet(apply_distortion(canonical_sheet, DistortionSpec()).image)
        assert result.normalized_image.shape[:2] == (1754, 1240)


def _blob_centroid_near(image, expected: Point, *, radius: int) -> Point | None:
    """Return the centroid of the ink within ``radius`` pixels of ``expected``."""
    top = max(0, round(expected.y) - radius)
    left = max(0, round(expected.x) - radius)
    window = image[top : top + 2 * radius, left : left + 2 * radius]
    ink = np.argwhere(window < 128)
    if ink.size == 0:
        return None
    return Point(x=float(ink[:, 1].mean()) + left, y=float(ink[:, 0].mean()) + top)

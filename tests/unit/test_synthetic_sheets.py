"""The synthetic sheet generator and its distortion engine.

Every accuracy claim in the alignment suite rests on these two things being
right, so they are tested on their own terms first: the rendered page really
carries ink where it says it does, and the reported homography really is the one
that was applied. A generator that quietly disagreed with its own ground truth
would make the whole suite measure nothing while passing.
"""

from __future__ import annotations

import numpy as np
import pytest

from omr_scanner.domain.geometry import NormalizedPoint
from omr_scanner.domain.template import MarkerRole
from omr_scanner.imaging.models import CANONICAL_CORNER_ORDER
from omr_scanner.imaging.synthetic import (
    DEFAULT_CONTROL_POINTS,
    DistortionSpec,
    SyntheticSheetSpec,
    apply_distortion,
    control_point_errors,
    project,
    render_sheet,
)

INK_LEVEL = 60
"""Grey level below which a rendered pixel counts as ink."""


class TestRenderSheet:
    def test_the_page_has_the_requested_size(self):
        sheet = render_sheet(SyntheticSheetSpec(width=800, height=1100))
        assert sheet.image.shape == (1100, 800)

    def test_the_page_is_grayscale_and_mostly_paper(self):
        sheet = render_sheet()
        assert sheet.image.ndim == 2
        assert sheet.image.dtype == np.uint8
        assert float(np.mean(sheet.image)) > 200.0

    def test_every_marker_centre_is_ink(self):
        sheet = render_sheet()
        for center in sheet.marker_centers:
            assert sheet.image[round(center.y), round(center.x)] < INK_LEVEL

    def test_marker_centres_follow_the_canonical_corner_order(self):
        sheet = render_sheet()
        top_left, top_right, bottom_right, bottom_left = sheet.marker_centers
        assert top_left.x < top_right.x
        assert top_left.y < bottom_left.y
        assert bottom_right.x == pytest.approx(top_right.x)
        assert bottom_right.y == pytest.approx(bottom_left.y)

    def test_every_control_point_is_ink(self):
        sheet = render_sheet()
        for point in sheet.control_points:
            assert sheet.image[round(point.y), round(point.x)] < INK_LEVEL

    def test_control_points_are_away_from_the_markers(self):
        # A control point next to a marker would measure what the fit already
        # guarantees rather than the recovered geometry.
        sheet = render_sheet()
        page_diagonal = float(np.hypot(sheet.spec.width, sheet.spec.height))
        for point in sheet.control_points:
            nearest = min(point.distance_to(marker) for marker in sheet.marker_centers)
            assert nearest > 0.15 * page_diagonal

    def test_the_orientation_mark_is_rendered_by_default(self):
        sheet = render_sheet()
        mark = sheet.spec.to_pixels(sheet.spec.orientation_center)
        assert sheet.image[round(mark.y), round(mark.x)] < INK_LEVEL

    def test_the_orientation_mark_can_be_omitted(self):
        sheet = render_sheet(SyntheticSheetSpec(omit_orientation_marker=True))
        mark = sheet.spec.to_pixels(sheet.spec.orientation_center)
        assert sheet.image[round(mark.y), round(mark.x)] > 200

    @pytest.mark.parametrize("role", list(MarkerRole))
    def test_a_marker_can_be_omitted(self, role):
        sheet = render_sheet(SyntheticSheetSpec(omit_markers=frozenset({role})))
        index = CANONICAL_CORNER_ORDER.index(role)
        center = sheet.marker_centers[index]
        assert sheet.image[round(center.y), round(center.x)] > 200

    def test_rendering_is_deterministic(self):
        first = render_sheet()
        second = render_sheet()
        assert np.array_equal(first.image, second.image)

    def test_decoy_markers_are_rendered_as_solid_ink(self):
        decoy = NormalizedPoint(x=0.25, y=0.20)
        sheet = render_sheet(SyntheticSheetSpec(decoy_markers=(decoy,)))
        point = sheet.spec.to_pixels(decoy)
        assert sheet.image[round(point.y), round(point.x)] < INK_LEVEL

    def test_answer_frames_are_hollow(self):
        # Their outline is indistinguishable from a marker's; only the interior
        # tells them apart, which is exactly the property under test elsewhere.
        sheet = render_sheet(SyntheticSheetSpec(draw_answer_frames=True))
        frame_center = sheet.spec.to_pixels(NormalizedPoint(x=0.50, y=0.40))
        assert sheet.image[round(frame_center.y), round(frame_center.x)] > 200

    def test_a_bare_page_has_no_clutter(self):
        sheet = render_sheet(
            SyntheticSheetSpec(
                draw_bubbles=False, draw_text_bars=False, draw_answer_frames=False
            )
        )
        assert float(np.mean(sheet.image)) > float(np.mean(render_sheet().image))

    def test_a_zero_sized_page_is_rejected(self):
        with pytest.raises(ValueError, match="dimensions must be positive"):
            SyntheticSheetSpec(width=0)

    def test_a_missing_corner_role_is_rejected(self):
        partial = {
            role: NormalizedPoint(x=0.1, y=0.1) for role in CANONICAL_CORNER_ORDER[:2]
        }
        with pytest.raises(ValueError, match="missing corner role"):
            SyntheticSheetSpec(marker_targets=partial)

    def test_the_default_control_points_span_the_page(self):
        xs = {point.x for point in DEFAULT_CONTROL_POINTS}
        ys = {point.y for point in DEFAULT_CONTROL_POINTS}
        assert len(DEFAULT_CONTROL_POINTS) == len(xs) * len(ys)
        assert max(xs) - min(xs) >= 0.5
        assert max(ys) - min(ys) >= 0.5


class TestDistortionSpec:
    def test_a_non_positive_scale_is_rejected(self):
        with pytest.raises(ValueError, match="Scale factors"):
            DistortionSpec(scale_x=0.0)

    def test_an_even_blur_kernel_is_rejected(self):
        with pytest.raises(ValueError, match="blur_kernel_px"):
            DistortionSpec(blur_kernel_px=4)

    def test_negative_noise_is_rejected(self):
        with pytest.raises(ValueError, match="noise_sigma"):
            DistortionSpec(noise_sigma=-1.0)

    def test_an_impossible_jpeg_quality_is_rejected(self):
        with pytest.raises(ValueError, match="jpeg_quality"):
            DistortionSpec(jpeg_quality=0)

    def test_an_impossible_illumination_gradient_is_rejected(self):
        with pytest.raises(ValueError, match="illumination_gradient"):
            DistortionSpec(illumination_gradient=1.0)


class TestApplyDistortion:
    def test_an_undistorted_page_keeps_its_size_plus_the_margin(self, canonical_sheet):
        distorted = apply_distortion(canonical_sheet, DistortionSpec(margin_px=30))
        assert distorted.image.shape == (
            canonical_sheet.spec.height + 60,
            canonical_sheet.spec.width + 60,
        )

    def test_rotation_grows_the_canvas(self, canonical_sheet):
        upright = apply_distortion(canonical_sheet, DistortionSpec())
        turned = apply_distortion(canonical_sheet, DistortionSpec(rotation_degrees=10.0))
        assert turned.image.shape[1] > upright.image.shape[1]

    def test_a_quarter_turn_swaps_the_canvas_axes(self, canonical_sheet):
        turned = apply_distortion(canonical_sheet, DistortionSpec(rotation_degrees=90.0))
        assert turned.image.shape[0] < turned.image.shape[1]

    def test_scaling_scales_the_canvas(self, canonical_sheet):
        half = apply_distortion(canonical_sheet, DistortionSpec(scale_x=0.5, scale_y=0.5))
        assert half.image.shape[0] < canonical_sheet.spec.height

    def test_the_reported_homography_maps_the_markers_where_they_land(
        self, canonical_sheet
    ):
        distorted = apply_distortion(
            canonical_sheet,
            DistortionSpec(rotation_degrees=7.0, scale_x=0.8, scale_y=0.8, seed=3),
        )
        for center in distorted.marker_centers:
            assert distorted.image[round(center.y), round(center.x)] < INK_LEVEL

    def test_the_reported_homography_maps_the_control_points_where_they_land(
        self, canonical_sheet
    ):
        distorted = apply_distortion(
            canonical_sheet, DistortionSpec(rotation_degrees=-5.0, perspective_strength=0.02)
        )
        for point in distorted.control_points:
            assert distorted.image[round(point.y), round(point.x)] < INK_LEVEL

    def test_the_inverse_of_the_applied_homography_recovers_the_canonical_page(
        self, canonical_sheet
    ):
        # The alignment engine's job stated as arithmetic: a perfect recovery is
        # exactly the inverse of what was applied, so measuring against it gives
        # zero error and proves the metric itself is sound.
        distorted = apply_distortion(
            canonical_sheet,
            DistortionSpec(rotation_degrees=6.0, scale_x=1.2, scale_y=1.2,
                           perspective_strength=0.03, seed=11),
        )
        errors = control_point_errors(np.linalg.inv(distorted.homography), distorted)
        assert max(errors) < 1e-6

    def test_a_wrong_transform_produces_a_large_error(self, canonical_sheet):
        # Guards against the metric passing by accident: an identity transform
        # is obviously wrong and must be reported as such.
        distorted = apply_distortion(canonical_sheet, DistortionSpec(rotation_degrees=6.0))
        errors = control_point_errors(np.eye(3, dtype=np.float64), distorted)
        assert min(errors) > 10.0

    @pytest.mark.parametrize(
        "spec",
        [
            DistortionSpec(noise_sigma=6.0, seed=5),
            DistortionSpec(perspective_strength=0.04, seed=5),
            DistortionSpec(noise_sigma=3.0, perspective_strength=0.02, seed=5),
        ],
    )
    def test_randomised_distortions_are_reproducible(self, canonical_sheet, spec):
        first = apply_distortion(canonical_sheet, spec)
        second = apply_distortion(canonical_sheet, spec)
        assert np.array_equal(first.image, second.image)
        assert np.allclose(first.homography, second.homography)

    def test_different_seeds_produce_different_perspective(self, canonical_sheet):
        a = apply_distortion(canonical_sheet, DistortionSpec(perspective_strength=0.05, seed=1))
        b = apply_distortion(canonical_sheet, DistortionSpec(perspective_strength=0.05, seed=2))
        assert not np.allclose(a.homography, b.homography)

    def test_brightness_gain_darkens_the_page(self, canonical_sheet):
        dark = apply_distortion(canonical_sheet, DistortionSpec(brightness_gain=0.5))
        plain = apply_distortion(canonical_sheet, DistortionSpec())
        assert float(np.mean(dark.image)) < float(np.mean(plain.image))

    def test_an_illumination_gradient_darkens_one_corner_more_than_the_other(
        self, canonical_sheet
    ):
        shaded = apply_distortion(canonical_sheet, DistortionSpec(illumination_gradient=0.5))
        top_left = float(np.mean(shaded.image[:100, :100]))
        bottom_right = float(np.mean(shaded.image[-100:, -100:]))
        assert bottom_right < top_left - 40.0

    def test_blur_softens_the_page(self, canonical_sheet):
        plain = apply_distortion(canonical_sheet, DistortionSpec())
        blurred = apply_distortion(canonical_sheet, DistortionSpec(blur_kernel_px=9))
        assert float(np.std(blurred.image)) < float(np.std(plain.image))

    def test_jpeg_encoding_changes_the_pixels_but_not_the_geometry(self, canonical_sheet):
        plain = apply_distortion(canonical_sheet, DistortionSpec())
        compressed = apply_distortion(canonical_sheet, DistortionSpec(jpeg_quality=30))
        assert not np.array_equal(plain.image, compressed.image)
        assert np.allclose(plain.homography, compressed.homography)

    def test_a_negative_margin_crops_into_the_page(self, canonical_sheet):
        cropped = apply_distortion(canonical_sheet, DistortionSpec(margin_px=-50))
        assert cropped.image.shape[1] < canonical_sheet.spec.width


class TestProject:
    def test_projecting_nothing_returns_nothing(self):
        assert project(np.eye(3, dtype=np.float64), []) == ()

    def test_the_identity_leaves_points_alone(self, canonical_sheet):
        projected = project(np.eye(3, dtype=np.float64), canonical_sheet.control_points)
        for original, mapped in zip(canonical_sheet.control_points, projected, strict=True):
            assert mapped.x == pytest.approx(original.x)
            assert mapped.y == pytest.approx(original.y)

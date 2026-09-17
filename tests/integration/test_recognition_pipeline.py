"""End-to-end recognition against geometrically transformed scans.

The point of every test here is the same: a sheet that has been rotated, skewed,
scaled, shifted, dimmed or photographed at an angle must yield *the same answers*
as the undistorted one. If template mapping drifts under a transformation, this
is where it shows up - as a wrong answer rather than as a pixel difference.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import pytest
from tests.conftest import build_answer_sheet_template, marked_sheet_spec

from omr_scanner.imaging.synthetic import DistortionSpec, apply_distortion, render_sheet
from omr_scanner.services.recognition_service import (
    RecognitionOutcome,
    RegistrationStatus,
    recognise_scan,
)

if TYPE_CHECKING:
    from pathlib import Path

ROLL = "120317"
SET_CODE = "B"
ANSWERS = ("A", "B", "C", "D", "A", "B", "C", "D", "A", "B")
"""One sheet's ground truth, repeated across both question blocks."""


def ground_truth_marks() -> dict:
    return {
        "roll_number": dict(enumerate(ROLL)),
        "set_code": {0: SET_CODE},
        "questions_0": dict(enumerate(ANSWERS)),
        "questions_1": dict(enumerate(ANSWERS)),
    }


@pytest.fixture
def template():
    return build_answer_sheet_template()


@pytest.fixture
def canonical(template):
    """The undistorted marked sheet, with its ground-truth geometry."""
    return render_sheet(marked_sheet_spec(template, ground_truth_marks()))


@pytest.fixture
def scan_under(tmp_path: Path, canonical):
    """Apply a distortion to the marked sheet and write it out as a scan."""
    counter = {"n": 0}

    def write(spec: DistortionSpec, suffix: str = ".png") -> Path:
        counter["n"] += 1
        distorted = apply_distortion(canonical, spec)
        path = tmp_path / f"scan{counter['n']}{suffix}"
        cv2.imwrite(str(path), distorted.image)
        return path

    return write


def assert_reads_correctly(result, *, expect_clean: bool = True) -> None:
    """Assert the sheet was registered and read exactly as it was marked."""
    assert result.registration is not RegistrationStatus.FAILED, (
        result.registration_message
    )
    assert result.identifier_value == ROLL
    assert result.set_code_value == SET_CODE
    assert len(result.answers) == 20
    expected = list(ANSWERS) * 2
    assert [answer.value for answer in result.answers] == expected
    if expect_clean:
        assert result.outcome is RecognitionOutcome.COMPLETE
        assert result.review_count == 0


class TestUndistorted:
    def test_a_clean_scan_reads_exactly_what_was_marked(self, tmp_path, template, canonical):
        path = tmp_path / "clean.png"
        cv2.imwrite(str(path), canonical.image)
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))

    def test_the_result_reports_both_page_frames(self, tmp_path, template, canonical):
        path = tmp_path / "clean.png"
        cv2.imwrite(str(path), canonical.image)
        result = recognise_scan(path, template, with_preview=False)
        assert (result.canonical_width, result.canonical_height) == (
            template.page.canonical_width_px,
            template.page.canonical_height_px,
        )
        assert (result.source_width, result.source_height) == (
            canonical.image.shape[1],
            canonical.image.shape[0],
        )


class TestSmallRotationAndSkew:
    @pytest.mark.parametrize("degrees", [-7.0, -2.5, -0.7, 0.7, 2.5, 7.0])
    def test_a_slightly_rotated_scan_reads_the_same(self, template, scan_under, degrees):
        path = scan_under(DistortionSpec(rotation_degrees=degrees))
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))

    def test_a_sheet_fed_crookedly_reads_the_same(self, template, scan_under):
        # Rotation plus a shift, which is what a sheet nudged against the feed
        # roller actually looks like.
        path = scan_under(
            DistortionSpec(rotation_degrees=3.5, translate_x_px=25, translate_y_px=-15)
        )
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))


class TestQuarterTurns:
    @pytest.mark.parametrize("degrees", [90.0, 180.0, 270.0])
    def test_a_sheet_scanned_the_wrong_way_up_is_turned_back(
        self, template, scan_under, degrees
    ):
        path = scan_under(DistortionSpec(rotation_degrees=degrees))
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))

    @pytest.mark.parametrize("degrees", [88.0, 182.0, 268.5])
    def test_a_quarter_turn_that_is_also_crooked_is_still_recovered(
        self, template, scan_under, degrees
    ):
        path = scan_under(DistortionSpec(rotation_degrees=degrees))
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))


class TestScale:
    @pytest.mark.parametrize("factor", [0.6, 0.8, 1.25, 1.6])
    def test_a_scan_at_a_different_resolution_reads_the_same(
        self, template, scan_under, factor
    ):
        path = scan_under(DistortionSpec(scale_x=factor, scale_y=factor))
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))

    def test_a_non_uniformly_stretched_scan_reads_the_same(self, template, scan_under):
        # A scanner whose feed speed does not match its optical resolution
        # stretches the page along one axis only.
        path = scan_under(DistortionSpec(scale_x=1.0, scale_y=1.18))
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))


class TestTranslation:
    @pytest.mark.parametrize(
        ("dx", "dy"), [(60, 0), (-60, 0), (0, 60), (0, -60), (45, -45)]
    )
    def test_a_shifted_page_reads_the_same(self, template, scan_under, dx, dy):
        path = scan_under(DistortionSpec(translate_x_px=dx, translate_y_px=dy))
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))


class TestPerspective:
    @pytest.mark.parametrize("strength", [0.02, 0.05, 0.08])
    def test_a_photographed_page_reads_the_same(self, template, scan_under, strength):
        path = scan_under(DistortionSpec(perspective_strength=strength, seed=7))
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))

    def test_perspective_combined_with_rotation_and_scale(self, template, scan_under):
        path = scan_under(
            DistortionSpec(
                rotation_degrees=4.0,
                scale_x=0.85,
                scale_y=0.85,
                perspective_strength=0.04,
                translate_x_px=20,
                seed=3,
            )
        )
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))


class TestPhotometricDegradation:
    @pytest.mark.parametrize(
        ("gain", "offset"), [(0.65, 0.0), (1.25, 0.0), (1.0, -45.0), (1.0, 20.0)]
    )
    def test_an_over_or_under_exposed_scan_reads_the_same(
        self, template, scan_under, gain, offset
    ):
        path = scan_under(DistortionSpec(brightness_gain=gain, brightness_offset=offset))
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))

    @pytest.mark.parametrize("strength", [0.25, 0.45])
    def test_an_unevenly_lit_scan_reads_the_same(self, template, scan_under, strength):
        # The degradation a single page-wide threshold actually fails on.
        path = scan_under(DistortionSpec(illumination_gradient=strength))
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))

    @pytest.mark.parametrize("sigma", [4.0, 9.0])
    def test_a_noisy_scan_reads_the_same(self, template, scan_under, sigma):
        path = scan_under(DistortionSpec(noise_sigma=sigma, seed=11))
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))

    def test_a_blurred_scan_reads_the_same(self, template, scan_under):
        path = scan_under(DistortionSpec(blur_kernel_px=5))
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))

    def test_a_jpeg_scan_reads_the_same(self, template, scan_under):
        path = scan_under(DistortionSpec(jpeg_quality=70), suffix=".jpg")
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))


class TestEverythingAtOnce:
    def test_a_thoroughly_degraded_scan_still_reads_correctly(self, template, scan_under):
        # The kind of scan a hurried invigilator actually produces.
        path = scan_under(
            DistortionSpec(
                rotation_degrees=-5.0,
                scale_x=0.9,
                scale_y=0.92,
                translate_x_px=30,
                translate_y_px=-20,
                perspective_strength=0.03,
                brightness_gain=0.85,
                illumination_gradient=0.3,
                blur_kernel_px=3,
                noise_sigma=5.0,
                jpeg_quality=80,
                seed=5,
            ),
            suffix=".jpg",
        )
        assert_reads_correctly(recognise_scan(path, template, with_preview=False))


class TestOverlayGeometryFollowsTheTransformation:
    def test_every_bubble_lands_inside_the_canonical_page(self, template, scan_under):
        path = scan_under(DistortionSpec(rotation_degrees=6.0, perspective_strength=0.04))
        result = recognise_scan(path, template, with_preview=False)

        assert result.bubbles
        for bubble in result.bubbles:
            assert 0 <= bubble.x <= result.canonical_width
            assert 0 <= bubble.y <= result.canonical_height

    def test_the_selected_bubbles_are_the_marked_ones(self, template, scan_under):
        path = scan_under(DistortionSpec(rotation_degrees=3.0))
        result = recognise_scan(path, template, with_preview=False)

        selected = [b for b in result.bubbles if b.selected]
        # Six roll digits + one set code + twenty answers.
        assert len(selected) == 27

    def test_markers_are_reported_in_source_pixels(self, template, scan_under):
        path = scan_under(DistortionSpec(rotation_degrees=8.0, translate_x_px=40))
        result = recognise_scan(path, template, with_preview=False)

        assert len(result.markers) == 4
        for marker in result.markers:
            assert 0 <= marker.x <= result.source_width
            assert 0 <= marker.y <= result.source_height

    def test_a_preview_is_produced_on_request_and_omitted_otherwise(
        self, template, scan_under
    ):
        path = scan_under(DistortionSpec(rotation_degrees=2.0))
        assert recognise_scan(path, template, with_preview=False).preview is None

        with_preview = recognise_scan(path, template, with_preview=True)
        assert with_preview.preview is not None
        assert with_preview.preview_scale > 0.0


class TestMarkConventionsSurviveTransformation:
    def _write(self, tmp_path, template, marks, spec, name: str = "sheet.png") -> Path:
        distorted = apply_distortion(render_sheet(marked_sheet_spec(template, marks)), spec)
        path = tmp_path / name
        cv2.imwrite(str(path), distorted.image)
        return path

    def test_a_double_mark_survives_rotation_and_perspective(self, tmp_path, template):
        marks = ground_truth_marks()
        marks["questions_0"][4] = ["A", "C"]
        path = self._write(
            tmp_path,
            template,
            marks,
            DistortionSpec(rotation_degrees=5.0, perspective_strength=0.03, seed=2),
        )
        result = recognise_scan(path, template, with_preview=False)

        answer = next(a for a in result.answers if a.number == 5)
        assert answer.value == "A-C"
        assert answer.status == "multiple"
        assert answer.display_value == "A-C"
        assert result.outcome is RecognitionOutcome.REVIEW

    def test_a_blank_answer_survives_rotation_and_perspective(self, tmp_path, template):
        marks = ground_truth_marks()
        del marks["questions_0"][4]
        path = self._write(
            tmp_path,
            template,
            marks,
            DistortionSpec(rotation_degrees=-4.0, perspective_strength=0.03, seed=2),
        )
        result = recognise_scan(path, template, with_preview=False)

        answer = next(a for a in result.answers if a.number == 5)
        assert answer.value == ""
        assert answer.status == "blank"
        assert answer.display_value == ""

    def test_a_faint_mark_is_flagged_rather_than_accepted_or_dropped(
        self, tmp_path, template
    ):
        marks = ground_truth_marks()
        marks["questions_0"][4] = ("A", 0.35)
        path = self._write(
            tmp_path, template, marks, DistortionSpec(rotation_degrees=2.0)
        )
        result = recognise_scan(path, template, with_preview=False)

        answer = next(a for a in result.answers if a.number == 5)
        assert answer.status == "uncertain"
        assert answer.needs_review is True
        assert answer.display_value == "?"
        assert result.outcome is RecognitionOutcome.REVIEW


class TestUnregistrableScans:
    def test_a_page_with_no_markers_fails_registration_and_produces_no_answers(
        self, tmp_path, template
    ):
        from omr_scanner.imaging.synthetic import MarkerRole, SyntheticSheetSpec

        spec = marked_sheet_spec(
            template,
            ground_truth_marks(),
            base=SyntheticSheetSpec(omit_markers=frozenset(MarkerRole)),
        )
        path = tmp_path / "no_markers.png"
        cv2.imwrite(str(path), render_sheet(spec).image)

        result = recognise_scan(path, template, with_preview=False)
        assert result.registration is RegistrationStatus.FAILED
        assert result.outcome is RecognitionOutcome.REGISTRATION_FAILED
        assert result.answers == ()
        assert result.identifier_value == ""
        assert result.registration_message

    def test_a_file_that_is_not_an_image_is_reported_not_raised(self, tmp_path, template):
        path = tmp_path / "broken.png"
        path.write_bytes(b"definitely not a PNG")

        result = recognise_scan(path, template, with_preview=False)
        assert result.outcome is RecognitionOutcome.ERROR
        assert result.registration_message
        assert result.error_code

    def test_a_missing_file_is_reported_not_raised(self, tmp_path, template):
        result = recognise_scan(tmp_path / "absent.png", template, with_preview=False)
        assert result.outcome is RecognitionOutcome.ERROR
        assert result.registration_message

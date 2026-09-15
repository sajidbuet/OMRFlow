"""Resolving which corner of a scan is the sheet's top-left.

The four corner markers are symmetric, so this is the stage that stops an
upside-down sheet becoming a complete, confident and wrong set of answers. The
tests therefore care as much about the refusals as about the successes.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from omr_scanner.domain.geometry import NormalizedPoint
from omr_scanner.errors import ImagingError, OrientationDetectionError
from omr_scanner.imaging import AlignmentConfig, AlignmentWarning, align_sheet
from omr_scanner.imaging.config import OrientationConfig
from omr_scanner.imaging.models import Point
from omr_scanner.imaging.orientation import determine_orientation, orientation_windows
from omr_scanner.imaging.preprocessing import prepare_for_detection
from omr_scanner.imaging.synthetic import (
    DistortionSpec,
    SyntheticSheetSpec,
    apply_distortion,
    render_sheet,
)

QUARTER_TURN_CASES = [
    pytest.param(0.0, 0, id="upright"),
    pytest.param(90.0, 1, id="quarter turn clockwise"),
    pytest.param(180.0, 2, id="upside down"),
    pytest.param(270.0, 3, id="quarter turn anticlockwise"),
]
"""Each cardinal feed orientation and the quarter-turn count it should report.

A page fed a quarter turn clockwise puts its top-left corner at the scan's
top-right, which is index 1 in the clockwise image-corner sequence - hence the
mapping from degrees to index is the identity divided by 90.
"""


class TestCardinalOrientations:
    @pytest.mark.parametrize(("degrees", "expected_turns"), QUARTER_TURN_CASES)
    def test_the_page_orientation_is_recovered(
        self, canonical_sheet, canonical_config, degrees, expected_turns
    ):
        distorted = apply_distortion(
            canonical_sheet, DistortionSpec(rotation_degrees=degrees)
        )
        result = align_sheet(distorted.image, config=canonical_config)
        assert result.orientation.quarter_turns == expected_turns

    @pytest.mark.parametrize(("degrees", "expected_turns"), QUARTER_TURN_CASES)
    def test_orientation_survives_skew_on_top_of_a_quarter_turn(
        self, canonical_sheet, canonical_config, degrees, expected_turns
    ):
        distorted = apply_distortion(
            canonical_sheet,
            DistortionSpec(rotation_degrees=degrees + 4.0, perspective_strength=0.015, seed=9),
        )
        result = align_sheet(distorted.image, config=canonical_config)
        assert result.orientation.quarter_turns == expected_turns

    @pytest.mark.parametrize(("degrees", "expected_turns"), QUARTER_TURN_CASES)
    def test_the_decision_is_unambiguous(
        self, canonical_sheet, canonical_config, degrees, expected_turns
    ):
        distorted = apply_distortion(
            canonical_sheet, DistortionSpec(rotation_degrees=degrees)
        )
        result = align_sheet(distorted.image, config=canonical_config)
        assert result.orientation.quarter_turns == expected_turns
        assert result.orientation.confidence > 0.8
        assert result.orientation.margin > canonical_config.orientation.min_margin

    def test_an_upside_down_page_is_rectified_the_right_way_up(
        self, canonical_sheet, canonical_config
    ):
        # The orientation mark itself is the proof: after rectification it must
        # be back in the top-left region the template declares.
        distorted = apply_distortion(
            canonical_sheet, DistortionSpec(rotation_degrees=180.0)
        )
        result = align_sheet(distorted.image, config=canonical_config)
        mark = canonical_config.normalized_to_canonical(
            NormalizedPoint(
                x=canonical_config.orientation.marker_center_x,
                y=canonical_config.orientation.marker_center_y,
            )
        )
        assert result.normalized_image[round(mark.y), round(mark.x)] < 80


class TestOrientationMarkReporting:
    def test_the_located_mark_is_reported_in_source_coordinates(
        self, canonical_sheet, canonical_config
    ):
        distorted = apply_distortion(canonical_sheet, DistortionSpec(rotation_degrees=8.0))
        result = align_sheet(distorted.image, config=canonical_config)

        assert result.orientation.marker_center is not None
        true_mark = canonical_sheet.spec.to_pixels(canonical_sheet.spec.orientation_center)
        expected = _project(distorted.homography, true_mark)
        assert result.orientation.marker_center.distance_to(expected) < 5.0

    def test_the_marker_box_is_reported(self, canonical_sheet, canonical_config):
        result = align_sheet(
            apply_distortion(canonical_sheet, DistortionSpec()).image, config=canonical_config
        )
        assert result.orientation.marker_box is not None
        assert result.orientation.marker_box.area > 0.0

    def test_all_four_hypotheses_are_recorded(self, canonical_sheet, canonical_config):
        result = align_sheet(
            apply_distortion(canonical_sheet, DistortionSpec()).image, config=canonical_config
        )
        assert len(result.orientation.hypotheses) == 4
        assert {item.quarter_turns for item in result.orientation.hypotheses} == {0, 1, 2, 3}

    def test_the_quarter_turn_hypotheses_are_pruned_by_geometry_on_a_portrait_page(
        self, canonical_sheet, canonical_config
    ):
        # Reading a portrait marker rectangle as landscape gives an aspect ratio
        # nowhere near the template's, so the two quarter turns are eliminated
        # before the mark is consulted. That is a free constraint, and it leaves
        # the mark with only upright-versus-inverted to settle.
        result = align_sheet(
            apply_distortion(canonical_sheet, DistortionSpec()).image, config=canonical_config
        )
        valid = {
            item.quarter_turns for item in result.orientation.hypotheses if item.geometry_valid
        }
        assert valid == {0, 2}


class TestOrientationFailure:
    def test_a_sheet_without_an_orientation_mark_is_refused(self, canonical_config):
        sheet = render_sheet(SyntheticSheetSpec(omit_orientation_marker=True))
        distorted = apply_distortion(sheet, DistortionSpec())
        with pytest.raises(OrientationDetectionError) as error:
            align_sheet(distorted.image, config=canonical_config)
        assert error.value.code == "ORIENTATION_NOT_FOUND"

    def test_the_failure_reports_every_hypothesis_it_tried(self, canonical_config):
        sheet = render_sheet(SyntheticSheetSpec(omit_orientation_marker=True))
        with pytest.raises(OrientationDetectionError) as error:
            align_sheet(apply_distortion(sheet, DistortionSpec()).image, config=canonical_config)
        message = str(error.value)
        assert "Confidences by quarter turn" in message
        for turns in (0, 1, 2, 3):
            assert f"{turns}:" in message

    def test_the_failure_is_an_imaging_error_with_a_plain_message(self, canonical_config):
        sheet = render_sheet(SyntheticSheetSpec(omit_orientation_marker=True))
        with pytest.raises(ImagingError) as error:
            align_sheet(apply_distortion(sheet, DistortionSpec()).image, config=canonical_config)
        assert "orientation" in error.value.user_message.lower()

    def test_an_ambiguous_mark_is_refused(self, canonical_config):
        # A second mark at the position the inverted hypothesis would sample
        # makes both hypotheses equally credible. Without a clear winner the
        # sheet must be refused, not decided by a coin toss.
        sheet = render_sheet(SyntheticSheetSpec())
        ambiguous = sheet.image.copy()
        mark = sheet.spec.orientation_center
        mirrored = sheet.spec.to_pixels(
            NormalizedPoint(x=1.0 - mark.x, y=1.0 - mark.y)
        )
        half_width = round(sheet.spec.orientation_width * sheet.spec.width / 2)
        half_height = round(sheet.spec.orientation_height * sheet.spec.height / 2)
        ambiguous[
            round(mirrored.y) - half_height : round(mirrored.y) + half_height,
            round(mirrored.x) - half_width : round(mirrored.x) + half_width,
        ] = 0

        two_marks = replace(sheet, image=ambiguous)
        with pytest.raises(OrientationDetectionError, match="margin"):
            align_sheet(
                apply_distortion(two_marks, DistortionSpec()).image, config=canonical_config
            )


class TestOrientationFallback:
    config = AlignmentConfig(orientation=OrientationConfig(allow_fallback=True))

    def test_the_fallback_produces_a_result(self):
        sheet = render_sheet(SyntheticSheetSpec(omit_orientation_marker=True))
        result = align_sheet(
            apply_distortion(sheet, DistortionSpec()).image, config=self.config
        )
        assert result.orientation.quarter_turns == 0

    def test_the_guess_is_always_declared(self):
        sheet = render_sheet(SyntheticSheetSpec(omit_orientation_marker=True))
        result = align_sheet(
            apply_distortion(sheet, DistortionSpec()).image, config=self.config
        )
        assert result.orientation.assumed is True
        assert AlignmentWarning.ORIENTATION_ASSUMED in result.warnings

    def test_no_mark_is_reported_when_none_was_found(self):
        sheet = render_sheet(SyntheticSheetSpec(omit_orientation_marker=True))
        result = align_sheet(
            apply_distortion(sheet, DistortionSpec()).image, config=self.config
        )
        assert result.orientation.marker_center is None
        assert result.orientation.marker_box is None

    def test_a_configured_fallback_orientation_is_honoured(self):
        sheet = render_sheet(SyntheticSheetSpec(omit_orientation_marker=True))
        config = AlignmentConfig(
            orientation=OrientationConfig(allow_fallback=True, fallback_quarter_turns=2)
        )
        result = align_sheet(apply_distortion(sheet, DistortionSpec()).image, config=config)
        assert result.orientation.quarter_turns == 2

    def test_the_fallback_does_not_hide_a_mark_that_is_present(
        self, canonical_sheet
    ):
        result = align_sheet(
            apply_distortion(canonical_sheet, DistortionSpec(rotation_degrees=180.0)).image,
            config=self.config,
        )
        assert result.orientation.assumed is False
        assert result.orientation.quarter_turns == 2


class TestOrientationWindows:
    def test_the_evidence_window_is_the_mark_scaled_by_the_margin(self):
        config = AlignmentConfig(canonical_width=1000, canonical_height=1000)
        evidence, _ = orientation_windows(config)
        orientation = config.orientation
        assert evidence.width == pytest.approx(
            orientation.marker_width * 1000 * orientation.window_margin
        )
        assert evidence.center.x == pytest.approx(orientation.marker_center_x * 1000)

    def test_the_localisation_window_is_larger_than_the_evidence_window(self):
        evidence, localisation = orientation_windows(AlignmentConfig())
        assert localisation.width > evidence.width
        assert localisation.height > evidence.height


class TestDetermineOrientationDirectly:
    def test_the_reported_positions_are_scaled_to_the_source_image(self, canonical_sheet):
        # determine_orientation works at the downscaled working resolution; the
        # positions it reports must already be back in source pixels.
        config = AlignmentConfig()
        distorted = apply_distortion(canonical_sheet, DistortionSpec())
        prepared = prepare_for_detection(distorted.image, config=config.preprocessing)
        corners = tuple(
            point.scaled(prepared.scale) for point in distorted.marker_centers
        )
        result = determine_orientation(
            prepared.binary,
            image_corner_points=corners,
            config=config,
            to_source_factor=prepared.to_source_factor,
        )
        assert result.quarter_turns == 0
        assert result.marker_center is not None
        assert result.marker_center.x < distorted.image.shape[1]
        assert result.marker_center.y < distorted.image.shape[0]


def _project(matrix, point) -> Point:
    """Map one point through a homography, independently of the code under test."""
    vector = np.array([point.x, point.y, 1.0], dtype=np.float64) @ matrix.T
    return Point(x=float(vector[0] / vector[2]), y=float(vector[1] / vector[2]))

"""Tests for the seam between the template designer and Phase 1's detector.

Runs real image decoding and marker detection against synthetic sheets - the
same generator Phase 1's own suite uses - so these are integration tests, not
unit tests, even though nothing here touches Qt.
"""

from __future__ import annotations

import numpy as np
import pytest

from omr_scanner.domain.template import MarkerRole
from omr_scanner.errors import ImageValidationError
from omr_scanner.imaging.models import IMAGE_CORNER_ORDER
from omr_scanner.imaging.synthetic import SyntheticSheetSpec, render_sheet
from omr_scanner.services.alignment_service import save_image
from omr_scanner.services.marker_detection_service import (
    MarkerSearchConfig,
    decode_image_file,
    detect_registration_markers,
)


@pytest.fixture
def sheet_path(tmp_path):
    """A clean synthetic sheet, written to a temporary PNG."""
    sheet = render_sheet(SyntheticSheetSpec())
    path = tmp_path / "sheet.png"
    save_image(sheet.image, path)
    return path, sheet


class TestDecodeImageFile:
    def test_a_colour_decode_reports_three_channels(self, sheet_path):
        path, sheet = sheet_path
        decoded = decode_image_file(path, color=True)
        assert decoded.channels == 3
        assert decoded.width == sheet.spec.width
        assert decoded.height == sheet.spec.height

    def test_a_grayscale_decode_reports_one_channel(self, sheet_path):
        path, _sheet = sheet_path
        decoded = decode_image_file(path, color=False)
        assert decoded.channels == 1

    def test_the_stride_matches_width_times_channels(self, sheet_path):
        path, _sheet = sheet_path
        decoded = decode_image_file(path, color=True)
        assert decoded.stride == decoded.width * decoded.channels

    def test_the_byte_count_matches_the_declared_dimensions(self, sheet_path):
        path, _sheet = sheet_path
        decoded = decode_image_file(path, color=True)
        assert len(decoded.data) == decoded.stride * decoded.height

    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(ImageValidationError):
            decode_image_file(tmp_path / "absent.png")

    def test_the_decoded_bytes_can_reconstruct_the_array(self, sheet_path):
        # What the GUI actually does: build a QImage-compatible buffer with no
        # numpy import of its own. Verified here by reconstructing the array
        # from the raw bytes and comparing against the source.
        path, sheet = sheet_path
        decoded = decode_image_file(path, color=False)
        reconstructed = np.frombuffer(decoded.data, dtype=np.uint8).reshape(
            decoded.height, decoded.width
        )
        assert np.array_equal(reconstructed, sheet.image)


class TestDetectRegistrationMarkers:
    def test_every_corner_is_found_on_a_clean_sheet(self, sheet_path):
        path, _sheet = sheet_path
        outcome = detect_registration_markers(path)
        for corner in IMAGE_CORNER_ORDER:
            assert outcome.markers[corner.value].found is True

    def test_found_markers_report_a_score_and_a_position(self, sheet_path):
        path, _sheet = sheet_path
        outcome = detect_registration_markers(path)
        for role in MarkerRole:
            detected = outcome.markers[role.value]
            assert 0.0 < detected.score <= 1.0
            assert detected.width > 0.0
            assert detected.height > 0.0

    def test_detected_positions_match_the_rendered_markers(self, sheet_path):
        path, sheet = sheet_path
        outcome = detect_registration_markers(path)
        for role, expected in zip(MarkerRole, sheet.marker_centers, strict=True):
            detected = outcome.markers[role.value]
            assert detected.x == pytest.approx(expected.x, abs=2.0)
            assert detected.y == pytest.approx(expected.y, abs=2.0)

    def test_a_missing_marker_is_reported_per_corner_not_as_a_failure(self, tmp_path):
        sheet = render_sheet(
            SyntheticSheetSpec(omit_markers=frozenset({MarkerRole.TOP_LEFT}))
        )
        path = tmp_path / "sheet.png"
        save_image(sheet.image, path)

        outcome = detect_registration_markers(path)  # must not raise
        assert outcome.markers[MarkerRole.TOP_LEFT.value].found is False
        assert outcome.markers[MarkerRole.TOP_LEFT.value].reason
        # The other three corners are unaffected by one missing marker - this
        # is the whole point of scoring corners independently (see the module
        # docstring): a person can still place the fourth by hand.
        for role in MarkerRole:
            if role is not MarkerRole.TOP_LEFT:
                assert outcome.markers[role.value].found is True

    def test_two_missing_markers_are_each_reported(self, tmp_path):
        sheet = render_sheet(
            SyntheticSheetSpec(
                omit_markers=frozenset({MarkerRole.TOP_LEFT, MarkerRole.BOTTOM_RIGHT})
            )
        )
        path = tmp_path / "sheet.png"
        save_image(sheet.image, path)

        outcome = detect_registration_markers(path)
        assert outcome.markers[MarkerRole.TOP_LEFT.value].found is False
        assert outcome.markers[MarkerRole.BOTTOM_RIGHT.value].found is False
        assert outcome.markers[MarkerRole.TOP_RIGHT.value].found is True
        assert outcome.markers[MarkerRole.BOTTOM_LEFT.value].found is True

    def test_a_blank_page_reports_every_corner_missing(self, tmp_path):
        blank = np.full((1754, 1240), 255, dtype=np.uint8)
        path = tmp_path / "blank.png"
        save_image(blank, path)

        outcome = detect_registration_markers(path)
        assert all(not marker.found for marker in outcome.markers.values())
        assert outcome.candidate_count == 0

    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(ImageValidationError):
            detect_registration_markers(tmp_path / "absent.png")

    def test_a_custom_search_config_is_honoured(self, sheet_path):
        path, _sheet = sheet_path
        # An unreasonably strict acceptance floor rejects every candidate.
        config = MarkerSearchConfig(min_candidate_score=0.999)
        outcome = detect_registration_markers(path, config=config)
        assert all(not marker.found for marker in outcome.markers.values())

    def test_the_result_never_returns_none_for_a_corner(self, sheet_path):
        # Every corner always has an entry, found or not - the GUI indexes
        # this dict unconditionally for all four roles plus orientation logic.
        path, _sheet = sheet_path
        outcome = detect_registration_markers(path)
        assert set(outcome.markers) == {corner.value for corner in IMAGE_CORNER_ORDER}

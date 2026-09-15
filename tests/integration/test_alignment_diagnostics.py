"""Diagnostic capture and rendering.

Two properties matter here. The first is that diagnostics say something useful
about why a particular shape was chosen. The second, and the one worth a test of
its own, is that turning them on changes nothing about the decision - a
diagnostic mode that quietly altered behaviour would be worse than none, because
every investigation would then be of a different system.

Nothing in this module writes into the repository; where a file is needed it
goes to pytest's temporary directory.
"""

from __future__ import annotations

import numpy as np
import pytest

from omr_scanner.imaging import AlignmentConfig, align_sheet
from omr_scanner.imaging.config import OrientationConfig
from omr_scanner.imaging.diagnostics import (
    render_detection_overlay,
    render_normalized_preview,
    summarize,
)
from omr_scanner.imaging.models import IMAGE_CORNER_ORDER
from omr_scanner.imaging.synthetic import (
    DistortionSpec,
    SyntheticSheetSpec,
    apply_distortion,
    render_sheet,
)
from omr_scanner.services.alignment_service import save_image

DIAGNOSTIC_CONFIG = AlignmentConfig(diagnostics=True)


@pytest.fixture
def scan(canonical_sheet):
    """A moderately distorted synthetic scan."""
    return apply_distortion(
        canonical_sheet,
        DistortionSpec(rotation_degrees=5.0, perspective_strength=0.02, seed=41),
    )


@pytest.fixture
def diagnosed(scan):
    """An alignment result carrying diagnostics."""
    return align_sheet(scan.image, config=DIAGNOSTIC_CONFIG)


class TestDiagnosticsAreInert:
    def test_the_decision_is_identical_with_and_without_diagnostics(
        self, scan, canonical_config
    ):
        plain = align_sheet(scan.image, config=canonical_config)
        diagnosed = align_sheet(scan.image, config=DIAGNOSTIC_CONFIG)
        assert np.allclose(plain.transform_matrix, diagnosed.transform_matrix)
        assert np.array_equal(plain.normalized_image, diagnosed.normalized_image)
        assert plain.warnings == diagnosed.warnings
        assert plain.orientation.quarter_turns == diagnosed.orientation.quarter_turns

    def test_diagnostics_are_off_by_default(self, scan):
        assert align_sheet(scan.image).diagnostics is None

    def test_rendering_an_overlay_does_not_modify_the_scan(self, scan, diagnosed):
        before = scan.image.copy()
        render_detection_overlay(scan.image, diagnosed)
        assert np.array_equal(scan.image, before)

    def test_rendering_a_preview_does_not_modify_the_result(self, diagnosed):
        before = diagnosed.normalized_image.copy()
        render_normalized_preview(diagnosed, config=DIAGNOSTIC_CONFIG)
        assert np.array_equal(diagnosed.normalized_image, before)


class TestDiagnosticContent:
    def test_the_accepted_candidates_are_recorded(self, diagnosed):
        assert diagnosed.diagnostics is not None
        assert len(diagnosed.diagnostics.candidates) == diagnosed.metrics.candidate_count

    def test_the_rejections_are_recorded_with_reasons(self, diagnosed):
        rejected = diagnosed.diagnostics.rejected
        assert rejected
        assert len(rejected) == diagnosed.metrics.rejected_count
        assert all(item.reason for item in rejected)

    def test_every_candidate_is_scored_against_every_corner(self, diagnosed):
        expected = len(diagnosed.diagnostics.candidates) * len(IMAGE_CORNER_ORDER)
        assert len(diagnosed.diagnostics.scored) == expected

    def test_the_working_images_are_retained(self, diagnosed):
        assert diagnosed.diagnostics.grayscale.ndim == 2
        assert diagnosed.diagnostics.binary.ndim == 2
        assert diagnosed.diagnostics.binary.shape == diagnosed.diagnostics.grayscale.shape

    def test_the_source_quadrilateral_is_in_canonical_corner_order(self, diagnosed):
        assert diagnosed.diagnostics.source_quadrilateral == diagnosed.source_quadrilateral

    def test_candidate_measurements_are_in_source_coordinates(self, diagnosed, scan):
        # The working copy may be downscaled; a diagnostic drawn on the original
        # scan has to use the original's coordinates.
        height, width = scan.image.shape[:2]
        for candidate in diagnosed.diagnostics.candidates:
            assert 0 <= candidate.center.x <= width
            assert 0 <= candidate.center.y <= height


class TestOverlayRendering:
    def test_the_overlay_is_a_colour_image_the_size_of_the_scan(self, scan, diagnosed):
        overlay = render_detection_overlay(scan.image, diagnosed)
        assert overlay.shape == (*scan.image.shape[:2], 3)
        assert overlay.dtype == np.uint8

    def test_the_overlay_adds_colour_to_a_grayscale_scan(self, scan, diagnosed):
        overlay = render_detection_overlay(scan.image, diagnosed)
        channels_differ = np.any(overlay[:, :, 0] != overlay[:, :, 1])
        assert channels_differ

    def test_the_overlay_works_without_diagnostics(self, scan, canonical_config):
        # A sheet aligned in production carries no diagnostics; the overlay must
        # still draw what the result does contain rather than fail.
        plain = align_sheet(scan.image, config=canonical_config)
        overlay = render_detection_overlay(scan.image, plain)
        assert overlay.shape == (*scan.image.shape[:2], 3)

    def test_rejected_contours_can_be_omitted(self, scan, diagnosed):
        with_rejects = render_detection_overlay(scan.image, diagnosed, include_rejected=True)
        without = render_detection_overlay(scan.image, diagnosed, include_rejected=False)
        assert not np.array_equal(with_rejects, without)

    @pytest.mark.parametrize("channels", [None, 3, 4])
    def test_the_overlay_accepts_every_scan_layout(self, scan, diagnosed, channels):
        image = scan.image
        if channels == 3:
            image = np.repeat(image[:, :, None], 3, axis=2)
        elif channels == 4:
            image = np.dstack([image] * 3 + [np.full_like(image, 255)])
        assert render_detection_overlay(image, diagnosed).shape[2] == 3

    def test_the_preview_is_the_canonical_page_in_colour(self, diagnosed):
        preview = render_normalized_preview(diagnosed, config=DIAGNOSTIC_CONFIG)
        assert preview.shape == (
            DIAGNOSTIC_CONFIG.canonical_height,
            DIAGNOSTIC_CONFIG.canonical_width,
            3,
        )


class TestSummary:
    def test_the_summary_reports_the_measured_quantities(self, diagnosed):
        text = summarize(diagnosed)
        for heading in (
            "source",
            "canonical",
            "candidates",
            "quarter turns",
            "orientation",
            "marker scores",
            "aspect ratio",
            "reprojection",
            "warnings",
        ):
            assert heading in text

    def test_the_summary_names_every_corner(self, diagnosed):
        text = summarize(diagnosed)
        for label in ("TL", "TR", "BR", "BL"):
            assert label in text

    def test_an_assumed_orientation_is_visible_in_the_summary(self):
        sheet = render_sheet(SyntheticSheetSpec(omit_orientation_marker=True))
        config = AlignmentConfig(orientation=OrientationConfig(allow_fallback=True))
        result = align_sheet(apply_distortion(sheet, DistortionSpec()).image, config=config)
        assert "(assumed)" in summarize(result)
        assert "ORIENTATION_ASSUMED" in summarize(result)

    def test_a_clean_alignment_reports_no_warnings(self, canonical_sheet, canonical_config):
        result = align_sheet(
            apply_distortion(canonical_sheet, DistortionSpec()).image, config=canonical_config
        )
        assert "warnings          none" in summarize(result)


class TestWritingDiagnostics:
    def test_diagnostic_images_can_be_written_to_a_temporary_directory(
        self, scan, diagnosed, tmp_path
    ):
        directory = tmp_path / "debug"
        overlay = render_detection_overlay(scan.image, diagnosed)
        preview = render_normalized_preview(diagnosed, config=DIAGNOSTIC_CONFIG)
        save_image(overlay, directory / "detection.png")
        save_image(preview, directory / "normalized.png")
        assert (directory / "detection.png").stat().st_size > 0
        assert (directory / "normalized.png").stat().st_size > 0

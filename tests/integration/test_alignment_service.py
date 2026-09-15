"""The boundary between a template document and the alignment engine.

This is the seam the architecture depends on: ``imaging`` knows nothing about
``.omrt`` files, and the template subsystem knows nothing about pixels. These
tests check that everything a template declares about geometry actually arrives
at the engine, and that reading and writing image files behaves.
"""

from __future__ import annotations

import numpy as np
import pytest

from omr_scanner.domain.geometry import NormalizedPoint, NormalizedSize
from omr_scanner.domain.template import (
    MarkerRole,
    MarkerShape,
    OmrTemplate,
    OrientationMarker,
    PageGeometry,
    RegistrationMarker,
)
from omr_scanner.errors import ImageValidationError
from omr_scanner.imaging import align_sheet
from omr_scanner.imaging.config import GeometryConfig, PreprocessingConfig, ThresholdStrategy
from omr_scanner.imaging.synthetic import (
    DistortionSpec,
    SyntheticSheetSpec,
    apply_distortion,
    control_point_errors,
    render_sheet,
)
from omr_scanner.services import load_template
from omr_scanner.services.alignment_service import (
    alignment_config_from_template,
    load_scan_image,
    save_image,
)


def template_for(
    *,
    width: int = 1000,
    height: int = 1400,
    marker_center: float = 0.04,
    marker_size: tuple[float, float] = (0.035, 0.025),
) -> OmrTemplate:
    """Build a minimal but valid template with a known geometry."""
    inset = marker_center
    corners = {
        MarkerRole.TOP_LEFT: (inset, inset),
        MarkerRole.TOP_RIGHT: (1.0 - inset, inset),
        MarkerRole.BOTTOM_RIGHT: (1.0 - inset, 1.0 - inset),
        MarkerRole.BOTTOM_LEFT: (inset, 1.0 - inset),
    }
    return OmrTemplate(
        name="Geometry fixture",
        page=PageGeometry(
            width_mm=210.0,
            height_mm=297.0,
            canonical_width_px=width,
            canonical_height_px=height,
        ),
        registration_markers=tuple(
            RegistrationMarker(
                role=role,
                shape=MarkerShape.FILLED_SQUARE,
                center=NormalizedPoint(x=x, y=y),
                size=NormalizedSize(width=marker_size[0], height=marker_size[1]),
                search_radius=0.06,
            )
            for role, (x, y) in corners.items()
        ),
        orientation_marker=OrientationMarker(
            shape=MarkerShape.FILLED_RECTANGLE,
            center=NormalizedPoint(x=0.16, y=0.04),
            size=NormalizedSize(width=0.06, height=0.014),
            expected_near=MarkerRole.TOP_LEFT,
            search_radius=0.07,
        ),
    )


class TestConfigurationFromTemplate:
    def test_the_canonical_page_size_comes_from_the_template(self):
        config = alignment_config_from_template(template_for(width=1500, height=2100))
        assert config.canonical_size == (1500, 2100)

    def test_every_marker_centre_comes_from_the_template(self):
        template = template_for(marker_center=0.07)
        config = alignment_config_from_template(template)
        for marker in template.registration_markers:
            assert config.marker_targets[marker.role] == marker.center

    def test_the_marker_size_comes_from_the_template(self):
        config = alignment_config_from_template(template_for(marker_size=(0.05, 0.03)))
        assert config.marker_detection.expected_marker_width == pytest.approx(0.05)
        assert config.marker_detection.expected_marker_height == pytest.approx(0.03)

    def test_the_orientation_mark_comes_from_the_template(self):
        template = template_for()
        config = alignment_config_from_template(template)
        mark = template.orientation_marker
        assert config.orientation.marker_center_x == pytest.approx(mark.center.x)
        assert config.orientation.marker_center_y == pytest.approx(mark.center.y)
        assert config.orientation.marker_width == pytest.approx(mark.size.width)
        assert config.orientation.marker_height == pytest.approx(mark.size.height)
        assert config.orientation.search_radius == pytest.approx(mark.search_radius)

    def test_engine_tuning_keeps_its_defaults(self):
        # Thresholds and score weights are engine tuning, not sheet design, so
        # they are not the template's to supply.
        config = alignment_config_from_template(template_for())
        assert config.marker_detection.min_candidate_score == pytest.approx(0.45)
        assert config.geometry.max_aspect_ratio_deviation == pytest.approx(0.30)

    def test_tuning_can_still_be_overridden(self):
        config = alignment_config_from_template(
            template_for(),
            preprocessing=PreprocessingConfig(
                threshold_strategy=ThresholdStrategy.ADAPTIVE_MEAN
            ),
            geometry=GeometryConfig(max_aspect_ratio_deviation=0.1),
        )
        assert config.preprocessing.threshold_strategy is ThresholdStrategy.ADAPTIVE_MEAN
        assert config.geometry.max_aspect_ratio_deviation == pytest.approx(0.1)

    def test_diagnostics_are_off_unless_requested(self):
        assert alignment_config_from_template(template_for()).diagnostics is False
        assert alignment_config_from_template(template_for(), diagnostics=True).diagnostics

    def test_the_shipped_example_template_produces_a_usable_configuration(
        self, example_template_path
    ):
        config = alignment_config_from_template(load_template(example_template_path))
        assert config.canonical_size == (1240, 1754)
        assert len(config.marker_targets) == 4
        assert config.expected_quadrilateral_aspect_ratio > 0.0

    def test_a_template_derived_configuration_actually_aligns_its_own_sheet(self):
        # The end-to-end check that the conversion is faithful: render a sheet
        # from the template's geometry, distort it, and align it back.
        template = template_for(width=1000, height=1400, marker_center=0.06)
        config = alignment_config_from_template(template)
        sheet = render_sheet(
            SyntheticSheetSpec(
                width=template.page.canonical_width_px,
                height=template.page.canonical_height_px,
                marker_targets=config.marker_targets,
                marker_width=config.marker_detection.expected_marker_width,
                marker_height=config.marker_detection.expected_marker_height,
                orientation_center=template.orientation_marker.center,
                orientation_width=template.orientation_marker.size.width,
                orientation_height=template.orientation_marker.size.height,
            )
        )
        distorted = apply_distortion(
            sheet, DistortionSpec(rotation_degrees=5.0, perspective_strength=0.02, seed=8)
        )
        result = align_sheet(distorted.image, config=config)
        assert result.normalized_image.shape[:2] == (1400, 1000)
        assert max(control_point_errors(result.transform_matrix, distorted)) < 1.5


class TestImageIO:
    def test_an_image_round_trips_through_disk(self, tmp_path, canonical_sheet):
        path = tmp_path / "sheet.png"
        save_image(canonical_sheet.image, path)
        assert np.array_equal(load_scan_image(path), canonical_sheet.image)

    def test_a_saved_sheet_still_aligns(self, tmp_path, canonical_sheet, canonical_config):
        distorted = apply_distortion(canonical_sheet, DistortionSpec(rotation_degrees=4.0))
        path = tmp_path / "scan.png"
        save_image(distorted.image, path)
        result = align_sheet(load_scan_image(path), config=canonical_config)
        assert max(control_point_errors(result.transform_matrix, distorted)) < 1.5

    def test_a_non_ascii_path_works(self, tmp_path, canonical_sheet):
        # Examination folders are frequently named in the local language, which
        # is why decoding goes through fromfile/imdecode rather than imread.
        path = tmp_path / "পরীক্ষা" / "শীট.png"
        save_image(canonical_sheet.image, path)
        assert load_scan_image(path).shape == canonical_sheet.image.shape

    def test_a_colour_read_returns_three_channels(self, tmp_path, canonical_sheet):
        path = tmp_path / "sheet.png"
        save_image(canonical_sheet.image, path)
        assert load_scan_image(path, color=True).ndim == 3

    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(ImageValidationError):
            load_scan_image(tmp_path / "absent.png")

    def test_an_empty_file_is_refused(self, tmp_path):
        path = tmp_path / "empty.png"
        path.write_bytes(b"")
        with pytest.raises(ImageValidationError, match="empty"):
            load_scan_image(path)

    def test_a_file_that_is_not_an_image_is_refused(self, tmp_path):
        path = tmp_path / "notes.png"
        path.write_text("this is not a scan", encoding="utf-8")
        with pytest.raises(ImageValidationError, match="decoded"):
            load_scan_image(path)

    def test_saving_refuses_to_replace_an_existing_file(self, tmp_path, canonical_sheet):
        path = tmp_path / "sheet.png"
        save_image(canonical_sheet.image, path)
        with pytest.raises(ImageValidationError, match="overwrite"):
            save_image(canonical_sheet.image, path)

    def test_saving_can_be_told_to_overwrite(self, tmp_path, canonical_sheet):
        path = tmp_path / "sheet.png"
        save_image(canonical_sheet.image, path)
        save_image(canonical_sheet.image[::2, ::2], path, overwrite=True)
        assert load_scan_image(path).shape[0] == canonical_sheet.image.shape[0] // 2

    def test_an_unknown_extension_is_refused(self, tmp_path, canonical_sheet):
        with pytest.raises(ImageValidationError, match=r"cannot write|encode"):
            save_image(canonical_sheet.image, tmp_path / "sheet.xyz")

    def test_missing_parent_directories_are_created(self, tmp_path, canonical_sheet):
        path = tmp_path / "a" / "b" / "sheet.png"
        save_image(canonical_sheet.image, path)
        assert path.is_file()

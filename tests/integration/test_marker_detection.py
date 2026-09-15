"""Registration-marker detection on rendered pages.

These tests run the real preprocessing and contour analysis over synthetic
sheets. The recurring question is not "were four shapes found" but "were the
*right* four found" - the sheets deliberately carry shapes that a weaker
detector would take instead.
"""

from __future__ import annotations

import numpy as np
import pytest

from omr_scanner.domain.geometry import NormalizedPoint
from omr_scanner.domain.template import MarkerRole
from omr_scanner.errors import AmbiguousMarkerError, InsufficientMarkersError
from omr_scanner.imaging.config import (
    AlignmentConfig,
    MarkerDetectionConfig,
    PreprocessingConfig,
    ThresholdStrategy,
)
from omr_scanner.imaging.marker_detection import (
    corner_reference_point,
    corner_search_region,
    detect_marker_candidates,
    score_candidate,
    select_corner_markers,
)
from omr_scanner.imaging.models import IMAGE_CORNER_ORDER, ImageCorner
from omr_scanner.imaging.preprocessing import prepare_for_detection
from omr_scanner.imaging.synthetic import (
    DistortionSpec,
    SyntheticSheetSpec,
    apply_distortion,
    render_sheet,
)

CENTRE_TOLERANCE_PX = 2.0
"""How far a detected centroid may sit from the rendered marker centre.

Detection runs on a thresholded, possibly downscaled copy, so a fraction of a
pixel of disagreement is expected; two pixels on a 1240 px page is 0.16 per cent
of the page width and well below what the marker-size filters care about.
"""


def detect(image, config: AlignmentConfig | None = None):
    """Run preprocessing and detection, returning candidates and the prepared image."""
    active = config if config is not None else AlignmentConfig()
    prepared = prepare_for_detection(image, config=active.preprocessing)
    accepted, rejected = detect_marker_candidates(prepared.binary, config=active)
    return prepared, accepted, rejected


def select(image, config: AlignmentConfig | None = None):
    """Run detection and corner selection, returning the four chosen candidates."""
    active = config if config is not None else AlignmentConfig()
    prepared, accepted, _ = detect(image, active)
    selected, alternatives = select_corner_markers(
        accepted,
        image_width=prepared.working_width,
        image_height=prepared.working_height,
        config=active,
    )
    factor = prepared.to_source_factor
    return tuple(item.candidate.scaled(factor) for item in selected), alternatives


def nearest_distance(candidates, target):
    """Return the distance from ``target`` to the closest candidate centre."""
    return min(candidate.center.distance_to(target) for candidate in candidates)


class TestCleanPage:
    def test_exactly_the_four_markers_are_accepted(self, canonical_sheet, canonical_config):
        _, accepted, _ = detect(canonical_sheet.image, canonical_config)
        assert len(accepted) == 4

    def test_the_accepted_candidates_are_the_rendered_markers(
        self, canonical_sheet, canonical_config
    ):
        _, accepted, _ = detect(canonical_sheet.image, canonical_config)
        for center in canonical_sheet.marker_centers:
            assert nearest_distance(accepted, center) < CENTRE_TOLERANCE_PX

    def test_the_page_clutter_is_rejected_with_a_named_reason(
        self, canonical_sheet, canonical_config
    ):
        _, _, rejected = detect(canonical_sheet.image, canonical_config)
        assert rejected
        assert all(item.reason for item in rejected)

    def test_hollow_answer_frames_are_rejected_for_their_interior(
        self, canonical_config
    ):
        # Their outline is identical to a marker's; only the interior ink test
        # separates them, so that must be the reason recorded.
        sheet = render_sheet(
            SyntheticSheetSpec(
                draw_bubbles=False, draw_text_bars=False, draw_answer_frames=True
            )
        )
        _, accepted, rejected = detect(sheet.image, canonical_config)
        assert len(accepted) == 4
        assert any(item.reason == "fill_ratio_too_low" for item in rejected)

    def test_the_selected_markers_are_one_per_corner(self, canonical_sheet, canonical_config):
        selected, _ = select(canonical_sheet.image, canonical_config)
        assert len(selected) == 4
        for center in canonical_sheet.marker_centers:
            assert nearest_distance(selected, center) < CENTRE_TOLERANCE_PX

    def test_a_clean_page_reports_no_competing_candidates(
        self, canonical_sheet, canonical_config
    ):
        _, alternatives = select(canonical_sheet.image, canonical_config)
        assert alternatives == (0, 0, 0, 0)


class TestMeasuredProperties:
    def test_a_marker_is_measured_as_a_solid_square(self, canonical_sheet, canonical_config):
        _, accepted, _ = detect(canonical_sheet.image, canonical_config)
        for candidate in accepted:
            assert candidate.aspect_ratio == pytest.approx(1.0, abs=0.15)
            assert candidate.rectangularity > 0.95
            assert candidate.solidity > 0.95
            assert candidate.fill_ratio > 0.95

    def test_the_measured_area_ratio_matches_the_printed_size(
        self, canonical_sheet, canonical_config
    ):
        _, accepted, _ = detect(canonical_sheet.image, canonical_config)
        expected = canonical_config.expected_marker_area_ratio
        for candidate in accepted:
            assert candidate.area_ratio == pytest.approx(expected, rel=0.15)

    def test_the_centre_barely_moves_when_a_marker_corner_is_chipped(self, canonical_config):
        # The centroid is used precisely because it is an area-weighted average:
        # damage moves it in proportion to the area lost, not by the full extent
        # of the missing pixels the way a bounding rectangle would.
        sheet = render_sheet(SyntheticSheetSpec())
        _, clean, _ = detect(sheet.image, canonical_config)
        intact = min(
            clean, key=lambda c: c.center.distance_to(sheet.marker_centers[0])
        )

        chipped = sheet.image.copy()
        center = sheet.marker_centers[0]
        side = round(canonical_config.marker_detection.expected_marker_width * sheet.spec.width)
        top, left = round(center.y - side / 2), round(center.x - side / 2)
        chip = max(2, side // 4)
        chipped[top : top + chip, left : left + chip] = 255

        _, damaged, _ = detect(chipped, canonical_config)
        moved = min(damaged, key=lambda c: c.center.distance_to(center))
        assert moved.center.distance_to(intact.center) < side * 0.12


class TestDegradedPages:
    @pytest.mark.parametrize(
        ("label", "spec"),
        [
            ("dark scan", DistortionSpec(brightness_gain=0.55)),
            ("bright scan", DistortionSpec(brightness_offset=70.0)),
            ("mild blur", DistortionSpec(blur_kernel_px=5)),
            ("heavy blur", DistortionSpec(blur_kernel_px=15)),
            ("scanner noise", DistortionSpec(noise_sigma=10.0, seed=17)),
            ("heavy noise", DistortionSpec(noise_sigma=30.0, seed=18)),
            ("jpeg artefacts", DistortionSpec(jpeg_quality=25)),
            ("low contrast", DistortionSpec(brightness_gain=0.4, brightness_offset=110.0)),
        ],
    )
    def test_the_markers_are_still_found(
        self, canonical_sheet, canonical_config, label, spec
    ):
        distorted = apply_distortion(canonical_sheet, spec)
        selected, _ = select(distorted.image, canonical_config)
        for center in distorted.marker_centers:
            assert nearest_distance(selected, center) < CENTRE_TOLERANCE_PX, label

    def test_small_noise_specks_do_not_become_candidates(
        self, canonical_sheet, canonical_config
    ):
        generator = np.random.default_rng(23)
        speckled = canonical_sheet.image.copy()
        rows = generator.integers(0, speckled.shape[0], size=4000)
        columns = generator.integers(0, speckled.shape[1], size=4000)
        speckled[rows, columns] = 0

        _, accepted, _ = detect(speckled, canonical_config)
        assert len(accepted) == 4


class TestCompetingShapes:
    def test_a_solid_decoy_inside_a_corner_region_does_not_win(self, canonical_config):
        # Four extra marker-sized solid squares, placed inside the corner search
        # regions but further from each corner than the real markers. A detector
        # that took "the four largest black shapes" would be undecidable here;
        # position is what settles it.
        decoys = (
            NormalizedPoint(x=0.22, y=0.16),
            NormalizedPoint(x=0.78, y=0.16),
            NormalizedPoint(x=0.22, y=0.84),
            NormalizedPoint(x=0.78, y=0.84),
        )
        sheet = render_sheet(SyntheticSheetSpec(decoy_markers=decoys))
        selected, alternatives = select(sheet.image, canonical_config)
        for center in sheet.marker_centers:
            assert nearest_distance(selected, center) < CENTRE_TOLERANCE_PX
        assert all(count > 0 for count in alternatives)

    def test_a_large_solid_logo_is_rejected_by_size(self, canonical_config):
        sheet = render_sheet(SyntheticSheetSpec())
        with_logo = sheet.image.copy()
        with_logo[300:500, 700:900] = 0
        _, accepted, rejected = detect(with_logo, canonical_config)
        assert len(accepted) == 4
        assert any(item.reason == "area_ratio_too_large" for item in rejected)

    def test_a_marker_sized_shape_in_the_middle_is_ignored(self, canonical_config):
        sheet = render_sheet(SyntheticSheetSpec())
        with_blob = sheet.image.copy()
        center = sheet.spec.to_pixels(NormalizedPoint(x=0.5, y=0.5))
        side = round(sheet.spec.marker_width * sheet.spec.width)
        top, left = round(center.y - side / 2), round(center.x - side / 2)
        with_blob[top : top + side, left : left + side] = 0

        selected, _ = select(with_blob, canonical_config)
        for candidate in selected:
            assert candidate.center.distance_to(center) > side * 4


class TestResolutionIndependence:
    @pytest.mark.parametrize(("width", "height"), [(620, 877), (1240, 1754), (2480, 3508)])
    def test_the_same_configuration_serves_every_resolution(self, width, height):
        sheet = render_sheet(SyntheticSheetSpec(width=width, height=height))
        config = AlignmentConfig(canonical_width=width, canonical_height=height)
        selected, _ = select(sheet.image, config)
        page_diagonal = float(np.hypot(width, height))
        for center in sheet.marker_centers:
            assert nearest_distance(selected, center) < 0.002 * page_diagonal


class TestThresholdStrategies:
    @pytest.mark.parametrize(
        "strategy",
        [
            ThresholdStrategy.OTSU,
            ThresholdStrategy.ADAPTIVE_MEAN,
            ThresholdStrategy.ADAPTIVE_GAUSSIAN,
        ],
    )
    def test_every_strategy_finds_the_markers_on_an_even_page(
        self, canonical_sheet, strategy
    ):
        config = AlignmentConfig(
            preprocessing=PreprocessingConfig(threshold_strategy=strategy)
        )
        selected, _ = select(canonical_sheet.image, config)
        for center in canonical_sheet.marker_centers:
            assert nearest_distance(selected, center) < CENTRE_TOLERANCE_PX


class TestSearchRegions:
    @pytest.mark.parametrize("corner", IMAGE_CORNER_ORDER)
    def test_each_region_sits_against_its_own_corner(self, corner, canonical_config):
        region = corner_search_region(
            corner, image_width=1000, image_height=2000, config=canonical_config
        )
        reference = corner_reference_point(corner, image_width=1000, image_height=2000)
        assert region.x <= reference.x <= region.right
        assert region.y <= reference.y <= region.bottom

    def test_regions_are_proportional_to_the_image(self, canonical_config):
        small = corner_search_region(
            ImageCorner.TOP_LEFT, image_width=500, image_height=700, config=canonical_config
        )
        large = corner_search_region(
            ImageCorner.TOP_LEFT, image_width=1000, image_height=1400, config=canonical_config
        )
        assert large.width == pytest.approx(small.width * 2.0)
        assert large.height == pytest.approx(small.height * 2.0)


class TestScoring:
    def test_a_marker_at_its_own_corner_scores_higher_than_one_further_away(
        self, canonical_sheet, canonical_config
    ):
        _, accepted, _ = detect(canonical_sheet.image, canonical_config)
        prepared = prepare_for_detection(
            canonical_sheet.image, config=canonical_config.preprocessing
        )
        scored = [
            score_candidate(
                candidate,
                corner=ImageCorner.TOP_LEFT,
                image_width=prepared.working_width,
                image_height=prepared.working_height,
                config=canonical_config,
            )
            for candidate in accepted
        ]
        best = max(scored, key=lambda item: item.score)
        assert best.candidate.center.x < prepared.working_width / 2
        assert best.candidate.center.y < prepared.working_height / 2

    def test_the_breakdown_names_every_component(self, canonical_sheet, canonical_config):
        _, accepted, _ = detect(canonical_sheet.image, canonical_config)
        prepared = prepare_for_detection(
            canonical_sheet.image, config=canonical_config.preprocessing
        )
        scored = score_candidate(
            accepted[0],
            corner=ImageCorner.TOP_LEFT,
            image_width=prepared.working_width,
            image_height=prepared.working_height,
            config=canonical_config,
        )
        assert set(scored.breakdown) == {
            "area",
            "aspect",
            "rectangularity",
            "solidity",
            "fill",
            "shape",
            "proximity",
        }
        assert all(0.0 <= value <= 1.0 for value in scored.breakdown.values())


class TestSelectionFailures:
    @pytest.mark.parametrize("corner", IMAGE_CORNER_ORDER)
    def test_a_missing_corner_fails_by_name(self, corner, canonical_config):
        role = MarkerRole(corner.value)
        sheet = render_sheet(SyntheticSheetSpec(omit_markers=frozenset({role})))
        with pytest.raises(InsufficientMarkersError) as error:
            select(sheet.image, canonical_config)
        assert corner.value in str(error.value)

    def test_a_blank_page_fails_for_all_four_corners(self, canonical_config):
        blank = np.full((1754, 1240), 255, dtype=np.uint8)
        with pytest.raises(InsufficientMarkersError) as error:
            select(blank, canonical_config)
        for corner in IMAGE_CORNER_ORDER:
            assert corner.value in str(error.value)

    def test_one_shared_candidate_cannot_serve_two_corners(self, canonical_config):
        # A page whose only marker-like shape sits where two overlapping corner
        # regions both see it. Selection must refuse rather than use it twice.
        tiny = np.full((300, 300), 255, dtype=np.uint8)
        side = 16
        tiny[142 : 142 + side, 142 : 142 + side] = 0
        config = AlignmentConfig(
            canonical_width=300,
            canonical_height=300,
            marker_targets=canonical_config.marker_targets,
            marker_detection=MarkerDetectionConfig(
                expected_marker_width=side / 300,
                expected_marker_height=side / 300,
                corner_search_width=1.0,
                corner_search_height=1.0,
            ),
        )
        with pytest.raises(AmbiguousMarkerError):
            select(tiny, config)

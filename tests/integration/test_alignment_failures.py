"""How alignment refuses a sheet it cannot handle.

For examination software a reliable refusal is worth more than a heroic
recovery: a sheet that fails visibly is reprocessed, while one that is
rectified from three real corners and one invented one produces a complete,
plausible and wrong set of answers that nobody looks at again.

Every case here therefore asserts three things: that the failure happens, that
it carries the right machine-readable code, and that the message a user sees is
plain language rather than a traceback.
"""

from __future__ import annotations

import numpy as np
import pytest

from omr_scanner.domain.geometry import NormalizedPoint
from omr_scanner.domain.template import MarkerRole
from omr_scanner.errors import (
    ImageValidationError,
    ImagingError,
    InsufficientMarkersError,
    InvalidPageGeometryError,
    OMRScannerError,
)
from omr_scanner.imaging import AlignmentConfig, align_sheet
from omr_scanner.imaging.config import GeometryConfig, MarkerDetectionConfig
from omr_scanner.imaging.synthetic import (
    DistortionSpec,
    SyntheticSheetSpec,
    apply_distortion,
    render_sheet,
)


def scan_of(spec: SyntheticSheetSpec, distortion: DistortionSpec | None = None):
    """Render a sheet and return a synthetic scan of it."""
    return apply_distortion(render_sheet(spec), distortion or DistortionSpec()).image


class TestMissingMarkers:
    @pytest.mark.parametrize("role", list(MarkerRole))
    def test_a_missing_corner_fails_rather_than_being_extrapolated(
        self, role, canonical_config
    ):
        image = scan_of(SyntheticSheetSpec(omit_markers=frozenset({role})))
        with pytest.raises(InsufficientMarkersError) as error:
            align_sheet(image, config=canonical_config)
        assert error.value.code == "INSUFFICIENT_MARKERS"

    @pytest.mark.parametrize("role", list(MarkerRole))
    def test_the_failure_names_the_corner(self, role, canonical_config):
        image = scan_of(SyntheticSheetSpec(omit_markers=frozenset({role})))
        with pytest.raises(InsufficientMarkersError) as error:
            align_sheet(image, config=canonical_config)
        assert role.value in str(error.value)

    def test_three_markers_are_not_enough(self, canonical_config):
        # The Phase 1 rule, stated as a test: three real corners plus one
        # invented one is worse than no answer at all.
        image = scan_of(
            SyntheticSheetSpec(omit_markers=frozenset({MarkerRole.BOTTOM_RIGHT}))
        )
        with pytest.raises(InsufficientMarkersError):
            align_sheet(image, config=canonical_config)

    def test_two_missing_markers_fail(self, canonical_config):
        image = scan_of(
            SyntheticSheetSpec(
                omit_markers=frozenset({MarkerRole.TOP_LEFT, MarkerRole.BOTTOM_RIGHT})
            )
        )
        with pytest.raises(InsufficientMarkersError):
            align_sheet(image, config=canonical_config)

    def test_a_blank_page_fails(self, canonical_config):
        blank = np.full((1754, 1240), 255, dtype=np.uint8)
        with pytest.raises(InsufficientMarkersError):
            align_sheet(blank, config=canonical_config)

    def test_a_page_of_clutter_without_markers_fails(self, canonical_config):
        image = scan_of(
            SyntheticSheetSpec(
                omit_markers=frozenset(MarkerRole),
                draw_bubbles=True,
                draw_text_bars=True,
                draw_answer_frames=True,
            )
        )
        with pytest.raises(InsufficientMarkersError):
            align_sheet(image, config=canonical_config)

    def test_a_heavily_cropped_scan_fails_rather_than_guessing(
        self, canonical_sheet, canonical_config
    ):
        distorted = apply_distortion(canonical_sheet, DistortionSpec(margin_px=-80))
        with pytest.raises(InsufficientMarkersError):
            align_sheet(distorted.image, config=canonical_config)


class TestInvalidGeometry:
    def test_four_markers_bunched_in_one_area_are_rejected(self):
        # Four genuine marker shapes that cannot bound a page: the quadrilateral
        # they span covers almost none of the scan.
        image = np.full((1754, 1240), 255, dtype=np.uint8)
        for x, y in ((100, 100), (260, 100), (260, 320), (100, 320)):
            image[y : y + 37, x : x + 37] = 0
        config = AlignmentConfig(
            marker_detection=MarkerDetectionConfig(
                corner_search_width=1.0, corner_search_height=1.0
            )
        )
        with pytest.raises(ImagingError) as error:
            align_sheet(image, config=config)
        assert error.value.code in {"INVALID_PAGE_GEOMETRY", "ORIENTATION_NOT_FOUND"}

    def test_a_landscape_marker_rectangle_fails_a_portrait_template(self):
        # The quadrilateral is a perfectly good rectangle - just the wrong shape
        # for the sheet the template describes.
        image = np.full((1240, 1754), 255, dtype=np.uint8)
        for x, y in ((90, 60), (1620, 60), (1620, 1130), (90, 1130)):
            image[y : y + 37, x : x + 37] = 0
        with pytest.raises(ImagingError) as error:
            align_sheet(image, config=AlignmentConfig())
        assert error.value.code in {"INVALID_PAGE_GEOMETRY", "ORIENTATION_NOT_FOUND"}

    def test_a_tightened_aspect_tolerance_rejects_a_stretched_page(
        self, canonical_sheet
    ):
        distorted = apply_distortion(
            canonical_sheet, DistortionSpec(scale_x=1.3, scale_y=0.8)
        )
        config = AlignmentConfig(
            geometry=GeometryConfig(max_aspect_ratio_deviation=0.05)
        )
        with pytest.raises(InvalidPageGeometryError) as error:
            align_sheet(distorted.image, config=config)
        assert error.value.code == "INVALID_PAGE_GEOMETRY"


class TestInvalidInput:
    @pytest.mark.parametrize(
        "image",
        [
            pytest.param(np.zeros((0, 0), dtype=np.uint8), id="empty array"),
            pytest.param(np.zeros((100, 0), dtype=np.uint8), id="zero width"),
            pytest.param(np.zeros(4000, dtype=np.uint8), id="one dimension"),
            pytest.param(np.zeros((2, 200, 200, 3), dtype=np.uint8), id="four dimensions"),
            pytest.param(np.zeros((200, 200, 2), dtype=np.uint8), id="two channels"),
            pytest.param(np.zeros((200, 200), dtype=np.float32), id="float pixels"),
            pytest.param(np.zeros((200, 200), dtype=np.uint16), id="16-bit pixels"),
            pytest.param(np.zeros((16, 16), dtype=np.uint8), id="thumbnail"),
        ],
    )
    def test_malformed_input_is_refused_cleanly(self, image, canonical_config):
        with pytest.raises(ImageValidationError):
            align_sheet(image, config=canonical_config)

    def test_a_non_array_is_refused(self, canonical_config):
        with pytest.raises(ImageValidationError):
            align_sheet("scan.png", config=canonical_config)

    def test_no_opencv_assertion_escapes(self, canonical_config):
        # cv2.error is not an OMRScannerError, so if OpenCV's own assertion ever
        # reached the caller this would fail rather than pass by luck.
        with pytest.raises(OMRScannerError):
            align_sheet(np.zeros((3, 3), dtype=np.uint8), config=canonical_config)


class TestFailureContract:
    @pytest.fixture(
        params=[
            ("no markers", SyntheticSheetSpec(omit_markers=frozenset(MarkerRole))),
            ("no orientation mark", SyntheticSheetSpec(omit_orientation_marker=True)),
            (
                "one missing corner",
                SyntheticSheetSpec(omit_markers=frozenset({MarkerRole.TOP_RIGHT})),
            ),
        ],
        ids=lambda item: item[0],
    )
    def failing_scan(self, request):
        return scan_of(request.param[1])

    def test_every_failure_derives_from_imaging_error(self, failing_scan, canonical_config):
        with pytest.raises(ImagingError):
            align_sheet(failing_scan, config=canonical_config)

    def test_every_failure_carries_a_machine_readable_code(
        self, failing_scan, canonical_config
    ):
        with pytest.raises(ImagingError) as error:
            align_sheet(failing_scan, config=canonical_config)
        assert error.value.code
        assert error.value.code.isupper()

    def test_every_failure_carries_a_plain_language_message(
        self, failing_scan, canonical_config
    ):
        with pytest.raises(ImagingError) as error:
            align_sheet(failing_scan, config=canonical_config)
        message = error.value.user_message
        assert message
        assert message != str(error.value)
        assert "Traceback" not in message

    def test_nothing_returns_a_sentinel_instead_of_raising(
        self, failing_scan, canonical_config
    ):
        # A blank result would travel silently into recognition and be scored.
        try:
            result = align_sheet(failing_scan, config=canonical_config)
        except ImagingError:
            return
        pytest.fail(f"Expected a refusal, received {result!r}")


class TestDamagedMarkers:
    def test_a_cross_shaped_mark_does_not_pass_as_a_marker(self, canonical_config):
        # A hand-drawn cross where the top-left marker should be: right area,
        # right bounding box, but neither rectangular nor solid. The corner is
        # left without an acceptable candidate and the sheet is refused.
        sheet = render_sheet(
            SyntheticSheetSpec(omit_markers=frozenset({MarkerRole.TOP_LEFT}))
        )
        marked = sheet.image.copy()
        center = sheet.spec.to_pixels(NormalizedPoint(x=0.05, y=0.035))
        half_width = round(sheet.spec.marker_width * sheet.spec.width / 2)
        half_height = round(sheet.spec.marker_height * sheet.spec.height / 2)
        arm = 3
        marked[
            round(center.y) - arm : round(center.y) + arm,
            round(center.x) - half_width : round(center.x) + half_width,
        ] = 0
        marked[
            round(center.y) - half_height : round(center.y) + half_height,
            round(center.x) - arm : round(center.x) + arm,
        ] = 0

        with pytest.raises(InsufficientMarkersError):
            align_sheet(marked, config=canonical_config)

    def test_a_slightly_damaged_marker_is_still_usable(self, canonical_sheet, canonical_config):
        # The other side of the same judgement: a marker with a chipped corner
        # is a smudged marker, not a different shape, and refusing it would send
        # readable sheets to a human for no reason.
        chipped = canonical_sheet.image.copy()
        center = canonical_sheet.marker_centers[0]
        side = round(canonical_sheet.spec.marker_width * canonical_sheet.spec.width)
        top, left = round(center.y - side / 2), round(center.x - side / 2)
        chip = max(2, side // 4)
        chipped[top : top + chip, left : left + chip] = 255

        result = align_sheet(chipped, config=canonical_config)
        assert result.marker(MarkerRole.TOP_LEFT).center.distance_to(center) < side * 0.15

    def test_a_marker_that_is_only_an_outline_is_rejected(self, canonical_config):
        # The exact failure mode a shape-only detector has: correct outline,
        # correct size, no ink inside.
        sheet = render_sheet(
            SyntheticSheetSpec(omit_markers=frozenset({MarkerRole.BOTTOM_LEFT}))
        )
        outlined = sheet.image.copy()
        center = sheet.spec.to_pixels(NormalizedPoint(x=0.05, y=0.965))
        half_width = round(sheet.spec.marker_width * sheet.spec.width / 2)
        half_height = round(sheet.spec.marker_height * sheet.spec.height / 2)
        top, left = round(center.y) - half_height, round(center.x) - half_width
        bottom, right = round(center.y) + half_height, round(center.x) + half_width
        outlined[top:bottom, left:right] = 0
        outlined[top + 2 : bottom - 2, left + 2 : right - 2] = 255

        with pytest.raises(InsufficientMarkersError):
            align_sheet(outlined, config=canonical_config)

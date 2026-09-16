"""ROI-scoped orientation-mark detection, on synthetic pages and the real sample.

What these lock down:
    The designer's orientation search takes a rectangle the user drew and must
    find the printed mark *inside* it, reporting the answer in full-image pixels.
    The two ways that goes wrong are both covered here:

    * rejecting a valid mark - in particular for the absurd reason that it lies
      inside the search rectangle, which is the rectangle's whole purpose
      (``docs/development/template_gui_fix_diagnosis.md`` §4);
    * finding it but reporting ROI-local coordinates, so the overlay lands in the
      wrong place - checked by asserting the *absolute* position of the mark on
      the page, never an offset within the crop.

    Synthetic pages first, because they have exact ground truth; then
    ``examples/ECE-0000.png``, because a detector that only works on drawings it
    was tuned against has not been tested.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from omr_scanner.imaging.models import BoundingBox
from omr_scanner.imaging.orientation_marker import (
    OrientationMarkerConfig,
    detect_orientation_marker,
    write_debug_overlay,
)
from omr_scanner.services import detect_orientation_marker_in_region

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SHEET = REPOSITORY_ROOT / "examples" / "ECE-0000.png"
"""The repository's real OMR page. Resolved from this file's own location, never
from the working directory, so the tests pass run from anywhere."""

SAMPLE_DASH = BoundingBox(x=158.0, y=309.0, width=86.0, height=44.0)
"""The orientation dash actually printed on ``examples/ECE-0000.png``, measured
from the image by connected-component analysis.

This is *ground truth for the assertion*, not a hint to the detector - nothing in
``src/`` knows these numbers, and the detector is given only a rectangle the way a
user would draw one."""

PAGE_WIDTH, PAGE_HEIGHT = 900, 1200
PAPER = 245
INK = 35


def _blank_page() -> np.ndarray:
    """A sheet of paper with realistic scanner mottling, not a flat white field."""
    rng = np.random.default_rng(seed=20240917)
    noise = rng.normal(0.0, 3.0, size=(PAGE_HEIGHT, PAGE_WIDTH))
    return np.clip(PAPER + noise, 0, 255).astype(np.uint8)


def _draw(page: np.ndarray, box: BoundingBox, *, value: int = INK) -> None:
    cv2.rectangle(
        page,
        (int(box.x), int(box.y)),
        (int(box.right), int(box.bottom)),
        value,
        thickness=cv2.FILLED,
    )


def _dash(x: float, y: float, *, width: float = 80.0, height: float = 40.0) -> BoundingBox:
    """A 2:1 orientation dash - the shape a conventional OMR sheet prints."""
    return BoundingBox(x=x, y=y, width=width, height=height)


def _assert_found_at(detection, expected: BoundingBox, *, tolerance: float = 3.0) -> None:
    """Assert the mark was found at an absolute position on the *page*.

    Absolute, deliberately: a detector that forgot to add the ROI origin back
    would still report a plausible-looking box, just in the wrong place, and only
    an absolute assertion catches that.
    """
    assert detection.found, detection.reason
    assert detection.box is not None
    assert detection.box.x == pytest.approx(expected.x, abs=tolerance)
    assert detection.box.y == pytest.approx(expected.y, abs=tolerance)
    assert detection.box.width == pytest.approx(expected.width, abs=tolerance)
    assert detection.box.height == pytest.approx(expected.height, abs=tolerance)


class TestSyntheticScenarios:
    def test_1_a_dash_centred_in_the_roi_is_found(self):
        page = _blank_page()
        dash = _dash(400.0, 500.0)
        _draw(page, dash)
        detection = detect_orientation_marker(
            page, roi=BoundingBox(x=300, y=420, width=280, height=200)
        )
        _assert_found_at(detection, dash)

    def test_2_a_dash_near_the_roi_edge_is_still_found(self):
        """Being off-centre costs score, never acceptance."""
        page = _blank_page()
        dash = _dash(410.0, 505.0)
        _draw(page, dash)
        detection = detect_orientation_marker(
            page, roi=BoundingBox(x=400, y=495, width=420, height=330)
        )
        _assert_found_at(detection, dash)

    def test_3_the_dash_wins_against_other_dark_objects_in_the_roi(self):
        page = _blank_page()
        dash = _dash(400.0, 500.0)
        _draw(page, dash)
        _draw(page, BoundingBox(x=330, y=430, width=36, height=36))       # a square
        _draw(page, BoundingBox(x=520, y=620, width=240, height=8))       # a rule
        cv2.circle(page, (350, 640), 14, INK, thickness=2)                # an outline
        detection = detect_orientation_marker(
            page, roi=BoundingBox(x=300, y=400, width=500, height=300)
        )
        _assert_found_at(detection, dash)

    def test_4_an_roi_with_no_mark_reports_a_reason_rather_than_guessing(self):
        detection = detect_orientation_marker(
            _blank_page(), roi=BoundingBox(x=300, y=400, width=300, height=200)
        )
        assert detection.found is False
        assert detection.box is None
        assert detection.reason

    def test_5_a_mark_wholly_inside_the_roi_is_never_rejected_for_being_inside(self):
        """The reported bug, stated as a property.

        Every ROI here fully contains the mark with room to spare on all four
        sides. Not one of them may be refused for that.
        """
        page = _blank_page()
        dash = _dash(400.0, 500.0)
        _draw(page, dash)
        margins = (5, 20, 60, 150, 300)
        for margin in margins:
            roi = BoundingBox(
                x=dash.x - margin,
                y=dash.y - margin,
                width=dash.width + 2 * margin,
                height=dash.height + 2 * margin,
            )
            detection = detect_orientation_marker(page, roi=roi)
            assert detection.found, f"margin {margin}px: {detection.reason}"
            _assert_found_at(detection, dash)

    def test_6_a_registration_square_beside_the_dash_is_not_mistaken_for_it(self):
        page = _blank_page()
        dash = _dash(400.0, 520.0)
        _draw(page, dash)
        _draw(page, BoundingBox(x=330, y=430, width=44, height=44))  # a 1:1 square
        detection = detect_orientation_marker(
            page, roi=BoundingBox(x=280, y=400, width=340, height=240)
        )
        _assert_found_at(detection, dash)

    def test_6b_a_registration_square_alone_is_rejected_as_too_square(self):
        page = _blank_page()
        _draw(page, BoundingBox(x=400, y=500, width=44, height=44))
        detection = detect_orientation_marker(
            page, roi=BoundingBox(x=340, y=440, width=180, height=170)
        )
        assert detection.found is False
        assert "square" in detection.reason

    def test_7_speckle_noise_does_not_produce_a_false_positive(self):
        rng = np.random.default_rng(seed=7)
        page = _blank_page()
        for _ in range(400):
            x = int(rng.integers(300, 600))
            y = int(rng.integers(400, 600))
            page[y : y + 2, x : x + 2] = INK
        detection = detect_orientation_marker(
            page, roi=BoundingBox(x=300, y=400, width=300, height=200)
        )
        assert detection.found is False

    def test_a_vertical_dash_is_found_too(self):
        """The mark's orientation on the page is the sheet designer's choice."""
        page = _blank_page()
        dash = _dash(430.0, 480.0, width=40.0, height=80.0)
        _draw(page, dash)
        detection = detect_orientation_marker(
            page, roi=BoundingBox(x=340, y=400, width=240, height=260)
        )
        _assert_found_at(detection, dash)


class TestCoordinateTranslation:
    """ROI-local results must come back in full-image pixels, always."""

    @pytest.mark.parametrize(
        "roi_origin", [(0, 0), (120, 90), (350, 470), (395, 495)]
    )
    def test_the_answer_is_the_same_wherever_the_roi_starts(
        self, roi_origin: tuple[int, int]
    ):
        page = _blank_page()
        dash = _dash(400.0, 500.0)
        _draw(page, dash)
        left, top = roi_origin
        roi = BoundingBox(
            x=left, y=top, width=dash.right + 60 - left, height=dash.bottom + 60 - top
        )
        detection = detect_orientation_marker(page, roi=roi)
        _assert_found_at(detection, dash)

    def test_the_centroid_is_also_in_image_coordinates(self):
        page = _blank_page()
        dash = _dash(400.0, 500.0)
        _draw(page, dash)
        detection = detect_orientation_marker(
            page, roi=BoundingBox(x=330, y=440, width=260, height=200)
        )
        assert detection.center is not None
        assert detection.center.x == pytest.approx(dash.center.x, abs=3.0)
        assert detection.center.y == pytest.approx(dash.center.y, abs=3.0)

    def test_an_roi_hanging_off_the_page_is_clipped_not_refused(self):
        page = _blank_page()
        dash = _dash(60.0, 40.0)
        _draw(page, dash)
        detection = detect_orientation_marker(
            page, roi=BoundingBox(x=-200, y=-150, width=500, height=400)
        )
        _assert_found_at(detection, dash)

    def test_an_roi_entirely_off_the_page_is_an_error(self):
        with pytest.raises(ValueError, match="does not overlap"):
            detect_orientation_marker(
                _blank_page(), roi=BoundingBox(x=-500, y=-500, width=100, height=100)
            )


class TestConfigurableThresholds:
    def test_the_expected_shape_is_configurable_rather_than_hard_coded(self):
        """A sheet using a long thin bar is re-tuned, not re-coded."""
        page = _blank_page()
        bar = _dash(320.0, 520.0, width=300.0, height=24.0)  # 12.5:1
        _draw(page, bar)
        roi = BoundingBox(x=280, y=460, width=400, height=160)

        assert detect_orientation_marker(page, roi=roi).found is False
        retuned = detect_orientation_marker(
            page,
            roi=roi,
            config=OrientationMarkerConfig(
                expected_aspect_ratio=12.5, min_aspect_ratio=6.0, max_aspect_ratio=20.0
            ),
        )
        _assert_found_at(retuned, bar)


class TestDebugOverlay:
    def test_an_overlay_is_written_when_a_path_is_given(self, tmp_path: Path):
        page = _blank_page()
        _draw(page, _dash(400.0, 500.0))
        destination = tmp_path / "debug" / "orientation.png"
        detection = detect_orientation_marker(
            page, roi=BoundingBox(x=300, y=420, width=280, height=200),
            debug_path=destination,
        )
        assert destination.is_file()
        assert detection.debug_image_path == destination
        assert cv2.imread(str(destination)) is not None

    def test_nothing_is_written_when_no_path_is_given(self, tmp_path: Path):
        page = _blank_page()
        _draw(page, _dash(400.0, 500.0))
        detection = detect_orientation_marker(
            page, roi=BoundingBox(x=300, y=420, width=280, height=200)
        )
        assert detection.debug_image_path is None
        assert not list(tmp_path.iterdir())

    def test_the_overlay_records_a_failure_too(self, tmp_path: Path):
        """A failed detection is exactly when the overlay is worth having."""
        destination = tmp_path / "failed.png"
        detection = detect_orientation_marker(
            _blank_page(),
            roi=BoundingBox(x=300, y=400, width=300, height=200),
            debug_path=destination,
        )
        assert detection.found is False
        assert destination.is_file()

    def test_write_debug_overlay_accepts_a_colour_image(self, tmp_path: Path):
        page = cv2.cvtColor(_blank_page(), cv2.COLOR_GRAY2BGR)
        detection = detect_orientation_marker(
            page, roi=BoundingBox(x=300, y=400, width=300, height=200)
        )
        written = write_debug_overlay(page, detection=detection, path=tmp_path / "c.png")
        assert written.is_file()


@pytest.mark.skipif(not SAMPLE_SHEET.is_file(), reason="sample sheet not present")
class TestTheRealSampleSheet:
    """``examples/ECE-0000.png`` - a real scan, with a real magenta-printed dash."""

    def test_the_printed_dash_is_found_inside_a_hand_drawn_rectangle(self):
        outcome = detect_orientation_marker_in_region(
            SAMPLE_SHEET, x=100, y=250, width=260, height=180
        )
        assert outcome.found, outcome.reason
        assert outcome.x == pytest.approx(SAMPLE_DASH.x, abs=4.0)
        assert outcome.y == pytest.approx(SAMPLE_DASH.y, abs=4.0)
        assert outcome.width == pytest.approx(SAMPLE_DASH.width, abs=6.0)
        assert outcome.height == pytest.approx(SAMPLE_DASH.height, abs=6.0)

    @pytest.mark.parametrize(
        ("x", "y", "width", "height", "description"),
        [
            (152, 303, 98, 56, "drawn snugly around the mark"),
            (140, 295, 130, 75, "drawn tightly"),
            (100, 250, 260, 180, "drawn generously"),
            (60, 200, 500, 300, "including the top-left registration square"),
            (150, 300, 400, 300, "with the mark at the ROI's own corner"),
            (0, 0, 1200, 800, "a careless sweep over the whole page header"),
        ],
    )
    def test_the_answer_does_not_depend_on_how_the_user_drew_the_rectangle(
        self, x: int, y: int, width: int, height: int, description: str
    ):
        outcome = detect_orientation_marker_in_region(
            SAMPLE_SHEET, x=x, y=y, width=width, height=height
        )
        assert outcome.found, f"{description}: {outcome.reason}"
        assert outcome.x == pytest.approx(SAMPLE_DASH.x, abs=4.0), description
        assert outcome.y == pytest.approx(SAMPLE_DASH.y, abs=4.0), description

    def test_the_centroid_lands_on_the_printed_mark(self):
        outcome = detect_orientation_marker_in_region(
            SAMPLE_SHEET, x=100, y=250, width=260, height=180
        )
        assert outcome.found
        assert outcome.center_x == pytest.approx(SAMPLE_DASH.center.x, abs=5.0)
        assert outcome.center_y == pytest.approx(SAMPLE_DASH.center.y, abs=5.0)

    def test_a_rectangle_over_blank_paper_finds_nothing(self):
        outcome = detect_orientation_marker_in_region(
            SAMPLE_SHEET, x=900, y=250, width=400, height=200
        )
        assert outcome.found is False
        assert outcome.reason

    def test_a_rectangle_over_a_registration_square_alone_finds_nothing(self):
        """The corner squares are 1:1; the orientation mark is not."""
        outcome = detect_orientation_marker_in_region(
            SAMPLE_SHEET, x=50, y=200, width=110, height=110
        )
        assert outcome.found is False

    def test_the_debug_overlay_can_be_written_for_the_real_sheet(self, tmp_path: Path):
        from omr_scanner.services import ORIENTATION_DEBUG_IMAGE_NAME

        outcome = detect_orientation_marker_in_region(
            SAMPLE_SHEET, x=100, y=250, width=260, height=180, debug_dir=tmp_path
        )
        assert outcome.debug_image_path == tmp_path / ORIENTATION_DEBUG_IMAGE_NAME
        assert outcome.debug_image_path.is_file()

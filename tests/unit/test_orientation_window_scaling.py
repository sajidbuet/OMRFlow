"""Orientation detection when the template's mark box is drawn loosely.

What this locks down:
    The evidence window is the template's declared orientation box scaled by
    ``window_margin``, and the confidence was ``fill / (1 / window_margin**2)``
    - arithmetic that assumes the declared box tightly bounds the printed
    mark. Nothing enforces that. The designer lets the box be drawn with
    margin around the mark, which is the natural way to draw one.

    A box drawn at twice the mark's size covers four times the area, so the
    ink is diluted with paper and the fill falls by the same factor. On a real
    sheet that took the correct orientation from 1.00 to 0.29 - below the 0.35
    floor - and, worse, left it 0.01 ahead of its own 180 degree twin, because
    a window mostly full of paper scores about the same wherever it is put.
    The sheet was then rejected with "the orientation mark was not found",
    about a mark that was present, black, and exactly where the template said.

    These tests are written against the measurement, not against one sheet:
    the mark is synthesised at a known size and the *declared box* is varied
    around it, which is the variable that actually broke.

Why synthetic images:
    The real scans that exposed this are examination material and cannot be
    committed. A drawn mark reproduces the geometry that matters - a dark
    rectangle of known size inside a larger declared box - without carrying
    any of the data.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from omr_scanner.errors import OrientationDetectionError
from omr_scanner.imaging.config import AlignmentConfig, OrientationConfig
from omr_scanner.imaging.geometry import perspective_transform, rotate_corner_order
from omr_scanner.imaging.models import OrientationResult, Point
from omr_scanner.imaging.orientation import (
    _best_window_fill,
    _concentric,
    determine_orientation,
    orientation_windows,
)

PAGE_W, PAGE_H = 1000, 1400

# The mark as actually printed, normalised to the page.
MARK_CX, MARK_CY = 0.12, 0.07
MARK_W, MARK_H = 0.018, 0.008


def _config(*, declared_scale: float) -> AlignmentConfig:
    """A config whose declared orientation box is ``declared_scale`` x the mark.

    ``1.0`` is a box drawn exactly around the mark; ``2.0`` is the same mark
    with a box drawn at twice its width and height, which is what a person
    drawing by eye produces and what used to fail.
    """
    base = AlignmentConfig(canonical_width=PAGE_W, canonical_height=PAGE_H)
    return dataclasses.replace(
        base,
        orientation=dataclasses.replace(
            base.orientation,
            marker_center_x=MARK_CX,
            marker_center_y=MARK_CY,
            marker_width=MARK_W * declared_scale,
            marker_height=MARK_H * declared_scale,
        ),
    )


def _page_with_mark(*, quarter_turns: int = 0, mark: bool = True) -> np.ndarray:
    """A blank binary page carrying the orientation mark, rotated if asked.

    Ink is white, as ``prepare_for_detection`` produces.
    """
    page = np.zeros((PAGE_H, PAGE_W), dtype=np.uint8)
    if mark:
        cx, cy = MARK_CX * PAGE_W, MARK_CY * PAGE_H
        hw, hh = MARK_W * PAGE_W / 2, MARK_H * PAGE_H / 2
        page[int(cy - hh) : int(cy + hh), int(cx - hw) : int(cx + hw)] = 255
    return np.rot90(page, k=-quarter_turns).copy() if quarter_turns else page


def _corners(config: AlignmentConfig) -> tuple[Point, ...]:
    """Marker positions that make the page its own canonical frame.

    Taken from the config's own targets rather than invented, so the
    perspective transform is the identity and the mark drawn at ``MARK_CX``
    lands exactly at ``MARK_CX``. Inventing corners at a different inset
    silently scales the page and moves the mark out of its window - which
    tests the fixture rather than the detector.
    """
    return tuple(config.canonical_marker_points())


def _detect(config: AlignmentConfig, page: np.ndarray) -> OrientationResult:
    return determine_orientation(
        page,
        image_corner_points=_corners(config),
        config=config,
        to_source_factor=1.0,
    )


class TestALooselyDrawnBoxStillResolves:
    @pytest.mark.parametrize("declared_scale", [1.0, 1.25, 1.5, 2.0, 2.5, 3.0])
    def test_the_mark_is_found_however_loosely_its_box_was_drawn(
        self, declared_scale: float
    ):
        """The regression. At 2.0 this used to report "mark not found"."""
        result = _detect(_config(declared_scale=declared_scale), _page_with_mark())
        assert result.quarter_turns == 0
        assert not result.assumed
        assert result.confidence >= 0.35

    @pytest.mark.parametrize("declared_scale", [1.0, 2.0, 3.0])
    def test_the_decision_keeps_a_clear_margin_over_the_alternatives(
        self, declared_scale: float
    ):
        """The half that made the failure dangerous rather than merely strict.

        A diluted window scores about the same wherever it is placed, so the
        correct orientation stopped being *distinguishable* - not just
        confident. The margin is what says the answer was chosen rather than
        landed on.
        """
        result = _detect(_config(declared_scale=declared_scale), _page_with_mark())
        assert result.margin >= 0.15

    @pytest.mark.parametrize("turns", [0, 2])
    def test_a_rotated_page_is_recognised_as_rotated(self, turns: int):
        """180 degrees is the rotation that matters.

        It keeps the aspect ratio, so geometry alone cannot rule it out and
        the mark is the only evidence there is.
        """
        config = _config(declared_scale=2.0)
        result = _detect(config, _page_with_mark(quarter_turns=turns))
        assert result.quarter_turns == turns
        assert not result.assumed


class TestBNoFalsePositives:
    def test_a_page_with_no_mark_is_still_refused(self):
        """The fix must not turn "look harder" into "accept anything"."""
        with pytest.raises(OrientationDetectionError):
            _detect(_config(declared_scale=2.0), _page_with_mark(mark=False))

    def test_ink_scattered_across_the_window_does_not_pass_as_a_mark(self):
        """Sparse print near the expected position is not a mark.

        Searching smaller concentric windows could in principle find a dense
        patch inside noise; this pins that a thin scatter of ink, at the same
        total quantity a real mark would have, does not reach the threshold.
        """
        config = _config(declared_scale=2.0)
        page = np.zeros((PAGE_H, PAGE_W), dtype=np.uint8)
        cx, cy = int(MARK_CX * PAGE_W), int(MARK_CY * PAGE_H)
        rng = np.random.default_rng(20260925)
        for _ in range(60):
            x = cx + int(rng.integers(-40, 40))
            y = cy + int(rng.integers(-20, 20))
            page[y : y + 1, x : x + 1] = 255
        with pytest.raises(OrientationDetectionError):
            _detect(config, page)


class TestCTheWindowSearchItself:
    def test_a_concentric_window_keeps_its_centre(self):
        window, _ = orientation_windows(_config(declared_scale=2.0))
        shrunk = _concentric(window, 0.5)
        assert shrunk.x + shrunk.width / 2 == pytest.approx(window.x + window.width / 2)
        assert shrunk.y + shrunk.height / 2 == pytest.approx(window.y + window.height / 2)
        assert shrunk.width == pytest.approx(window.width * 0.5)

    def test_a_tight_box_is_still_scored_at_full_confidence(self):
        """A template whose box already fits the mark is not made worse.

        The best scale for a tight box is *not* 1.0 - the evidence window is
        already the box times ``window_margin``, so a smaller window sits
        wholly inside the mark and reads as completely full. What matters is
        that the confidence saturates either way, which is what it did before.
        """
        config = _config(declared_scale=1.0)
        window, _ = orientation_windows(config)
        page = _page_with_mark()
        matrix = perspective_transform(
            rotate_corner_order(_corners(config), 0), config.canonical_marker_points()
        )
        fill, _scale = _best_window_fill(
            page,
            matrix=matrix,
            window=window,
            scales=config.orientation.evidence_window_scales,
        )
        assert fill >= config.orientation.expected_window_fill
        assert _detect(config, page).confidence == pytest.approx(1.0)

    def test_a_loose_box_is_measured_at_a_smaller_scale(self):
        config = _config(declared_scale=2.0)
        window, _ = orientation_windows(config)
        page = _page_with_mark()
        matrix = perspective_transform(
            rotate_corner_order(_corners(config), 0), config.canonical_marker_points()
        )
        fill, scale = _best_window_fill(
            page,
            matrix=matrix,
            window=window,
            scales=config.orientation.evidence_window_scales,
        )
        assert scale < 1.0
        # The whole point: measured at the right scale the mark fills its
        # window, where measured across the loose box it did not.
        assert fill > _best_window_fill(
            page, matrix=matrix, window=window, scales=(1.0,)
        )[0]

    def test_the_scales_start_at_the_whole_window(self):
        """Largest first, so a tight box is scored exactly as it always was."""
        scales = OrientationConfig().evidence_window_scales
        assert scales[0] == 1.0
        assert list(scales) == sorted(scales, reverse=True)
        assert min(scales) >= 0.25, "a window this small stops describing the mark"

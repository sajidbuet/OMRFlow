"""Tests for the normalised coordinate primitives."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omr_scanner.domain.geometry import NormalizedPoint, NormalizedRect, NormalizedSize


def test_point_projects_onto_pixels():
    point = NormalizedPoint(x=0.25, y=0.5)

    assert point.to_pixels(1240, 1754) == pytest.approx((310.0, 877.0))


@pytest.mark.parametrize(("x", "y"), [(-0.01, 0.5), (0.5, 1.01), (1.5, 1.5)])
def test_point_rejects_coordinates_outside_the_page(x: float, y: float):
    with pytest.raises(ValidationError):
        NormalizedPoint(x=x, y=y)


def test_size_rejects_zero_extent():
    with pytest.raises(ValidationError):
        NormalizedSize(width=0.0, height=0.1)


def test_rect_edges_and_center():
    rect = NormalizedRect(x=0.1, y=0.2, width=0.4, height=0.2)

    assert rect.right == pytest.approx(0.5)
    assert rect.bottom == pytest.approx(0.4)
    assert rect.center.x == pytest.approx(0.3)
    assert rect.center.y == pytest.approx(0.3)


def test_rect_contains_points_on_its_edges():
    rect = NormalizedRect(x=0.1, y=0.1, width=0.2, height=0.2)

    assert rect.contains_point(NormalizedPoint(x=0.1, y=0.1))
    assert rect.contains_point(NormalizedPoint(x=0.3, y=0.3))
    assert not rect.contains_point(NormalizedPoint(x=0.31, y=0.2))


def test_rect_detects_overflow_past_the_page_edge():
    on_page = NormalizedRect(x=0.5, y=0.5, width=0.5, height=0.5)
    overflowing = NormalizedRect(x=0.8, y=0.1, width=0.3, height=0.1)

    assert on_page.is_within_page()
    assert not overflowing.is_within_page()

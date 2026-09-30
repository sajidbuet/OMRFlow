"""The zoomed preview never oscillates with its own scroll bars.

A real freeze on the Resolve stage ("Python is not responding", 2026-09-30):
the Zoomed field for Student ID position 1 - the leftmost column, at the page
edge - in a wide, short pane. `ScanPreviewView` refits its framed region on
every resize, and sized the fit against the *current* viewport. Widening that
edge region to the pane's shape runs past the page, so one zoom showed a
scroll bar, the narrower viewport then gave a zoom that hid it, and each
refit's viewport resize triggered the other - indefinitely, on the GUI thread,
with nothing logged.

The pane sizes and regions below are the ones a sweep found oscillating before
the fix (46 of 1,386 combinations; none after).
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QApplication

from omr_scanner.gui.scan.preview import ScanPreviewView

pytestmark = pytest.mark.gui

PAGE = (2526, 3417)
LEFT_EDGE_ID_COLUMN = QRectF(20, 300, 60, 420)
SECOND_ID_COLUMN = QRectF(90, 300, 60, 420)
OSCILLATING_BEFORE_THE_FIX = [(1202, 300), (1251, 300), (1328, 330), (1370, 330), (1482, 360)]
MAX_REFITS = 12
"""A settled framing takes a handful of refits (show, resize, scroll-bar
change); the oscillation took unboundedly many."""


class RefitLimitExceededError(Exception):
    pass


@pytest.fixture
def view(qtbot):
    widget = ScanPreviewView()
    qtbot.addWidget(widget)
    widget.set_page(None, canonical_width=PAGE[0], canonical_height=PAGE[1])
    widget.show()
    qtbot.waitExposed(widget)
    return widget


def _count_refits(view: ScanPreviewView, monkeypatch) -> dict[str, int]:
    calls = {"n": 0}
    original = view._apply_focus

    def counted() -> None:
        calls["n"] += 1
        if calls["n"] > 200:  # a hang would never return; fail instead
            raise RefitLimitExceededError
        original()

    monkeypatch.setattr(view, "_apply_focus", counted)
    return calls


@pytest.mark.parametrize("size", OSCILLATING_BEFORE_THE_FIX)
@pytest.mark.parametrize("region", [LEFT_EDGE_ID_COLUMN, SECOND_ID_COLUMN], ids=["pos1", "pos2"])
def test_framing_an_edge_region_settles(view, qtbot, monkeypatch, size, region):
    view.resize(*size)
    qtbot.wait(20)
    calls = _count_refits(view, monkeypatch)
    view.focus_on(region)
    for _ in range(20):
        QApplication.processEvents()
    assert calls["n"] <= MAX_REFITS
    assert view.focus_rect is not None


def test_the_zoom_does_not_depend_on_scroll_bars(view, qtbot):
    view.resize(1202, 300)
    qtbot.wait(20)
    view.focus_on(LEFT_EDGE_ID_COLUMN)
    QApplication.processEvents()
    settled = view.zoom
    # A further resize event with no size change - what a scroll bar toggling
    # produces - leaves the zoom exactly where it was.
    view._apply_focus()
    QApplication.processEvents()
    assert view.zoom == settled

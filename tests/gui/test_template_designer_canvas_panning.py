"""GUI tests for middle/right-button canvas panning (Part C/E).

Every test drives the canvas with *relative* pointer offsets (never fixed
physical screen coordinates, per the brief) and asserts on scrollbar values,
signals and cursor shape - never on pixel colour or exact widget geometry.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from omr_scanner.gui.template_designer.canvas import RegionSpec, TemplateCanvasView

pytestmark = pytest.mark.gui

_A_ZONE = RegionSpec(item_id="z", kind="zone", x=50, y=50, width=100, height=100, color="#1E88E5")


@pytest.fixture
def canvas(qtbot) -> TemplateCanvasView:
    view = TemplateCanvasView()
    qtbot.addWidget(view)
    view.resize(400, 400)
    # A canvas larger than the viewport, so panning has scrollbar range to
    # actually move within.
    view.set_blank_canvas(2000, 2000)
    view.zoom_to_actual_size()
    return view


def _scroll_position(view: TemplateCanvasView) -> tuple[int, int]:
    return (view.horizontalScrollBar().value(), view.verticalScrollBar().value())


class TestMiddleButtonPanning:
    def test_a_middle_drag_moves_the_viewport(self, canvas: TemplateCanvasView):
        before = _scroll_position(canvas)
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.MiddleButton, pos=QPoint(200, 200))
        QTest.mouseMove(canvas.viewport(), QPoint(150, 160))
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.MiddleButton, pos=QPoint(150, 160))
        after = _scroll_position(canvas)
        assert after != before

    def test_panning_works_with_no_region_selected_and_selects_nothing(
        self, canvas: TemplateCanvasView
    ):
        selections: list[object] = []
        canvas.selection_changed.connect(lambda item_id, _kind: selections.append(item_id))
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.MiddleButton, pos=QPoint(100, 100))
        QTest.mouseMove(canvas.viewport(), QPoint(60, 60))
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.MiddleButton, pos=QPoint(60, 60))
        assert selections == []

    def test_a_middle_drag_over_a_selected_region_does_not_move_it(
        self, canvas: TemplateCanvasView
    ):
        canvas.rebuild_regions([_A_ZONE])
        canvas.select_region("z")
        committed: list[object] = []
        canvas.geometry_committed.connect(lambda *args: committed.append(args))
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.MiddleButton, pos=QPoint(100, 100))
        QTest.mouseMove(canvas.viewport(), QPoint(40, 40))
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.MiddleButton, pos=QPoint(40, 40))
        assert committed == []


class TestRightButtonPanning:
    def test_a_plain_right_click_with_no_movement_does_not_pan(self, canvas: TemplateCanvasView):
        before = _scroll_position(canvas)
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.RightButton, pos=QPoint(200, 200))
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.RightButton, pos=QPoint(200, 200))
        assert _scroll_position(canvas) == before

    def test_a_right_drag_past_the_threshold_pans(self, canvas: TemplateCanvasView):
        before = _scroll_position(canvas)
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.RightButton, pos=QPoint(200, 200))
        QTest.mouseMove(canvas.viewport(), QPoint(130, 140))
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.RightButton, pos=QPoint(130, 140))
        assert _scroll_position(canvas) != before

    def test_a_right_drag_does_not_select_or_move_a_region(self, canvas: TemplateCanvasView):
        canvas.rebuild_regions([_A_ZONE])
        selections: list[object] = []
        canvas.selection_changed.connect(lambda item_id, _kind: selections.append(item_id))
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.RightButton, pos=QPoint(100, 100))
        QTest.mouseMove(canvas.viewport(), QPoint(30, 30))
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.RightButton, pos=QPoint(30, 30))
        assert selections == []


class TestPanningNeverAltersDocumentGeometry:
    def test_panning_at_a_high_zoom_level_only_moves_the_viewport(
        self, canvas: TemplateCanvasView
    ):
        canvas.rebuild_regions([_A_ZONE])
        canvas.zoom_in()
        canvas.zoom_in()
        before_rect = canvas._scene.region_items["z"].scene_rect()
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.MiddleButton, pos=QPoint(200, 200))
        QTest.mouseMove(canvas.viewport(), QPoint(120, 90))
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.MiddleButton, pos=QPoint(120, 90))
        after_rect = canvas._scene.region_items["z"].scene_rect()
        assert after_rect == before_rect


class TestLeftButtonBehaviourIsUnaffected:
    def test_a_left_drag_on_a_selected_region_still_moves_it(self, canvas: TemplateCanvasView):
        canvas.rebuild_regions([_A_ZONE])
        canvas.select_region("z")
        item = canvas._scene.region_items["z"]
        # Press inside the rectangle's body (not near an edge, so this is a
        # move rather than a resize) and drag.
        start = canvas.mapFromScene(item.scene_rect().center())
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas.viewport(), start + QPoint(20, 15))
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start + QPoint(20, 15))
        moved_rect = canvas._scene.region_items["z"].scene_rect()
        assert moved_rect.x() != 50 or moved_rect.y() != 50

"""Region item geometry: resize anchoring, and the scene/local coordinate contract.

What these lock down:
    ``RegionHandleItem`` used to capture its press rectangle in **item-local**
    coordinates, add a **scene-space** mouse delta to it, and hand the result to
    ``set_scene_rect``, which reads a scene rectangle. Since every item is built
    as ``setPos(x, y)`` + ``setRect(0, 0, w, h)``, the local rectangle's origin is
    always ``(0, 0)``, so the first resize threw the region to the scene origin.
    See ``docs/development/template_gui_fix_diagnosis.md`` §3.

    The tests are written as *invariants* rather than as "reproduce the bug":
    dragging the right edge must not change x, y or height, whatever the
    implementation does internally. A future refactor that reintroduces a
    coordinate-frame mix-up fails these without anyone having to remember why
    they exist.

Interaction style:
    Qt-native throughout - `QTest` mouse events at viewport-relative points
    derived from the item's own scene rectangle, never absolute screen
    coordinates (which depend on DPI, window placement and the OS).
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtTest import QTest

from omr_scanner.gui.template_designer.canvas import RegionSpec, TemplateCanvasView
from omr_scanner.gui.template_designer.items import HANDLE_MARGIN_PX, RegionHandleItem

pytestmark = pytest.mark.gui

# Deliberately far from the origin: a region at (0, 0) cannot show a
# jump-to-the-origin bug at all.
REGION_X, REGION_Y, REGION_W, REGION_H = 300.0, 400.0, 200.0, 500.0

SPEC = RegionSpec(
    item_id="region",
    kind="zone",
    x=REGION_X,
    y=REGION_Y,
    width=REGION_W,
    height=REGION_H,
    color="#1E88E5",
)


@pytest.fixture
def canvas(qtbot) -> TemplateCanvasView:
    view = TemplateCanvasView()
    qtbot.addWidget(view)
    view.resize(900, 900)
    view.set_blank_canvas(1200, 1600)
    view.zoom_to_actual_size()
    view.rebuild_regions([SPEC])
    return view


def _item(canvas: TemplateCanvasView) -> RegionHandleItem:
    """The region item, found by its domain id rather than by scene index."""
    return canvas._scene.region_items["region"]


def _drag(canvas: TemplateCanvasView, *, scene_from: QPointF, scene_by: QPointF) -> None:
    """Press at a scene point, drag by a scene offset, release - all through Qt.

    Scene points are mapped to the viewport with ``mapFromScene`` so the test
    states *where on the drawing* it is grabbing, and Qt works out where that
    lands on screen at the current zoom and scroll position.
    """
    start = canvas.mapFromScene(scene_from)
    end = canvas.mapFromScene(scene_from + scene_by)
    QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(canvas.viewport(), end)
    QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)


def _edge_point(rect: QRectF, edge: str) -> QPointF:
    """A scene point squarely inside ``edge``'s grab zone.

    Placed one third of the grab margin inside the rectangle, so it is
    unambiguously on that edge and not on a neighbouring corner.
    """
    inset = HANDLE_MARGIN_PX / 3.0
    points = {
        "right": QPointF(rect.right() - inset, rect.center().y()),
        "bottom": QPointF(rect.center().x(), rect.bottom() - inset),
        "bottom_right": QPointF(rect.right() - inset, rect.bottom() - inset),
        "left": QPointF(rect.left() + inset, rect.center().y()),
        "top": QPointF(rect.center().x(), rect.top() + inset),
        "top_left": QPointF(rect.left() + inset, rect.top() + inset),
    }
    return points[edge]


class TestTheItemCoordinateContract:
    """`pos()` holds the position; `rect()` holds the size. Never both."""

    def test_a_fresh_item_keeps_its_position_in_pos_and_its_size_in_rect(
        self, canvas: TemplateCanvasView
    ):
        item = _item(canvas)
        assert item.pos() == QPointF(REGION_X, REGION_Y)
        assert item.rect() == QRectF(0.0, 0.0, REGION_W, REGION_H)

    def test_an_item_constructed_from_a_positioned_rectangle_is_normalised(self):
        """Construction cannot produce the inconsistent state the bug relied on."""
        from PySide6.QtGui import QColor

        item = RegionHandleItem(
            "z", QRectF(120.0, 240.0, 60.0, 80.0), color=QColor("#1E88E5")
        )
        assert item.pos() == QPointF(120.0, 240.0)
        assert item.rect() == QRectF(0.0, 0.0, 60.0, 80.0)
        assert item.scene_rect() == QRectF(120.0, 240.0, 60.0, 80.0)

    def test_scene_rect_combines_pos_and_rect(self, canvas: TemplateCanvasView):
        item = _item(canvas)
        assert item.scene_rect() == QRectF(REGION_X, REGION_Y, REGION_W, REGION_H)

    def test_set_scene_rect_round_trips(self, canvas: TemplateCanvasView):
        item = _item(canvas)
        target = QRectF(77.0, 88.0, 99.0, 111.0)
        item.set_scene_rect(target)
        assert item.scene_rect() == target
        assert item.pos() == target.topLeft()
        assert item.rect() == QRectF(0.0, 0.0, target.width(), target.height())


class TestResizeAnchoring:
    """Standard graphics-editor semantics, asserted edge by edge."""

    def test_dragging_the_right_edge_changes_only_the_width(
        self, canvas: TemplateCanvasView
    ):
        before = _item(canvas).scene_rect()
        _drag(canvas, scene_from=_edge_point(before, "right"), scene_by=QPointF(60.0, 0.0))
        after = _item(canvas).scene_rect()
        assert after.x() == pytest.approx(REGION_X, abs=1.0)
        assert after.y() == pytest.approx(REGION_Y, abs=1.0)
        assert after.height() == pytest.approx(REGION_H, abs=1.0)
        assert after.width() > before.width() + 20.0

    def test_dragging_the_bottom_edge_changes_only_the_height(
        self, canvas: TemplateCanvasView
    ):
        before = _item(canvas).scene_rect()
        _drag(canvas, scene_from=_edge_point(before, "bottom"), scene_by=QPointF(0.0, 70.0))
        after = _item(canvas).scene_rect()
        assert after.x() == pytest.approx(REGION_X, abs=1.0)
        assert after.y() == pytest.approx(REGION_Y, abs=1.0)
        assert after.width() == pytest.approx(REGION_W, abs=1.0)
        assert after.height() > before.height() + 20.0

    def test_dragging_the_bottom_right_corner_keeps_the_top_left_anchored(
        self, canvas: TemplateCanvasView
    ):
        """The headline regression: this is where the jump to the origin happened."""
        before = _item(canvas).scene_rect()
        _drag(
            canvas,
            scene_from=_edge_point(before, "bottom_right"),
            scene_by=QPointF(55.0, 65.0),
        )
        after = _item(canvas).scene_rect()
        assert after.topLeft().x() == pytest.approx(REGION_X, abs=1.0)
        assert after.topLeft().y() == pytest.approx(REGION_Y, abs=1.0)
        assert after.width() > before.width() + 20.0
        assert after.height() > before.height() + 20.0

    def test_the_region_never_lands_anywhere_near_the_scene_origin(
        self, canvas: TemplateCanvasView
    ):
        """The symptom itself, stated as bluntly as it was reported."""
        for edge in ("right", "bottom", "bottom_right", "left", "top", "top_left"):
            canvas.rebuild_regions([SPEC])
            rect = _item(canvas).scene_rect()
            _drag(canvas, scene_from=_edge_point(rect, edge), scene_by=QPointF(40.0, 40.0))
            after = _item(canvas).scene_rect()
            assert after.x() > 100.0, f"dragging '{edge}' threw the region to x={after.x()}"
            assert after.y() > 100.0, f"dragging '{edge}' threw the region to y={after.y()}"

    def test_dragging_the_top_left_corner_moves_the_top_left_boundary(
        self, canvas: TemplateCanvasView
    ):
        """The one handle that *should* change the position - and only by the drag."""
        before = _item(canvas).scene_rect()
        delta = QPointF(30.0, 25.0)
        _drag(canvas, scene_from=_edge_point(before, "top_left"), scene_by=delta)
        after = _item(canvas).scene_rect()
        assert after.x() == pytest.approx(REGION_X + delta.x(), abs=2.0)
        assert after.y() == pytest.approx(REGION_Y + delta.y(), abs=2.0)
        assert after.right() == pytest.approx(before.right(), abs=1.0)
        assert after.bottom() == pytest.approx(before.bottom(), abs=1.0)

    def test_a_resize_cannot_collapse_the_region_to_nothing(
        self, canvas: TemplateCanvasView
    ):
        before = _item(canvas).scene_rect()
        _drag(
            canvas,
            scene_from=_edge_point(before, "right"),
            scene_by=QPointF(-10_000.0, 0.0),
        )
        assert _item(canvas).scene_rect().width() > 0.0


class TestResizeReporting:
    """What the canvas tells the page must match what the item actually did."""

    def test_the_committed_geometry_is_the_item_scene_rectangle(
        self, canvas: TemplateCanvasView
    ):
        committed: list[tuple] = []
        canvas.geometry_committed.connect(lambda *args: committed.append(args))
        rect = _item(canvas).scene_rect()
        _drag(canvas, scene_from=_edge_point(rect, "right"), scene_by=QPointF(40.0, 0.0))
        assert committed, "a completed resize must commit exactly once"
        _item_id, _kind, x, y, width, height = committed[-1]
        assert (x, y, width, height) == pytest.approx(
            tuple(_item(canvas).scene_rect().getRect())
        )

    def test_one_completed_gesture_commits_exactly_once(self, canvas: TemplateCanvasView):
        """Undo must step over a whole drag, not over every mouse-move."""
        committed: list[tuple] = []
        canvas.geometry_committed.connect(lambda *args: committed.append(args))
        rect = _item(canvas).scene_rect()
        start = canvas.mapFromScene(_edge_point(rect, "bottom_right"))
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
        for step in (10, 20, 30, 40):
            QTest.mouseMove(canvas.viewport(), start + QPoint(step, step))
        QTest.mouseRelease(
            canvas.viewport(), Qt.MouseButton.LeftButton, pos=start + QPoint(40, 40)
        )
        assert len(committed) == 1


class TestGeometryIsIndependentOfZoom:
    """Zoom lives in the view's transform; it never touches stored geometry."""

    @pytest.mark.parametrize("zoom_steps", [-2, 0, 2])
    def test_the_scene_rectangle_is_the_same_at_every_zoom_level(
        self, canvas: TemplateCanvasView, zoom_steps: int
    ):
        for _ in range(abs(zoom_steps)):
            canvas.zoom_in() if zoom_steps > 0 else canvas.zoom_out()
        assert _item(canvas).scene_rect() == QRectF(REGION_X, REGION_Y, REGION_W, REGION_H)

    def test_a_resize_at_high_zoom_still_anchors_the_top_left(
        self, canvas: TemplateCanvasView
    ):
        canvas.zoom_in()
        canvas.zoom_in()
        before = _item(canvas).scene_rect()
        _drag(
            canvas,
            scene_from=_edge_point(before, "bottom_right"),
            scene_by=QPointF(20.0, 20.0),
        )
        after = _item(canvas).scene_rect()
        assert after.x() == pytest.approx(REGION_X, abs=1.0)
        assert after.y() == pytest.approx(REGION_Y, abs=1.0)


class TestBubblePreviewSize:
    """The overlay draws the template's own bubble size, not a fixed dot."""

    def test_the_item_carries_the_size_it_was_given(self, canvas: TemplateCanvasView):
        canvas.rebuild_regions(
            [
                RegionSpec(
                    item_id="region",
                    kind="zone",
                    x=REGION_X,
                    y=REGION_Y,
                    width=REGION_W,
                    height=REGION_H,
                    color="#1E88E5",
                    bubble_points=((350.0, 450.0),),
                    bubble_size=(16.0, 12.0),
                )
            ]
        )
        item = _item(canvas)
        assert item.bubble_size is not None
        assert (item.bubble_size.width(), item.bubble_size.height()) == (16.0, 12.0)

    def test_a_larger_radius_produces_a_larger_item_bubble_size(
        self, canvas: TemplateCanvasView
    ):
        sizes = []
        for radius in (4.0, 12.0):
            canvas.rebuild_regions(
                [
                    RegionSpec(
                        item_id="region", kind="zone", x=REGION_X, y=REGION_Y,
                        width=REGION_W, height=REGION_H, color="#1E88E5",
                        bubble_points=((350.0, 450.0),),
                        bubble_size=(2 * radius, 2 * radius),
                    )
                ]
            )
            size = _item(canvas).bubble_size
            assert size is not None
            sizes.append(size.width())
        assert sizes == [8.0, 24.0]

    def test_bubble_size_survives_a_zoom_change(self, canvas: TemplateCanvasView):
        canvas.rebuild_regions(
            [
                RegionSpec(
                    item_id="region", kind="zone", x=REGION_X, y=REGION_Y,
                    width=REGION_W, height=REGION_H, color="#1E88E5",
                    bubble_points=((350.0, 450.0),), bubble_size=(16.0, 16.0),
                )
            ]
        )
        canvas.zoom_in()
        canvas.zoom_in()
        size = _item(canvas).bubble_size
        assert size is not None
        assert size.width() == 16.0

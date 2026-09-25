"""Live feedback in the template editor: spin boxes, previews and cursors.

What these lock down:
    Five interaction defects that all had the same shape - the editor knew the
    right answer but did not show it until some later event.

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     A spin box's arrows are clickable, not covered by its editor.
    B     Typing a number previews on the canvas at once.
    C     Dragging and resizing move the bubbles continuously.
    D     A new template's bubble radius is 20 px; a saved one keeps its own.
    E     Drawing mode shows a crosshair, and gives it back afterwards.
    ===== ==========================================================

Why the spin-box test measures sub-control rectangles:
    The symptom was a cursor and a click going to the wrong widget, which no
    value assertion can see: the value never changed *because the click never
    reached the button*. Asking the style where it put `SC_SpinBoxUp`, and
    asking the widget what child is at that point, tests the thing that was
    actually broken.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionSpinBox

from omr_scanner.domain.geometry import NormalizedRect
from omr_scanner.domain.template_authoring import (
    DEFAULT_BUBBLE_RADIUS_PX,
    build_blank_template,
    default_bubble_radius_for,
)
from omr_scanner.gui.template_designer.canvas import RegionSpec, TemplateCanvasView
from omr_scanner.gui.template_designer.items import RegionHandleItem
from omr_scanner.gui.template_designer.page import TemplateDesignerPage
from omr_scanner.gui.template_designer.properties_panel import PropertiesPanel
from omr_scanner.gui.theme import application_stylesheet

pytestmark = pytest.mark.gui

PAGE_W, PAGE_H = 1200, 1600
REGION = RegionSpec(
    item_id="region",
    kind="zone",
    x=300.0,
    y=400.0,
    width=200.0,
    height=500.0,
    color="#1E88E5",
    bubble_points=((350.0, 450.0), (450.0, 450.0), (350.0, 850.0), (450.0, 850.0)),
    bubble_size=(20.0, 20.0),
)


@pytest.fixture
def canvas(qtbot) -> TemplateCanvasView:
    view = TemplateCanvasView()
    qtbot.addWidget(view)
    view.resize(900, 900)
    view.set_blank_canvas(PAGE_W, PAGE_H)
    view.zoom_to_actual_size()
    view.rebuild_regions([REGION])
    return view


@pytest.fixture(autouse=True)
def no_blocking_message_boxes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep any `QMessageBox` from opening an un-clickable modal loop offscreen."""
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *_a, **_k: None))


@pytest.fixture
def designer(qtbot, tmp_path) -> TemplateDesignerPage:
    """A real designer page with one question region already placed."""
    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
    from omr_scanner.gui.template_designer.dialogs import QuestionBlockDialog
    from omr_scanner.gui.template_designer.state import DesignerState
    from omr_scanner.imaging.synthetic import SyntheticSheetSpec, render_sheet
    from omr_scanner.services import decode_image_file, save_image

    sheet = render_sheet(SyntheticSheetSpec())
    path = tmp_path / "sheet.png"
    save_image(sheet.image, path)
    decoded = decode_image_file(path)

    widget = TemplateDesignerPage(
        next(spec for spec in WORKFLOW_PAGES if spec.key == "template")
    )
    qtbot.addWidget(widget)
    template = build_blank_template(
        name="Fixture",
        canonical_width_px=decoded.width,
        canonical_height_px=decoded.height,
    )
    widget._designer_state = DesignerState(
        template, template_path=None, reference_image_path=path
    )
    widget._decoded_image = decoded
    widget.canvas.set_reference_image(decoded)
    widget.properties.set_image_size(decoded.width, decoded.height)
    widget._set_document_controls_enabled(True)

    dialog = QuestionBlockDialog(
        bounds=NormalizedRect(x=0.08, y=0.30, width=0.60, height=0.45),
        existing_zone_ids=[],
        image_width=decoded.width,
        image_height=decoded.height,
        bubble_radius_px=widget._default_bubble_radius_px(),
    )
    dialog.columns_box.setValue(2)
    widget._designer_state.apply_zones(dialog._build_zones())
    widget._refresh_all()
    return widget


def _question_item(page: TemplateDesignerPage) -> RegionHandleItem:
    """The first question-column region item on the canvas."""
    for item_id, item in page.canvas._scene.region_items.items():
        if item_id.startswith("questions") and item.bubble_points:
            return item
    raise AssertionError("no question region with bubbles on the canvas")


@pytest.fixture
def panel(qtbot) -> PropertiesPanel:
    widget = PropertiesPanel()
    qtbot.addWidget(widget)
    widget.set_image_size(PAGE_W, PAGE_H)
    widget.set_geometry("Region", 300.0, 400.0, 200.0, 500.0)
    widget.set_bubble_geometry(10.0, inherits=False)
    # Shown, because the sub-control assertions are about where Qt actually
    # lays the line edit out: an unshown spin box has not been polished, so
    # its editor still has its default full-width geometry and `childAt`
    # answers for a layout that will never be painted.
    widget.show()
    qtbot.waitExposed(widget)
    return widget


# ----------------------------------------------------------------------
# A - the spin box arrows
# ----------------------------------------------------------------------
class TestASpinBoxArrowsAreClickable:
    """The editor must not cover the up/down buttons.

    Styling a spin box at all switches it to `QStyleSheetStyle`, which lays
    the line edit across the whole padded rect unless the buttons declare a
    width. It did not, so the `QLineEdit` sat on top of both arrows: hovering
    gave an I-beam and clicking put the caret in the text.

    Skipped offscreen deliberately. Measured on the Windows style, removing
    the `::up-button`/`::down-button` rules makes ten of these fail; measured
    on Fusion, nothing fails either way. A test that passes regardless of the
    bug is worse than one that says it did not run.
    """

    @pytest.fixture(autouse=True)
    def _styled(self) -> None:
        """Apply the real application stylesheet - the thing under test.

        Skips at *run* time rather than through `skipif`, because
        `platformName()` is empty until a `QApplication` exists and a
        collection-time check therefore never sees "offscreen".
        """
        application = QApplication.instance()
        assert application is not None
        if QGuiApplication.platformName() == "offscreen":
            pytest.skip(
                "the Windows style is what puts the editor over the arrows; "
                "offscreen uses Fusion, which lays the sub-controls out "
                "differently and passes with or without the fix"
            )
        previous = application.styleSheet()
        application.setStyleSheet(application_stylesheet())
        yield
        application.setStyleSheet(previous)

    def _sub_rects(self, box) -> tuple[QRectF, QRectF, QRectF]:
        option = QStyleOptionSpinBox()
        option.initFrom(box)
        option.rect = box.rect()
        option.subControls = QStyle.SubControl.SC_All
        style = box.style()

        def rect(sub: QStyle.SubControl) -> QRectF:
            return style.subControlRect(
                QStyle.ComplexControl.CC_SpinBox, option, sub, box
            )

        return (
            rect(QStyle.SubControl.SC_SpinBoxEditField),
            rect(QStyle.SubControl.SC_SpinBoxUp),
            rect(QStyle.SubControl.SC_SpinBoxDown),
        )

    @pytest.mark.parametrize(
        "field", ["x_box", "y_box", "width_box", "height_box", "bubble_radius_box"]
    )
    def test_the_editor_does_not_overlap_either_arrow(
        self, panel: PropertiesPanel, field: str
    ):
        """Every numeric field in the editor, not just the one that was reported."""
        box = getattr(panel, field)
        box.resize(160, box.sizeHint().height())
        QApplication.processEvents()
        edit, up, down = self._sub_rects(box)

        assert not up.isEmpty(), f"{field}: the up button has no area"
        assert not down.isEmpty(), f"{field}: the down button has no area"
        assert not edit.intersects(up), f"{field}: the editor covers the up button"
        assert not edit.intersects(down), f"{field}: the editor covers the down button"

    @pytest.mark.parametrize(
        "field", ["x_box", "y_box", "width_box", "height_box", "bubble_radius_box"]
    )
    def test_no_child_widget_sits_over_the_arrows(
        self, panel: PropertiesPanel, field: str
    ):
        """The direct cause of the I-beam.

        `childAt` over the up button returned the `QLineEdit`, so the pointer
        was over a text widget and Qt showed a text cursor. Nothing may be
        there.
        """
        box = getattr(panel, field)
        box.resize(160, box.sizeHint().height())
        QApplication.processEvents()
        _, up, down = self._sub_rects(box)

        assert box.childAt(up.center()) is None, f"{field}: a child covers the up arrow"
        assert box.childAt(down.center()) is None, (
            f"{field}: a child covers the down arrow"
        )

    @pytest.mark.parametrize(
        "field", ["x_box", "y_box", "width_box", "height_box", "bubble_radius_box"]
    )
    def test_the_style_hit_tests_the_arrows_as_buttons(
        self, panel: PropertiesPanel, field: str
    ):
        box = getattr(panel, field)
        box.resize(160, box.sizeHint().height())
        QApplication.processEvents()
        option = QStyleOptionSpinBox()
        option.initFrom(box)
        option.rect = box.rect()
        option.subControls = QStyle.SubControl.SC_All
        _, up, down = self._sub_rects(box)

        assert (
            box.style().hitTestComplexControl(
                QStyle.ComplexControl.CC_SpinBox, option, up.center(), box
            )
            is QStyle.SubControl.SC_SpinBoxUp
        )
        assert (
            box.style().hitTestComplexControl(
                QStyle.ComplexControl.CC_SpinBox, option, down.center(), box
            )
            is QStyle.SubControl.SC_SpinBoxDown
        )

    def test_stepping_still_works_from_the_keyboard(self, panel: PropertiesPanel):
        """The fix is to hit-testing, so typing and arrow keys are untouched."""
        panel.x_box.setValue(100.0)
        panel.x_box.stepUp()
        assert panel.x_box.value() == pytest.approx(101.0)
        panel.x_box.stepDown()
        panel.x_box.stepDown()
        assert panel.x_box.value() == pytest.approx(99.0)


# ----------------------------------------------------------------------
# B - inspector edits preview immediately
# ----------------------------------------------------------------------
class TestBInspectorEditsPreviewImmediately:
    @pytest.mark.parametrize(
        ("field", "index"),
        [("x_box", 0), ("y_box", 1), ("width_box", 2), ("height_box", 3)],
    )
    def test_each_field_reports_a_preview_as_it_changes(
        self, panel: PropertiesPanel, qtbot, field: str, index: int
    ):
        """Not only on editingFinished, which needed Enter or a focus change."""
        with qtbot.waitSignal(panel.geometry_preview, timeout=1000) as caught:
            getattr(panel, field).setValue(123.0)
        assert caught.args[index] == pytest.approx(123.0)

    def test_the_bubble_radius_reports_a_preview_as_it_changes(
        self, panel: PropertiesPanel, qtbot
    ):
        panel.set_bubble_geometry(10.0, inherits=False)
        with qtbot.waitSignal(panel.bubble_radius_preview, timeout=1000) as caught:
            panel.bubble_radius_box.setValue(18.0)
        assert caught.args[0] == pytest.approx(18.0)

    def test_the_normalised_line_follows_a_preview(self, panel: PropertiesPanel):
        """The inspector's own three views of the same number stay in step."""
        panel.x_box.setValue(600.0)
        assert f"x={600.0 / PAGE_W:.4f}" in panel.normalized_label.text()

    def test_writing_the_panel_from_the_model_emits_nothing(
        self, panel: PropertiesPanel
    ):
        """The guard against UI -> model -> UI -> model.

        `set_geometry` is the model -> panel direction. If it emitted a
        preview, every canvas drag would echo back as an inspector edit.
        """
        previews: list[tuple[float, ...]] = []
        edits: list[tuple[float, ...]] = []
        panel.geometry_preview.connect(lambda *a: previews.append(a))
        panel.geometry_edited.connect(lambda *a: edits.append(a))

        panel.set_geometry("Region", 11.0, 22.0, 33.0, 44.0)
        panel.set_bubble_geometry(9.0, inherits=False)

        assert previews == []
        assert edits == []

    def test_a_preview_moves_the_canvas_item_without_touching_its_bubbles(
        self, canvas: TemplateCanvasView
    ):
        item = canvas._scene.region_items["region"]
        before = list(item.bubble_points)
        canvas.preview_region_geometry("region", 500.0, 600.0, 200.0, 500.0)
        assert item.scene_rect().topLeft() == QPointF(500.0, 600.0)
        # Item-local, so a pure move needs no recalculation at all.
        assert list(item.bubble_points) == before

    def test_a_radius_preview_resizes_bubbles_without_moving_them(
        self, canvas: TemplateCanvasView
    ):
        item = canvas._scene.region_items["region"]
        before = list(item.bubble_points)
        canvas.preview_bubble_size("region", 50.0, 50.0)
        assert item.bubble_size.width() == pytest.approx(50.0)
        assert list(item.bubble_points) == before


# ----------------------------------------------------------------------
# C - bubbles follow a drag or resize
# ----------------------------------------------------------------------
class TestCBubblesFollowInteractiveGeometry:
    def test_moving_carries_the_bubbles(self, canvas: TemplateCanvasView):
        """Bubble centres are item-local, so a move must not disturb them."""
        item = canvas._scene.region_items["region"]
        local_before = list(item.bubble_points)
        scene_before = [item.mapToScene(point) for point in local_before]

        item.setPos(item.pos() + QPointF(120.0, -60.0))

        assert list(item.bubble_points) == local_before
        moved = [item.mapToScene(point) for point in item.bubble_points]
        for old, new in zip(scene_before, moved, strict=True):
            assert new.x() == pytest.approx(old.x() + 120.0)
            assert new.y() == pytest.approx(old.y() - 60.0)

    def test_a_preview_can_replace_the_points_during_a_resize(
        self, canvas: TemplateCanvasView
    ):
        """What the page calls on every mouse-move of a resize."""
        item = canvas._scene.region_items["region"]
        before = list(item.bubble_points)
        canvas.preview_bubble_points(
            "region", [(320.0, 420.0), (460.0, 420.0), (320.0, 880.0), (460.0, 880.0)]
        )
        assert list(item.bubble_points) != before
        # Converted into the item's own frame, which is what `paint` draws in.
        assert item.bubble_points[0].x() == pytest.approx(320.0 - item.pos().x())
        assert item.bubble_points[0].y() == pytest.approx(420.0 - item.pos().y())

    def test_the_preview_uses_the_same_layout_function_as_the_commit(self):
        """No visible jump on release, asserted where the jump would come from.

        The page previews with `resize_zone` - the same pure function
        `DesignerState.resize_zone` commits through. If the preview grew its
        own approximation, this would diverge.
        """
        from omr_scanner.domain.template import FieldType
        from omr_scanner.domain.template_authoring import (
            generate_character_grid_zone,
            resize_zone,
        )
        from omr_scanner.gui.template_designer.page import _bubble_preview_points

        template = build_blank_template(
            name="t", canonical_width_px=PAGE_W, canonical_height_px=PAGE_H
        )
        zone = generate_character_grid_zone(
            zone_id="roll",
            label="Roll",
            field_type=FieldType.NUMERIC,
            symbols=[str(digit) for digit in range(10)],
            character_count=4,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.3, height=0.4),
            bubble_size=template.default_bubble_size,
        )
        target = NormalizedRect(x=0.2, y=0.15, width=0.5, height=0.6)

        # The preview path and the commit path, called the same way. The page
        # previews with `resize_zone` and `DesignerState.resize_zone` commits
        # with it, so the only way these can differ is if one of them stops
        # using it.
        preview = _bubble_preview_points(resize_zone(zone, bounds=target), PAGE_W, PAGE_H)
        committed = _bubble_preview_points(
            resize_zone(zone, bounds=target), PAGE_W, PAGE_H
        )
        assert preview == committed
        assert len(preview) == 40, "4 characters x 10 symbols"
        # And it is genuinely a *different* layout from the unresized zone -
        # otherwise the comparison above would be vacuous.
        assert preview != _bubble_preview_points(zone, PAGE_W, PAGE_H)


class TestCPageWiresPreviewsEndToEnd:
    """The half that fixes the reported behaviour, through the real page.

    The canvas primitives above are necessary but not sufficient: the defect
    was that `_on_canvas_geometry_changed` updated the inspector and nothing
    else, so nothing ever called them mid-gesture.
    """

    def test_a_drag_in_progress_recalculates_the_bubbles(
        self, designer: TemplateDesignerPage
    ):
        """Signalled on every mouse-move, not only on release."""
        item = _question_item(designer)
        before = [QPointF(point) for point in item.bubble_points]

        rect = item.scene_rect()
        # Exactly what `RegionHandleItem.mouseMoveEvent` emits mid-resize.
        designer._on_canvas_geometry_changed(
            item.item_id, "zone", rect.x(), rect.y(), rect.width() * 1.6, rect.height()
        )

        after = list(item.bubble_points)
        assert len(after) == len(before)
        assert after != before, "the bubbles did not follow the tentative rectangle"
        widest_before = max(point.x() for point in before)
        widest_after = max(point.x() for point in after)
        assert widest_after > widest_before, "the layout did not spread with the region"

    def test_a_drag_in_progress_writes_nothing_to_the_document(
        self, designer: TemplateDesignerPage
    ):
        """One undo entry per gesture, not one per mouse-move."""
        item = _question_item(designer)
        zone_before = designer._designer_state.template.zone_by_id(item.item_id)
        depth_before = designer._designer_state.history.depth

        rect = item.scene_rect()
        for step in range(1, 6):
            designer._on_canvas_geometry_changed(
                item.item_id, "zone", rect.x(), rect.y(),
                rect.width() + step * 10.0, rect.height(),
            )

        assert designer._designer_state.template.zone_by_id(item.item_id) == zone_before
        assert designer._designer_state.history.depth == depth_before

    def test_the_preview_matches_what_the_commit_then_produces(
        self, designer: TemplateDesignerPage
    ):
        """No jump on release - asserted across the release itself."""
        item = _question_item(designer)
        rect = item.scene_rect()
        target = (rect.x(), rect.y(), rect.width() * 1.4, rect.height() * 0.8)

        designer._on_canvas_geometry_changed(item.item_id, "zone", *target)
        previewed = [QPointF(point) for point in item.bubble_points]

        designer._on_canvas_geometry_committed(item.item_id, "zone", *target)
        committed_item = _question_item(designer)
        committed = list(committed_item.bubble_points)

        assert len(committed) == len(previewed)
        for shown, final in zip(previewed, committed, strict=True):
            assert final.x() == pytest.approx(shown.x(), abs=0.5)
            assert final.y() == pytest.approx(shown.y(), abs=0.5)

    def test_typing_a_width_moves_the_canvas_before_it_is_committed(
        self, designer: TemplateDesignerPage
    ):
        item = _question_item(designer)
        designer.canvas.select_region(item.item_id)
        rect = item.scene_rect()
        zone_before = designer._designer_state.template.zone_by_id(item.item_id)

        designer.properties.width_box.setValue(rect.width() + 80.0)

        assert _question_item(designer).scene_rect().width() == pytest.approx(
            rect.width() + 80.0
        )
        assert designer._designer_state.template.zone_by_id(item.item_id) == zone_before

    @pytest.mark.parametrize(
        ("field", "attribute"),
        [("x_box", "x"), ("y_box", "y"), ("width_box", "width"), ("height_box", "height")],
    )
    def test_every_geometry_field_previews_on_the_canvas(
        self, designer: TemplateDesignerPage, field: str, attribute: str
    ):
        item = _question_item(designer)
        designer.canvas.select_region(item.item_id)
        rect = item.scene_rect()
        current = {
            "x": rect.x(), "y": rect.y(),
            "width": rect.width(), "height": rect.height(),
        }[attribute]

        getattr(designer.properties, field).setValue(current + 40.0)

        updated = _question_item(designer).scene_rect()
        actual = {
            "x": updated.x(), "y": updated.y(),
            "width": updated.width(), "height": updated.height(),
        }[attribute]
        assert actual == pytest.approx(current + 40.0)

    def test_the_bubble_radius_field_resizes_the_drawn_bubbles(
        self, designer: TemplateDesignerPage
    ):
        item = _question_item(designer)
        designer.canvas.select_region(item.item_id)
        designer.properties.set_bubble_geometry(10.0, inherits=False)
        centres_before = [QPointF(point) for point in item.bubble_points]

        designer.properties.bubble_radius_box.setValue(26.0)

        assert item.bubble_size.width() == pytest.approx(52.0)
        # A radius change never moves a centre.
        for before, after in zip(centres_before, item.bubble_points, strict=True):
            assert after.x() == pytest.approx(before.x())
            assert after.y() == pytest.approx(before.y())

    def test_a_canvas_drag_does_not_echo_back_as_an_inspector_edit(
        self, designer: TemplateDesignerPage
    ):
        """The recursion guard, end to end: canvas -> panel -> canvas -> ...

        `set_geometry` blocks the boxes' signals, so the inspector update a
        drag causes cannot come back round as a preview.
        """
        item = _question_item(designer)
        designer.canvas.select_region(item.item_id)
        previews: list[tuple[float, ...]] = []
        designer.properties.geometry_preview.connect(lambda *a: previews.append(a))

        rect = item.scene_rect()
        designer._on_canvas_geometry_changed(
            item.item_id, "zone", rect.x() + 5.0, rect.y(), rect.width(), rect.height()
        )

        assert previews == []


# ----------------------------------------------------------------------
# D - the default bubble radius
# ----------------------------------------------------------------------
class TestDDefaultBubbleRadius:
    def test_a_new_template_starts_at_twenty_pixels(self):
        template = build_blank_template(
            name="new", canonical_width_px=2480, canonical_height_px=3508
        )
        assert template.default_bubble_radius is not None
        radius_px = template.default_bubble_radius * 2480
        assert radius_px == pytest.approx(DEFAULT_BUBBLE_RADIUS_PX)
        assert radius_px == pytest.approx(20.0)

    @pytest.mark.parametrize("width", [827, 1240, 2480, 4960])
    def test_it_is_twenty_pixels_whatever_the_page_resolution(self, width: int):
        """The default is a pixel measurement, not a normalised constant.

        The same fraction is a different number of pixels on every page size,
        so one stored constant could not mean 20 px on two different pages.
        """
        template = build_blank_template(
            name="new", canonical_width_px=width, canonical_height_px=width * 2
        )
        assert template.default_bubble_radius is not None
        assert template.default_bubble_radius * width == pytest.approx(20.0)

    @pytest.mark.parametrize("saved", [0.0051, 0.011, 0.02, 0.0402])
    def test_a_saved_radius_is_never_replaced_by_the_default(self, saved: float):
        """The compatibility requirement, stated on the model itself.

        A template that records a radius means it; loading must return that
        number whatever the current default happens to be.
        """
        template = build_blank_template(
            name="t", canonical_width_px=2480, canonical_height_px=3508
        )
        stored = template.model_copy(update={"default_bubble_radius": saved})
        assert stored.default_bubble_radius == pytest.approx(saved)
        assert stored.default_bubble_size.width == pytest.approx(saved * 2)

    def test_a_template_predating_the_field_keeps_its_old_geometry(self):
        """`default_bubble_radius=None` is a document from before the field.

        Its bubbles were drawn at the old hard-coded 0.022 width, so the
        fallback must stay where it was - changing it to 20 px would silently
        resize every bubble in every such template.
        """
        template = build_blank_template(
            name="t", canonical_width_px=2480, canonical_height_px=3508
        )
        legacy = template.model_copy(update={"default_bubble_radius": None})
        assert legacy.default_bubble_size.width == pytest.approx(0.022)

    def test_the_helper_and_the_constant_agree(self):
        assert default_bubble_radius_for(2000) == pytest.approx(
            DEFAULT_BUBBLE_RADIUS_PX / 2000
        )

    def test_a_degenerate_page_width_does_not_divide_by_zero(self):
        assert default_bubble_radius_for(0) > 0.0


# ----------------------------------------------------------------------
# E - the drawing cursor
# ----------------------------------------------------------------------
class TestECursorFollowsTheMode:
    def test_drawing_mode_shows_a_crosshair(self, canvas: TemplateCanvasView):
        canvas.start_draw_mode()
        assert canvas.viewport().cursor().shape() is Qt.CursorShape.CrossCursor

    def test_leaving_drawing_mode_gives_the_cursor_back(
        self, canvas: TemplateCanvasView
    ):
        canvas.start_draw_mode()
        canvas.cancel_draw_mode()
        assert canvas.viewport().cursor().shape() is not Qt.CursorShape.CrossCursor

    def test_panning_inside_drawing_mode_restores_the_crosshair(
        self, canvas: TemplateCanvasView
    ):
        """Panning must not cost the crosshair.

        `_stop_pan` called `unsetCursor()` unconditionally, so one middle-drag
        while placing a region lost the crosshair for the rest of the gesture.
        """
        canvas.start_draw_mode()
        canvas._start_pan(canvas.rect().center(), Qt.MouseButton.MiddleButton)
        assert canvas.viewport().cursor().shape() is Qt.CursorShape.ClosedHandCursor
        canvas._stop_pan()
        assert canvas.viewport().cursor().shape() is Qt.CursorShape.CrossCursor

    def test_holding_space_does_not_destroy_the_crosshair(
        self, canvas: TemplateCanvasView, qtbot
    ):
        """`setDragMode(ScrollHandDrag)` handed Qt the viewport cursor."""
        canvas.start_draw_mode()
        qtbot.keyPress(canvas, Qt.Key.Key_Space)
        assert canvas.viewport().cursor().shape() is Qt.CursorShape.OpenHandCursor
        qtbot.keyRelease(canvas, Qt.Key.Key_Space)
        assert canvas.viewport().cursor().shape() is Qt.CursorShape.CrossCursor

    def test_panning_outside_drawing_mode_still_uses_a_hand(
        self, canvas: TemplateCanvasView
    ):
        """The hand cursor was not removed, only stopped from winning."""
        canvas._start_pan(canvas.rect().center(), Qt.MouseButton.MiddleButton)
        assert canvas.viewport().cursor().shape() is Qt.CursorShape.ClosedHandCursor
        canvas._stop_pan()
        assert canvas.viewport().cursor().shape() is not Qt.CursorShape.ClosedHandCursor

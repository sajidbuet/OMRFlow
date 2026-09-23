"""Page-level regressions: bubble radius, numeric resize, layout, undo/redo.

Scope:
    These drive the `TemplateDesignerPage` the way the application does - through
    its widgets and its `DesignerState` - and assert on **program state**, not on
    pixels. Screenshot comparison is for clipping and alignment; "did the model
    change correctly" is never a question a screenshot can answer (see
    ``.claude/skills/qtguitesting/SKILL.md``).

    Per ``docs/TESTING.md``, modal dialogs are not `exec()`d; where the page's own
    flow would show one, the test drives the dialog object directly and then the
    page method the accepted dialog would have called.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QDoubleSpinBox, QToolBar, QWidget

from omr_scanner.domain.geometry import NormalizedRect
from omr_scanner.domain.template_authoring import (
    build_blank_template,
    zone_inherits_bubble_size,
)
from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
from omr_scanner.gui.template_designer.dialogs import QuestionBlockDialog
from omr_scanner.gui.template_designer.page import TemplateDesignerPage
from omr_scanner.gui.template_designer.state import DesignerState
from omr_scanner.imaging.synthetic import SyntheticSheetSpec, render_sheet
from omr_scanner.services import decode_image_file, save_image

pytestmark = pytest.mark.gui

TEMPLATE_SPEC = next(spec for spec in WORKFLOW_PAGES if spec.key == "template")

CONTAINER = NormalizedRect(x=0.08, y=0.30, width=0.60, height=0.45)
TOLERANCE = 1e-9


@pytest.fixture(autouse=True)
def no_blocking_message_boxes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep any `QMessageBox` from opening an un-clickable modal loop offscreen."""
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *_a, **_k: None))
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *_a, **_k: QMessageBox.StandardButton.Cancel),
    )


@pytest.fixture
def sheet_image_path(tmp_path: Path) -> Path:
    sheet = render_sheet(SyntheticSheetSpec())
    path = tmp_path / "sheet.png"
    save_image(sheet.image, path)
    return path


@pytest.fixture
def page(qtbot, sheet_image_path: Path) -> TemplateDesignerPage:
    """A designer page with a blank template open against a reference image."""
    widget = TemplateDesignerPage(TEMPLATE_SPEC)
    qtbot.addWidget(widget)
    decoded = decode_image_file(sheet_image_path)
    template = build_blank_template(
        name="Fixture",
        canonical_width_px=decoded.width,
        canonical_height_px=decoded.height,
    )
    widget._designer_state = DesignerState(
        template, template_path=None, reference_image_path=sheet_image_path
    )
    widget._decoded_image = decoded
    widget.canvas.set_reference_image(decoded)
    widget.properties.set_image_size(decoded.width, decoded.height)
    widget._set_document_controls_enabled(True)
    widget._refresh_all()
    return widget


def _add_question_region(page: TemplateDesignerPage, *, columns: int) -> None:
    """Create a Question Region through the real dialog, at ``columns`` columns."""
    dialog = QuestionBlockDialog(
        bounds=CONTAINER,
        existing_zone_ids=[zone.id for zone in page._designer_state.template.zones],
        image_width=page._decoded_image.width,
        image_height=page._decoded_image.height,
        bubble_radius_px=page._default_bubble_radius_px(),
    )
    dialog.columns_box.setValue(columns)
    page._designer_state.apply_zones(dialog._build_zones())
    page._refresh_all()


def _outer_rect(page: TemplateDesignerPage) -> tuple[float, float, float, float]:
    """The Question Region's outer rectangle, in image pixels, off the canvas.

    Read from the *scene items* rather than the model, so this asserts what the
    user can actually see - the model is checked separately in
    ``tests/unit/test_question_region_container.py``.
    """
    items = [
        item
        for item_id, item in page.canvas._scene.region_items.items()
        if item_id.startswith("questions")
    ]
    assert items, "no question region on the canvas"
    rects = [item.scene_rect() for item in items]
    left = min(rect.x() for rect in rects)
    top = min(rect.y() for rect in rects)
    right = max(rect.right() for rect in rects)
    bottom = max(rect.bottom() for rect in rects)
    return (left, top, right - left, bottom - top)


class TestQuestionRegionContainerThroughTheDialog:
    """The user-visible half of the container invariant: what lands on the canvas."""

    def test_the_dialog_still_defaults_to_four_columns(self, qtbot):
        dialog = QuestionBlockDialog(bounds=CONTAINER, existing_zone_ids=[])
        qtbot.addWidget(dialog)
        assert dialog.columns_box.value() == 4

    @pytest.mark.parametrize(("before", "after"), [(4, 1), (1, 5), (5, 2), (2, 4)])
    def test_changing_the_column_count_does_not_move_or_resize_the_region(
        self, page: TemplateDesignerPage, before: int, after: int
    ):
        _add_question_region(page, columns=before)
        original = _outer_rect(page)

        page._designer_state.apply_zones(())
        _add_question_region(page, columns=after)

        assert _outer_rect(page) == pytest.approx(original, abs=1e-6)

    def test_the_region_matches_the_rectangle_that_was_drawn(
        self, page: TemplateDesignerPage
    ):
        _add_question_region(page, columns=4)
        width = page._decoded_image.width
        height = page._decoded_image.height
        assert _outer_rect(page) == pytest.approx(
            (
                CONTAINER.x * width,
                CONTAINER.y * height,
                CONTAINER.width * width,
                CONTAINER.height * height,
            ),
            abs=1e-6,
        )

    def test_only_the_internal_layout_changes(self, page: TemplateDesignerPage):
        _add_question_region(page, columns=4)
        four = [zone.bounds.width for zone in page._designer_state.template.zones]
        page._designer_state.apply_zones(())
        _add_question_region(page, columns=1)
        one = [zone.bounds.width for zone in page._designer_state.template.zones]
        assert len(four) == 4
        assert len(one) == 1
        assert one[0] > four[0]

    def test_fitting_the_spacing_reflows_the_bubbles_across_the_new_width(
        self, page: TemplateDesignerPage
    ):
        """1 column must spread its bubbles over the whole container, not a quarter."""
        _add_question_region(page, columns=1)
        zone = page._designer_state.template.zones[0]
        assert zone.grid is not None
        last = zone.grid.bubble_center(zone.field.rows - 1, zone.field.columns - 1)
        # The rightmost bubble sits near the container's right edge, i.e. the
        # lattice really was recomputed for the wider strip.
        assert last.x > CONTAINER.x + CONTAINER.width * 0.8


class TestBubbleRadius:
    def test_the_toolbar_exposes_a_bubble_radius_control(self, page: TemplateDesignerPage):
        assert isinstance(page.bubble_radius_box, QDoubleSpinBox)
        assert page.bubble_radius_box.objectName() == "bubble_radius"

    def test_the_control_lives_on_a_toolbar_row(self, page: TemplateDesignerPage):
        parent = page.bubble_radius_box.parentWidget()
        assert isinstance(parent, QToolBar)

    def test_it_is_disabled_until_a_document_is_open(self, qtbot):
        widget = TemplateDesignerPage(TEMPLATE_SPEC)
        qtbot.addWidget(widget)
        assert widget.bubble_radius_box.isEnabled() is False

    def test_it_is_enabled_once_a_document_is_open(self, page: TemplateDesignerPage):
        assert page.bubble_radius_box.isEnabled() is True

    def test_it_shows_the_open_template_default_in_image_pixels(
        self, page: TemplateDesignerPage
    ):
        template = page._designer_state.template
        expected = template.default_bubble_radius * page._decoded_image.width
        assert page.bubble_radius_box.value() == pytest.approx(expected, abs=0.05)

    @pytest.mark.parametrize("radius", [4.0, 8.0, 12.0])
    def test_the_stored_diameter_is_twice_the_radius(
        self, page: TemplateDesignerPage, radius: float
    ):
        _add_question_region(page, columns=4)
        page.bubble_radius_box.setValue(radius)
        width = page._decoded_image.width
        height = page._decoded_image.height
        for zone in page._designer_state.template.zones:
            assert zone.grid is not None
            assert zone.grid.bubble_size.width * width == pytest.approx(2 * radius, abs=0.1)
            assert zone.grid.bubble_size.height * height == pytest.approx(2 * radius, abs=0.1)

    def test_the_rendered_overlay_changes_size_with_the_radius(
        self, page: TemplateDesignerPage
    ):
        """The brief's acceptance test: radius 5 -> 10 doubles the drawn diameter."""
        _add_question_region(page, columns=4)
        drawn: list[float] = []
        for radius in (5.0, 10.0):
            page.bubble_radius_box.setValue(radius)
            item = next(
                item
                for item_id, item in page.canvas._scene.region_items.items()
                if item_id.startswith("questions")
            )
            assert item.bubble_size is not None
            drawn.append(item.bubble_size.width())
        assert drawn[0] == pytest.approx(10.0, abs=0.2)
        assert drawn[1] == pytest.approx(20.0, abs=0.2)

    def test_changing_the_radius_moves_no_bubble_centre(self, page: TemplateDesignerPage):
        _add_question_region(page, columns=4)
        before = [
            zone.grid.bubble_center(row, column)
            for zone in page._designer_state.template.zones
            if zone.grid
            for row in range(zone.field.rows)
            for column in range(zone.field.columns)
        ]
        page.bubble_radius_box.setValue(6.0)
        after = [
            zone.grid.bubble_center(row, column)
            for zone in page._designer_state.template.zones
            if zone.grid
            for row in range(zone.field.rows)
            for column in range(zone.field.columns)
        ]
        assert after == before

    def test_changing_the_radius_does_not_move_the_parent_region(
        self, page: TemplateDesignerPage
    ):
        _add_question_region(page, columns=4)
        before = _outer_rect(page)
        page.bubble_radius_box.setValue(6.0)
        assert _outer_rect(page) == pytest.approx(before, abs=1e-6)

    def test_the_radius_is_in_image_pixels_and_survives_zooming(
        self, page: TemplateDesignerPage
    ):
        _add_question_region(page, columns=4)
        page.bubble_radius_box.setValue(9.0)
        stored = page._designer_state.template.zones[0].grid.bubble_size
        for _ in range(3):
            page.canvas.zoom_in()
        page.canvas.fit_to_window()
        page.canvas.zoom_out()
        assert page._designer_state.template.zones[0].grid.bubble_size == stored

    def test_the_whole_change_is_one_undo_step(self, page: TemplateDesignerPage):
        _add_question_region(page, columns=4)
        before = page._designer_state.template
        page.bubble_radius_box.setValue(7.0)
        assert page._designer_state.template != before
        page.undo()
        assert page._designer_state.template == before

    def test_redo_restores_the_new_radius(self, page: TemplateDesignerPage):
        _add_question_region(page, columns=4)
        page.bubble_radius_box.setValue(7.0)
        after = page._designer_state.template
        page.undo()
        page.redo()
        assert page._designer_state.template == after

    def test_setting_the_same_radius_twice_adds_no_undo_step(
        self, page: TemplateDesignerPage
    ):
        _add_question_region(page, columns=4)
        page.bubble_radius_box.setValue(7.0)
        depth = page._designer_state.history.depth
        page._on_bubble_radius_changed(7.0)
        assert page._designer_state.history.depth == depth


class TestRegionBubbleRadiusOverride:
    def test_the_properties_panel_hides_bubble_controls_for_a_marker(
        self, page: TemplateDesignerPage
    ):
        page.canvas.select_region("marker:top_left")
        assert page.properties.bubble_group.isVisibleTo(page.properties) is False

    def test_selecting_a_bubble_region_shows_its_radius(self, page: TemplateDesignerPage):
        _add_question_region(page, columns=2)
        zone_id = page._designer_state.template.zones[0].id
        page.canvas.select_region(zone_id)
        expected = (
            page._designer_state.template.zones[0].grid.bubble_size.width
            * page._decoded_image.width
            / 2.0
        )
        assert page.properties.bubble_radius_box.value() == pytest.approx(expected, abs=0.2)
        assert page.properties.bubble_inherit_box.isChecked() is True

    def test_a_region_radius_applies_only_to_that_region(self, page: TemplateDesignerPage):
        _add_question_region(page, columns=2)
        first, second = page._designer_state.template.zones
        page.canvas.select_region(first.id)
        untouched = second.grid.bubble_size

        page.properties.bubble_radius_box.setValue(15.0)
        page.properties._emit_bubble_radius()

        updated = page._designer_state.template
        assert updated.zone_by_id(first.id).grid.bubble_size.width == pytest.approx(
            30.0 / page._decoded_image.width, abs=1e-6
        )
        assert updated.zone_by_id(second.id).grid.bubble_size == untouched

    def test_an_overridden_region_is_left_alone_by_the_template_default(
        self, page: TemplateDesignerPage
    ):
        _add_question_region(page, columns=2)
        first, second = page._designer_state.template.zones
        page.canvas.select_region(first.id)
        page.properties.bubble_radius_box.setValue(15.0)
        page.properties._emit_bubble_radius()
        overridden = page._designer_state.template.zone_by_id(first.id).grid.bubble_size

        page.bubble_radius_box.setValue(6.0)

        updated = page._designer_state.template
        assert updated.zone_by_id(first.id).grid.bubble_size == overridden
        assert zone_inherits_bubble_size(
            updated.zone_by_id(second.id), default=updated.default_bubble_size
        )

    def test_re_checking_inherit_returns_the_region_to_the_default(
        self, page: TemplateDesignerPage
    ):
        _add_question_region(page, columns=2)
        zone_id = page._designer_state.template.zones[0].id
        page.canvas.select_region(zone_id)
        page.properties.bubble_radius_box.setValue(15.0)
        page.properties._emit_bubble_radius()

        page.canvas.select_region(zone_id)
        page.properties.bubble_inherit_box.setChecked(True)

        template = page._designer_state.template
        assert template.zone_by_id(zone_id).grid.bubble_size == template.default_bubble_size


class TestNumericResizePreservesPosition:
    """Typing a Width must not move the region. The brief's §18, exactly."""

    def _select_a_zone(self, page: TemplateDesignerPage) -> str:
        _add_question_region(page, columns=1)
        zone_id = page._designer_state.template.zones[0].id
        page.canvas.select_region(zone_id)
        return zone_id

    def test_changing_the_width_leaves_x_y_and_height_alone(
        self, page: TemplateDesignerPage
    ):
        zone_id = self._select_a_zone(page)
        before = page._designer_state.template.zone_by_id(zone_id).bounds

        page.properties.width_box.setValue(page.properties.width_box.value() + 40.0)
        page.properties._emit_change()

        after = page._designer_state.template.zone_by_id(zone_id).bounds
        assert after.x == pytest.approx(before.x, abs=1e-6)
        assert after.y == pytest.approx(before.y, abs=1e-6)
        assert after.height == pytest.approx(before.height, abs=1e-6)
        assert after.width > before.width

    def test_changing_the_height_leaves_x_y_and_width_alone(
        self, page: TemplateDesignerPage
    ):
        zone_id = self._select_a_zone(page)
        before = page._designer_state.template.zone_by_id(zone_id).bounds

        page.properties.height_box.setValue(page.properties.height_box.value() + 60.0)
        page.properties._emit_change()

        after = page._designer_state.template.zone_by_id(zone_id).bounds
        assert after.x == pytest.approx(before.x, abs=1e-6)
        assert after.y == pytest.approx(before.y, abs=1e-6)
        assert after.width == pytest.approx(before.width, abs=1e-6)
        assert after.height > before.height

    def test_the_region_never_jumps_to_the_origin(self, page: TemplateDesignerPage):
        zone_id = self._select_a_zone(page)
        for box in (page.properties.width_box, page.properties.height_box):
            box.setValue(box.value() + 25.0)
            page.properties._emit_change()
            bounds = page._designer_state.template.zone_by_id(zone_id).bounds
            assert bounds.x > 0.01
            assert bounds.y > 0.01

    def test_the_canvas_item_follows_the_model_after_a_numeric_edit(
        self, page: TemplateDesignerPage
    ):
        """The refresh direction: model changes, then the scene item matches it."""
        zone_id = self._select_a_zone(page)
        page.properties.width_box.setValue(page.properties.width_box.value() + 40.0)
        page.properties._emit_change()

        bounds = page._designer_state.template.zone_by_id(zone_id).bounds
        rect = page.canvas._scene.region_items[zone_id].scene_rect()
        assert rect.x() == pytest.approx(bounds.x * page._decoded_image.width, abs=0.01)
        assert rect.width() == pytest.approx(
            bounds.width * page._decoded_image.width, abs=0.01
        )

    def test_a_numeric_resize_is_undoable(self, page: TemplateDesignerPage):
        zone_id = self._select_a_zone(page)
        before = page._designer_state.template.zone_by_id(zone_id).bounds
        page.properties.width_box.setValue(page.properties.width_box.value() + 40.0)
        page.properties._emit_change()
        page.undo()
        assert page._designer_state.template.zone_by_id(zone_id).bounds == before


class TestThePageHasNoHeadingOfItsOwn:
    """The canvas starts at the top of the page.

    This page used to carry a heading reading "Template" with the stage's
    summary suppressed to a tooltip - a compromise that saved one row of the
    two the heading cost. The workflow ribbon names the stage now, so both
    rows are gone, and the canvas is the first thing in the page.
    """

    def test_there_is_no_heading_row(self, page: TemplateDesignerPage):
        assert page.header is None
        assert page.title_widget is None
        assert page.summary_widget is None

    def test_no_label_carries_the_stage_name_or_its_summary(
        self, page: TemplateDesignerPage
    ):
        """Removed, not hidden - a hidden label still holds its layout row."""
        from PySide6.QtWidgets import QLabel

        texts = {label.text() for label in page.findChildren(QLabel)}
        assert TEMPLATE_SPEC.summary not in texts
        assert TEMPLATE_SPEC.title not in texts

    def test_the_canvas_starts_near_the_top_of_the_page(
        self, page: TemplateDesignerPage
    ):
        """No margin left behind where the heading used to be.

        The toolbar is above the canvas and is functional, so the assertion is
        that the page's own first widget is at its margin - not that the
        canvas itself is at zero.
        """
        from omr_scanner.gui.pages.base_page import COMPACT_MARGIN_PX

        children = [
            child
            for child in page.findChildren(QWidget)
            if child.parentWidget() is page and child.isVisibleTo(page)
        ]
        assert children
        assert min(child.y() for child in children) <= COMPACT_MARGIN_PX

    def test_a_stage_not_yet_implemented_still_says_what_it_will_do(self, qtbot):
        """The summary moved into the body rather than disappearing with the heading.

        A placeholder page's whole job is to describe a stage that does not
        exist yet, and that sentence used to live in the heading's summary
        row.
        """
        from omr_scanner.gui.pages.placeholder_page import PlaceholderPage

        spec = next(spec for spec in WORKFLOW_PAGES if spec.key == "scan")
        widget = PlaceholderPage(spec)
        qtbot.addWidget(widget)
        assert widget.header is None
        assert widget.summary_widget is not None
        assert widget.summary_widget.text() == spec.summary


class TestUndoRedoOfInteractiveGeometry:
    def test_a_canvas_resize_is_one_undoable_step(self, page: TemplateDesignerPage):
        _add_question_region(page, columns=1)
        zone_id = page._designer_state.template.zones[0].id
        before = page._designer_state.template.zone_by_id(zone_id).bounds

        rect = page.canvas._scene.region_items[zone_id].scene_rect()
        page._on_canvas_geometry_committed(
            zone_id, "zone", rect.x(), rect.y(), rect.width() + 30.0, rect.height()
        )
        assert page._designer_state.template.zone_by_id(zone_id).bounds != before

        page.undo()
        assert page._designer_state.template.zone_by_id(zone_id).bounds == before

    def test_a_canvas_move_is_one_undoable_step(self, page: TemplateDesignerPage):
        _add_question_region(page, columns=1)
        zone_id = page._designer_state.template.zones[0].id
        before = page._designer_state.template.zone_by_id(zone_id).bounds

        rect = page.canvas._scene.region_items[zone_id].scene_rect()
        page._on_canvas_geometry_committed(
            zone_id, "zone", rect.x() + 20.0, rect.y() + 15.0, rect.width(), rect.height()
        )
        page.undo()
        assert page._designer_state.template.zone_by_id(zone_id).bounds == before

    def test_undo_restores_the_visual_state_as_well_as_the_model(
        self, page: TemplateDesignerPage
    ):
        _add_question_region(page, columns=1)
        zone_id = page._designer_state.template.zones[0].id
        before_rect = page.canvas._scene.region_items[zone_id].scene_rect()

        page._on_canvas_geometry_committed(
            zone_id, "zone", before_rect.x(), before_rect.y(),
            before_rect.width() + 30.0, before_rect.height(),
        )
        page.undo()

        after_rect = page.canvas._scene.region_items[zone_id].scene_rect()
        assert after_rect.width() == pytest.approx(before_rect.width(), abs=0.01)
        assert after_rect.x() == pytest.approx(before_rect.x(), abs=0.01)


class TestPanningDoesNotAlterTheDocument:
    def test_a_middle_drag_leaves_every_zone_exactly_as_it_was(
        self, page: TemplateDesignerPage
    ):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        _add_question_region(page, columns=4)
        page.canvas.zoom_in()
        page.canvas.zoom_in()
        before = page._designer_state.template

        viewport = page.canvas.viewport()
        QTest.mousePress(viewport, Qt.MouseButton.MiddleButton, pos=QPoint(200, 200))
        QTest.mouseMove(viewport, QPoint(120, 140))
        QTest.mouseRelease(viewport, Qt.MouseButton.MiddleButton, pos=QPoint(120, 140))

        assert page._designer_state.template == before

    def test_a_right_drag_leaves_every_zone_exactly_as_it_was(
        self, page: TemplateDesignerPage
    ):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        _add_question_region(page, columns=4)
        page.canvas.zoom_in()
        before = page._designer_state.template

        viewport = page.canvas.viewport()
        QTest.mousePress(viewport, Qt.MouseButton.RightButton, pos=QPoint(200, 200))
        QTest.mouseMove(viewport, QPoint(110, 130))
        QTest.mouseRelease(viewport, Qt.MouseButton.RightButton, pos=QPoint(110, 130))

        assert page._designer_state.template == before


class TestOrientationDetectionFromThePage:
    def test_the_action_exists_and_is_wired(self, page: TemplateDesignerPage):
        assert page.detect_orientation_action in page.toolbar_actions()
        assert page.detect_orientation_action.isEnabled() is True

    def test_detection_without_a_reference_image_does_not_crash(
        self, page: TemplateDesignerPage
    ):
        page._decoded_image = None
        page.detect_orientation_marker()

    def test_the_search_region_is_the_orientation_rectangle_widened(
        self, page: TemplateDesignerPage
    ):
        from omr_scanner.gui.template_designer.page import ORIENTATION_SEARCH_MARGIN

        marker = page._designer_state.template.orientation_marker
        width = page._decoded_image.width
        x, _y, search_width, _height = page._orientation_search_region()
        assert search_width == pytest.approx(
            marker.size.width * width * ORIENTATION_SEARCH_MARGIN
        )
        # Widened about the same centre, not shifted.
        assert x + search_width / 2 == pytest.approx(marker.center.x * width)

    def test_the_debug_directory_is_off_unless_the_environment_names_one(
        self, page: TemplateDesignerPage, monkeypatch: pytest.MonkeyPatch
    ):
        from omr_scanner.gui.template_designer.page import ORIENTATION_DEBUG_ENV

        monkeypatch.delenv(ORIENTATION_DEBUG_ENV, raising=False)
        assert page._orientation_debug_dir() is None
        monkeypatch.setenv(ORIENTATION_DEBUG_ENV, "some/where")
        assert page._orientation_debug_dir() == Path("some/where")

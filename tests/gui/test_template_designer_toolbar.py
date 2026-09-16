"""Tests for the Template Designer's toolbar: the UI polish pass.

Scope:
    This toolbar was rebuilt from a plain `QHBoxLayout` of full-text
    `QPushButton`s (which had no overflow behaviour and simply compressed
    every button below its own text width once the row ran out of space -
    the reported clipping) into a real `QToolBar` of `QAction`s with bundled
    icons. These tests check the *contract* the rebuild must preserve
    (every action exists, is wired to the same underlying behaviour, is
    enabled/disabled exactly when it was before) and the *new* properties the
    brief asked for (icons, tooltips, checked/disabled styling) - not pixel
    layout, which `docs/TESTING.md` already rules out for GUI tests.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QToolBar, QWidget

from omr_scanner.gui.template_designer.page import TemplateDesignerPage

pytestmark = pytest.mark.gui

# Every action the toolbar is expected to expose, in the order the toolbar
# lists them. Used both to check each one exists and, via `_add_toolbar_action`,
# that each one was actually given an icon and a tooltip - the two properties
# the brief's automated-test section calls out explicitly.
TOOLBAR_ACTION_NAMES = [
    # Row 1 - file, editing, detection, validation.
    "new_action", "open_action", "save_action", "save_as_action",
    "undo_action", "redo_action",
    "detect_action", "confirm_markers_action", "detect_orientation_action",
    "validate_action",
    # Row 2 - region tools, bubble editing, view.
    "add_student_id_action", "add_question_set_action", "add_question_block_action",
    "create_array_action", "distribute_columns_action",
    "add_custom_action", "add_ignored_action",
    "fine_tune_action", "clear_overrides_action",
    "zoom_out_action", "zoom_in_action", "fit_action", "actual_size_action",
    "grid_action",
]


def _lay_out(page: TemplateDesignerPage, *, width: int, height: int) -> None:
    """Give ``page`` real laid-out geometry at this size, without a visible window.

    Geometry is only propagated to children on a show/resize cycle - a splitter
    keeps its default sizes otherwise - so a test that reads a child's height has
    to ask for one. ``WA_DontShowOnScreen`` runs the whole polish and layout path
    without mapping a window onto the developer's desktop.
    """
    page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    page.show()
    page.resize(width, height)
    for _ in range(3):
        QApplication.processEvents()


def _button_for(page: TemplateDesignerPage, action: QAction) -> QWidget | None:
    """The `QToolButton` Qt built for ``action``, from whichever row holds it."""
    for toolbar in (page.toolbar, page.toolbar_view):
        button = toolbar.widgetForAction(action)
        if button is not None:
            return button
    return None


TOOLBAR_TEST_WIDTHS = [2000, 1600, 1280, 1024, 900]
"""Window widths to exercise the toolbar at, from a wide desktop down to a small
laptop - the range over which Qt's own overflow handling has to do its work."""


@pytest.fixture
def page(qtbot) -> TemplateDesignerPage:
    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES

    spec = next(s for s in WORKFLOW_PAGES if s.key == "template")
    widget = TemplateDesignerPage(spec)
    qtbot.addWidget(widget)
    return widget


class TestToolbarCanBeInstantiated:
    def test_the_page_builds_a_real_toolbar(self, page: TemplateDesignerPage):
        assert isinstance(page.toolbar, QToolBar)

    def test_the_page_builds_a_second_toolbar_row(self, page: TemplateDesignerPage):
        assert isinstance(page.toolbar_view, QToolBar)

    def test_the_two_rows_are_distinct_widgets(self, page: TemplateDesignerPage):
        assert page.toolbar is not page.toolbar_view

    def test_both_rows_are_part_of_the_page(self, page: TemplateDesignerPage):
        for toolbar in (page.toolbar, page.toolbar_view):
            assert toolbar.parentWidget() is not None
            assert page.isAncestorOf(toolbar)

    def test_each_row_actually_carries_actions(self, page: TemplateDesignerPage):
        # Two *used* rows, not one row plus an empty one - the point of the
        # split is that neither row has to hold everything.
        assert len([a for a in page.toolbar.actions() if not a.isSeparator()]) >= 4
        assert len([a for a in page.toolbar_view.actions() if not a.isSeparator()]) >= 4


class TestEveryActionExists:
    @pytest.mark.parametrize("name", TOOLBAR_ACTION_NAMES)
    def test_the_action_exists_and_is_a_qaction(self, page: TemplateDesignerPage, name: str):
        action = getattr(page, name)
        assert isinstance(action, QAction)

    @pytest.mark.parametrize("name", TOOLBAR_ACTION_NAMES)
    def test_the_action_is_actually_on_the_toolbar(self, page: TemplateDesignerPage, name: str):
        # Either row: `toolbar_actions()` exists so that splitting the toolbar
        # again later cannot silently make this check incomplete.
        assert getattr(page, name) in page.toolbar_actions()


class TestIconsAreNonNull:
    """Every icon-form action must actually carry an icon.

    The failure mode this guards against is the one that would silently
    reintroduce unreadable blank toolbar buttons.
    """

    @pytest.mark.parametrize("name", TOOLBAR_ACTION_NAMES)
    def test_the_action_has_a_non_null_icon(self, page: TemplateDesignerPage, name: str):
        action: QAction = getattr(page, name)
        assert not action.icon().isNull(), f"{name} has no icon"

    def test_region_list_duplicate_and_delete_buttons_have_icons(
        self, page: TemplateDesignerPage
    ):
        assert not page.region_list.duplicate_button.icon().isNull()
        assert not page.region_list.delete_button.icon().isNull()


class TestTooltips:
    @pytest.mark.parametrize("name", TOOLBAR_ACTION_NAMES)
    def test_the_action_has_a_non_empty_tooltip(self, page: TemplateDesignerPage, name: str):
        action: QAction = getattr(page, name)
        assert action.toolTip().strip() != ""

    @pytest.mark.parametrize("name", TOOLBAR_ACTION_NAMES)
    def test_the_action_has_a_status_tip_for_the_status_bar(
        self, page: TemplateDesignerPage, name: str
    ):
        action: QAction = getattr(page, name)
        assert action.statusTip().strip() != ""

    @pytest.mark.parametrize(
        ("name", "shortcut_text"),
        [
            ("new_action", "Ctrl+Shift+N"),
            ("open_action", "Ctrl+Shift+O"),
            ("save_action", "Ctrl+S"),
            ("save_as_action", "Ctrl+Shift+S"),
            ("undo_action", "Ctrl+Z"),
            ("redo_action", "Ctrl+Y"),
            ("fit_action", "Ctrl+0"),
        ],
    )
    def test_a_documented_shortcut_is_mentioned_in_the_tooltip(
        self, page: TemplateDesignerPage, name: str, shortcut_text: str
    ):
        action: QAction = getattr(page, name)
        assert shortcut_text in action.toolTip()

    def test_region_list_buttons_have_tooltips(self, page: TemplateDesignerPage):
        assert page.region_list.duplicate_button.toolTip().strip() != ""
        assert page.region_list.delete_button.toolTip().strip() != ""


class TestIconOnlyVersusIconWithText:
    """Universal commands are icon-only; OMR-specific actions keep their text.

    Checked through the actual `QToolButton` style Qt applies, not by
    re-reading the construction call.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "new_action", "open_action", "save_action", "save_as_action",
            "undo_action", "redo_action",
            "zoom_out_action", "zoom_in_action", "fit_action", "actual_size_action",
            "grid_action",
        ],
    )
    def test_universal_commands_are_icon_only(self, page: TemplateDesignerPage, name: str):
        from PySide6.QtWidgets import QToolButton

        action = getattr(page, name)
        button = _button_for(page, action)
        assert isinstance(button, QToolButton)
        assert button.toolButtonStyle() == button.toolButtonStyle().ToolButtonIconOnly

    @pytest.mark.parametrize(
        "name",
        [
            "detect_action", "confirm_markers_action", "detect_orientation_action",
            "add_student_id_action", "add_question_set_action",
            "add_question_block_action", "create_array_action", "distribute_columns_action",
            "add_custom_action", "add_ignored_action",
            "fine_tune_action", "clear_overrides_action", "validate_action",
        ],
    )
    def test_omr_specific_actions_keep_their_text(self, page: TemplateDesignerPage, name: str):
        action = getattr(page, name)
        assert action.text().strip() != ""
        # The toolbar's own default style is TextBesideIcon; these actions
        # simply never had IconOnly forced onto them (see `icon_only=` in
        # `_build_toolbar`).
        from PySide6.QtWidgets import QToolButton

        button = _button_for(page, action)
        assert isinstance(button, QToolButton)
        assert button.toolButtonStyle() == button.toolButtonStyle().ToolButtonTextBesideIcon


class TestKeyboardShortcutsRemainIntact:
    def test_eleven_page_level_shortcuts_are_still_registered(self, page: TemplateDesignerPage):
        # Unchanged from before this task: the page's own QShortcut objects,
        # not QAction.setShortcut - see `_add_toolbar_action`'s docstring for
        # why the two must not both bind the same sequence.
        assert len(page._shortcuts) == 11

    def test_ctrl_z_still_triggers_undo(self, page: TemplateDesignerPage, monkeypatch):
        called = []
        monkeypatch.setattr(page, "undo", lambda: called.append(True))
        undo_shortcut = next(
            s for s in page._shortcuts if s.key().toString() in ("Ctrl+Z", "Undo")
        )
        undo_shortcut.activated.emit()
        assert called == [True]

    def test_ctrl_d_still_triggers_duplicate(self, page: TemplateDesignerPage, monkeypatch):
        called = []
        monkeypatch.setattr(page, "_duplicate_selected", lambda: called.append(True))
        shortcut = next(s for s in page._shortcuts if s.key().toString() == "Ctrl+D")
        shortcut.activated.emit()
        assert called == [True]


class TestEnabledStateFollowsApplicationState:
    """Re-confirms the enable/disable behaviour the pre-toolbar tests covered.

    Checked through the renamed `_action` attributes; a regression here means
    the rebuild changed *behaviour*, not just appearance.
    """

    def test_document_actions_start_disabled(self, page: TemplateDesignerPage):
        for name in (
            "save_action", "save_as_action", "detect_action", "confirm_markers_action",
            "add_student_id_action", "add_question_set_action", "add_question_block_action",
            "add_custom_action", "add_ignored_action", "fine_tune_action",
            "clear_overrides_action", "validate_action",
        ):
            assert getattr(page, name).isEnabled() is False

    def test_new_and_open_are_available_without_a_document(self, page: TemplateDesignerPage):
        # Unlike the document-scoped actions above, New/Open must always be
        # usable - this was already true of the original QPushButtons.
        assert page.new_action.isEnabled() is True
        assert page.open_action.isEnabled() is True

    def test_document_actions_enable_once_a_template_is_loaded(self, page: TemplateDesignerPage):
        from omr_scanner.domain.template_authoring import build_blank_template
        from omr_scanner.gui.template_designer.state import DesignerState

        template = build_blank_template(
            name="T", canonical_width_px=200, canonical_height_px=200
        )
        page._designer_state = DesignerState(
            template, template_path=None, reference_image_path=None
        )
        page._set_document_controls_enabled(True)

        assert page.save_action.isEnabled() is True
        assert page.validate_action.isEnabled() is True

    def test_array_and_distribute_stay_disabled_until_a_question_column_is_selected(
        self, page: TemplateDesignerPage
    ):
        from dataclasses import dataclass

        from omr_scanner.domain.geometry import NormalizedRect, NormalizedSize
        from omr_scanner.domain.template import FieldType
        from omr_scanner.domain.template_authoring import (
            build_blank_template,
            generate_character_grid_zone,
        )
        from omr_scanner.gui.template_designer.state import DesignerState

        @dataclass
        class _FakeDecodedImage:
            width: int
            height: int

        template = build_blank_template(name="T", canonical_width_px=200, canonical_height_px=200)
        zone = generate_character_grid_zone(
            zone_id="sid", label="Student ID", field_type=FieldType.NUMERIC, symbols=("0", "1"),
            character_count=2, bounds=NormalizedRect(x=0.1, y=0.1, width=0.3, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.02),
        )
        state = DesignerState(template, template_path=None, reference_image_path=None)
        state.add_zones((zone,))
        page._designer_state = state
        page._decoded_image = _FakeDecodedImage(width=200, height=200)
        page._set_document_controls_enabled(True)
        page._refresh_all()

        assert page.create_array_action.isEnabled() is False
        assert page.distribute_columns_action.isEnabled() is False

        page._show_properties_for("sid", "zone")
        assert page.create_array_action.isEnabled() is False
        assert page.distribute_columns_action.isEnabled() is False


class TestActionsTriggerTheOriginalBehaviour:
    """Triggering each action must still reach the exact method it always did.

    The toolbar rebuild must not have quietly disconnected anything.
    """

    @pytest.fixture(autouse=True)
    def _enable_document_actions(self, page: TemplateDesignerPage) -> None:
        # Every action under test here is document-scoped (Save, Detect, add
        # a region, ...) and a *disabled* `QAction.trigger()` is a Qt no-op by
        # design - this fixture puts the page in the state a user triggering
        # these from a real toolbar would already be in, an open document.
        page._set_document_controls_enabled(True)

    @pytest.mark.parametrize(
        ("action_name", "method_name"),
        [
            ("new_action", "new_template_from_image"),
            ("open_action", "open_template"),
            ("save_action", "save"),
            ("save_as_action", "save_as"),
            ("undo_action", "undo"),
            ("redo_action", "redo"),
            ("detect_action", "detect_markers"),
            ("confirm_markers_action", "confirm_detected_markers"),
            ("validate_action", "show_validation"),
            ("clear_overrides_action", "_clear_selected_zone_overrides"),
        ],
    )
    def test_triggering_the_action_calls_the_expected_method(
        self, page: TemplateDesignerPage, monkeypatch, action_name: str, method_name: str
    ):
        called = []
        monkeypatch.setattr(page, method_name, lambda *_a, **_k: called.append(True))
        getattr(page, action_name).trigger()
        assert called == [True]

    @pytest.mark.parametrize(
        ("action_name", "region_kind"),
        [
            ("add_student_id_action", "student_id"),
            ("add_question_set_action", "question_set"),
            ("add_question_block_action", "question_block"),
            ("add_custom_action", "custom"),
            ("add_ignored_action", "ignored"),
        ],
    )
    def test_triggering_an_add_region_action_starts_the_right_region_kind(
        self, page: TemplateDesignerPage, monkeypatch, action_name: str, region_kind: str
    ):
        called = []
        monkeypatch.setattr(
            page, "_start_add_region", lambda kind: called.append(kind)
        )
        getattr(page, action_name).trigger()
        assert called == [region_kind]

    def test_triggering_zoom_in_zooms_the_canvas(self, page: TemplateDesignerPage):
        before = page.canvas.zoom
        page.zoom_in_action.trigger()
        assert page.canvas.zoom > before

    def test_triggering_zoom_out_zooms_the_canvas(self, page: TemplateDesignerPage):
        page.zoom_in_action.trigger()
        zoomed_in = page.canvas.zoom
        page.zoom_out_action.trigger()
        assert page.canvas.zoom < zoomed_in

    def test_triggering_actual_size_sets_zoom_to_one(self, page: TemplateDesignerPage):
        page.zoom_in_action.trigger()
        page.actual_size_action.trigger()
        assert page.canvas.zoom == pytest.approx(1.0)

    def test_toggling_grid_shows_and_hides_the_grid_overlay(self, page: TemplateDesignerPage):
        page.grid_action.setChecked(True)
        assert page.canvas._grid_visible is True
        page.grid_action.setChecked(False)
        assert page.canvas._grid_visible is False

    def test_toggling_edit_bubbles_without_a_selection_is_refused(
        self, page: TemplateDesignerPage, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *_a, **_k: None))
        page.fine_tune_action.setChecked(True)
        assert page.fine_tune_action.isChecked() is False


class TestCheckableActionsShowAnObviousCheckedState:
    """Toggled controls must not rely on colour alone.

    `QAction` itself exposes `isChecked()`, which is what a screen
    reader/accessible client uses regardless of the stylesheet; the visual
    "checked" background is additionally confirmed by inspection, see
    `docs/testing/ui_polish_manual_test.md`.
    """

    def test_grid_action_is_checkable(self, page: TemplateDesignerPage):
        assert page.grid_action.isCheckable() is True

    def test_fine_tune_action_is_checkable(self, page: TemplateDesignerPage):
        assert page.fine_tune_action.isCheckable() is True

    def test_non_toggle_actions_are_not_checkable(self, page: TemplateDesignerPage):
        for name in ("save_action", "validate_action", "detect_action", "zoom_in_action"):
            assert getattr(page, name).isCheckable() is False


class TestToolbarSurvivesNarrowWindows:
    """Two ordinary `QToolBar` rows, so Qt handles narrowing - not fixed positions.

    The brief's requirement is that at any display scaling or window width the
    controls must not overlap, labels must not clip, icons must stay visible and
    the canvas must keep its space. What actually guarantees that is *using Qt
    layouts*: a `QToolBar` moves what does not fit into its own overflow menu
    rather than compressing buttons below their text width.

    These assert the structural consequence - each row keeps its height and hands
    surplus actions to the overflow - rather than pixel positions, which
    `docs/TESTING.md` rules out for GUI tests.
    """

    @pytest.mark.parametrize("width", TOOLBAR_TEST_WIDTHS)
    def test_neither_row_grows_taller_as_the_window_narrows(
        self, page: TemplateDesignerPage, width: int
    ):
        page.resize(2000, 950)
        reference = (page.toolbar.sizeHint().height(), page.toolbar_view.sizeHint().height())
        page.resize(width, 950)
        assert (
            page.toolbar.sizeHint().height(),
            page.toolbar_view.sizeHint().height(),
        ) == reference

    @pytest.mark.parametrize("width", TOOLBAR_TEST_WIDTHS)
    def test_every_action_remains_reachable_at_any_width(
        self, page: TemplateDesignerPage, width: int
    ):
        """Overflowed is not lost: an action in the "»" menu is still on the toolbar."""
        page.resize(width, 950)
        for name in TOOLBAR_ACTION_NAMES:
            assert getattr(page, name) in page.toolbar_actions()

    def test_the_first_row_still_fits_at_a_small_laptop_width(
        self, page: TemplateDesignerPage
    ):
        """Row 1 is the short one by design, so file and detection stay visible."""
        _lay_out(page, width=1024, height=950)
        hidden = [
            action.text()
            for action in page.toolbar.actions()
            if not action.isSeparator()
            and (button := page.toolbar.widgetForAction(action)) is not None
            and not button.isVisible()
        ]
        assert not hidden, f"row 1 overflowed at 1024px: {hidden}"

    def test_the_canvas_keeps_most_of_the_height(self, page: TemplateDesignerPage):
        """The point of the compact header and the two short toolbar rows."""
        _lay_out(page, width=1600, height=950)
        assert page.canvas.height() > 950 * 0.7, (
            f"canvas got {page.canvas.height()}px of 950 - the header is eating the page"
        )

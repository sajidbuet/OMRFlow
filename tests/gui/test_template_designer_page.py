"""GUI smoke tests for the Template Designer page.

Per the project's GUI testing policy (``docs/TESTING.md``): modal dialogs are
never exercised directly. Where the page's own flow would show one (a region
dialog, a file picker), the test drives the underlying method the dialog would
otherwise call - `page._designer_state`, `page._on_region_drawn`, and so on -
exactly the same split `main_window.py` already uses between `_prompt_*`
(dialog-owning) and the behaviour it triggers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omr_scanner.domain.geometry import NormalizedRect, NormalizedSize
from omr_scanner.domain.template import FieldType, MarkerRole, Zone
from omr_scanner.domain.template_authoring import build_blank_template, generate_character_grid_zone
from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
from omr_scanner.gui.template_designer.page import TemplateDesignerPage
from omr_scanner.gui.template_designer.state import DesignerState
from omr_scanner.imaging.synthetic import SyntheticSheetSpec, render_sheet
from omr_scanner.services import decode_image_file, save_image

pytestmark = pytest.mark.gui

DIGITS = tuple(str(digit) for digit in range(10))

TEMPLATE_SPEC = next(spec for spec in WORKFLOW_PAGES if spec.key == "template")


@pytest.fixture(autouse=True)
def no_blocking_message_boxes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent any `QMessageBox` from opening a real, un-clickable modal loop.

    Several page methods show one (a missing-marker notice, "select a bubble
    region first", the unsaved-changes prompt); under the offscreen platform
    there is nothing to click, so `exec()` on a real message box blocks the
    test forever rather than failing loudly. Individual tests that care about
    *which* button was pressed override these with their own `monkeypatch`
    call, which simply replaces this one for the duration of that test.
    """
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *_a, **_k: None))
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *_a, **_k: QMessageBox.StandardButton.Cancel),
    )


@pytest.fixture
def sheet_image_path(tmp_path: Path) -> Path:
    """A clean synthetic reference sheet, written to disk."""
    sheet = render_sheet(SyntheticSheetSpec())
    path = tmp_path / "sheet.png"
    save_image(sheet.image, path)
    return path


@pytest.fixture
def page(qtbot) -> TemplateDesignerPage:
    widget = TemplateDesignerPage(TEMPLATE_SPEC)
    qtbot.addWidget(widget)
    return widget


@pytest.fixture
def opened_page(page: TemplateDesignerPage, sheet_image_path: Path) -> TemplateDesignerPage:
    """A page with a blank template already loaded against a reference image."""
    decoded = decode_image_file(sheet_image_path)
    template = build_blank_template(
        name="Fixture template",
        canonical_width_px=decoded.width,
        canonical_height_px=decoded.height,
    )
    page._designer_state = DesignerState(
        template, template_path=None, reference_image_path=sheet_image_path
    )
    page._decoded_image = decoded
    page.canvas.set_reference_image(decoded)
    page.properties.set_image_size(decoded.width, decoded.height)
    page._set_document_controls_enabled(True)
    page._refresh_all()
    return page


def _zone(zone_id: str = "sid", x: float = 0.1, y: float = 0.1) -> Zone:
    return generate_character_grid_zone(
        zone_id=zone_id,
        label="Student ID",
        field_type=FieldType.NUMERIC,
        symbols=DIGITS,
        character_count=5,
        bounds=NormalizedRect(x=x, y=y, width=0.2, height=0.3),
        bubble_size=NormalizedSize(width=0.02, height=0.015),
    )


class TestPageConstruction:
    def test_the_page_instantiates_without_a_document(self, page: TemplateDesignerPage):
        assert page._designer_state is None
        assert page.save_action.isEnabled() is False

    def test_document_controls_are_disabled_until_a_template_exists(
        self, page: TemplateDesignerPage
    ):
        for control in page._document_controls:
            assert control.isEnabled() is False


class TestOpeningAReferenceImage:
    def test_opening_a_template_enables_document_controls(self, opened_page: TemplateDesignerPage):
        assert opened_page.save_action.isEnabled() is True
        assert opened_page.detect_action.isEnabled() is True

    def test_the_canvas_shows_the_reference_image_size(self, opened_page: TemplateDesignerPage):
        assert opened_page.canvas._image_size == (
            opened_page._decoded_image.width, opened_page._decoded_image.height
        )

    def test_all_four_markers_and_the_orientation_marker_appear_on_the_canvas(
        self, opened_page: TemplateDesignerPage
    ):
        item_ids = set(opened_page.canvas._scene.region_items)
        for role in MarkerRole:
            assert f"marker:{role.value}" in item_ids
        assert "orientation" in item_ids

    def test_the_region_list_shows_the_four_markers_and_orientation(
        self, opened_page: TemplateDesignerPage
    ):
        marker_group = opened_page.region_list._groups["marker"]
        assert marker_group.childCount() == 4
        orientation_group = opened_page.region_list._groups["orientation"]
        assert orientation_group.childCount() == 1


class TestMarkerDetection:
    def test_detecting_markers_moves_them_onto_the_printed_squares(
        self, opened_page: TemplateDesignerPage
    ):
        opened_page.detect_markers()
        for role in MarkerRole:
            status = opened_page._designer_state.marker_status[role]
            assert status.confidence is not None
            assert status.confidence > 0.5

    def test_confirming_detected_markers_marks_them_confirmed(
        self, opened_page: TemplateDesignerPage
    ):
        opened_page.detect_markers()
        opened_page.confirm_detected_markers()
        for role in MarkerRole:
            assert opened_page._designer_state.marker_status[role].confirmed is True

    def test_detection_without_a_reference_image_does_not_crash(
        self, page: TemplateDesignerPage
    ):
        page.detect_markers()  # no-op: no designer state at all


class TestZoneManagement:
    def test_adding_a_zone_updates_the_canvas_and_region_list(
        self, opened_page: TemplateDesignerPage
    ):
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all()
        assert "sid" in opened_page.canvas._scene.region_items
        assert opened_page._designer_state.template.zone_by_id("sid") is not None

    def test_deleting_a_zone_via_the_region_list_signal(self, opened_page: TemplateDesignerPage):
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all()
        opened_page._on_delete_requested("sid")
        assert opened_page._designer_state.template.zone_by_id("sid") is None

    def test_a_marker_cannot_be_deleted_through_the_region_list(
        self, opened_page: TemplateDesignerPage
    ):
        opened_page._on_delete_requested("marker:top_left")
        assert len(opened_page._designer_state.template.registration_markers) == 4

    def test_duplicating_a_zone_creates_a_second_one(self, opened_page: TemplateDesignerPage):
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all()
        opened_page._on_duplicate_requested("sid")
        assert len(opened_page._designer_state.template.zones) == 2

    def test_renaming_a_zone_updates_its_label(self, opened_page: TemplateDesignerPage):
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all()
        opened_page._on_rename_requested("sid", "Roll Number")
        assert opened_page._designer_state.template.zone_by_id("sid").label == "Roll Number"

    def test_region_drawn_with_a_pending_dialog_kind_opens_the_right_generator(
        self, opened_page: TemplateDesignerPage, monkeypatch: pytest.MonkeyPatch
    ):
        from PySide6.QtWidgets import QDialog

        from omr_scanner.gui.template_designer import dialogs as dialogs_module

        # Auto-accept whichever dialog is constructed, using its own defaults -
        # equivalent to a user clicking OK without changing anything.
        def accept_with_defaults(dialog: object) -> QDialog.DialogCode:
            dialog._on_accept()  # type: ignore[attr-defined]
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(dialogs_module.StudentIdDialog, "exec", accept_with_defaults)

        opened_page._start_add_region("student_id")
        assert opened_page.canvas.is_drawing is True
        # `_on_region_drawn` is called directly here, standing in for the real
        # mouse-release gesture (which calls `cancel_draw_mode()` itself
        # before emitting `region_drawn` - see `canvas.mouseReleaseEvent`).
        opened_page.canvas.cancel_draw_mode()
        opened_page._on_region_drawn(100.0, 150.0, 300.0, 500.0)

        assert opened_page.canvas.is_drawing is False
        zones = opened_page._designer_state.template.zones
        assert len(zones) == 1
        assert zones[0].field.type is FieldType.NUMERIC


class TestGeometryEditing:
    def test_moving_a_zone_through_the_canvas_commit_path(self, opened_page: TemplateDesignerPage):
        opened_page._designer_state.add_zones((_zone(x=0.1, y=0.1),))
        opened_page._refresh_all()
        item = opened_page.canvas._scene.region_items["sid"]
        rect = item.scene_rect()

        opened_page._on_canvas_geometry_committed(
            "sid", "zone", rect.x() + 40.0, rect.y() + 20.0, rect.width(), rect.height()
        )

        moved = opened_page._designer_state.template.zone_by_id("sid")
        assert moved.bounds.x > 0.1

    def test_resizing_a_zone_through_the_canvas_commit_path_keeps_bubble_count(
        self, opened_page: TemplateDesignerPage
    ):
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all()
        item = opened_page.canvas._scene.region_items["sid"]
        rect = item.scene_rect()
        original_count = opened_page._designer_state.template.zone_by_id("sid").bubble_count

        opened_page._on_canvas_geometry_committed(
            "sid", "zone", rect.x(), rect.y(), rect.width() * 1.5, rect.height()
        )

        resized = opened_page._designer_state.template.zone_by_id("sid")
        assert resized.bubble_count == original_count
        assert resized.bounds.width > _zone().bounds.width

    def test_editing_properties_numerically_applies_to_the_selection(
        self, opened_page: TemplateDesignerPage
    ):
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all(keep_selection="sid")
        image = opened_page._decoded_image
        zone = opened_page._designer_state.template.zone_by_id("sid")
        new_x_px = (zone.bounds.x + 0.05) * image.width

        opened_page._on_properties_edited(
            new_x_px,
            zone.bounds.y * image.height,
            zone.bounds.width * image.width,
            zone.bounds.height * image.height,
        )

        assert opened_page._designer_state.template.zone_by_id("sid").bounds.x == pytest.approx(
            zone.bounds.x + 0.05, abs=1e-3
        )

    def test_moving_a_registration_marker_marks_it_manually_confirmed(
        self, opened_page: TemplateDesignerPage
    ):
        item = opened_page.canvas._scene.region_items["marker:top_left"]
        rect = item.scene_rect()
        opened_page._on_canvas_geometry_committed(
            "marker:top_left", "marker", rect.x() + 5, rect.y() + 5, rect.width(), rect.height()
        )
        status = opened_page._designer_state.marker_status[MarkerRole.TOP_LEFT]
        assert status.confirmed is True


class TestUndoRedo:
    def test_undo_reverts_the_last_zone_addition(self, opened_page: TemplateDesignerPage):
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all()
        opened_page.undo()
        assert opened_page._designer_state.template.zone_by_id("sid") is None

    def test_redo_reapplies_an_undone_change(self, opened_page: TemplateDesignerPage):
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all()
        opened_page.undo()
        opened_page.redo()
        assert opened_page._designer_state.template.zone_by_id("sid") is not None

    def test_undo_redo_buttons_reflect_history_state(self, opened_page: TemplateDesignerPage):
        opened_page._refresh_all()
        assert opened_page.undo_action.isEnabled() is False
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all()
        assert opened_page.undo_action.isEnabled() is True
        opened_page.undo()
        assert opened_page.redo_action.isEnabled() is True


class TestFineTuneMode:
    def test_toggling_fine_tune_without_a_selection_is_refused(
        self, opened_page: TemplateDesignerPage
    ):
        opened_page.fine_tune_action.setChecked(True)
        assert opened_page.fine_tune_action.isChecked() is False

    def test_toggling_fine_tune_with_a_grid_zone_selected_shows_dots(
        self, opened_page: TemplateDesignerPage
    ):
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all()
        opened_page.canvas.select_region("sid")

        opened_page.fine_tune_action.setChecked(True)

        assert opened_page._fine_tune_zone_id == "sid"
        assert len(opened_page.canvas._scene.bubble_items) == _zone().bubble_count

    def test_moving_a_bubble_dot_creates_an_override(self, opened_page: TemplateDesignerPage):
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all()
        opened_page.canvas.select_region("sid")
        opened_page.fine_tune_action.setChecked(True)

        opened_page._on_bubble_moved(0, 0, 500.0, 600.0)

        zone = opened_page._designer_state.template.zone_by_id("sid")
        assert len(zone.grid.overrides) == 1

    def test_clearing_overrides_removes_them_all(self, opened_page: TemplateDesignerPage):
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all()
        opened_page._designer_state.set_bubble_override(
            "sid", row=0, column=0, center_x=0.5, center_y=0.5
        )
        opened_page.canvas.select_region("sid")

        opened_page._clear_selected_zone_overrides()

        zone = opened_page._designer_state.template.zone_by_id("sid")
        assert zone.grid.overrides == ()


class TestValidation:
    def test_show_validation_does_not_raise_on_a_clean_template(
        self, opened_page: TemplateDesignerPage, monkeypatch: pytest.MonkeyPatch
    ):
        from PySide6.QtWidgets import QDialog

        monkeypatch.setattr(QDialog, "exec", lambda _self: QDialog.DialogCode.Accepted)
        opened_page.show_validation()  # must not raise


class TestSaveAndReload:
    def test_saving_writes_a_loadable_template(
        self, opened_page: TemplateDesignerPage, tmp_path: Path
    ):
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all()

        destination = tmp_path / "saved.omrt"
        saved = opened_page._save_to(destination)

        assert saved is True
        assert destination.is_file()
        assert opened_page._designer_state.is_dirty is False

    def test_the_saved_template_carries_a_relative_reference_image_path(
        self, opened_page: TemplateDesignerPage, tmp_path: Path
    ):
        destination = tmp_path / "saved.omrt"
        opened_page._save_to(destination)

        from omr_scanner.services import load_template

        reloaded = load_template(destination)
        assert reloaded.reference_image is not None
        assert (destination.parent / reloaded.reference_image).resolve() == (
            opened_page._designer_state.reference_image_path.resolve()
        )

    def test_loading_a_saved_template_restores_its_zones(
        self, opened_page: TemplateDesignerPage, tmp_path: Path
    ):
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all()
        destination = tmp_path / "saved.omrt"
        opened_page._save_to(destination)

        fresh = TemplateDesignerPage(TEMPLATE_SPEC)
        fresh._load_template_file(destination)

        assert fresh._designer_state.template.zone_by_id("sid") is not None
        assert fresh._decoded_image is not None

    def test_loading_a_template_whose_image_moved_falls_back_to_a_blank_canvas(
        self, opened_page: TemplateDesignerPage, tmp_path: Path, sheet_image_path: Path
    ):
        destination = tmp_path / "saved.omrt"
        opened_page._save_to(destination)
        sheet_image_path.unlink()

        fresh = TemplateDesignerPage(TEMPLATE_SPEC)
        fresh._load_template_file(destination)  # must not raise

        assert fresh._designer_state is not None
        assert fresh._decoded_image is None


class TestStatusDisplay:
    def test_the_title_shows_unsaved_changes(self, opened_page: TemplateDesignerPage):
        opened_page._designer_state.add_zones((_zone(),))
        opened_page._refresh_all()
        assert "*" in opened_page.title_label.text()

    def test_the_title_has_no_marker_once_saved(
        self, opened_page: TemplateDesignerPage, tmp_path: Path
    ):
        opened_page._save_to(tmp_path / "saved.omrt")
        assert "*" not in opened_page.title_label.text()

    def test_zoom_label_updates_on_zoom(self, opened_page: TemplateDesignerPage):
        opened_page.canvas.zoom_to_actual_size()
        assert "100" in opened_page.zoom_label.text()

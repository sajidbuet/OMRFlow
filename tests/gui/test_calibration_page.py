"""End-to-end GUI validation of the Calibration workflow (Phase 4).

Scope:
    Drives :class:`~omr_scanner.gui.calibration.page.CalibrationPage` through
    its public commands - the same ones its buttons call - and asserts
    against program state, never a screenshot. Covers the acceptance criteria
    in order:

    ===== ==========================================================
    Test  Workflow
    ===== ==========================================================
    A     Launch and navigation to the Calibration stage.
    B     Loading a template and adding representative scans.
    C     Running a test scan: registration, geometry, quality summary.
    D     Marker and bubble overlay geometry matches the engine's own.
    E     Click-to-inspect a bubble.
    F     Field-level filtering.
    G     Threshold controls: immediate feedback, reset, defaults.
    H     Saving calibration to the template, and staleness afterwards.
    I     Multi-scan sample summary and aggregate status.
    J     A deliberately mismatched template is flagged, never a false pass.
    ===== ==========================================================

Why the ``_prompt_*`` methods are never called:
    Each owns a native modal file dialog; the command beside it
    (``load_template_from``, ``add_scan_paths``) does the work and is what
    these tests exercise (``docs/TESTING.md``).

Why running a scan is awaited on a signal:
    :class:`~omr_scanner.gui.calibration.worker.CalibrationWorker` is a real
    ``QThread``. Every wait here is on
    :attr:`~omr_scanner.gui.calibration.page.CalibrationPage.run_finished`,
    never a fixed sleep.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import QObject
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.domain.geometry import NormalizedPoint
from omr_scanner.gui.calibration.page import CalibrationPage
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.services import CalibrationStatus, RegistrationStatus, load_template, save_template

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.domain.template import OmrTemplate

pytestmark = pytest.mark.gui

WORKER_TIMEOUT_MS = 60_000
"""A ceiling, not a delay - every wait returns as soon as its signal arrives."""

STANDARD_MARKS = {
    "roll_number": dict(enumerate("120317")),
    "set_code": {0: "A"},
    "questions_0": dict.fromkeys(range(10), "B"),
    "questions_1": dict.fromkeys(range(10), "C"),
}


def confirm_save(monkeypatch: pytest.MonkeyPatch, seen: list[str] | None = None) -> None:
    """Answer the "save calibration?" confirmation dialog with Yes.

    Args:
        monkeypatch: The test's fixture.
        seen: When given, every dialog message is appended to it, so a test
            can assert on what the confirmation actually said.
    """
    from PySide6.QtWidgets import QMessageBox

    def answer_yes(
        _parent: object, _title: str, text: str, *_args: object, **_kwargs: object
    ) -> QMessageBox.StandardButton:
        if seen is not None:
            seen.append(text)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(
        "omr_scanner.gui.calibration.page.QMessageBox.question", staticmethod(answer_yes)
    )


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture
def template() -> OmrTemplate:
    return build_answer_sheet_template()


@pytest.fixture
def template_path(tmp_path: Path, template: OmrTemplate) -> Path:
    path = tmp_path / "templates" / "synthetic.omrt"
    path.parent.mkdir(parents=True, exist_ok=True)
    return save_template(template, path)


@pytest.fixture
def write_sheet(tmp_path: Path, template: OmrTemplate):
    import cv2

    scans = tmp_path / "scans"
    scans.mkdir(parents=True, exist_ok=True)
    counter = {"n": 0}

    def write(marks: dict | None = None, name: str | None = None) -> Path:
        counter["n"] += 1
        image = render_marked_sheet(template, marks or STANDARD_MARKS)
        path = scans / (name or f"sheet_{counter['n']:02d}.png")
        cv2.imwrite(str(path), image)
        return path

    return write


@pytest.fixture
def page(qtbot) -> CalibrationPage:
    spec = next(item for item in WORKFLOW_PAGES if item.key == "calibration")
    calibration_page = CalibrationPage(spec)
    qtbot.addWidget(calibration_page)
    return calibration_page


@pytest.fixture
def loaded_page(page: CalibrationPage, template_path: Path) -> CalibrationPage:
    assert page.load_template_from(template_path) is True
    return page


# ----------------------------------------------------------------------
# A. Launch and navigation
# ----------------------------------------------------------------------
class TestALaunch:
    @pytest.fixture
    def window(self, qtbot, tmp_path: Path) -> MainWindow:
        from omr_scanner.config import AppConfig

        main_window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(main_window)
        return main_window

    def test_calibration_is_a_real_stage_between_template_and_scan(self):
        keys = [spec.key for spec in WORKFLOW_PAGES]
        assert keys.index("template") < keys.index("calibration") < keys.index("scan")

    def test_navigating_to_calibration_shows_the_real_page(self, window: MainWindow):
        assert window.show_page("calibration") is True
        assert window.stack.currentWidget().objectName() == "calibrationPage"

    def test_stable_object_names_exist_for_gui_testing(self, loaded_page: CalibrationPage):
        # Spec section 50's minimum list, read back by objectName the way a
        # screenshot or automation script would find them.
        assert loaded_page.objectName() == "calibrationPage"
        required = [
            "testScanList", "calibrationImageView",
            "markerOverlayToggle", "bubbleOverlayToggle", "scoreOverlayToggle",
            "bubbleThresholdSpinBox", "bubbleThresholdSlider",
            "runCalibrationButton", "runAllCalibrationButton",
            "resetCalibrationButton", "saveCalibrationButton",
            "calibrationSummaryPanel", "calibrationStatusLabel",
            "bubbleDiagnosticPanel",
            "sampleWindowOverlayToggle", "bubbleCenterOverlayToggle",
            "calibrationViewModeCombo", "fieldDiagnosticsLabel",
            "thresholdStateLabel",
        ]
        missing = [name for name in required if loaded_page.findChild(QObject, name) is None]
        assert not missing, missing


# ----------------------------------------------------------------------
# B. Template and scans
# ----------------------------------------------------------------------
class TestBTemplateAndScans:
    def test_loading_a_template_enables_adding_scans(
        self, page: CalibrationPage, template_path: Path
    ):
        assert page.add_scan_button.isEnabled() is False
        assert page.load_template_from(template_path) is True
        assert page.add_scan_button.isEnabled() is True
        assert "Synthetic" in page.template_name_label.text()

    def test_adding_scans_populates_the_list(self, loaded_page: CalibrationPage, write_sheet):
        paths = [write_sheet(), write_sheet()]
        added = loaded_page.add_scan_paths(paths)
        assert added == 2
        assert loaded_page.test_scan_list.count() == 2
        assert len(loaded_page.state.entries) == 2

    def test_adding_the_same_scan_twice_does_not_duplicate_it(
        self, loaded_page: CalibrationPage, write_sheet
    ):
        path = write_sheet()
        loaded_page.add_scan_paths([path])
        added_again = loaded_page.add_scan_paths([path])
        assert added_again == 0
        assert loaded_page.test_scan_list.count() == 1

    def test_removing_and_clearing_scans(self, loaded_page: CalibrationPage, write_sheet):
        loaded_page.add_scan_paths([write_sheet(), write_sheet()])
        loaded_page.test_scan_list.setCurrentRow(0)
        loaded_page.remove_selected_scan()
        assert len(loaded_page.state.entries) == 1
        loaded_page.clear_scans()
        assert len(loaded_page.state.entries) == 0
        assert loaded_page.test_scan_list.count() == 0


# ----------------------------------------------------------------------
# C. Running a test scan
# ----------------------------------------------------------------------
class TestCRunningATestScan:
    def test_running_the_selected_scan_registers_and_reports(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            assert loaded_page.run_selected() is True

        entry = loaded_page.state.entries[0]
        assert entry.result is not None
        assert entry.result.registration is not RegistrationStatus.FAILED
        assert entry.report is not None
        assert entry.report.status in (
            CalibrationStatus.PASSED, CalibrationStatus.PASSED_WITH_WARNINGS
        )
        assert "Registration" in loaded_page.calibration_summary_panel.text()
        assert loaded_page.calibration_status_label.text() == entry.report.status.label

    def test_the_sample_table_gains_a_row_per_scan(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        loaded_page.add_scan_paths([write_sheet(), write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            assert loaded_page.run_all() is True
        assert loaded_page.sample_table.rowCount() == 2
        for row in range(2):
            assert loaded_page.sample_table.item(row, 0).text()
            # "WARNING" is a legitimate alignment-warning outcome even for a
            # clean synthetic render; what matters here is that registration
            # did not fail, which "PASS"/"WARNING" both confirm.
            assert loaded_page.sample_table.item(row, 1).text() in ("PASS", "WARNING")

    def test_advanced_diagnostics_are_hidden_until_asked_for(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()
        # `.isVisible()` is unreliable for a page that was never `.show()`n -
        # Qt folds in the (also-hidden) top-level ancestor's own visibility.
        # `.isVisibleTo(loaded_page)` asks the only question that matters
        # here: would this widget show if its container did.
        assert loaded_page.advanced_panel.isVisibleTo(loaded_page) is False
        loaded_page.advanced_toggle.setChecked(True)
        assert loaded_page.advanced_panel.isVisibleTo(loaded_page) is True
        assert "Reprojection" in loaded_page.advanced_panel.text()


# ----------------------------------------------------------------------
# D. Overlay geometry - the major Phase 4 geometry requirement
# ----------------------------------------------------------------------
class TestDOverlayGeometryMatchesTheEngine:
    def test_bubble_overlay_coordinates_are_the_engines_own(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()

        entry = loaded_page.state.entries[0]
        overlay = loaded_page.preview._overlay
        # The overlay was fed exactly the result's own bubbles (the "All"
        # filter is selected by default) - not a recomputed approximation.
        assert {(b.zone_id, b.row, b.column) for b in overlay._bubbles} == {
            (b.zone_id, b.row, b.column) for b in entry.result.bubbles
        }
        one = next(iter(overlay._bubbles))
        matching = next(
            b for b in entry.result.bubbles
            if (b.zone_id, b.row, b.column) == (one.zone_id, one.row, one.column)
        )
        assert one.x == matching.x
        assert one.y == matching.y

    def test_marker_overlay_carries_expected_and_canonical_positions(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()

        entry = loaded_page.state.entries[0]
        assert len(entry.result.markers) == 4
        for marker in entry.result.markers:
            # A clean synthetic render's detected marker, reprojected, lands
            # almost exactly on its expected position.
            assert marker.canonical_x == pytest.approx(marker.expected_x, abs=1.0)
            assert marker.canonical_y == pytest.approx(marker.expected_y, abs=1.0)


    def test_the_sampling_overlay_draws_the_measured_region_not_the_printed_bubble(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        # The defect this guards: drawing the printed bubble and calling it
        # the sampling window shows the operator a region recognition never
        # read, while looking entirely convincing.
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()

        loaded_page.sample_overlay_toggle.setChecked(True)
        overlay = loaded_page.preview._overlay
        assert overlay.show_sample_windows is True
        for bubble in overlay._bubbles:
            assert bubble.sample_half_width > 0.0
            assert bubble.sample_half_width < bubble.width / 2.0
            assert bubble.sample_half_height < bubble.height / 2.0

    def test_the_centres_overlay_can_be_switched_on_independently(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()
        overlay = loaded_page.preview._overlay
        assert overlay.show_centers is False
        loaded_page.center_overlay_toggle.setChecked(True)
        assert overlay.show_centers is True

    def test_the_overlay_and_the_image_stay_aligned_at_every_zoom_level(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        # A double transform - overlay coordinates converted once by the
        # engine and again by the viewer - produces a display that looks
        # nearly right and drifts with zoom. This pins the two frames
        # together: the image pixel under a bubble's canonical centre must be
        # the same point, in viewport coordinates, at any magnification.
        from PySide6.QtCore import QPointF

        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()

        entry = loaded_page.state.entries[0]
        preview = loaded_page.preview
        background = preview._background
        assert background is not None
        scale = entry.result.preview_scale
        bubble = entry.result.bubbles[0]

        # The pixmap pixel that corresponds to this canonical coordinate.
        pixmap_point = QPointF(bubble.x * scale, bubble.y * scale)
        in_scene = background.mapToScene(pixmap_point)
        assert in_scene.x() == pytest.approx(bubble.x, abs=0.01)
        assert in_scene.y() == pytest.approx(bubble.y, abs=0.01)

        for zoom in (0.25, 1.0, 4.0):
            preview._apply_zoom(zoom)
            from_overlay = preview.mapFromScene(QPointF(bubble.x, bubble.y))
            from_image = preview.mapFromScene(background.mapToScene(pixmap_point))
            assert from_overlay == from_image, f"overlay drifted from the image at {zoom}x"

    def test_the_original_scan_view_shows_no_overlay(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        # Overlay coordinates are canonical-page pixels; the original scan is
        # not in that frame. Drawing them over it would be wrong everywhere
        # and plausible-looking.
        from omr_scanner.gui.calibration.page import VIEW_MODE_ORIGINAL

        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()

        assert loaded_page.preview._overlay._bubbles
        loaded_page.view_mode_combo.setCurrentText(VIEW_MODE_ORIGINAL)
        assert loaded_page.overlay_is_drawable is False
        assert loaded_page.preview._overlay._bubbles == ()
        assert loaded_page.preview._overlay.show_bubbles is False

        # Toggling a layer while in this view must not put it back.
        loaded_page.center_overlay_toggle.setChecked(True)
        assert loaded_page.preview._overlay._bubbles == ()

    def test_clicking_in_the_original_view_does_not_report_a_bubble(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        from omr_scanner.gui.calibration.page import VIEW_MODE_ORIGINAL

        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()
        bubble = loaded_page.state.entries[0].result.bubbles[0]

        loaded_page.view_mode_combo.setCurrentText(VIEW_MODE_ORIGINAL)
        loaded_page._on_preview_clicked(bubble.x, bubble.y)
        assert "registered page" in loaded_page.inspector_label.text().lower()


# ----------------------------------------------------------------------
# E. Click to inspect
# ----------------------------------------------------------------------
class TestEClickToInspect:
    def test_clicking_a_selected_bubble_shows_its_raw_score(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()

        entry = loaded_page.state.entries[0]
        selected = next(
            b for b in entry.result.bubbles if b.selected and b.zone_id == "questions_0"
        )
        loaded_page._on_preview_clicked(selected.x, selected.y)

        text = loaded_page.inspector_label.text()
        assert f"value {selected.label}" in text
        assert "Fill score" in text
        assert "FILLED" in text

    def test_the_question_number_formatter_agrees_with_the_engines_own_numbering(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        # The safety net promised in `_question_number`'s docstring: the
        # presentation-only formatter must never silently drift from what
        # the engine itself decided the question number is.
        from omr_scanner.gui.calibration.page import _question_number

        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()
        entry = loaded_page.state.entries[0]
        template = loaded_page.state.template

        checked = 0
        for bubble in entry.result.bubbles:
            zone = next(z for z in template.zones if z.id == bubble.zone_id)
            number = _question_number(zone.field, bubble)
            if number is None:
                continue
            answer = entry.result.answer(number)
            assert answer is not None, f"formatter invented question {number}"
            checked += 1
        assert checked > 0

    def test_the_inspector_reports_the_sampled_window_and_the_printed_bubble(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()
        bubble = loaded_page.state.entries[0].result.bubbles[0]
        loaded_page._on_preview_clicked(bubble.x, bubble.y)

        text = loaded_page.inspector_label.text()
        assert "Sampling window" in text
        assert "Printed bubble" in text
        # The two must be reported as the different sizes they are.
        assert f"{bubble.sample_half_width * 2.0:.1f}" in text
        assert f"{bubble.width:.1f}" in text

    def test_clicking_empty_space_does_not_crash_or_change_the_inspector(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()
        before = loaded_page.inspector_label.text()
        loaded_page._on_preview_clicked(-500.0, -500.0)
        assert loaded_page.inspector_label.text() == before


# ----------------------------------------------------------------------
# F. Field-level filtering
# ----------------------------------------------------------------------
class TestFFieldFiltering:
    def test_the_identifier_filter_shows_only_identifier_bubbles(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()

        entry = loaded_page.state.entries[0]
        loaded_page.field_filter_combo.setCurrentText("Student ID")
        overlay = loaded_page.preview._overlay
        assert overlay._bubbles
        assert all(b.zone_id == entry.result.identifier_zone_id for b in overlay._bubbles)

    def test_the_questions_filter_excludes_identifier_bubbles(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()

        entry = loaded_page.state.entries[0]
        loaded_page.field_filter_combo.setCurrentText("Questions")
        overlay = loaded_page.preview._overlay
        assert overlay._bubbles
        assert all(b.zone_id != entry.result.identifier_zone_id for b in overlay._bubbles)


# ----------------------------------------------------------------------
# G. Threshold controls
# ----------------------------------------------------------------------
class TestGThresholdControls:
    def test_the_slider_and_spin_box_stay_in_sync(self, loaded_page: CalibrationPage):
        loaded_page.fill_spin.setValue(0.42)
        assert loaded_page.fill_slider.value() == 420
        loaded_page.fill_slider.setValue(300)
        assert loaded_page.fill_spin.value() == pytest.approx(0.3)

    def test_a_threshold_change_reclassifies_immediately_without_a_worker_run(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        marks = {
            "roll_number": dict(enumerate("120317")),
            "set_code": {0: "A"},
            "questions_0": {0: "B", 1: ("A", 0.35)},
        }
        loaded_page.add_scan_paths([write_sheet(marks)])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()

        entry = loaded_page.state.entries[0]
        before = entry.result.answer(2)
        assert before.status == "uncertain"

        # No worker is started for a threshold change - it must be instant.
        loaded_page.fill_spin.setValue(before.top_fill - 0.05)
        after = entry.result.answer(2)
        assert after.status == "resolved"
        assert after.value == "A"

    def test_the_page_says_whether_the_working_values_are_saved_or_not(
        self, loaded_page: CalibrationPage
    ):
        # Spec sections 26 and 62: no ambiguous hidden state. The operator
        # must never have to guess which of the two values is in force.
        assert "saved values" in loaded_page.threshold_state_label.text().lower()
        loaded_page.fill_spin.setValue(0.42)
        assert "modified" in loaded_page.threshold_state_label.text().lower()
        loaded_page.reset_to_template()
        assert "saved values" in loaded_page.threshold_state_label.text().lower()

    def test_reset_to_defaults_restores_the_application_defaults(
        self, loaded_page: CalibrationPage
    ):
        from omr_scanner.domain.template import RecognitionSettings

        loaded_page.fill_spin.setValue(0.9)
        loaded_page.reset_to_defaults()
        assert loaded_page.state.working_settings == RecognitionSettings()
        assert loaded_page.fill_spin.value() == pytest.approx(
            RecognitionSettings().fill_ratio_threshold
        )

    def test_reset_to_template_restores_the_saved_values_not_defaults(
        self, page: CalibrationPage, template: OmrTemplate, template_path: Path
    ):
        custom = template.model_copy(
            update={
                "recognition": template.recognition.model_copy(
                    update={"fill_ratio_threshold": 0.71}
                )
            }
        )
        save_template(custom, template_path)
        page.load_template_from(template_path)
        page.fill_spin.setValue(0.2)

        page.reset_to_template()
        assert page.state.working_settings.fill_ratio_threshold == pytest.approx(0.71)


# ----------------------------------------------------------------------
# H. Saving calibration
# ----------------------------------------------------------------------
class TestHSavingCalibration:
    def test_saving_writes_the_working_thresholds_and_a_calibration_record(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, loaded_page: CalibrationPage,
        template_path: Path, write_sheet,
    ):
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()

        loaded_page.fill_spin.setValue(0.6)
        confirm_save(monkeypatch)
        loaded_page.save_to_template()

        reloaded = load_template(template_path)
        assert reloaded.recognition.fill_ratio_threshold == pytest.approx(0.6)
        assert reloaded.calibration.is_recorded
        assert reloaded.is_calibration_current()

    def test_a_stale_calibration_is_reported_as_out_of_date(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, loaded_page: CalibrationPage, write_sheet,
    ):
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()

        confirm_save(monkeypatch)
        loaded_page.save_to_template()
        assert "current" in loaded_page.calibration_state_label.text().lower()

        loaded_page.fill_spin.setValue(0.61)
        loaded_page._refresh_template_label()
        # The saved template on disk has not changed; re-loading it and
        # comparing against a *different* working value shows staleness.
        working_template = loaded_page._working_template()
        assert working_template.is_calibration_current() is False


# ----------------------------------------------------------------------
# H2. Field-level diagnostics
# ----------------------------------------------------------------------
class TestFieldDiagnostics:
    def test_the_student_id_is_broken_down_position_by_position(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        # Spec section 20: not just the final ID string with no explanation.
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()

        text = loaded_page.field_diagnostics_label.text()
        identifier = loaded_page.state.entries[0].result.identifier
        assert identifier is not None
        assert identifier.label in text
        # One line per printed position, each carrying its own evidence.
        for character in identifier.characters:
            assert f"{character.position + 1}:" in text
        assert text.count("fill ") >= len(identifier.characters)

    def test_the_set_code_is_reported_by_position_not_assumed_single(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        # Spec section 21: a set code may be multi-position and its symbols
        # multi-character. The panel iterates whatever the template declares.
        loaded_page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()

        result = loaded_page.state.entries[0].result
        set_code = result.set_code
        assert set_code is not None
        text = loaded_page.field_diagnostics_label.text()
        assert set_code.label in text
        for character in set_code.characters:
            assert character.value in text or "(blank)" in text

    def test_flagged_questions_are_listed_with_their_evidence(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        # Spec section 22: which bubble, raw score, ambiguity - for the
        # questions that actually need a look.
        marks = {
            "roll_number": dict(enumerate("120317")),
            "set_code": {0: "A"},
            "questions_0": {0: "B", 1: ("A", 0.35)},
        }
        loaded_page.add_scan_paths([write_sheet(marks)])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            loaded_page.run_selected()

        text = loaded_page.field_diagnostics_label.text()
        assert "Questions" in text
        assert "Q2:" in text
        assert "uncertain" in text


# ----------------------------------------------------------------------
# I. Multi-scan sample summary
# ----------------------------------------------------------------------
class TestIMultiScanSample:
    def test_the_sample_summary_aggregates_every_tested_scan(
        self, qtbot, loaded_page: CalibrationPage, write_sheet
    ):
        loaded_page.add_scan_paths([write_sheet(), write_sheet(), write_sheet()])
        with qtbot.waitSignal(loaded_page.run_finished, timeout=WORKER_TIMEOUT_MS):
            assert loaded_page.run_all() is True

        text = loaded_page.sample_summary_label.text()
        assert "3 scan(s)" in text
        assert "Registration successful: 3 / 3" in text


# ----------------------------------------------------------------------
# J. Miscalibration is flagged, never a false pass
# ----------------------------------------------------------------------
class TestJMiscalibrationIsFlagged:
    def test_a_template_with_displaced_markers_is_reported_as_failed(
        self, qtbot, page: CalibrationPage, template: OmrTemplate, tmp_path: Path, write_sheet
    ):
        mismatched = template.model_copy(
            update={
                "registration_markers": tuple(
                    marker.model_copy(
                        update={
                            "center": NormalizedPoint(
                                x=min(marker.center.x + 0.3, 1.0),
                                y=min(marker.center.y + 0.3, 1.0),
                            )
                        }
                    )
                    for marker in template.registration_markers
                )
            }
        )
        bad_path = tmp_path / "mismatched.omrt"
        save_template(mismatched, bad_path)
        assert page.load_template_from(bad_path) is True

        page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(page.run_finished, timeout=WORKER_TIMEOUT_MS):
            assert page.run_selected() is True

        entry = page.state.entries[0]
        assert entry.report.status is CalibrationStatus.FAILED
        assert entry.result.registration is RegistrationStatus.FAILED
        assert entry.result.fields == ()
        assert entry.result.answers == ()
        assert page.calibration_status_label.text() == CalibrationStatus.FAILED.label
        assert page.sample_table.item(0, 5).text() == CalibrationStatus.FAILED.label

    def test_a_displaced_template_that_still_registers_is_not_shown_as_passed(
        self, qtbot, page: CalibrationPage, template: OmrTemplate, tmp_path: Path, write_sheet
    ):
        # The dangerous sibling of the marker case above: registration
        # succeeds, nothing falls off the page, and every group reads a
        # confident blank because the sampling windows sit on bare paper.
        # The operator must not be shown a pass.
        from omr_scanner.domain.geometry import NormalizedRect

        zones = []
        for zone in template.zones:
            bounds = zone.bounds
            update: dict = {
                "bounds": NormalizedRect(
                    x=min(bounds.x + 0.02, 1.0 - bounds.width),
                    y=min(bounds.y + 0.02, 1.0 - bounds.height),
                    width=bounds.width,
                    height=bounds.height,
                )
            }
            if zone.grid is not None:
                origin = zone.grid.origin
                update["grid"] = zone.grid.model_copy(
                    update={
                        "origin": NormalizedPoint(
                            x=min(origin.x + 0.02, 1.0), y=min(origin.y + 0.02, 1.0)
                        )
                    }
                )
            zones.append(zone.model_copy(update=update))
        displaced = template.model_copy(update={"zones": tuple(zones)})

        bad_path = tmp_path / "displaced.omrt"
        save_template(displaced, bad_path)
        assert page.load_template_from(bad_path) is True
        page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(page.run_finished, timeout=WORKER_TIMEOUT_MS):
            assert page.run_selected() is True

        entry = page.state.entries[0]
        assert entry.result.registration is not RegistrationStatus.FAILED
        assert entry.report.status in (
            CalibrationStatus.NEEDS_REVIEW, CalibrationStatus.FAILED
        )
        assert page.calibration_status_label.text() == entry.report.status.label
        # And the summary tells the operator what to actually go and look at.
        summary = page.calibration_summary_panel.text()
        assert "Marks detected in 0 of" in summary

    def test_a_failed_scan_cannot_be_saved_as_a_passing_calibration(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, page: CalibrationPage,
        template: OmrTemplate, tmp_path: Path, write_sheet,
    ):
        mismatched = template.model_copy(
            update={
                "registration_markers": tuple(
                    marker.model_copy(
                        update={
                            "center": NormalizedPoint(
                                x=min(marker.center.x + 0.3, 1.0),
                                y=min(marker.center.y + 0.3, 1.0),
                            )
                        }
                    )
                    for marker in template.registration_markers
                )
            }
        )
        bad_path = tmp_path / "mismatched.omrt"
        save_template(mismatched, bad_path)
        page.load_template_from(bad_path)
        page.add_scan_paths([write_sheet()])
        with qtbot.waitSignal(page.run_finished, timeout=WORKER_TIMEOUT_MS):
            page.run_selected()

        seen: list[str] = []
        confirm_save(monkeypatch, seen)
        page.save_to_template()
        # The confirmation dialog itself states the true (failed) status -
        # nothing here silently launders it into a pass.
        assert any("failed" in text.lower() for text in seen)

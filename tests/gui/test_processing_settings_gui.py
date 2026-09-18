"""GUI validation of the Processing settings and multicore batch processing.

Scope:
    Everything the Phase 3 multicore brief asks the GUI to prove, driven the way
    a user drives it and asserted against program state:

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     Settings opens and shows the Processing section.
    B     Automatic, Single core and Custom can each be selected.
    C     The worker selector enables only for Custom, and refuses
          values outside ``1 .. detected CPU threads``.
    D     The choice is persisted, and is still there when the
          application is started again.
    E     The Scan page uses the chosen mode, stays responsive,
          advances its progress indicator and reports completion.
    ===== ==========================================================

Why the dialog is never ``exec()``-ed:
    It is modal; offscreen there is nothing to click and ``exec()`` would block
    forever. The dialog is constructed, its widgets are driven, and its result
    is read - the same policy the rest of the GUI suite follows for dialogs.

Why ``cpu_count`` is passed explicitly:
    The offered worker range depends on the machine. Fixing it makes these tests
    say the same thing on a two-core CI runner and a thirty-two-core
    workstation, which is the only way "the range is 1..detected" can be
    asserted at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from PySide6.QtWidgets import QComboBox, QGroupBox, QLabel, QSpinBox
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.config import AppConfig, load_app_config, save_app_config
from omr_scanner.config.processing import (
    AUTOMATIC_WORKER_LIMIT,
    ProcessingMode,
    ProcessingSettings,
)
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.gui.settings_dialog import SettingsDialog
from omr_scanner.services import save_template

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable
    from pathlib import Path

    from omr_scanner.domain.template import OmrTemplate

pytestmark = pytest.mark.gui

BATCH_TIMEOUT_MS = 120_000
"""A ceiling, not a delay: every wait returns as soon as the signal arrives.

Higher than the single-core suite's because a multicore run has to start worker
processes, and a cold ``spawn`` on Windows imports NumPy and OpenCV in each."""

TEST_CPU_COUNT = 16
"""The machine these tests pretend to run on."""


def sheet_marks(roll: str, *, set_code: str = "A", answer: str = "B") -> dict:
    return {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: set_code},
        "questions_0": dict.fromkeys(range(10), answer),
        "questions_1": dict.fromkeys(range(10), answer),
    }


@pytest.fixture
def template() -> OmrTemplate:
    return build_answer_sheet_template()


@pytest.fixture
def template_path(tmp_path: Path, template: OmrTemplate) -> Path:
    path = tmp_path / "templates" / "synthetic.omrt"
    path.parent.mkdir(parents=True, exist_ok=True)
    return save_template(template, path)


@pytest.fixture
def write_sheet(tmp_path: Path, template: OmrTemplate) -> Callable[..., Path]:
    import cv2

    scans = tmp_path / "scans"
    scans.mkdir(parents=True, exist_ok=True)

    def write(name: str, roll: str = "120317", **kwargs: str) -> Path:
        path = scans / name
        cv2.imwrite(str(path), render_marked_sheet(template, sheet_marks(roll, **kwargs)))
        return path

    return write


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "omrflow.config.json"


@pytest.fixture
def window(qtbot, config_path: Path) -> MainWindow:
    main_window = MainWindow(config=AppConfig(), config_path=config_path)
    qtbot.addWidget(main_window)
    return main_window


@pytest.fixture
def dialog(qtbot, window: MainWindow) -> SettingsDialog:
    settings = SettingsDialog(window.config, window, cpu_count=TEST_CPU_COUNT)
    qtbot.addWidget(settings)
    return settings


def scan_page_of(window: MainWindow) -> ScanPage:
    """The window's Scan page, found the way the navigator finds it."""
    row = next(i for i, spec in enumerate(WORKFLOW_PAGES) if spec.key == "scan")
    window.navigation.setCurrentRow(row)
    page = window.stack.currentWidget()
    assert isinstance(page, ScanPage)
    return page


# ----------------------------------------------------------------------
# Test A - the Settings dialog opens and shows Processing
# ----------------------------------------------------------------------
class TestASettingsOpens:
    def test_the_file_menu_offers_settings(self, window: MainWindow):
        assert window.settings_action.isEnabled()
        assert "Settings" in window.settings_action.text()

    def test_the_dialog_has_a_processing_section(self, dialog: SettingsDialog):
        group = dialog.findChild(QGroupBox, "processingSettingsGroup")
        assert group is not None
        assert group.title() == "Processing"

    def test_the_processing_controls_are_all_present_and_named(
        self, dialog: SettingsDialog
    ):
        for name, widget_type in (
            ("processingModeCombo", QComboBox),
            ("workerCountSpinBox", QSpinBox),
            ("detectedCpuLabel", QLabel),
            ("activeWorkersLabel", QLabel),
        ):
            assert dialog.findChild(widget_type, name) is not None, name

    def test_the_detected_cpu_count_is_shown(self, dialog: SettingsDialog):
        assert dialog.detected_label.text() == str(TEST_CPU_COUNT)

    def test_the_dialog_opens_on_the_stored_setting(self, qtbot, window: MainWindow):
        window.apply_processing_settings(
            ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=6)
        )
        reopened = SettingsDialog(window.config, window, cpu_count=TEST_CPU_COUNT)
        qtbot.addWidget(reopened)

        assert reopened.selected_mode is ProcessingMode.CUSTOM
        assert reopened.worker_spin.value() == 6


# ----------------------------------------------------------------------
# Test B - each mode can be selected
# ----------------------------------------------------------------------
class TestBModeSelection:
    def test_automatic_is_the_mode_a_new_installation_starts_in(
        self, dialog: SettingsDialog
    ):
        assert dialog.selected_mode is ProcessingMode.AUTOMATIC

    @pytest.mark.parametrize(
        ("label", "mode"),
        [
            ("Automatic", ProcessingMode.AUTOMATIC),
            ("Single core", ProcessingMode.SINGLE_CORE),
            ("Custom", ProcessingMode.CUSTOM),
        ],
    )
    def test_every_mode_can_be_chosen_by_its_visible_name(
        self, dialog: SettingsDialog, label: str, mode: ProcessingMode
    ):
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText(label))
        assert dialog.selected_mode is mode
        assert dialog.processing_settings().mode is mode

    def test_automatic_reports_the_workers_it_would_actually_use(
        self, dialog: SettingsDialog
    ):
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText("Automatic"))
        expected = min(TEST_CPU_COUNT - 1, AUTOMATIC_WORKER_LIMIT)
        assert dialog.active_label.text() == str(expected)

    def test_single_core_reports_exactly_one_worker(self, dialog: SettingsDialog):
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText("Single core"))
        assert dialog.active_label.text() == "1"

    def test_custom_reports_the_number_the_user_typed(self, dialog: SettingsDialog):
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText("Custom"))
        dialog.worker_spin.setValue(12)
        assert dialog.active_label.text() == "12"

    def test_each_mode_explains_itself(self, dialog: SettingsDialog):
        for label in ("Automatic", "Single core", "Custom"):
            dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText(label))
            assert dialog.explanation_label.text()


# ----------------------------------------------------------------------
# Test C - the worker selector
# ----------------------------------------------------------------------
class TestCWorkerSelector:
    def test_the_selector_is_disabled_unless_the_mode_is_custom(
        self, dialog: SettingsDialog
    ):
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText("Automatic"))
        assert dialog.worker_spin.isEnabled() is False

        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText("Single core"))
        assert dialog.worker_spin.isEnabled() is False

        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText("Custom"))
        assert dialog.worker_spin.isEnabled() is True

    def test_the_offered_range_is_one_to_the_detected_cpu_count(
        self, dialog: SettingsDialog
    ):
        assert dialog.worker_spin.minimum() == 1
        assert dialog.worker_spin.maximum() == TEST_CPU_COUNT

    def test_zero_and_negative_worker_counts_cannot_be_entered(
        self, dialog: SettingsDialog
    ):
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText("Custom"))
        dialog.worker_spin.setValue(0)
        assert dialog.worker_spin.value() == 1

        dialog.worker_spin.setValue(-4)
        assert dialog.worker_spin.value() == 1
        assert dialog.processing_settings().worker_count == 1

    def test_more_workers_than_the_machine_has_cannot_be_entered(
        self, dialog: SettingsDialog
    ):
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText("Custom"))
        dialog.worker_spin.setValue(TEST_CPU_COUNT * 4)
        assert dialog.worker_spin.value() == TEST_CPU_COUNT

    def test_a_stored_count_from_a_larger_machine_is_shown_clamped(
        self, qtbot, window: MainWindow
    ):
        stored = AppConfig().with_processing(
            ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=64)
        )
        small = SettingsDialog(stored, window, cpu_count=4)
        qtbot.addWidget(small)
        assert small.worker_spin.value() == 4


# ----------------------------------------------------------------------
# Test C2 - the diagnostics controls
# ----------------------------------------------------------------------
class TestC2Diagnostics:
    def test_the_dialog_has_a_diagnostics_section(self, dialog: SettingsDialog):
        group = dialog.findChild(QGroupBox, "diagnosticsSettingsGroup")
        assert group is not None
        assert group.title() == "Diagnostics"

    def test_diagnostics_are_off_on_a_new_installation(self, dialog: SettingsDialog):
        # Several full-page images per sheet is not something a user should
        # discover they have been writing all along.
        assert dialog.diagnostics_checkbox.isChecked() is False
        assert dialog.processing_settings().diagnostics_enabled is False

    def test_the_folder_button_is_enabled_only_once_diagnostics_are_wanted(
        self, dialog: SettingsDialog
    ):
        assert dialog.diagnostics_folder_button.isEnabled() is False
        dialog.diagnostics_checkbox.setChecked(True)
        assert dialog.diagnostics_folder_button.isEnabled() is True

    def test_switching_them_on_without_a_folder_says_so(self, dialog: SettingsDialog):
        dialog.diagnostics_checkbox.setChecked(True)
        assert "Choose a folder" in dialog.diagnostics_folder_label.text()
        # And nothing is written, because there is nowhere to write it.
        assert dialog.processing_settings().writes_diagnostics is False

    def test_choosing_a_folder_shows_it_and_arms_the_setting(
        self, dialog: SettingsDialog, tmp_path: Path
    ):
        dialog.diagnostics_checkbox.setChecked(True)
        dialog.set_diagnostics_directory(tmp_path / "diag")
        assert str(tmp_path / "diag") in dialog.diagnostics_folder_label.text()
        assert dialog.processing_settings().writes_diagnostics is True


# ----------------------------------------------------------------------
# Test D - persistence
# ----------------------------------------------------------------------
class TestDPersistence:
    def test_accepting_the_dialog_saves_the_choice_to_the_configuration_file(
        self, window: MainWindow, dialog: SettingsDialog, config_path: Path
    ):
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText("Custom"))
        dialog.worker_spin.setValue(3)
        window.apply_processing_settings(dialog.processing_settings())

        assert config_path.is_file()
        stored = load_app_config(config_path, strict=True)
        assert stored.processing.mode is ProcessingMode.CUSTOM
        assert stored.processing.worker_count == 3

    def test_the_setting_is_still_there_when_the_application_starts_again(
        self, qtbot, window: MainWindow, dialog: SettingsDialog, config_path: Path
    ):
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText("Single core"))
        window.apply_processing_settings(dialog.processing_settings())
        window.close()

        restarted = MainWindow(
            config=load_app_config(config_path, strict=True), config_path=config_path
        )
        qtbot.addWidget(restarted)
        assert restarted.config.processing.mode is ProcessingMode.SINGLE_CORE
        assert scan_page_of(restarted).state.processing.mode is ProcessingMode.SINGLE_CORE

    def test_cancelling_the_dialog_changes_nothing(
        self, window: MainWindow, dialog: SettingsDialog, config_path: Path
    ):
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText("Custom"))
        dialog.worker_spin.setValue(9)
        dialog.reject()

        assert window.config.processing.mode is ProcessingMode.AUTOMATIC
        assert not config_path.exists()

    def test_the_diagnostics_choice_is_persisted_too(
        self, window: MainWindow, dialog: SettingsDialog, tmp_path: Path, config_path: Path
    ):
        folder = tmp_path / "diagnostics"
        dialog.diagnostics_checkbox.setChecked(True)
        dialog.set_diagnostics_directory(folder)
        window.apply_processing_settings(dialog.processing_settings())

        stored = load_app_config(config_path, strict=True)
        assert stored.processing.diagnostics_enabled is True
        assert stored.processing.diagnostics_dir == folder
        assert stored.processing.writes_diagnostics is True

    def test_changing_processing_does_not_disturb_the_recent_project_list(
        self, window: MainWindow, dialog: SettingsDialog, tmp_path: Path
    ):
        project = tmp_path / "a-project"
        project.mkdir()
        window._remember_recent_project(project)
        before = window.config.recent_projects

        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText("Single core"))
        window.apply_processing_settings(dialog.processing_settings())

        assert window.config.recent_projects == before


# ----------------------------------------------------------------------
# Test E - the Scan page honours the setting
# ----------------------------------------------------------------------
class TestEScanPageUsesTheSetting:
    @pytest.fixture
    def loaded(self, window: MainWindow, template_path: Path, write_sheet) -> ScanPage:
        page = scan_page_of(window)
        assert page.load_template_from(template_path) is True
        page.add_scan_paths(
            [write_sheet(f"scan{index:03d}.png", roll=f"12031{index}") for index in range(4)]
        )
        return page

    def test_the_page_starts_from_the_applications_setting(self, window: MainWindow):
        window.apply_config(
            AppConfig().with_processing(
                ProcessingSettings(mode=ProcessingMode.SINGLE_CORE)
            )
        )
        assert scan_page_of(window).state.processing.mode is ProcessingMode.SINGLE_CORE

    def test_single_core_plans_one_worker_however_many_scans_there_are(
        self, window: MainWindow, loaded: ScanPage
    ):
        window.apply_config(
            AppConfig().with_processing(
                ProcessingSettings(mode=ProcessingMode.SINGLE_CORE)
            )
        )
        assert loaded.planned_worker_count() == 1

    def test_custom_plans_the_chosen_number_but_never_more_than_the_scans(
        self, window: MainWindow, loaded: ScanPage
    ):
        window.apply_config(
            AppConfig().with_processing(
                ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=2)
            )
        )
        assert loaded.planned_worker_count() == 2

        window.apply_config(
            AppConfig().with_processing(
                ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=32)
            )
        )
        # Four scans: never more workers than there is work.
        assert loaded.planned_worker_count() == len(loaded.state.entries)

    def test_the_page_says_how_many_scans_and_workers_the_run_will_use(
        self, window: MainWindow, loaded: ScanPage
    ):
        window.apply_config(
            AppConfig().with_processing(
                ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=2)
            )
        )
        text = loaded.workers_label.text()
        assert "4 scans" in text
        assert "2 parallel workers" in text

    @pytest.mark.parametrize(
        "processing",
        [
            ProcessingSettings(mode=ProcessingMode.SINGLE_CORE),
            ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=2),
        ],
        ids=["single-core", "two-workers"],
    )
    def test_a_batch_runs_to_completion_in_the_selected_mode(
        self, qtbot, window: MainWindow, loaded: ScanPage, processing: ProcessingSettings
    ):
        window.apply_config(AppConfig().with_processing(processing))

        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            assert loaded.process_all() is True
        report = blocker.args[0]

        assert report.total == 4
        assert report.complete_count == 4
        assert report.worker_count == processing.resolve_worker_count(4)
        assert [item.source_path for item in report.processed] == [
            entry.path for entry in loaded.state.entries
        ]

    def test_two_workers_and_one_worker_produce_the_same_scan_list(
        self, qtbot, window: MainWindow, loaded: ScanPage
    ):
        def run() -> list[tuple[str, str, str]]:
            with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS):
                loaded.reprocess_all()
            return [
                (entry.path.name, entry.identifier, entry.outcome)
                for entry in loaded.state.entries
            ]

        window.apply_config(
            AppConfig().with_processing(
                ProcessingSettings(mode=ProcessingMode.SINGLE_CORE)
            )
        )
        single = run()

        window.apply_config(
            AppConfig().with_processing(
                ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=4)
            )
        )
        assert run() == single

    def test_the_window_stays_responsive_while_a_multicore_batch_runs(
        self, qtbot, window: MainWindow, loaded: ScanPage
    ):
        window.apply_config(
            AppConfig().with_processing(
                ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=4)
            )
        )
        ticks: list[int] = []
        loaded.progress_bar.valueChanged.connect(lambda value: ticks.append(value))

        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert loaded.process_all() is True
            # The event loop kept running - Qt delivered the worker's queued
            # progress signals while the sheets were being read elsewhere.
            qtbot.wait(50)

        assert ticks, "no progress was delivered to the GUI thread"
        assert ticks == sorted(ticks), ticks
        assert ticks[-1] == 4

    def test_the_progress_label_counts_completions_and_names_the_worker_count(
        self, qtbot, window: MainWindow, loaded: ScanPage
    ):
        window.apply_config(
            AppConfig().with_processing(
                ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=2)
            )
        )
        seen: list[str] = []
        loaded.progress_bar.valueChanged.connect(
            lambda _value: seen.append(loaded.progress_label.text())
        )

        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert loaded.process_all() is True

        assert any("Completed 1 / 4" in text for text in seen), seen
        assert any("2 workers" in text for text in seen), seen

    def test_completion_is_reported_with_the_worker_count_and_the_time_taken(
        self, qtbot, window: MainWindow, loaded: ScanPage
    ):
        window.apply_config(
            AppConfig().with_processing(
                ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=2)
            )
        )
        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert loaded.process_all() is True

        summary = loaded.progress_label.text()
        assert "4 processed" in summary
        assert "2 worker(s)" in summary
        assert loaded.progress_bar.value() == loaded.progress_bar.maximum()

    def test_the_controls_come_back_after_a_multicore_run(
        self, qtbot, window: MainWindow, loaded: ScanPage
    ):
        window.apply_config(
            AppConfig().with_processing(
                ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=2)
            )
        )
        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert loaded.process_all() is True

        assert loaded.process_all_button.isEnabled()
        assert loaded.export_csv_button.isEnabled()
        assert loaded.cancel_button.isEnabled() is False

    def test_a_batch_writes_diagnostics_when_the_setting_asks_for_them(
        self, qtbot, window: MainWindow, loaded: ScanPage, tmp_path: Path
    ):
        folder = tmp_path / "diagnostics"
        window.apply_processing_settings(
            ProcessingSettings(
                mode=ProcessingMode.SINGLE_CORE,
                diagnostics_enabled=True,
                diagnostics_dir=folder,
            )
        )
        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert loaded.process_all() is True

        assert folder.is_dir()
        assert sorted(item.name for item in folder.iterdir()) == [
            f"scan{index:03d}" for index in range(4)
        ]
        assert (folder / "scan000" / "02_overlay.png").is_file()

    def test_a_batch_writes_no_diagnostics_by_default(
        self, qtbot, loaded: ScanPage, tmp_path: Path
    ):
        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert loaded.process_all() is True
        assert not (tmp_path / "diagnostics").exists()

    def test_no_worker_process_is_left_running_when_the_page_closes(
        self, qtbot, window: MainWindow, loaded: ScanPage
    ):
        import multiprocessing

        window.apply_config(
            AppConfig().with_processing(
                ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=4)
            )
        )
        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert loaded.process_all() is True

        window.close()
        assert not multiprocessing.active_children()


def test_saving_and_reloading_the_configuration_preserves_the_mode(tmp_path: Path):
    """The persistence guarantee, without a window in the way."""
    path = tmp_path / "config.json"
    save_app_config(
        AppConfig().with_processing(
            ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=2)
        ),
        path,
    )
    assert load_app_config(path, strict=True).processing.worker_count == 2

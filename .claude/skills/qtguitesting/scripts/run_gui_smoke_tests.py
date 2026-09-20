"""A fast, non-destructive sanity check on OMRFlow's Qt GUI.

Purpose:
    Answer "is the GUI fundamentally intact?" in a couple of seconds, so a
    developer or an agent can tell a broken import from a failed assertion
    without waiting for, or reading, the full suite.

    Deliberately **not** a second copy of the pytest suite. It checks only the
    things whose failure makes every other test's output meaningless: the
    application initialises, the main window and the Template page construct, the
    sample image decodes and loads, the toolbars and their actions exist, the
    canvas and properties panel exist, and each region dialog can be built.

    Anything behavioural belongs in `tests/`, where it can be asserted properly.

Usage:
    python .claude/skills/qtguitesting/scripts/run_gui_smoke_tests.py
    python .claude/skills/qtguitesting/scripts/run_gui_smoke_tests.py --image other.png

Exit code 0 if every check passed, 1 otherwise. Nothing is written to disk.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import (
    SAMPLE_SHEET,
    SAMPLE_TEMPLATE,
    build_designer,
    build_empty_designer,
    build_scan_page,
    ensure_application,
    question_region_bounds,
)

CheckResult = tuple[bool, str]


def _check_application() -> CheckResult:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    return app is not None, f"QApplication: {type(app).__name__ if app else 'missing'}"


def _check_main_window() -> CheckResult:
    from omr_scanner.gui.main_window import MainWindow

    window = MainWindow()
    pages = window._pages
    return len(pages) > 0, f"MainWindow constructed with {len(pages)} workflow page(s)"


def _check_empty_page() -> CheckResult:
    harness = build_empty_designer()
    page = harness.page
    ok = page._designer_state is None and not page.save_action.isEnabled()
    return ok, "Template page constructs with no document, save disabled"


def _check_sample_loads(image: Path) -> CheckResult:
    harness = build_designer(image)
    size = harness.page.canvas._image_size
    ok = size == (harness.image_width, harness.image_height)
    return ok, f"sample loaded at {size[0]}x{size[1]} px, zoom {harness.page.canvas.zoom:.0%}"


def _check_toolbars(image: Path) -> CheckResult:
    harness = build_designer(image)
    page = harness.page
    row_one = [a for a in page.toolbar.actions() if not a.isSeparator()]
    row_two = [a for a in page.toolbar_view.actions() if not a.isSeparator()]
    ok = len(row_one) >= 4 and len(row_two) >= 4
    return ok, f"toolbar rows carry {len(row_one)} and {len(row_two)} action(s)"


def _check_named_actions(image: Path) -> CheckResult:
    harness = build_designer(image)
    page = harness.page
    required = [
        "new_action", "open_action", "save_action", "save_as_action",
        "undo_action", "redo_action", "detect_action", "confirm_markers_action",
        "detect_orientation_action", "validate_action",
        "add_student_id_action", "add_question_set_action",
        "add_question_block_action", "add_custom_action", "add_ignored_action",
        "fine_tune_action", "clear_overrides_action",
        "zoom_in_action", "zoom_out_action", "fit_action", "actual_size_action",
        "grid_action",
    ]
    on_toolbar = page.toolbar_actions()
    missing = [
        name
        for name in required
        if not hasattr(page, name) or getattr(page, name) not in on_toolbar
    ]
    return not missing, (
        f"{len(required)} named actions present"
        if not missing
        else f"missing from the toolbar: {', '.join(missing)}"
    )


def _check_canvas_and_panels(image: Path) -> CheckResult:
    harness = build_designer(image)
    page = harness.page
    parts = {
        "canvas": page.canvas is not None,
        "properties panel": page.properties is not None,
        "region list": page.region_list is not None,
        "bubble radius box": page.bubble_radius_box is not None,
    }
    missing = [name for name, present in parts.items() if not present]
    return not missing, "canvas, properties panel, region list and radius box exist"


def _check_bubble_radius_reflects_the_document(image: Path) -> CheckResult:
    harness = build_designer(image)
    page = harness.page
    expected = (
        page._designer_state.template.default_bubble_radius * harness.image_width
    )
    shown = page.bubble_radius_box.value()
    ok = abs(shown - expected) < 0.5
    return ok, f"bubble radius shows {shown:.1f} px (document says {expected:.1f} px)"


def _check_dialogs_instantiate(image: Path) -> CheckResult:
    from omr_scanner.gui.template_designer.dialogs import (
        CustomBubbleDialog,
        IgnoredRegionDialog,
        QuestionBlockDialog,
        QuestionSetDialog,
        StudentIdDialog,
    )

    harness = build_designer(image)
    bounds = question_region_bounds()
    built: list[str] = []
    for dialog_cls in (
        StudentIdDialog, QuestionSetDialog, QuestionBlockDialog,
        CustomBubbleDialog, IgnoredRegionDialog,
    ):
        # Constructed, never `exec()`d - a modal has nothing to click offscreen
        # and would block forever (`docs/TESTING.md`).
        dialog_cls(
            bounds=bounds,
            existing_zone_ids=[],
            image_width=harness.image_width,
            image_height=harness.image_height,
        )
        built.append(dialog_cls.__name__)
    return True, f"{len(built)} region dialogs construct"


def _check_orientation_detection(image: Path) -> CheckResult:
    """The detector runs end to end and reports full-image coordinates."""
    from omr_scanner.services import detect_orientation_marker_in_region

    outcome = detect_orientation_marker_in_region(
        image, x=110.0, y=255.0, width=250.0, height=175.0
    )
    if not outcome.found:
        return False, f"orientation mark not found: {outcome.reason}"
    inside = (
        110.0 <= outcome.center_x <= 360.0 and 255.0 <= outcome.center_y <= 430.0
    )
    return inside, (
        f"orientation mark at ({outcome.center_x:.0f}, {outcome.center_y:.0f}) px, "
        f"score {outcome.score:.2f}"
    )


def _check_page_header_is_compact(image: Path) -> CheckResult:
    harness = build_designer(image)
    ok = harness.page.summary_widget is None
    return ok, "Template header spends no row on the summary sentence"


def _check_scan_page_constructs() -> CheckResult:
    """The Scan page builds, and nothing that needs a template is enabled yet."""
    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
    from omr_scanner.gui.scan.page import ScanPage

    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    page = ScanPage(spec)
    premature = [
        name
        for name, button in (
            ("Add Scan(s)", page.add_scans_button),
            ("Add Folder", page.add_folder_button),
            ("Process All", page.process_all_button),
            ("Export CSV", page.export_csv_button),
        )
        if button.isEnabled()
    ]
    return not premature, (
        "Scan page constructs with everything template-dependent disabled"
        if not premature
        else f"enabled before a template was loaded: {', '.join(premature)}"
    )


def _check_scan_object_names() -> CheckResult:
    """Every stable selector the scenario reference lists is present."""
    from PySide6.QtWidgets import QCheckBox, QProgressBar, QPushButton, QTableView, QTableWidget

    from omr_scanner.gui.scan.preview import ScanPreviewView

    harness = build_scan_page()
    required = [
        ("loadTemplateButton", QPushButton),
        ("addScansButton", QPushButton),
        ("addFolderButton", QPushButton),
        ("clearScansButton", QPushButton),
        ("processAllButton", QPushButton),
        ("processSelectedButton", QPushButton),
        ("reprocessButton", QPushButton),
        ("cancelButton", QPushButton),
        ("outputFolderButton", QPushButton),
        ("exportCsvButton", QPushButton),
        ("renameScansCheckBox", QCheckBox),
        ("scanTable", QTableView),
        ("resultFieldsTable", QTableWidget),
        ("resultAnswersTable", QTableWidget),
        ("scanPreview", ScanPreviewView),
        ("progressBar", QProgressBar),
    ]
    missing = [
        name for name, widget_type in required if harness.page.findChild(widget_type, name) is None
    ]
    harness.shutdown()
    return not missing, (
        f"{len(required)} stable objectNames present"
        if not missing
        else f"missing objectName(s): {', '.join(missing)}"
    )


def _check_scan_template_and_import(image: Path) -> CheckResult:
    """The real template loads and the real sample reaches the scan list."""
    harness = build_scan_page([image])
    page = harness.page
    ok = (
        page.state.template is not None
        and page.scan_table.model().rowCount() == 1
        and page.process_all_button.isEnabled()
    )
    harness.shutdown()
    return ok, (
        f"template '{page.state.template.name}' loaded, "
        f"{page.scan_table.model().rowCount()} scan(s) listed, Process All enabled"
    )


def _check_scan_recognises_the_real_sample(image: Path) -> CheckResult:
    """The whole pipeline, end to end, on an actual scan of an actual sheet."""
    harness = build_scan_page([image])
    report = harness.run_batch()
    result = harness.page.state.entries[0].processed.result
    ok = (
        report.total == 1
        and result.registration.value != "failed"
        and bool(result.identifier_value)
        and len(result.answers) > 0
    )
    harness.shutdown()
    return ok, (
        f"roll '{result.identifier_value}', set '{result.set_code_value}', "
        f"{len(result.answers)} answer(s), registration {result.registration.value}"
    )


def _check_scan_preview_renders(image: Path) -> CheckResult:
    """Selecting a processed row produces a rectified page to look at."""
    harness = build_scan_page([image])
    harness.run_batch()
    harness.page.select_scan(0)
    if not harness.await_preview():
        harness.shutdown()
        return False, "the preview never finished rendering"
    detail = (
        f"preview rendered, zoom {harness.page.preview.zoom:.0%}, "
        f"status '{harness.page.preview_status_label.text()}'"
    )
    ok = harness.page.preview.has_page
    harness.shutdown()
    return ok, detail


def _check_settings_dialog_processing_section() -> CheckResult:
    """File > Settings offers the Processing section and its three modes."""
    from omr_scanner.config import AppConfig
    from omr_scanner.config.processing import ProcessingMode
    from omr_scanner.gui.settings_dialog import SettingsDialog

    # Constructed, never `exec()`d - see `_check_dialogs_instantiate`.
    dialog = SettingsDialog(AppConfig(), cpu_count=8)
    labels = [dialog.mode_combo.itemText(i) for i in range(dialog.mode_combo.count())]

    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText("Custom"))
    custom_enabled = dialog.worker_spin.isEnabled()
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText("Automatic"))
    automatic_disabled = not dialog.worker_spin.isEnabled()

    ok = (
        labels == ["Automatic", "Single core", "Custom"]
        and custom_enabled
        and automatic_disabled
        and dialog.worker_spin.maximum() == 8
        and dialog.selected_mode is ProcessingMode.AUTOMATIC
    )
    return ok, (
        f"modes {labels}, worker range {dialog.worker_spin.minimum()}-"
        f"{dialog.worker_spin.maximum()}, automatic uses "
        f"{dialog.active_label.text()} worker(s)"
    )


def _check_settings_dialog_diagnostics_section() -> CheckResult:
    """The Diagnostics section is present, off by default, and needs a folder."""
    from pathlib import Path as _Path

    from omr_scanner.config import AppConfig
    from omr_scanner.gui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(AppConfig(), cpu_count=8)
    off_by_default = not dialog.diagnostics_checkbox.isChecked()
    folder_disabled = not dialog.diagnostics_folder_button.isEnabled()

    dialog.diagnostics_checkbox.setChecked(True)
    folder_enabled = dialog.diagnostics_folder_button.isEnabled()
    # Switched on with nowhere to write: the setting must stay inert.
    inert_without_folder = not dialog.processing_settings().writes_diagnostics

    dialog.set_diagnostics_directory(_Path("diagnostics"))
    armed = dialog.processing_settings().writes_diagnostics

    ok = all(
        (off_by_default, folder_disabled, folder_enabled, inert_without_folder, armed)
    )
    return ok, (
        f"off by default: {off_by_default}, folder button follows the checkbox: "
        f"{folder_disabled and folder_enabled}, inert until a folder is chosen: "
        f"{inert_without_folder}"
    )


def _check_scan_page_reports_its_worker_plan(image: Path) -> CheckResult:
    """The Scan page says how many workers the next run will use."""
    from omr_scanner.config.processing import ProcessingMode, ProcessingSettings

    harness = build_scan_page(
        [image],
        processing=ProcessingSettings(mode=ProcessingMode.SINGLE_CORE),
    )
    text = harness.page.workers_label.text()
    planned = harness.page.planned_worker_count()
    harness.shutdown()
    return planned == 1, f"single core plans {planned} worker(s); label reads '{text}'"


def _check_processing_section_is_readable(image: Path) -> CheckResult:
    """Step 4's Processing buttons keep their full size, and never overlap.

    Guards the layout defect this pass fixed: the left-hand control column
    (Template + Scans + Processing + Output) has no scroll area, and its
    combined minimum height - about 900 logical pixels, nearly half of which
    is the Processing group's seven buttons, status line and progress readout
    - used to become the *whole page's* minimum height. Any window shorter
    than that (a laptop at 1366x768, a restored rather than maximised window,
    or higher Windows display scaling) left Qt nothing to do but compress
    every widget in the column below its own size hint, which is what made
    button icons and text overlap.

    The control column is now inside its own ``QScrollArea``, so this checks
    both the symptom (no overlap, nothing squeezed, at the normal window size)
    and the mechanism (the buttons still have their full, uncompressed size at
    a genuinely short one, because the column scrolls instead).
    """
    from PySide6.QtWidgets import QScrollArea

    harness = build_scan_page([image])
    page = harness.page
    buttons = [
        page.process_all_button,
        page.process_selected_button,
        page.resume_button,
        page.retry_failed_button,
        page.reprocess_button,
        page.cancel_button,
        page.review_button,
    ]

    zero_sized = [b.objectName() for b in buttons if b.width() <= 0 or b.height() <= 0]
    overlaps = [
        (a.objectName(), b.objectName())
        for i, a in enumerate(buttons)
        for b in buttons[i + 1 :]
        if a.geometry().intersects(b.geometry())
    ]
    squeezed = [b.objectName() for b in buttons if b.height() < b.sizeHint().height()]
    status_collides = page.workers_label.geometry().intersects(
        page.process_all_button.geometry()
    )
    footer_collides = page.review_button.geometry().intersects(
        page.batch_state_label.geometry()
    )

    scroll_area = page.findChild(QScrollArea, "scanControlScrollArea")
    page.resize(1280, 620)
    harness.process_events(rounds=4)
    still_full_size = all(b.geometry().height() == b.sizeHint().height() for b in buttons)
    page.resize(1600, 1000)
    harness.process_events(rounds=4)
    survived_resize = all(b.isVisible() for b in buttons)

    harness.shutdown()

    ok = (
        not zero_sized
        and not overlaps
        and not squeezed
        and not status_collides
        and not footer_collides
        and scroll_area is not None
        and still_full_size
        and survived_resize
    )
    return ok, (
        f"{len(buttons)} button(s): no overlap={not overlaps}, "
        f"none squeezed={not squeezed}, scroll area present="
        f"{scroll_area is not None}, full size kept at 1280x620="
        f"{still_full_size}"
    )


def _check_multicore_batch_matches_single_core(image: Path) -> CheckResult:
    """Two copies of the real sample read identically on one core and on two."""
    import shutil

    from _harness import OUTPUT_ROOT

    from omr_scanner.config.processing import ProcessingMode, ProcessingSettings

    folder = OUTPUT_ROOT / "scan_multicore_inputs"
    folder.mkdir(parents=True, exist_ok=True)
    copies = []
    for index in range(2):
        destination = folder / f"IMG_{index + 1:03d}{image.suffix}"
        shutil.copyfile(image, destination)
        copies.append(destination)

    def run(processing: object) -> tuple[list[tuple[str, str]], object]:
        harness = build_scan_page(copies, processing=processing)
        report = harness.run_batch()
        values = [
            (item.result.identifier_value, item.result.outcome.value)
            for item in report.processed
        ]
        harness.shutdown()
        return values, report

    single, single_report = run(ProcessingSettings(mode=ProcessingMode.SINGLE_CORE))
    parallel, parallel_report = run(
        ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=2)
    )
    ok = single == parallel and parallel_report.worker_count == 2
    return ok, (
        f"1 worker {single_report.elapsed_seconds:.2f}s, "
        f"{parallel_report.worker_count} workers "
        f"{parallel_report.elapsed_seconds:.2f}s, identical results: "
        f"{single == parallel}"
    )


def _check_progress_panel_renders_a_large_batch() -> CheckResult:
    """The progress readout describes a ten-thousand-sheet batch correctly."""
    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
    from omr_scanner.gui.scan.page import ScanPage
    from omr_scanner.services.batch_progress import BatchState, ProgressSnapshot

    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    page = ScanPage(spec)
    page._render_progress(
        ProgressSnapshot(
            state=BatchState.PROCESSING,
            total=10_000,
            successful=6_301,
            warnings=28,
            failed=13,
            elapsed_seconds=1_122.0,
            rate=5.65,
            eta_seconds=647.0,
            workers=12,
        )
    )
    counts = page.progress_counts_label.text()
    timing = page.progress_timing_label.text()
    ok = (
        counts == "6,342 / 10,000 processed"
        and page.progress_bar.value() == 6_342
        and page.progress_bar.format() == "63.4%"
        and "Remaining ~00:10:47" in timing
    )
    page.close()
    return ok, f"{counts} | {page.progress_bar.format()} | {timing}"


def _check_progress_panel_has_no_per_scan_widgets() -> CheckResult:
    """Ten thousand rows must not become ten thousand widgets."""
    from pathlib import Path as _Path

    from PySide6.QtWidgets import QProgressBar

    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
    from omr_scanner.gui.scan.page import ScanEntry, ScanPage

    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    page = ScanPage(spec)
    page.state.entries.extend(
        ScanEntry(path=_Path(f"scan{index:05d}.png")) for index in range(10_000)
    )
    started = time.perf_counter()
    page._rebuild_scan_table()
    elapsed = time.perf_counter() - started

    bars = len(page.findChildren(QProgressBar))
    rows = page.scan_table.model().rowCount()
    page.close()
    return bars == 1 and rows == 10_000, (
        f"{rows:,} rows built in {elapsed:.2f}s with {bars} progress bar"
    )


def _check_tools_menu_offers_the_developer_commands() -> CheckResult:
    """Tools > Developer / Testing carries both commands, always enabled."""
    from PySide6.QtWidgets import QMenu

    from omr_scanner.gui.main_window import MainWindow

    window = MainWindow()
    titles = [menu.title() for menu in window.menuBar().findChildren(QMenu)]
    ok = (
        any("Tools" in title for title in titles)
        and any("Developer" in title for title in titles)
        and window.generate_dataset_action.isEnabled()
        and window.run_benchmark_action.isEnabled()
    )
    window.close()
    return ok, f"menus {[t for t in titles if t]}"


def _check_generation_dialog_builds_a_request() -> CheckResult:
    """The generation dialog collects a complete request from its widgets."""
    from _harness import OUTPUT_ROOT, SAMPLE_TEMPLATE

    from omr_scanner.evaluation.synthetic_dataset import DatasetProfile, ImageFormat
    from omr_scanner.gui.devtools import GenerateDatasetDialog

    # Constructed, never `exec()`d - see `_check_dialogs_instantiate`.
    dialog = GenerateDatasetDialog(
        template_path=SAMPLE_TEMPLATE, output_dir=OUTPUT_ROOT / "synthetic"
    )
    dialog.count_spin.setValue(24)
    dialog.seed_spin.setValue(4242)
    dialog.profile_combo.setCurrentIndex(
        dialog.profile_combo.findData(DatasetProfile.DEGRADATION)
    )
    dialog.format_combo.setCurrentIndex(dialog.format_combo.findData(ImageFormat.JPEG))

    request = dialog.request()
    ok = (
        request is not None
        and request.count == 24
        and request.seed == 4242
        and request.profile is DatasetProfile.DEGRADATION
        and request.image_format is ImageFormat.JPEG
        and dialog.quality_spin.isEnabled()
    )
    return ok, (
        f"profile {request.profile.value}, {request.count} sheet(s) at "
        f"{request.dpi} dpi as {request.image_format.value}"
        if request is not None
        else "the dialog produced no request"
    )


def _check_generate_and_benchmark_end_to_end() -> CheckResult:
    """Generate a small dataset, score it, and read the category table.

    The one check here that exercises the whole developer feature: the
    generator, the batch pipeline, the scorer and the report files.
    """
    from _harness import OUTPUT_ROOT, SAMPLE_TEMPLATE, build_scan_page

    from omr_scanner.evaluation.session import REPORT_DIRNAME
    from omr_scanner.evaluation.synthetic_dataset import (
        DatasetProfile,
        generate_dataset,
    )
    from omr_scanner.services import load_template

    dataset = OUTPUT_ROOT / "smoke_dataset"
    template = load_template(SAMPLE_TEMPLATE)
    generate_dataset(
        dataset,
        template,
        count=6,
        seed=4242,
        profile=DatasetProfile.BASELINE,
        name="smoke",
    )

    harness = build_scan_page()
    page = harness.page
    page.benchmark_auto_show = False
    if not page.enter_benchmark_mode(dataset):
        harness.shutdown()
        return False, "benchmark mode did not start"

    harness.run_batch()
    report = page.last_benchmark
    harness.shutdown()
    if report is None:
        return False, "the run was not scored"

    ok = (
        report.summary.scans == 6
        and report.summary.question_accuracy == 1.0
        and bool(report.categories)
        and (dataset / REPORT_DIRNAME / "summary.json").is_file()
        and (dataset / REPORT_DIRNAME / "category_metrics.csv").is_file()
    )
    return ok, (
        f"{report.summary.scans} sheet(s), sheet accuracy "
        f"{report.summary.sheet_accuracy:.4f}, answer accuracy "
        f"{report.summary.question_accuracy:.4f}, "
        f"{len(report.categories)} test-case categor(ies), "
        f"{len(report.errors)} disagreement(s)"
    )


def _check_benchmark_dialog_constructs() -> CheckResult:
    """The results dialog builds from an empty report without a run behind it."""
    from PySide6.QtWidgets import QTableWidget

    from omr_scanner.evaluation.benchmark import BenchmarkReport, BenchmarkSummary
    from omr_scanner.gui.devtools import BenchmarkResultsDialog

    dialog = BenchmarkResultsDialog(
        BenchmarkReport(summary=BenchmarkSummary(dataset="empty"))
    )
    required = ("benchmarkSummaryTable", "benchmarkCategoryTable", "benchmarkErrorTable")
    missing = [name for name in required if dialog.findChild(QTableWidget, name) is None]
    return not missing, (
        "summary, category and error tables present"
        if not missing
        else f"missing: {', '.join(missing)}"
    )


def _check_calibration_page_object_names() -> CheckResult:
    """Every stable selector the calibration scenario reference lists exists."""
    from _harness import build_calibration_page
    from PySide6.QtCore import QObject

    harness = build_calibration_page()
    required = [
        "testScanList", "calibrationImageView", "markerOverlayToggle",
        "bubbleOverlayToggle", "scoreOverlayToggle", "regionOverlayToggle",
        "bubbleThresholdSpinBox", "bubbleThresholdSlider",
        "runCalibrationButton", "runAllCalibrationButton",
        "resetCalibrationButton", "saveCalibrationButton",
        "calibrationSummaryPanel", "calibrationStatusLabel", "bubbleDiagnosticPanel",
        "sampleWindowOverlayToggle", "bubbleCenterOverlayToggle",
        "calibrationViewModeCombo", "fieldDiagnosticsLabel", "thresholdStateLabel",
    ]
    missing = [name for name in required if harness.page.findChild(QObject, name) is None]
    ok = harness.page.objectName() == "calibrationPage" and not missing
    harness.shutdown()
    return ok, (
        f"{len(required)} stable objectName(s) present"
        if not missing
        else f"missing objectName(s): {', '.join(missing)}"
    )


def _check_calibration_runs_and_scores_the_real_sample() -> CheckResult:
    """Run calibration end to end on the real sample sheet and template.

    The one calibration check that exercises the whole feature: registration,
    marker/expected geometry, bubble measurement, the calibration verdict and
    the quality summary, all against `examples/ECE-0000.png`.
    """
    from _harness import build_calibration_page

    harness = build_calibration_page()
    harness.run_all()
    entry = harness.page.state.entries[0]
    result = entry.result
    report = entry.report
    ok = (
        result is not None
        and report is not None
        and result.registration.value != "registration_failed"
        and len(result.markers) == 4
        and report.status.value in ("passed", "passed_with_warnings")
    )
    detail = (
        f"status={report.status.value if report else '?'}, "
        f"markers={len(result.markers) if result else 0}/4, "
        f"bubbles={len(result.bubbles) if result else 0}, "
        f"near-threshold={report.near_threshold_count if report else '?'}"
    )
    harness.shutdown()
    return ok, detail


def _check_a_mismatched_template_fails_calibration_not_a_false_pass() -> CheckResult:
    """The load-bearing Phase 4 check: a bad template is never a confident pass."""
    from _harness import OUTPUT_ROOT, SAMPLE_TEMPLATE, build_calibration_page

    from omr_scanner.domain.geometry import NormalizedPoint
    from omr_scanner.services import CalibrationStatus, load_template, save_template

    template = load_template(SAMPLE_TEMPLATE)
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
    bad_path = OUTPUT_ROOT / "mismatched_calibration.omrt"
    save_template(mismatched, bad_path)

    harness = build_calibration_page(template_path=bad_path)
    harness.run_all()
    entry = harness.page.state.entries[0]
    ok = (
        entry.report is not None
        and entry.report.status is CalibrationStatus.FAILED
        and entry.result is not None
        and entry.result.answers == ()
        and entry.result.bubbles == ()
    )
    detail = f"status={entry.report.status.value if entry.report else '?'}"
    harness.shutdown()
    return ok, detail


def _check_a_displaced_template_that_registers_is_not_a_false_pass() -> CheckResult:
    """The harder miscalibration case, on the real sample sheet.

    Only the bubble geometry is wrong here - the registration markers are
    untouched, so Phase 1 rectifies the page perfectly, no sampling window
    falls off it, and every group reads a confident BLANK from bare paper.
    None of the registration-level checks can see this; the verdict must
    still not be a pass.
    """
    from _harness import OUTPUT_ROOT, SAMPLE_TEMPLATE, build_calibration_page

    from omr_scanner.domain.geometry import NormalizedPoint, NormalizedRect
    from omr_scanner.services import CalibrationStatus, load_template, save_template

    template = load_template(SAMPLE_TEMPLATE)
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

    bad_path = OUTPUT_ROOT / "displaced_calibration.omrt"
    save_template(displaced, bad_path)

    harness = build_calibration_page(template_path=bad_path)
    harness.run_all()
    entry = harness.page.state.entries[0]
    report = entry.report
    result = entry.result
    ok = (
        report is not None
        and result is not None
        and result.registration.value != "registration_failed"
        and report.status
        in (CalibrationStatus.NEEDS_REVIEW, CalibrationStatus.FAILED)
    )
    detail = (
        f"registration={result.registration.value if result else '?'}, "
        f"status={report.status.value if report else '?'}, "
        f"marks in {report.groups_with_marks if report else '?'}/"
        f"{report.groups_total if report else '?'} positions"
    )
    harness.shutdown()
    return ok, detail


def _check_the_overlay_shows_the_sampled_region_not_the_printed_bubble() -> CheckResult:
    """The sampling overlay must describe the region recognition actually read."""
    from _harness import build_calibration_page

    harness = build_calibration_page()
    harness.run_all()
    entry = harness.page.state.entries[0]
    bubbles = entry.result.bubbles if entry.result else ()
    ok = bool(bubbles) and all(
        0.0 < bubble.sample_half_width < bubble.width / 2.0
        and 0.0 < bubble.sample_half_height < bubble.height / 2.0
        for bubble in bubbles
    )
    sample = bubbles[0] if bubbles else None
    detail = (
        f"{len(bubbles)} bubbles; sampled ellipse "
        f"{sample.sample_half_width * 2:.1f}x{sample.sample_half_height * 2:.1f} px "
        f"inside printed {sample.width:.1f}x{sample.height:.1f} px"
        if sample is not None
        else "no bubbles measured"
    )
    harness.shutdown()
    return ok, detail


def _check_a_batch_is_recorded_in_the_project_database() -> CheckResult:
    """Phase 5: processing a batch writes durable per-scan state."""
    from _harness import build_scan_page

    harness = build_scan_page(with_project=True)
    harness.run_batch()
    summary = harness.batch_summary
    ok = (
        summary is not None
        and summary.total == 1
        and summary.processed == 1
        and summary.pending == 0
    )
    detail = (
        f"batch {summary.batch_id[:8]}: {summary.resume_label}, status={summary.status}"
        if summary is not None
        else "no batch was recorded"
    )
    harness.shutdown()
    return ok, detail


def _check_a_partial_run_can_be_resumed() -> CheckResult:
    """Phase 5: a batch stopped part-way finishes without redoing its work."""
    import shutil

    from _harness import OUTPUT_ROOT, SAMPLE_SHEET, build_scan_page

    # Four copies of the real sample, so there is genuinely something left
    # over after a partial run.
    scans_dir = OUTPUT_ROOT / "resume_scans"
    shutil.rmtree(scans_dir, ignore_errors=True)
    scans_dir.mkdir(parents=True, exist_ok=True)
    scans = []
    for index in range(4):
        copy = scans_dir / f"sheet_{index}.png"
        shutil.copy2(SAMPLE_SHEET, copy)
        scans.append(copy)

    harness = build_scan_page(scans, with_project=True)
    first = harness.run_paths(scans[:2])
    partial = harness.batch_summary
    resumed = harness.resume()
    final = harness.batch_summary

    ok = (
        first.total == 2
        and partial is not None
        and partial.pending == 2
        # The resumed run read the two that were left, not all four.
        and resumed.total == 2
        and final is not None
        and final.processed == 4
        and final.pending == 0
    )
    detail = (
        f"first run {first.total}, then {partial.pending if partial else '?'} pending, "
        f"resumed {resumed.total}, final {final.processed if final else '?'}/"
        f"{final.total if final else '?'}"
    )
    harness.shutdown()
    return ok, detail


def _check_originals_are_unchanged_by_processing() -> CheckResult:
    """Phase 5 exit criterion: a batch never modifies its source scans."""
    import hashlib
    import shutil

    from _harness import OUTPUT_ROOT, SAMPLE_SHEET, build_scan_page

    scans_dir = OUTPUT_ROOT / "integrity_scans"
    shutil.rmtree(scans_dir, ignore_errors=True)
    scans_dir.mkdir(parents=True, exist_ok=True)
    scans = []
    for index in range(2):
        copy = scans_dir / f"sheet_{index}.png"
        shutil.copy2(SAMPLE_SHEET, copy)
        scans.append(copy)

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    before = {path: digest(path) for path in scans}
    harness = build_scan_page(
        scans, rename=True, output_dir=OUTPUT_ROOT / "integrity_output", with_project=True
    )
    harness.run_batch()
    after = {path: digest(path) for path in scans}
    harness.shutdown()

    changed = [path.name for path in scans if before[path] != after[path]]
    return not changed, (
        f"{len(scans)} original(s) byte-for-byte unchanged after renaming"
        if not changed
        else f"MODIFIED: {', '.join(changed)}"
    )


def _check_conflict_review_records_a_named_decision() -> CheckResult:
    """Phase 6: a correction is stored, named, reasoned - and keeps the machine value."""
    from _harness import build_review_page

    harness = build_review_page()
    conflict = harness.first_conflict()
    if conflict is None:
        harness.shutdown()
        return False, "the prepared batch produced no conflicts to review"

    machine_value = conflict.observation.value
    harness.select(conflict.conflict_id)
    ok_correct = harness.correct("A")

    from omr_scanner.services import review_store

    found = review_store.provenance_for(harness.database, conflict.conflict_id)
    history = review_store.history_for(harness.database, conflict.conflict_id)
    stored = review_store.get_conflict(harness.database, conflict.conflict_id)

    ok = (
        ok_correct
        and found.value == "A"
        and found.source.value == "human"
        and found.reviewer == harness.reviewer
        # The machine's own reading is untouched by the correction.
        and stored.observation.value == machine_value
        and [item.action.value for item in history] == ["detected", "corrected"]
    )
    detail = (
        f"machine={machine_value!r} kept, effective={found.value!r} by "
        f"{found.reviewer!r}, {len(history)} audit event(s)"
    )
    harness.shutdown()
    return ok, detail


def _check_the_audit_ledger_cannot_be_rewritten() -> CheckResult:
    """Phase 6: the database itself refuses to update or delete an audit event."""
    from _harness import build_review_page
    from sqlalchemy import delete, update

    from omr_scanner.database.models import AuditEvent

    harness = build_review_page()
    conflict = harness.first_conflict()
    if conflict is None:
        harness.shutdown()
        return False, "the prepared batch produced no conflicts to review"
    harness.select(conflict.conflict_id)
    harness.correct("A")

    blocked = []
    for label, statement in (
        ("update", update(AuditEvent).values(reviewer="Someone Else")),
        ("delete", delete(AuditEvent)),
    ):
        try:
            with harness.database.session() as session:
                session.execute(statement)
        except Exception:
            blocked.append(label)

    harness.shutdown()
    return len(blocked) == 2, f"blocked at the database: {', '.join(blocked) or 'nothing'}"


def _check_a_correction_needs_a_named_reviewer() -> CheckResult:
    """Phase 6: an unnamed correction is refused, and nothing is written."""
    from _harness import build_review_page

    from omr_scanner.services import review_store

    harness = build_review_page(reviewer="")
    conflict = harness.first_conflict()
    if conflict is None:
        harness.shutdown()
        return False, "the prepared batch produced no conflicts to review"
    harness.select(conflict.conflict_id)

    refused = not harness.correct("A", expect_failure=True)
    state = review_store.get_conflict(harness.database, conflict.conflict_id).state.value
    harness.shutdown()
    return refused and state == "open", (
        f"correction refused with no reviewer, conflict still {state!r}"
    )


def _check_reconciliation_classifies_every_case() -> CheckResult:
    """Phase 7: a roster and a batch that disagree produce every exception."""
    from _harness import build_reconciliation_page

    harness = build_reconciliation_page()
    harness.show_everything()
    found = {
        candidate_id: entry.status.value
        for candidate_id, entry in harness.entries().items()
    }
    harness.shutdown()

    expected = {
        "100001": "matched",
        "100002": "duplicate_script",
        "100003": "absent_confirmed",
        "100004": "present_without_script",
        "100005": "absent_with_script",
        "999999": "unknown_id",
    }
    wrong = {
        key: (found.get(key), value)
        for key, value in expected.items()
        if found.get(key) != value
    }
    return not wrong, (
        "matched, duplicate, absent-confirmed, present-without-script, "
        "absent-with-script and unknown-ID all produced"
        if not wrong
        else f"unexpected: {wrong}"
    )


def _check_reconciliation_keeps_the_machine_and_imported_values() -> CheckResult:
    """Phase 7: a human decision is added beside the source, never over it."""
    from _harness import build_reconciliation_page

    from omr_scanner.domain.reconciliation import (
        AttendanceState,
        ReconciliationReason,
    )
    from omr_scanner.services import reconciliation_store

    harness = build_reconciliation_page()
    harness.show_everything()
    unknown = harness.entries()["999999"]
    scan_id = unknown.scripts[0].script.scan_id

    reconciliation_store.assign_script(
        harness.database, harness.roster_id, harness.batch_id, scan_id,
        candidate_id="100004", operator=harness.operator,
        reason=ReconciliationReason.MISREAD_IDENTIFIER,
    )
    reconciliation_store.override_attendance(
        harness.database, harness.roster_id, harness.batch_id, "100005",
        attendance=AttendanceState.PRESENT, operator=harness.operator,
        reason=ReconciliationReason.CANDIDATE_ATTENDED,
    )
    harness.reconcile()
    harness.show_everything()

    entries = harness.entries()
    assigned = entries["100004"]
    overridden = entries["100005"]
    machine = assigned.scripts[0].script.machine_candidate_id
    imported = overridden.candidate.imported_attendance.value
    effective = overridden.effective_attendance.value
    history = reconciliation_store.history_for(
        harness.database, "script", str(scan_id)
    )
    harness.shutdown()

    ok = (
        assigned.status.value == "matched"
        and machine == "999999"
        and imported == "absent"
        and effective == "present"
        and len(history) == 1
        and history[0].reviewer == "Dr. Smoke Test"
    )
    return ok, (
        f"script assigned to 100004 but machine kept {machine!r}; "
        f"100005 imported {imported!r}, effective {effective!r}; "
        f"{len(history)} audit event(s)"
    )


def _check_a_reconciliation_decision_needs_a_named_operator() -> CheckResult:
    """Phase 7: an unnamed decision is refused, and nothing is written."""
    from _harness import build_reconciliation_page

    from omr_scanner.domain.reconciliation import ReconciliationReason
    from omr_scanner.errors import OMRScannerError
    from omr_scanner.services import reconciliation_store

    harness = build_reconciliation_page(operator="")
    harness.show_everything()
    unknown = harness.entries()["999999"]
    scan_id = unknown.scripts[0].script.scan_id

    refused = False
    try:
        reconciliation_store.assign_script(
            harness.database, harness.roster_id, harness.batch_id, scan_id,
            candidate_id="100004", operator="",
            reason=ReconciliationReason.MISREAD_IDENTIFIER,
        )
    except OMRScannerError:
        refused = True

    harness.reconcile()
    harness.show_everything()
    state = harness.entries()["999999"].status.value
    harness.shutdown()
    return refused and state == "unknown_id", (
        f"decision refused with no operator, script still {state!r}"
    )


def _check_the_sample_candidate_list_can_be_saved() -> CheckResult:
    """Phase 7: the packaged sample is handed out, and imports straight back."""
    from _harness import OUTPUT_ROOT, ensure_application, uuid_hex

    from omr_scanner.services.candidate_import import (
        read_roster,
        sample_template_bytes,
        save_sample_template,
    )

    ensure_application()
    destination = OUTPUT_ROOT / "reconciliation" / uuid_hex() / "sample.xlsx"
    save_sample_template(destination)
    identical = destination.read_bytes() == sample_template_bytes()
    found = read_roster(destination)
    names = {item.display_name for item in found.candidates}
    return (
        identical and found.can_import and names == {"CANDIDATE NAME GOES HERE"}
    ), (
        f"sample saved ({destination.stat().st_size} bytes), "
        f"{len(found.candidates)} placeholder candidate(s), "
        f"importable={found.can_import}"
    )


def _check_no_candidate_data_reaches_the_log() -> CheckResult:
    """Phase 7's mandatory exit criterion, exercised end to end."""
    import io
    import logging

    from _harness import build_reconciliation_page

    from omr_scanner.domain.reconciliation import ReconciliationReason
    from omr_scanner.services import reconciliation_store

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        harness = build_reconciliation_page()
        harness.show_everything()
        scan_id = harness.entries()["999999"].scripts[0].script.scan_id
        reconciliation_store.assign_script(
            harness.database, harness.roster_id, harness.batch_id, scan_id,
            candidate_id="100004", operator=harness.operator,
            reason=ReconciliationReason.MISREAD_IDENTIFIER,
        )
        harness.reconcile()
        harness.shutdown()
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)

    captured = stream.getvalue()
    # Candidate IDs and names from the harness's fictional roster.
    secrets = ["100001", "100004", "999999", "CANDIDATE A", "CANDIDATE E"]
    leaked = [item for item in secrets if item in captured]
    return not leaked, (
        f"{len(captured.splitlines())} log line(s), no candidate data"
        if not leaked
        else f"candidate data reached the log: {leaked}"
    )


def _check_scoring_needs_a_verified_key() -> CheckResult:
    """Phase 8: a draft key produces no marks, whatever else is in place."""
    from _harness import build_scoring_pages

    harness = build_scoring_pages()
    harness.write_key("A", "A" * harness.plan.question_count)
    harness.score()
    before = {
        candidate: item.status.value for candidate, item in harness.results().items()
    }
    scored = sum(1 for value in before.values() if value == "scored")
    harness.shutdown()
    return scored == 0, (
        f"draft key produced {scored} mark(s); statuses {sorted(set(before.values()))}"
    )


def _check_scoring_produces_defensible_marks() -> CheckResult:
    """Phase 8: the acceptance outcomes, with the key revision recorded."""
    from _harness import build_scoring_pages

    from omr_scanner.domain.scoring import format_mark

    harness = build_scoring_pages()
    total = harness.plan.question_count
    harness.write_key("A", "A" * total)
    harness.verify_key("A")
    harness.score()
    found = harness.results()

    perfect = found.get("200001")
    partial = found.get("200002")
    absent = found.get("200003")
    no_key = found.get("200004")
    harness.shutdown()

    ok = (
        perfect is not None
        and perfect.status.value == "scored"
        and perfect.final_score == total
        and perfect.answer_key_revision == 1
        and partial is not None
        and partial.final_score == total - 3
        and absent is not None
        and absent.status.value == "absent"
        and absent.final_score is None
        and no_key is not None
        and no_key.status.value == "blocked"
    )
    return ok, (
        f"perfect={format_mark(perfect.final_score) if perfect and perfect.has_mark else '-'} "
        f"(key rev {perfect.answer_key_revision if perfect else '?'}), "
        f"three wrong={format_mark(partial.final_score) if partial and partial.has_mark else '-'}, "
        f"absent has no mark, no-key candidate blocked"
    )


def _check_a_rule_change_makes_results_stale() -> CheckResult:
    """Phase 8: changing a rule invalidates marks rather than editing them."""
    from fractions import Fraction

    from _harness import build_scoring_pages

    from omr_scanner.domain.scoring import NegativeMarking, ScoringPolicy

    harness = build_scoring_pages()
    total = harness.plan.question_count
    harness.write_key("A", "A" * total)
    harness.verify_key("A")
    harness.score()
    before = harness.results()["200002"]

    harness.results_page.apply_policy(
        ScoringPolicy(
            correct_mark=Fraction(1),
            incorrect_penalty=Fraction(1, 4),
            mode=NegativeMarking.FIXED,
            clamp_minimum=False,
        )
    )
    stale = harness.results()["200002"]
    kept_mark = stale.final_score == before.final_score

    harness.score()
    after = harness.results()["200002"]
    harness.shutdown()

    expected = Fraction(total - 3) - 3 * Fraction(1, 4)
    ok = (
        stale.is_stale
        and kept_mark
        and not after.is_stale
        and after.final_score == expected
        and after.policy_revision == before.policy_revision + 1
        and after.answer_string == before.answer_string
    )
    return ok, (
        f"policy change made the result stale and kept its mark; recomputation "
        f"gave {after.final_score} from unchanged answers "
        f"(policy rev {before.policy_revision} -> {after.policy_revision})"
    )


def _check_recomputation_ignores_a_corrupted_mark() -> CheckResult:
    """Phase 8: a rescore derives the mark; it never adjusts the stored one."""
    from _harness import build_scoring_pages
    from sqlalchemy import select

    from omr_scanner.database.models import CandidateResult

    harness = build_scoring_pages()
    total = harness.plan.question_count
    harness.write_key("A", "A" * total)
    harness.verify_key("A")
    harness.score()

    with harness.database.session() as session:
        row = session.scalars(
            select(CandidateResult).where(CandidateResult.candidate_id == "200001")
        ).first()
        row.final_score = "999"
        row.raw_score = "999"

    harness.score()
    after = harness.results()["200001"]
    harness.shutdown()
    return after.final_score == total, (
        f"stored mark corrupted to 999; recomputation produced {after.final_score} "
        f"(expected {total})"
    )


def _check_a_wrong_question_pays_everyone() -> CheckResult:
    """Phase 8: a withdrawn question credits every response, with no deduction."""
    from _harness import build_scoring_pages

    harness = build_scoring_pages()
    total = harness.plan.question_count
    harness.write_key("A", "A" * total, wrong="1, 2, 3")
    harness.verify_key("A")
    harness.score()

    partial = harness.results()["200002"]  # answered 1-3 wrongly
    harness.select("200002")
    outcomes = {
        harness.results_page.detail_table.item(row, 4).text()
        for row in range(3)
    }
    harness.shutdown()
    return partial.final_score == total and outcomes == {
        "Wrong question - full credit"
    }, (
        f"three wrongly-answered questions withdrawn: mark {partial.final_score} "
        f"of {total}, outcomes {sorted(outcomes)}"
    )


def _check_reports_page_lists_the_associated_set() -> CheckResult:
    """Phase 9: Result Management shows the set and its associated template."""
    from _harness import build_reports_page

    harness = build_reports_page()
    overview = harness.page.state.sets[0] if harness.page.state.sets else None
    ok = (
        overview is not None
        and overview.set_code == "A"
        and overview.template is not None
        and overview.has_verified_key
    )
    detail = (
        f"set={overview.set_code if overview else None}, "
        f"template={'associated' if overview and overview.template else 'none'}, "
        f"key_verified={overview.has_verified_key if overview else False}"
    )
    harness.shutdown()
    return ok, detail


def _check_generating_xlsx_produces_every_row_and_rank_formulas() -> CheckResult:
    """Phase 9: Rollwise keeps every candidate, including the absentee, with
    Excel rank formulas over scored candidates."""
    import openpyxl

    from _harness import build_reports_page

    harness = build_reports_page()
    from PySide6.QtCore import QElapsedTimer
    from PySide6.QtWidgets import QApplication

    done: list[object] = []
    harness.page.reports_generated.connect(lambda: done.append(True))
    harness.page.set_table.selectRow(0)
    harness.page.generate_selected_xlsx()
    clock = QElapsedTimer()
    clock.start()
    while not done and clock.elapsed() < 60_000:
        QApplication.processEvents()

    outputs = list((harness.session.project.layout.exports_dir).glob("*.xlsx"))
    ok = bool(outputs) and bool(done)
    rolls: list[object] = []
    rank_formula = ""
    if outputs:
        workbook = openpyxl.load_workbook(outputs[0])
        sheet = workbook["Rollwise"]
        rolls = [sheet.cell(row=r, column=2).value for r in range(2, 5)]
        rank_formula = str(sheet.cell(row=2, column=5).value)
        ok = ok and rolls == ["200001", "200002", "200003"]
        ok = ok and "RANK.EQ" in rank_formula
    harness.shutdown()
    return ok, (
        f"generated={bool(outputs)}, rolls={rolls}, "
        f"rank formula present={'RANK.EQ' in rank_formula}"
    )


def _check_a_second_generation_does_not_overwrite_the_first() -> CheckResult:
    """Phase 9: an existing output file is never silently replaced."""
    from _harness import build_reports_page

    from omr_scanner.services import report_store

    harness = build_reports_page()
    outcome1 = report_store.generate_xlsx(
        harness.database, harness.roster_id, harness.batch_id, harness.template, "A",
        project_name="GUI Test Examination",
        output_dir=harness.session.project.layout.exports_dir,
        computed_by=harness.operator,
    )
    outcome2 = report_store.generate_xlsx(
        harness.database, harness.roster_id, harness.batch_id, harness.template, "A",
        project_name="GUI Test Examination",
        output_dir=harness.session.project.layout.exports_dir,
        computed_by=harness.operator,
    )
    ok = (
        outcome1.ok and outcome2.ok
        and outcome1.output_path != outcome2.output_path
        and outcome1.output_path.exists()
        and outcome2.output_path.exists()
    )
    detail = f"first={outcome1.output_path.name}, second={outcome2.output_path.name}"
    harness.shutdown()
    return ok, detail


def _check_project_configuration_defines_sets_that_survive_a_reopen() -> CheckResult:
    """The Project Configuration dialog, driven end to end against real files.

    Builds the brief's own example - one exam name, three sets - through the
    dialog itself, then closes the project and opens it again from disk, so
    what is asserted is what a reopened project actually contains rather
    than what a widget still happens to be holding.
    """
    import shutil

    from _harness import OUTPUT_ROOT

    from omr_scanner.gui.project_config_dialog import ProjectConfigDialog
    from omr_scanner.services import create_project, open_project, project_sets

    workspace = OUTPUT_ROOT / "project_configuration"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)

    exam_name = "Recruitment Exam, Bangladesh Submarine Cable Regulatory Authority"
    wanted = (
        ("10", "Name of Post: Assistant Engineer (Electrical)"),
        ("11", "Name of Post: Assistant Engineer (Civil)"),
        ("12", "Name of Post: Assistant Engineer (Mechanical)"),
    )

    session = create_project(workspace, "BSCRA Recruitment")
    dialog = ProjectConfigDialog(session)
    dialog.exam_name_edit.setText(exam_name)
    saved = dialog.save_exam_name()
    added = [dialog.add_set(code, description) for code, description in wanted]
    duplicate_refused = not dialog.add_set("10", "A second Set 10")
    duplicate_message = dialog.sets_status_label.text()
    rows_shown = dialog.sets_table.rowCount()
    dialog.deleteLater()
    root = session.root
    session.close()

    with open_project(root) as reopened:
        stored = project_sets.list_sets(reopened.database)
        reloaded = tuple((item.code, item.description) for item in stored)
        reloaded_exam_name = reopened.exam_name
        stable_ids = len({item.set_id for item in stored})

    ok = (
        saved
        and all(added)
        and duplicate_refused
        and rows_shown == 3
        and reloaded == wanted
        and reloaded_exam_name == exam_name
        and stable_ids == 3
    )
    detail = (
        f"exam name {'kept' if reloaded_exam_name == exam_name else 'LOST'}; "
        f"{len(reloaded)} set(s) reloaded {'correctly' if reloaded == wanted else 'WRONG'}; "
        f"duplicate {'refused' if duplicate_refused else 'ACCEPTED'} "
        f"({duplicate_message[:60]})"
    )
    return ok, detail


def _check_no_worker_processes_are_left_behind() -> CheckResult:
    """Nothing from a finished batch is still running."""
    import multiprocessing

    children = multiprocessing.active_children()
    return not children, (
        "no worker processes remain"
        if not children
        else f"{len(children)} worker process(es) still running"
    )


def main(argv: list[str] | None = None) -> int:
    """Run every check, print a report, and return a shell exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", type=Path, default=SAMPLE_SHEET)
    args = parser.parse_args(argv)

    if not args.image.is_file():
        print(f"Reference image not found: {args.image}", file=sys.stderr)
        return 2

    ensure_application()

    checks: list[tuple[str, Callable[[], CheckResult]]] = [
        ("application initialises", _check_application),
        ("main window constructs", _check_main_window),
        ("template page constructs empty", _check_empty_page),
        ("sample image loads", lambda: _check_sample_loads(args.image)),
        ("both toolbar rows exist", lambda: _check_toolbars(args.image)),
        ("every named action is on a toolbar", lambda: _check_named_actions(args.image)),
        ("canvas and panels exist", lambda: _check_canvas_and_panels(args.image)),
        (
            "bubble radius reflects the document",
            lambda: _check_bubble_radius_reflects_the_document(args.image),
        ),
        ("region dialogs instantiate", lambda: _check_dialogs_instantiate(args.image)),
        ("orientation detection runs", lambda: _check_orientation_detection(args.image)),
        ("page header is compact", lambda: _check_page_header_is_compact(args.image)),
        ("scan page constructs", _check_scan_page_constructs),
        ("settings dialog offers Processing", _check_settings_dialog_processing_section),
        ("settings dialog offers Diagnostics", _check_settings_dialog_diagnostics_section),
        ("progress panel renders a large batch", _check_progress_panel_renders_a_large_batch),
        ("no per-scan widgets at 10,000 rows", _check_progress_panel_has_no_per_scan_widgets),
        (
            "tools menu offers the developer commands",
            _check_tools_menu_offers_the_developer_commands,
        ),
        ("benchmark results dialog constructs", _check_benchmark_dialog_constructs),
        (
            "project configuration defines sets that survive a reopen",
            _check_project_configuration_defines_sets_that_survive_a_reopen,
        ),
    ]

    # The Scan checks drive the real template that describes the real sample.
    # Without it there is nothing honest to check, so they are skipped aloud
    # rather than quietly passing on a substitute.
    if SAMPLE_TEMPLATE.is_file():
        checks.extend(
            [
                ("scan page object names", _check_scan_object_names),
                (
                    "scan template loads and imports",
                    lambda: _check_scan_template_and_import(args.image),
                ),
                (
                    "scan recognises the real sample",
                    lambda: _check_scan_recognises_the_real_sample(args.image),
                ),
                ("scan preview renders", lambda: _check_scan_preview_renders(args.image)),
                (
                    "scan page reports its worker plan",
                    lambda: _check_scan_page_reports_its_worker_plan(args.image),
                ),
                (
                    "processing section is readable and never overlaps",
                    lambda: _check_processing_section_is_readable(args.image),
                ),
                (
                    "multicore reads the same as single core",
                    lambda: _check_multicore_batch_matches_single_core(args.image),
                ),
                (
                    "generation dialog builds a request",
                    _check_generation_dialog_builds_a_request,
                ),
                (
                    "generate and benchmark end to end",
                    _check_generate_and_benchmark_end_to_end,
                ),
                ("calibration page object names", _check_calibration_page_object_names),
                (
                    "calibration runs and scores the real sample",
                    _check_calibration_runs_and_scores_the_real_sample,
                ),
                (
                    "a mismatched template fails calibration, not a false pass",
                    _check_a_mismatched_template_fails_calibration_not_a_false_pass,
                ),
                (
                    "a displaced template that still registers is not a false pass",
                    _check_a_displaced_template_that_registers_is_not_a_false_pass,
                ),
                (
                    "the overlay shows the sampled region, not the printed bubble",
                    _check_the_overlay_shows_the_sampled_region_not_the_printed_bubble,
                ),
                (
                    "a batch is recorded in the project database",
                    _check_a_batch_is_recorded_in_the_project_database,
                ),
                ("a partial run can be resumed", _check_a_partial_run_can_be_resumed),
                (
                    "originals are unchanged by processing",
                    _check_originals_are_unchanged_by_processing,
                ),
                (
                    "conflict review records a named decision",
                    _check_conflict_review_records_a_named_decision,
                ),
                (
                    "the audit ledger cannot be rewritten",
                    _check_the_audit_ledger_cannot_be_rewritten,
                ),
                (
                    "a correction needs a named reviewer",
                    _check_a_correction_needs_a_named_reviewer,
                ),
                (
                    "reconciliation classifies every case",
                    _check_reconciliation_classifies_every_case,
                ),
                (
                    "reconciliation keeps the machine and imported values",
                    _check_reconciliation_keeps_the_machine_and_imported_values,
                ),
                (
                    "a reconciliation decision needs a named operator",
                    _check_a_reconciliation_decision_needs_a_named_operator,
                ),
                (
                    "the sample candidate list can be saved",
                    _check_the_sample_candidate_list_can_be_saved,
                ),
                (
                    "no candidate data reaches the log",
                    _check_no_candidate_data_reaches_the_log,
                ),
                (
                    "scoring needs a verified answer key",
                    _check_scoring_needs_a_verified_key,
                ),
                (
                    "scoring produces defensible marks",
                    _check_scoring_produces_defensible_marks,
                ),
                (
                    "a rule change makes results stale",
                    _check_a_rule_change_makes_results_stale,
                ),
                (
                    "recomputation ignores a corrupted mark",
                    _check_recomputation_ignores_a_corrupted_mark,
                ),
                (
                    "a wrong question pays everyone",
                    _check_a_wrong_question_pays_everyone,
                ),
                (
                    "reports page lists the associated set",
                    _check_reports_page_lists_the_associated_set,
                ),
                (
                    "generating xlsx produces every row and rank formulas",
                    _check_generating_xlsx_produces_every_row_and_rank_formulas,
                ),
                (
                    "a second generation does not overwrite the first",
                    _check_a_second_generation_does_not_overwrite_the_first,
                ),
                ("no worker processes left behind", _check_no_worker_processes_are_left_behind),
            ]
        )
    else:
        print(f"[ skip ] scan checks: {SAMPLE_TEMPLATE} is not present", file=sys.stderr)

    failures = 0
    for name, check in checks:
        try:
            ok, detail = check()
        except Exception:
            # The traceback is the whole value of a smoke test failure; printing
            # it here is what saves a second run with more instrumentation.
            print(f"[ERROR] {name}")
            traceback.print_exc()
            failures += 1
            continue
        marker = "  ok  " if ok else "FAILED"
        print(f"[{marker}] {name}: {detail}")
        failures += 0 if ok else 1

    total = len(checks)
    print(f"\n{total - failures}/{total} checks passed")
    if failures:
        print("Run the full suite for detail: pytest -m gui", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""End-to-end GUI validation of the Scan workflow.

Scope:
    These tests drive :class:`~omr_scanner.gui.scan.page.ScanPage` the way a
    user drives it - through the same public commands the buttons are connected
    to - and assert against **program state**, not screenshots. They cover the
    ten workflows the Phase 3 brief enumerates:

    ===== ==========================================================
    Test  Workflow
    ===== ==========================================================
    A     Launch, navigation, reaching the Scan stage.
    B     Loading a template and what it enables.
    C     Importing a scan and previewing it.
    D     Processing one sheet, with the window staying responsive.
    E     Preview interaction: fit, zoom, overlays, selection stability.
    F     Batch processing, including one unreadable file mid-batch.
    G     Renaming a recognised sheet after its roll number.
    H     Two and three sheets claiming the *same* roll number.
    I     A name already present in the output directory.
    J     CSV export.
    ===== ==========================================================

Why the ``_prompt_*`` methods are never called:
    Each one owns a native modal file dialog and contains no logic - offscreen
    there is nothing to click and ``exec()`` would block forever. The command
    beside it (``load_template_from``, ``add_scan_paths``, ``export_csv_to``)
    does the work and is what these tests exercise, which is the policy
    ``docs/TESTING.md`` already sets for the rest of the GUI suite.

Why processing is awaited on a signal:
    :class:`~omr_scanner.gui.scan.worker.BatchWorker` is a real ``QThread``. A
    test that slept a fixed interval would be both slow and flaky, so every one
    of these waits on ``ScanPage.batch_finished`` - the same signal that tells
    the page itself the run is over.
"""

from __future__ import annotations

import csv
from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QCheckBox, QProgressBar, QPushButton, QTableView, QTableWidget
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.gui.scan.preview import ScanPreviewView
from omr_scanner.services import RecognitionOutcome, RegistrationStatus, save_template

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from omr_scanner.domain.template import OmrTemplate

pytestmark = pytest.mark.gui

BATCH_TIMEOUT_MS = 60_000
"""Generous, because it is a *ceiling*, not a delay.

Every wait returns as soon as the signal arrives; this only bounds how long a
genuinely stuck run is allowed to hang the suite."""

ROLL = "120317"
OTHER_ROLL = "120318"


def cell(table: QTableView, row: int, column: int) -> str:
    """The scan list's displayed text at (row, column).

    ``scan_table`` is a plain ``QTableView`` over ``ScanTableModel`` (Phase
    10, §"lazy GUI models") rather than a ``QTableWidget``, so there is no
    ``.item(row, column).text()`` - this reads the same text through the
    model directly.
    """
    value = table.model().index(row, column).data()
    return "" if value is None else str(value)


def row_count(table: QTableView) -> int:
    """How many rows the scan list's model currently reports."""
    return table.model().rowCount()


def sheet_marks(roll: str, *, set_code: str = "A", answer: str = "B") -> dict:
    """The marks for one fully-filled synthetic sheet."""
    return {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: set_code},
        "questions_0": dict.fromkeys(range(10), answer),
        "questions_1": dict.fromkeys(range(10), answer),
    }


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture
def silent_message_boxes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture every modal the page might raise, so a headless run cannot hang.

    Returns the ``(title, text)`` of each, so a test can assert that the user
    *was* told something rather than merely that nothing crashed.
    """
    shown: list[tuple[str, str]] = []

    def capture(_parent: object, title: str, text: str, *_args: object) -> None:
        shown.append((title, text))

    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(
            f"omr_scanner.gui.scan.page.QMessageBox.{name}", staticmethod(capture)
        )
    monkeypatch.setattr(
        "omr_scanner.gui.error_reporting.QMessageBox.warning", staticmethod(capture)
    )
    return shown


@pytest.fixture
def template() -> OmrTemplate:
    """The synthetic template every sheet in this module is rendered from."""
    return build_answer_sheet_template()


@pytest.fixture
def template_path(tmp_path: Path, template: OmrTemplate) -> Path:
    """The template written to disk, because the page loads from a file."""
    path = tmp_path / "templates" / "synthetic.omrt"
    path.parent.mkdir(parents=True, exist_ok=True)
    return save_template(template, path)


@pytest.fixture
def write_sheet(tmp_path: Path, template: OmrTemplate) -> Callable[..., Path]:
    """Factory rendering a marked sheet into the scans folder."""
    import cv2

    scans = tmp_path / "scans"
    scans.mkdir(parents=True, exist_ok=True)

    def write(
        name: str, roll: str = ROLL, marks: Mapping | None = None, **kwargs: str
    ) -> Path:
        image = render_marked_sheet(template, marks or sheet_marks(roll, **kwargs))
        path = scans / name
        cv2.imwrite(str(path), image)
        return path

    return write


@pytest.fixture
def page(qtbot, silent_message_boxes) -> ScanPage:
    """A bare Scan page, outside the main window, for the focused tests."""
    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    scan_page = ScanPage(spec)
    qtbot.addWidget(scan_page)
    return scan_page


@pytest.fixture
def loaded_page(page: ScanPage, template_path: Path) -> ScanPage:
    """A Scan page with the synthetic template already loaded."""
    assert page.load_template_from(template_path) is True
    return page


def process(qtbot, page: ScanPage, *, started: bool = True):
    """Run the page's batch and wait for it to finish, returning the report."""
    with qtbot.waitSignal(page.batch_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
        assert page.process_all() is started
    return blocker.args[0]


# ----------------------------------------------------------------------
# Test A - launch and navigation
# ----------------------------------------------------------------------
class TestALaunch:
    """The application starts and the Scan stage is reachable."""

    @pytest.fixture
    def window(self, qtbot, tmp_path: Path) -> MainWindow:
        from omr_scanner.config import AppConfig

        main_window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(main_window)
        return main_window

    def test_the_application_starts_with_every_workflow_stage(self, window: MainWindow):
        assert window.navigation.count() == len(WORKFLOW_PAGES)

    def test_step_one_and_step_two_are_both_visible_in_the_navigator(
        self, window: MainWindow
    ):
        labels = [window.navigation.item(row).text() for row in range(window.navigation.count())]
        assert any("Template" in text for text in labels), labels
        assert any("Scan" in text for text in labels), labels

    def test_navigating_to_scan_shows_the_real_page_not_a_placeholder(
        self, window: MainWindow
    ):
        row = next(i for i, spec in enumerate(WORKFLOW_PAGES) if spec.key == "scan")
        window.navigation.setCurrentRow(row)

        current = window.stack.currentWidget()
        assert isinstance(current, ScanPage)
        assert current.spec.key == "scan"
        assert current.spec.is_implemented
        assert current.objectName() == "scanPage"

    def test_the_scan_page_exposes_the_documented_object_names(self, window: MainWindow):
        row = next(i for i, spec in enumerate(WORKFLOW_PAGES) if spec.key == "scan")
        window.navigation.setCurrentRow(row)
        scan_page = window.stack.currentWidget()

        # The stable selectors the qtguitesting skill's scenario table lists.
        # Named rather than positional, so adding a control cannot silently
        # repoint a test at the wrong widget.
        for name, widget_type in (
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
        ):
            assert scan_page.findChild(widget_type, name) is not None, name


# ----------------------------------------------------------------------
# Test B - loading a template
# ----------------------------------------------------------------------
class TestBLoadTemplate:
    """A template must be loaded before anything else is possible."""

    def test_nothing_that_needs_a_template_is_enabled_before_one_is_loaded(
        self, page: ScanPage
    ):
        assert page.state.template is None
        assert page.add_scans_button.isEnabled() is False
        assert page.add_folder_button.isEnabled() is False
        assert page.process_all_button.isEnabled() is False
        assert page.export_csv_button.isEnabled() is False
        assert "No template" in page.template_name_label.text()

    def test_loading_a_template_shows_its_name_and_size(
        self, page: ScanPage, template_path: Path, template: OmrTemplate
    ):
        assert page.load_template_from(template_path) is True

        assert page.state.template is not None
        assert page.state.template_path == template_path
        text = page.template_name_label.text()
        assert template.name in text
        assert template_path.name in text
        assert "20 question(s)" in text

    def test_loading_a_template_enables_scan_import(
        self, page: ScanPage, template_path: Path
    ):
        page.load_template_from(template_path)

        assert page.add_scans_button.isEnabled() is True
        assert page.add_folder_button.isEnabled() is True
        # Still nothing to process or export - there are no scans yet.
        assert page.process_all_button.isEnabled() is False
        assert page.export_csv_button.isEnabled() is False

    def test_an_unreadable_template_is_reported_and_changes_nothing(
        self, page: ScanPage, tmp_path: Path, silent_message_boxes
    ):
        broken = tmp_path / "broken.omrt"
        broken.write_text("{not json", encoding="utf-8")

        assert page.load_template_from(broken) is False
        assert page.state.template is None
        assert silent_message_boxes, "the user must be told why it failed"


# ----------------------------------------------------------------------
# Test C - importing scans
# ----------------------------------------------------------------------
class TestCImportScan:
    """Files and folders reach the scan list; unrelated files do not."""

    def test_a_single_scan_appears_in_the_list_and_is_selected(
        self, loaded_page: ScanPage, write_sheet
    ):
        path = write_sheet("IMG_0001.png")

        assert loaded_page.add_scan_paths([path]) == 1
        assert row_count(loaded_page.scan_table) == 1
        assert cell(loaded_page.scan_table, 0, 0) == "IMG_0001.png"
        assert cell(loaded_page.scan_table, 0, 3) == "Pending"
        assert loaded_page.scan_table.currentIndex().row() == 0

    def test_importing_the_same_file_twice_does_not_duplicate_the_row(
        self, loaded_page: ScanPage, write_sheet
    ):
        path = write_sheet("IMG_0001.png")
        loaded_page.add_scan_paths([path])

        assert loaded_page.add_scan_paths([path]) == 0
        assert row_count(loaded_page.scan_table) == 1

    def test_a_folder_contributes_its_images_in_natural_order(
        self, loaded_page: ScanPage, write_sheet, tmp_path: Path
    ):
        for name in ("scan1.png", "scan2.png", "scan3.png", "scan10.png"):
            write_sheet(name)
        # A file the importer must ignore rather than try to decode.
        (tmp_path / "scans" / "notes.txt").write_text("not a scan", encoding="utf-8")

        added = loaded_page.add_scan_paths([tmp_path / "scans"])

        assert added == 4
        names = [entry.path.name for entry in loaded_page.state.entries]
        assert names == ["scan1.png", "scan2.png", "scan3.png", "scan10.png"]

    def test_selecting_a_scan_reports_it_and_says_it_is_unprocessed(
        self, loaded_page: ScanPage, write_sheet, qtbot
    ):
        loaded_page.add_scan_paths([write_sheet("a.png"), write_sheet("b.png")])

        with qtbot.waitSignal(loaded_page.scan_selected, timeout=5_000) as blocker:
            loaded_page.select_scan(1)

        assert blocker.args == [1]
        assert loaded_page.scan_table.currentIndex().row() == 1
        # Nothing has been recognised, so the page must say so rather than
        # showing a stale or invented preview.
        assert "Not processed" in loaded_page.preview_status_label.text()
        assert loaded_page.preview.has_page is False

    def test_clearing_the_list_empties_the_table_and_the_preview(
        self, loaded_page: ScanPage, write_sheet
    ):
        loaded_page.add_scan_paths([write_sheet("a.png")])

        loaded_page.clear_scans()

        assert row_count(loaded_page.scan_table) == 0
        assert loaded_page.state.entries == []
        assert loaded_page.preview.has_page is False


# ----------------------------------------------------------------------
# Test D - processing one sheet
# ----------------------------------------------------------------------
class TestDProcess:
    """One sheet goes through the pipeline and its values reach the panel."""

    @pytest.fixture
    def processed_page(self, qtbot, loaded_page: ScanPage, write_sheet) -> ScanPage:
        loaded_page.add_scan_paths([write_sheet("IMG_0001.png")])
        process(qtbot, loaded_page)
        return loaded_page

    def test_processing_completes_and_reports_a_registration_status(
        self, processed_page: ScanPage
    ):
        entry = processed_page.state.entries[0]
        assert entry.processed is not None
        result = entry.processed.result
        # More than one marker-like shape falls inside a corner search region,
        # so the honest status is "registered, with a reservation" rather than a
        # clean "registered" - and the reservation is *named* rather than
        # hidden. This is not an artefact of the synthetic fixture: the real
        # scan, examples/ECE-0000.png, reports exactly the same warning, because
        # printed sheets genuinely carry other dark rectangles near their
        # corners. What matters is that the chosen four still rectified the page
        # correctly, which the recognised values below demonstrate.
        assert result.registration is RegistrationStatus.REGISTERED_WITH_WARNING
        assert result.warnings == ("MULTIPLE_CORNER_CANDIDATES",)
        assert "multiple corner candidates" in result.registration_message
        # A reservation about the geometry is not a reservation about the
        # reading: every bubble was still measured cleanly.
        assert entry.outcome == RecognitionOutcome.COMPLETE.value

    def test_the_scan_list_shows_the_roll_the_set_code_and_the_status(
        self, processed_page: ScanPage
    ):
        assert cell(processed_page.scan_table, 0, 1) == ROLL
        assert cell(processed_page.scan_table, 0, 2) == "A"
        assert cell(processed_page.scan_table, 0, 3) == "Complete"

    def test_the_results_panel_is_populated_with_every_question(
        self, processed_page: ScanPage
    ):
        processed_page.select_scan(0)

        assert processed_page.answers_table.rowCount() == 20
        assert processed_page.answers_table.item(0, 0).text() == "1"
        assert processed_page.answers_table.item(0, 1).text() == "B"
        # The identifier and the set code are fields, not questions.
        labels = [
            processed_page.fields_table.item(row, 0).text()
            for row in range(processed_page.fields_table.rowCount())
        ]
        values = [
            processed_page.fields_table.item(row, 1).text()
            for row in range(processed_page.fields_table.rowCount())
        ]
        assert ROLL in values, labels
        assert "A" in values, labels

    def test_the_window_stayed_responsive_while_the_worker_ran(
        self, qtbot, loaded_page: ScanPage, write_sheet
    ):
        # Responsiveness is "the GUI thread kept processing events", which is
        # exactly what a queued signal arriving mid-run demonstrates: the
        # progress callbacks below are delivered on the main thread while the
        # worker thread is still inside OpenCV.
        loaded_page.add_scan_paths(
            [write_sheet("a.png"), write_sheet("b.png"), write_sheet("c.png")]
        )
        seen: list[str] = []
        loaded_page.progress_bar.valueChanged.connect(
            lambda _value: seen.append(loaded_page.progress_label.text())
        )

        process(qtbot, loaded_page)

        assert seen, "no progress reached the GUI thread during the run"
        assert any("Processing" in text for text in seen)
        assert loaded_page.progress_bar.value() == loaded_page.progress_bar.maximum()

    def test_the_controls_come_back_after_a_run(self, processed_page: ScanPage):
        assert processed_page.process_all_button.isEnabled() is True
        assert processed_page.reprocess_button.isEnabled() is True
        assert processed_page.export_csv_button.isEnabled() is True
        assert processed_page.cancel_button.isEnabled() is False

    def test_reprocessing_discards_the_previous_results_first(
        self, qtbot, processed_page: ScanPage
    ):
        with qtbot.waitSignal(processed_page.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert processed_page.reprocess_all() is True

        entry = processed_page.state.entries[0]
        assert entry.processed is not None
        assert entry.identifier == ROLL

    def test_processing_without_a_template_does_not_start(self, page: ScanPage):
        page.state.entries.clear()
        assert page.process_all() is False


# ----------------------------------------------------------------------
# Test E - preview interaction
# ----------------------------------------------------------------------
class TestEPreview:
    """Zoom, fit, pan and the overlay toggles behave, and selection is stable."""

    @pytest.fixture
    def previewed(self, qtbot, loaded_page: ScanPage, write_sheet) -> ScanPage:
        loaded_page.add_scan_paths([write_sheet("IMG_0001.png"), write_sheet("IMG_0002.png")])
        process(qtbot, loaded_page)
        loaded_page.select_scan(0)
        # The batch discards previews; the page re-renders the selected one in
        # a PreviewWorker, so wait for a page to actually arrive.
        qtbot.waitUntil(lambda: loaded_page.preview.has_page, timeout=BATCH_TIMEOUT_MS)
        return loaded_page

    def test_the_rectified_page_is_shown_at_the_templates_canonical_size(
        self, previewed: ScanPage, template: OmrTemplate
    ):
        assert previewed.preview.has_page is True
        assert str(template.page.canonical_width_px) in previewed.preview_status_label.text()
        assert "registered" in previewed.preview_status_label.text()

    def test_fit_to_window_then_zoom_in_and_out_changes_the_scale(
        self, previewed: ScanPage
    ):
        previewed.preview.fit_to_window()
        fitted = previewed.preview.zoom

        previewed.preview.zoom_in()
        zoomed_in = previewed.preview.zoom
        previewed.preview.zoom_out()
        back = previewed.preview.zoom

        assert zoomed_in > fitted
        assert back == pytest.approx(fitted, rel=1e-3)

    def test_actual_size_is_one_to_one(self, previewed: ScanPage):
        previewed.preview.zoom_to_actual_size()
        assert previewed.preview.zoom == pytest.approx(1.0, rel=1e-3)

    def test_panning_with_the_middle_button_moves_the_view_not_the_page(self, previewed):
        from PySide6.QtTest import QTest

        previewed.preview.zoom_to_actual_size()
        viewport = previewed.preview.viewport()
        before = previewed.preview.horizontalScrollBar().value()

        start = QPoint(viewport.width() // 2, viewport.height() // 2)
        QTest.mousePress(viewport, Qt.MouseButton.MiddleButton, pos=start)
        QTest.mouseMove(viewport, start - QPoint(60, 0))
        QTest.mouseRelease(
            viewport, Qt.MouseButton.MiddleButton, pos=start - QPoint(60, 0)
        )

        # The scroll position moved; the zoom - i.e. the image itself - did not.
        assert previewed.preview.horizontalScrollBar().value() != before
        assert previewed.preview.zoom == pytest.approx(1.0, rel=1e-3)

    def test_reselecting_the_same_scan_does_not_start_a_second_preview_worker(
        self, previewed: ScanPage
    ):
        """A late duplicate worker would re-fit the view and undo a user's zoom.

        Finishing a batch re-selects the current row by itself, so a user who
        also clicks that row produces two workers for one file; the second
        result arrives after they have zoomed in and resets the view.
        """
        previewed.preview.zoom_to_actual_size()
        zoomed = previewed.preview.zoom

        # Select the same row again while its preview is being rendered.
        previewed._preview_cache.clear()
        previewed.select_scan(0)
        first = previewed._preview_worker
        previewed.select_scan(0)

        assert previewed._preview_worker is first, "a second worker was started"
        assert previewed.preview.zoom == pytest.approx(zoomed, rel=1e-3)

    def test_the_overlay_toggles_do_not_disturb_the_page(self, previewed: ScanPage):
        zoom_before = previewed.preview.zoom

        previewed.zones_checkbox.setChecked(False)
        previewed.bubbles_checkbox.setChecked(False)
        previewed.empty_checkbox.setChecked(True)
        previewed.zones_checkbox.setChecked(True)
        previewed.bubbles_checkbox.setChecked(True)

        assert previewed.preview.has_page is True
        assert previewed.preview.zoom == pytest.approx(zoom_before, rel=1e-3)

    def test_stepping_next_and_previous_returns_to_the_same_scan(
        self, previewed: ScanPage
    ):
        assert previewed.scan_table.currentIndex().row() == 0

        previewed.select_next()
        assert previewed.scan_table.currentIndex().row() == 1
        previewed.select_previous()
        assert previewed.scan_table.currentIndex().row() == 0

        # And neither end runs off the list.
        previewed.select_previous()
        assert previewed.scan_table.currentIndex().row() == 0
        previewed.select_next()
        previewed.select_next()
        assert previewed.scan_table.currentIndex().row() == 1


# ----------------------------------------------------------------------
# Test F - batch processing
# ----------------------------------------------------------------------
class TestFBatch:
    """Many sheets at once, and one bad file that must not stop the rest."""

    def test_every_imported_scan_is_listed_and_processed(
        self, qtbot, loaded_page: ScanPage, write_sheet
    ):
        names = ["scan1.png", "scan2.png", "scan3.png", "scan10.png"]
        loaded_page.add_scan_paths([write_sheet(name) for name in names])

        report = process(qtbot, loaded_page)

        assert row_count(loaded_page.scan_table) == len(names)
        assert report.total == len(names)
        assert report.complete_count == len(names)
        assert all(entry.processed is not None for entry in loaded_page.state.entries)
        assert all(
            cell(loaded_page.scan_table, row, 3) == "Complete"
            for row in range(row_count(loaded_page.scan_table))
        )

    def test_one_corrupt_file_is_flagged_and_the_batch_continues(
        self, qtbot, loaded_page: ScanPage, write_sheet, tmp_path: Path
    ):
        good_a = write_sheet("good_a.png")
        corrupt = tmp_path / "scans" / "corrupt.png"
        corrupt.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not an image")
        good_b = write_sheet("good_b.png")
        loaded_page.add_scan_paths([good_a, corrupt, good_b])

        report = process(qtbot, loaded_page)

        assert report.total == 3
        assert report.complete_count == 2, "both good sheets must still be read"
        by_name = {item.source_path.name: item for item in report.processed}
        assert by_name["corrupt.png"].outcome in {
            RecognitionOutcome.ERROR,
            RecognitionOutcome.REGISTRATION_FAILED,
        }
        assert by_name["good_a.png"].result.identifier_value == ROLL
        assert by_name["good_b.png"].result.identifier_value == ROLL

    def test_the_failed_row_says_so_in_plain_language(
        self, qtbot, loaded_page: ScanPage, write_sheet, tmp_path: Path
    ):
        corrupt = tmp_path / "scans" / "corrupt.png"
        corrupt.write_bytes(b"nonsense")
        loaded_page.add_scan_paths([write_sheet("good.png"), corrupt])

        process(qtbot, loaded_page)

        statuses = {
            cell(loaded_page.scan_table, row, 0): cell(loaded_page.scan_table, row, 3)
            for row in range(row_count(loaded_page.scan_table))
        }
        assert statuses["good.png"] == "Complete"
        assert statuses["corrupt.png"] in {"Error", "Registration failed"}

        # And the summary counts it rather than hiding it - in the outcome
        # line, which is where the success/review/failure tallies live.
        assert "Failed 1" in loaded_page.progress_outcome_label.text()

    def test_processing_only_the_selected_rows_leaves_the_others_pending(
        self, qtbot, loaded_page: ScanPage, write_sheet
    ):
        loaded_page.add_scan_paths(
            [write_sheet("a.png"), write_sheet("b.png"), write_sheet("c.png")]
        )
        loaded_page.scan_table.selectRow(1)

        with qtbot.waitSignal(loaded_page.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert loaded_page.process_selected() is True

        assert loaded_page.state.entries[0].processed is None
        assert loaded_page.state.entries[1].processed is not None
        assert loaded_page.state.entries[2].processed is None


# ----------------------------------------------------------------------
# Test G - renaming by roll number
# ----------------------------------------------------------------------
class TestGRenaming:
    """The optional roll-number rename, and what it refuses to do."""

    def test_renaming_is_off_by_default(self, loaded_page: ScanPage):
        assert loaded_page.rename_checkbox.isChecked() is False
        assert loaded_page.state.rename_enabled is False

    def test_without_renaming_nothing_is_written_and_no_name_is_shown(
        self, qtbot, loaded_page: ScanPage, write_sheet
    ):
        loaded_page.add_scan_paths([write_sheet("IMG_0034.png")])

        report = process(qtbot, loaded_page)

        assert report.written_count == 0
        assert loaded_page.state.entries[0].output_name == ""

    def test_a_recognised_sheet_is_copied_under_its_roll_number(
        self, qtbot, loaded_page: ScanPage, write_sheet, tmp_path: Path
    ):
        output = tmp_path / "output"
        source = write_sheet("IMG_0034.png")
        loaded_page.add_scan_paths([source])
        loaded_page.set_output_directory(output)
        loaded_page.rename_checkbox.setChecked(True)

        process(qtbot, loaded_page)

        assert (output / f"{ROLL}.png").exists()
        assert cell(loaded_page.scan_table, 0, 4) == f"{ROLL}.png"
        # The original is never moved or altered.
        assert source.exists()

    def test_the_original_extension_is_preserved(
        self, qtbot, loaded_page: ScanPage, template: OmrTemplate, tmp_path: Path
    ):
        import cv2

        scans = tmp_path / "scans"
        scans.mkdir(parents=True, exist_ok=True)
        jpeg = scans / "IMG_0034.jpg"
        cv2.imwrite(str(jpeg), render_marked_sheet(template, sheet_marks(ROLL)))

        output = tmp_path / "output"
        loaded_page.add_scan_paths([jpeg])
        loaded_page.set_output_directory(output)
        loaded_page.rename_checkbox.setChecked(True)

        process(qtbot, loaded_page)

        assert (output / f"{ROLL}.jpg").exists()
        assert not (output / f"{ROLL}.png").exists()

    def test_renaming_without_an_output_folder_asks_rather_than_guessing(
        self, loaded_page: ScanPage, write_sheet, silent_message_boxes
    ):
        loaded_page.add_scan_paths([write_sheet("IMG_0034.png")])
        loaded_page.rename_checkbox.setChecked(True)

        assert loaded_page.process_all() is False
        assert any("Output folder" in title for title, _text in silent_message_boxes)

    def test_an_unreliable_roll_number_is_not_used_as_a_file_name(
        self, qtbot, loaded_page: ScanPage, write_sheet, tmp_path: Path
    ):
        # A sheet whose third digit column was left blank: the roll number is
        # incomplete, so naming a file "12_317" would be a fabrication.
        marks = sheet_marks(ROLL)
        del marks["roll_number"][2]
        output = tmp_path / "output"
        loaded_page.add_scan_paths([write_sheet("IMG_0099.png", marks=marks)])
        loaded_page.set_output_directory(output)
        loaded_page.rename_checkbox.setChecked(True)

        process(qtbot, loaded_page)

        name = loaded_page.state.entries[0].output_name
        assert name == "UNRESOLVED_001.png"
        assert (output / name).exists()
        assert not list(output.glob("12*.png"))
        assert "reliably" in loaded_page.state.entries[0].processed.message


# ----------------------------------------------------------------------
# Test H - duplicate roll numbers
# ----------------------------------------------------------------------
class TestHDuplicateRolls:
    """Three sheets claiming one roll number; none may overwrite another."""

    @pytest.fixture
    def three_duplicates(
        self, qtbot, loaded_page: ScanPage, write_sheet, tmp_path: Path
    ) -> tuple[ScanPage, Path]:
        output = tmp_path / "output"
        loaded_page.add_scan_paths(
            [
                write_sheet("IMG_001.png"),
                write_sheet("IMG_002.png"),
                write_sheet("IMG_003.png"),
            ]
        )
        loaded_page.set_output_directory(output)
        loaded_page.rename_checkbox.setChecked(True)
        process(qtbot, loaded_page)
        return loaded_page, output

    def test_the_three_files_get_the_documented_suffix_sequence(self, three_duplicates):
        _page, output = three_duplicates

        assert (output / f"{ROLL}.png").exists()
        assert (output / f"{ROLL}_a.png").exists()
        assert (output / f"{ROLL}_b.png").exists()

    def test_nothing_was_overwritten(self, three_duplicates):
        _page, output = three_duplicates

        written = sorted(item.name for item in output.iterdir())
        assert written == [f"{ROLL}.png", f"{ROLL}_a.png", f"{ROLL}_b.png"]
        # Three distinct files, one per scan - not one file written three times.
        assert len(written) == 3

    def test_the_scan_list_shows_each_output_name(self, three_duplicates):
        page, _output = three_duplicates

        names = [cell(page.scan_table, row, 4) for row in range(3)]
        assert names == [f"{ROLL}.png", f"{ROLL}_a.png", f"{ROLL}_b.png"]
        # And every row still reports the same recognised roll, because the
        # suffix is a *file naming* decision, not a changed reading.
        assert [cell(page.scan_table, row, 1) for row in range(3)] == [ROLL] * 3

    def test_a_different_roll_in_the_same_batch_keeps_its_own_plain_name(
        self, qtbot, loaded_page: ScanPage, write_sheet, tmp_path: Path
    ):
        output = tmp_path / "output"
        loaded_page.add_scan_paths(
            [
                write_sheet("a.png", roll=ROLL),
                write_sheet("b.png", roll=OTHER_ROLL),
                write_sheet("c.png", roll=ROLL),
            ]
        )
        loaded_page.set_output_directory(output)
        loaded_page.rename_checkbox.setChecked(True)

        process(qtbot, loaded_page)

        assert (output / f"{ROLL}.png").exists()
        assert (output / f"{OTHER_ROLL}.png").exists()
        assert (output / f"{ROLL}_a.png").exists()
        assert not (output / f"{OTHER_ROLL}_a.png").exists()


# ----------------------------------------------------------------------
# Test I - collision with a file already in the output folder
# ----------------------------------------------------------------------
class TestIExistingFileCollision:
    """A name already on disk is as taken as one issued this session."""

    def test_an_existing_output_file_pushes_the_new_scan_to_the_a_suffix(
        self, qtbot, loaded_page: ScanPage, write_sheet, tmp_path: Path
    ):
        output = tmp_path / "output"
        output.mkdir(parents=True)
        pre_existing = output / f"{ROLL}.png"
        pre_existing.write_bytes(b"an earlier run's file")
        original_bytes = pre_existing.read_bytes()

        loaded_page.add_scan_paths([write_sheet("IMG_001.png")])
        loaded_page.set_output_directory(output)
        loaded_page.rename_checkbox.setChecked(True)

        process(qtbot, loaded_page)

        assert (output / f"{ROLL}_a.png").exists()
        assert pre_existing.read_bytes() == original_bytes, "the earlier file was overwritten"
        assert cell(loaded_page.scan_table, 0, 4) == f"{ROLL}_a.png"

    def test_a_run_continues_past_whatever_suffixes_already_exist(
        self, qtbot, loaded_page: ScanPage, write_sheet, tmp_path: Path
    ):
        output = tmp_path / "output"
        output.mkdir(parents=True)
        for name in (f"{ROLL}.png", f"{ROLL}_a.png", f"{ROLL}_b.png"):
            (output / name).write_bytes(b"earlier")

        loaded_page.add_scan_paths([write_sheet("IMG_001.png")])
        loaded_page.set_output_directory(output)
        loaded_page.rename_checkbox.setChecked(True)

        process(qtbot, loaded_page)

        assert (output / f"{ROLL}_c.png").exists()
        assert all(
            (output / name).read_bytes() == b"earlier"
            for name in (f"{ROLL}.png", f"{ROLL}_a.png", f"{ROLL}_b.png")
        )


# ----------------------------------------------------------------------
# Test J - CSV export
# ----------------------------------------------------------------------
class TestJCsvExport:
    """The batch leaves the application as a CSV a spreadsheet can read."""

    @pytest.fixture
    def exported(
        self, qtbot, loaded_page: ScanPage, write_sheet, tmp_path: Path
    ) -> tuple[ScanPage, Path]:
        output = tmp_path / "output"
        loaded_page.add_scan_paths(
            [write_sheet("IMG_001.png", roll=ROLL), write_sheet("IMG_002.png", roll=ROLL)]
        )
        loaded_page.set_output_directory(output)
        loaded_page.rename_checkbox.setChecked(True)
        process(qtbot, loaded_page)

        destination = tmp_path / "results.csv"
        written = loaded_page.export_csv_to(destination)
        assert written == destination
        return loaded_page, destination

    def read_rows(self, path: Path) -> list[list[str]]:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.reader(handle))

    def test_the_file_exists_and_has_one_row_per_scan(self, exported):
        _page, path = exported
        rows = self.read_rows(path)

        assert path.exists()
        assert len(rows) == 3, "a header and two scans"

    def test_the_header_is_the_documented_column_order(self, exported):
        _page, path = exported
        header = self.read_rows(path)[0]

        # The Phase 3 columns keep their positions. Phase 6 appended
        # `value_source` and `unresolved_conflicts` *after* them, deliberately,
        # so a consumer that indexes the original seven positionally still
        # works.
        assert header[:7] == [
            "original_filename",
            "output_filename",
            "roll",
            "set_code",
            "registration_status",
            "recognition_status",
            "warning_count",
        ]
        assert header[7:9] == ["value_source", "unresolved_conflicts"]
        assert header[9:] == [f"Q{number}" for number in range(1, 21)]

    def test_an_unreviewed_export_says_the_values_are_the_machines(self, exported):
        # The point of the column: a row nobody has reviewed must not be
        # indistinguishable from one a human confirmed.
        _page, path = exported
        rows = self.read_rows(path)
        record = dict(zip(rows[0], rows[1], strict=True))
        assert record["value_source"] == "machine"
        assert record["unresolved_conflicts"] == "0"


    def test_the_roll_the_set_code_and_the_answers_are_correct(self, exported):
        _page, path = exported
        rows = self.read_rows(path)
        header, first = rows[0], rows[1]
        record = dict(zip(header, first, strict=True))

        assert record["original_filename"] == "IMG_001.png"
        assert record["roll"] == ROLL
        assert record["set_code"] == "A"
        # See TestDProcess: the synthetic page's decoy corner squares make this
        # "registered_with_warning", and the CSV reports that rather than
        # flattening it to a clean pass.
        assert record["registration_status"] == "registered_with_warning"
        assert record["recognition_status"] == "complete"
        assert record["warning_count"] == "1"
        assert [record[f"Q{n}"] for n in range(1, 21)] == ["B"] * 20

    def test_the_duplicate_renamed_output_names_are_recorded(self, exported):
        _page, path = exported
        rows = self.read_rows(path)

        assert rows[1][1] == f"{ROLL}.png"
        assert rows[2][1] == f"{ROLL}_a.png"

    def test_exporting_the_same_batch_twice_produces_the_same_bytes(
        self, exported, tmp_path: Path
    ):
        page, first = exported
        second = page.export_csv_to(tmp_path / "again.csv")

        assert second is not None
        assert second.read_bytes() == first.read_bytes()

    def test_exporting_with_nothing_processed_tells_the_user_instead_of_writing(
        self, loaded_page: ScanPage, write_sheet, tmp_path: Path, silent_message_boxes
    ):
        loaded_page.add_scan_paths([write_sheet("a.png")])

        result = loaded_page.export_csv_to(tmp_path / "empty.csv")

        assert result is None
        assert not (tmp_path / "empty.csv").exists()
        assert any("Nothing to export" in title for title, _text in silent_message_boxes)

    def test_a_missing_suffix_is_supplied(self, exported, tmp_path: Path):
        page, _first = exported

        written = page.export_csv_to(tmp_path / "no_suffix")

        assert written is not None
        assert written.suffix == ".csv"


# ----------------------------------------------------------------------
# Test K - the Processing section's layout (readability/robustness pass)
# ----------------------------------------------------------------------
def _lay_out(page: ScanPage, *, width: int, height: int) -> None:
    """Give ``page`` real laid-out geometry at this size, without a visible window.

    Geometry is only propagated to children on a show/resize cycle - a
    scroll area or splitter keeps its default sizes otherwise - so a test that
    reads a child's height has to ask for one. ``WA_DontShowOnScreen`` runs
    the whole layout path without mapping a window onto the developer's
    desktop. Same technique as
    ``tests/gui/test_template_designer_toolbar.py``'s ``_lay_out``.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    page.show()
    page.resize(width, height)
    for _ in range(3):
        QApplication.processEvents()


# Window heights to exercise the control column at: a roomy desktop, a
# standard laptop panel, and shorter still - the range over which the column
# used to have no choice but to compress its widgets before it had its own
# scroll area. Width is held at a normal value throughout; this defect was
# about vertical space, not horizontal.
PROCESSING_TEST_HEIGHTS = [1000, 900, 768, 700, 620]

_PROCESSING_BUTTON_NAMES = [
    "process_all_button",
    "process_selected_button",
    "resume_button",
    "retry_failed_button",
    "reprocess_button",
    "cancel_button",
    "review_button",
]


class TestKProcessingSectionLayout:
    """The Processing group's buttons keep their full size and never overlap.

    Root cause of the reported clipping/overlap: the left-hand control column
    (Template + Scans + Processing + Output, stacked with no scroll area) had
    a combined minimum height of roughly 900 logical pixels - nearly half of
    it the Processing group's seven buttons, status line and progress readout
    - and that became the *whole page's* minimum height. Any window shorter
    than that left Qt nothing to do but compress every widget in the column
    below its own size hint. The column now lives in its own ``QScrollArea``,
    so it is laid out at full size regardless of the window, and a short
    window scrolls instead.

    These assert the structural contract (real geometry, no overlap, no
    compression below size hint) rather than pixel positions, which
    ``docs/TESTING.md`` rules out for GUI tests - the same style
    ``test_template_designer_toolbar.py`` uses for its own layout-polish pass.
    """

    def _buttons(self, page: ScanPage) -> list[QPushButton]:
        return [getattr(page, name) for name in _PROCESSING_BUTTON_NAMES]

    @pytest.mark.parametrize("height", PROCESSING_TEST_HEIGHTS)
    def test_every_button_has_real_positive_geometry(self, page: ScanPage, height: int):
        _lay_out(page, width=1600, height=height)
        for button in self._buttons(page):
            assert button.width() > 0 and button.height() > 0, button.objectName()

    @pytest.mark.parametrize("height", PROCESSING_TEST_HEIGHTS)
    def test_no_two_processing_buttons_overlap(self, page: ScanPage, height: int):
        _lay_out(page, width=1600, height=height)
        buttons = self._buttons(page)
        for i, a in enumerate(buttons):
            for b in buttons[i + 1 :]:
                assert not a.geometry().intersects(b.geometry()), (
                    f"{a.objectName()} overlaps {b.objectName()} at height={height}"
                )

    @pytest.mark.parametrize("height", PROCESSING_TEST_HEIGHTS)
    def test_no_button_is_compressed_below_its_own_size_hint(
        self, page: ScanPage, height: int
    ):
        # The assertion that actually distinguishes "scrolls" from "squeezes":
        # at every one of these heights, down to a window shorter than the
        # column's own content, a button must render at exactly the size it
        # asked for - never smaller.
        _lay_out(page, width=1600, height=height)
        for button in self._buttons(page):
            assert button.geometry().height() >= button.sizeHint().height(), (
                f"{button.objectName()} was compressed at height={height}: "
                f"{button.geometry().height()}px < sizeHint "
                f"{button.sizeHint().height()}px"
            )

    def test_the_status_line_does_not_collide_with_the_first_button(self, page: ScanPage):
        _lay_out(page, width=1600, height=620)
        assert not page.workers_label.geometry().intersects(
            page.process_all_button.geometry()
        )

    def test_the_footer_warning_does_not_collide_with_the_last_button(self, page: ScanPage):
        _lay_out(page, width=1600, height=620)
        assert not page.review_button.geometry().intersects(
            page.batch_state_label.geometry()
        )

    def test_a_short_window_scrolls_the_column_instead_of_squeezing_it(
        self, page: ScanPage
    ):
        from PySide6.QtWidgets import QScrollArea

        _lay_out(page, width=1600, height=620)
        scroll_area = page.findChild(QScrollArea, "scanControlScrollArea")
        assert scroll_area is not None
        assert scroll_area.widgetResizable() is True

    def test_the_page_survives_being_resized_repeatedly(self, page: ScanPage):
        # Grow, shrink, grow again - the sequence a user resizing or
        # maximising/restoring a real window produces.
        for width, height in [(1600, 1000), (1280, 620), (1920, 1080), (1366, 768)]:
            _lay_out(page, width=width, height=height)
        for button in self._buttons(page):
            assert button.isVisible()
            assert button.geometry().height() >= button.sizeHint().height()

    def test_processing_button_signals_still_reach_their_handlers(
        self, qtbot, loaded_page: ScanPage, write_sheet
    ):
        # The layout rebuild must not have duplicated or dropped a connection.
        # Process All is the one action safe to actually invoke here; the
        # others' handlers are exercised end-to-end by TestD-TestJ above, and
        # this only guards that rebuilding the container did not change *how*
        # a click reaches the handler it always reached.
        loaded_page.add_scan_paths([write_sheet("IMG_0001.png")])
        assert loaded_page.process_all_button.isEnabled()
        with qtbot.waitSignal(loaded_page.batch_finished, timeout=BATCH_TIMEOUT_MS):
            loaded_page.process_all_button.click()

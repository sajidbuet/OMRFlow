"""GUI validation of the developer testing tools.

Scope:
    The two things Tools > Developer / Testing offers - generating a labelled
    synthetic dataset, and scoring recognition against one - driven through the
    same public commands the menu items are connected to.

    ===== ==========================================================
    Test  Workflow
    ===== ==========================================================
    A     The menu exists and reaches both commands.
    B     The generation dialog collects a complete, valid request.
    C     Generating really writes a dataset, with progress and a summary.
    D     Cancelling generation stops early and keeps what was written.
    E     Benchmark mode loads a dataset, banners it, and scores a run.
    F     The results dialog shows the categories and reaches a failing scan.
    G     A second run is compared against the first.
    ===== ==========================================================

Why the dialogs are constructed rather than ``exec``-ed:
    ``exec()`` blocks on a modal event loop that nothing offscreen will ever
    dismiss. Every dialog here is built, driven through its own methods and
    read - the same policy ``docs/TESTING.md`` sets for the rest of the suite.

Why benchmark scoring is awaited on a signal:
    A benchmark runs through the real batch worker. ``benchmark_finished``
    fires when a run has been scored, so a test waits on that instead of
    sleeping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMenu, QTableWidget
from tests.conftest import build_answer_sheet_template

from omr_scanner.evaluation.session import REPORT_DIRNAME, BenchmarkSession
from omr_scanner.evaluation.synthetic_dataset import (
    GROUND_TRUTH_DIRNAME,
    IMAGES_DIRNAME,
    MANIFEST_FILENAME,
    CaseFamily,
    DatasetProfile,
    ImageFormat,
    generate_dataset,
)
from omr_scanner.gui.devtools import (
    BenchmarkResultsDialog,
    DatasetWorker,
    GenerateDatasetDialog,
    GenerationRequest,
)
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.services import save_template

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.domain.template import OmrTemplate

pytestmark = pytest.mark.gui

GENERATION_TIMEOUT_MS = 120_000
BATCH_TIMEOUT_MS = 120_000
"""Ceilings, not delays: every wait returns as soon as its signal arrives."""


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
def silent_message_boxes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture every modal these commands might raise."""
    shown: list[tuple[str, str]] = []

    def capture(_parent: object, title: str, text: str, *_args: object) -> None:
        shown.append((title, text))

    for module in (
        "omr_scanner.gui.scan.page",
        "omr_scanner.gui.devtools.generate_dialog",
    ):
        for name in ("warning", "information", "critical"):
            monkeypatch.setattr(f"{module}.QMessageBox.{name}", staticmethod(capture))
    monkeypatch.setattr(
        "omr_scanner.gui.error_reporting.QMessageBox.warning", staticmethod(capture)
    )
    return shown


@pytest.fixture
def dataset(tmp_path: Path, template: OmrTemplate) -> Path:
    """A small labelled dataset on disk, for the benchmark tests."""
    out = tmp_path / "dataset"
    generate_dataset(
        out, template, count=4, seed=4242, profile=DatasetProfile.BASELINE, name="fixture"
    )
    return out


@pytest.fixture
def page(qtbot, silent_message_boxes) -> ScanPage:
    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    scan_page = ScanPage(spec)
    scan_page.benchmark_auto_show = False  # no modal in a headless run
    qtbot.addWidget(scan_page)
    return scan_page


# ----------------------------------------------------------------------
# A. The menu
# ----------------------------------------------------------------------
class TestTheDeveloperMenu:
    def test_tools_menu_offers_both_developer_commands(self, qtbot, tmp_path: Path):
        window = MainWindow(config_path=tmp_path / "config.json")
        qtbot.addWidget(window)

        titles = [menu.title() for menu in window.menuBar().findChildren(QMenu)]
        assert any("Tools" in title for title in titles)
        assert any("Developer" in title for title in titles)
        assert window.generate_dataset_action.text().startswith("&Generate")
        assert window.run_benchmark_action.text().startswith("Run Recognition")

    def test_the_commands_are_always_available(self, qtbot, tmp_path: Path):
        # Not gated on an open project: a developer testing recognition has no
        # examination to open, and a disabled menu item with no explanation is
        # how a testing tool becomes invisible.
        window = MainWindow(config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        assert window.generate_dataset_action.isEnabled()
        assert window.run_benchmark_action.isEnabled()


# ----------------------------------------------------------------------
# B. The generation dialog
# ----------------------------------------------------------------------
class TestTheGenerationDialog:
    def test_it_builds_a_complete_request(self, qtbot, tmp_path: Path, template_path: Path):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.count_spin.setValue(12)
        dialog.seed_spin.setValue(7)
        dialog.profile_combo.setCurrentIndex(
            dialog.profile_combo.findData(DatasetProfile.RECOGNITION)
        )

        request = dialog.request()
        assert request is not None
        assert request.template_path == template_path
        assert request.output_dir == tmp_path
        assert request.count == 12
        assert request.seed == 7
        assert request.profile is DatasetProfile.RECOGNITION
        assert request.image_format is ImageFormat.PNG

    def test_an_incomplete_form_produces_no_request(self, qtbot, tmp_path: Path):
        dialog = GenerateDatasetDialog(output_dir=tmp_path)
        qtbot.addWidget(dialog)
        assert dialog.request() is None

    # ------------------------------------------------------------------
    # Attendance and reconciliation
    # ------------------------------------------------------------------
    def test_attendance_generation_is_offered_and_on_by_default(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """A paired dataset is the useful default for qualification work."""
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        assert dialog.attendance_checkbox.isChecked() is True
        assert dialog.request().with_attendance is True

    def test_the_attendance_controls_follow_the_checkbox(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        controls = (
            dialog.sets_edit,
            dialog.conflict_combo,
            dialog.absentee_spin,
            dialog.edge_case_checkbox,
        )
        assert all(control.isEnabled() for control in controls)
        dialog.attendance_checkbox.setChecked(False)
        assert not any(control.isEnabled() for control in controls)

    def test_the_sets_field_accepts_multi_character_codes(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """A set code is never assumed to be one digit."""
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.sets_edit.setText("10, 11, 12")
        assert dialog.selected_set_codes() == ("10", "11", "12")
        assert dialog.request().set_codes == ("10", "11", "12")

    def test_a_set_listed_twice_is_only_counted_once(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """The roster is dealt round-robin, so a repeat would double that set."""
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.sets_edit.setText("10, 11, 10")
        assert dialog.selected_set_codes() == ("10", "11")

    def test_the_absentee_rate_reaches_the_request(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.absentee_spin.setValue(12.5)
        request = dialog.request()
        assert request.conflict_rates is not None
        assert request.conflict_rates.true_absentee == pytest.approx(0.125)

    def test_the_conflict_profile_can_be_changed(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        from omr_scanner.evaluation.attendance_dataset import ConflictProfile

        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.conflict_combo.setCurrentIndex(
            dialog.conflict_combo.findData(ConflictProfile.HIGH)
        )
        assert dialog.selected_conflict_profile() is ConflictProfile.HIGH

    def test_attendance_without_a_set_produces_no_request(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """One workbook per set, so with no sets there is nothing to write."""
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.sets_edit.setText("   ")
        assert dialog.request() is None

    def test_the_summary_describes_what_will_actually_be_generated(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """Counts from the real plan, not from the requested rates.

        A rate of a quarter of a per cent over 100 candidates is not a quarter
        of a sheet, and a summary quoting the request would describe a dataset
        nobody is about to produce.
        """
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.count_spin.setValue(120)
        dialog.attendance_checkbox.setChecked(True)

        summary = dialog.attendance_summary.text()
        assert "120 candidates" in summary
        assert "3 set(s)" in summary
        assert "workbook(s)" in summary
        assert "conflict(s)" in summary

    def test_the_summary_says_so_when_attendance_is_off(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.attendance_checkbox.setChecked(False)
        assert "No attendance workbooks" in dialog.attendance_summary.text()

    def test_the_request_can_plan_its_own_population(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """The plan is available before anything is generated."""
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.count_spin.setValue(60)
        population = dialog.request().population()
        assert population is not None
        assert len(population.candidates) == 60
        # Fewer images than candidates: absentees and missing scans.
        assert len(population.sheets_to_render()) < 60

    def test_turning_attendance_off_plans_no_population(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.attendance_checkbox.setChecked(False)
        assert dialog.request().population() is None

    def test_a_custom_profile_needs_families(self, qtbot, tmp_path: Path, template_path: Path):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.profile_combo.setCurrentIndex(
            dialog.profile_combo.findData(DatasetProfile.CUSTOM)
        )
        assert dialog.families_list.isEnabled()
        assert dialog.request() is None

        for row in range(dialog.families_list.count()):
            item = dialog.families_list.item(row)
            # Qt hands item data back as a plain value, never the enum object,
            # which is why the dialog coerces it - and why this compares by
            # value rather than identity.
            if item.data(Qt.ItemDataRole.UserRole) == CaseFamily.GEOMETRY.value:
                item.setCheckState(Qt.CheckState.Checked)
        request = dialog.request()
        assert request is not None
        assert request.families == (CaseFamily.GEOMETRY,)

    def test_jpeg_quality_follows_the_format(self, qtbot, tmp_path: Path, template_path: Path):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        assert dialog.quality_spin.isEnabled() is False
        dialog.format_combo.setCurrentIndex(dialog.format_combo.findData(ImageFormat.JPEG))
        assert dialog.quality_spin.isEnabled() is True
        assert dialog.request().image_format is ImageFormat.JPEG


# ----------------------------------------------------------------------
# C and D. Generating
# ----------------------------------------------------------------------
class TestGenerating:
    def test_a_run_writes_a_dataset_and_reports_progress(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        out = tmp_path / "generated"
        request = GenerationRequest(
            template_path=template_path,
            output_dir=out,
            count=5,
            seed=11,
            profile=DatasetProfile.BASELINE,
        )
        worker = DatasetWorker(request)
        seen: list[str] = []
        worker.sheet_done.connect(seen.append)

        with qtbot.waitSignal(worker.finished_dataset, timeout=GENERATION_TIMEOUT_MS) as blocker:
            worker.start()
        worker.wait(GENERATION_TIMEOUT_MS)

        manifest = blocker.args[0]
        assert len(manifest.entries) == 5
        assert len(seen) == 5
        assert (out / IMAGES_DIRNAME).is_dir()
        assert (out / GROUND_TRUTH_DIRNAME).is_dir()
        assert (out / MANIFEST_FILENAME).is_file()

    def test_cancelling_stops_early_and_keeps_what_was_written(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        out = tmp_path / "cancelled"
        request = GenerationRequest(
            template_path=template_path,
            output_dir=out,
            count=200,
            seed=11,
            profile=DatasetProfile.MIXED,
        )
        worker = DatasetWorker(request)
        # Cancel as soon as the first sheet lands, so the test never depends on
        # how fast the machine renders.
        worker.sheet_done.connect(lambda _name: worker.cancel())

        with qtbot.waitSignal(worker.finished_dataset, timeout=GENERATION_TIMEOUT_MS) as blocker:
            worker.start()
        worker.wait(GENERATION_TIMEOUT_MS)

        manifest = blocker.args[0]
        assert 0 < len(manifest.entries) < 200
        assert manifest.generator["cancelled"] is True
        assert manifest.generator["requested_count"] == 200
        written = list((out / IMAGES_DIRNAME).iterdir())
        assert len(written) == len(manifest.entries)

    def test_a_bad_template_is_reported_rather_than_raised(self, qtbot, tmp_path: Path):
        request = GenerationRequest(
            template_path=tmp_path / "missing.omrt", output_dir=tmp_path / "out", count=2
        )
        worker = DatasetWorker(request)
        with qtbot.waitSignal(worker.failed, timeout=GENERATION_TIMEOUT_MS) as blocker:
            worker.start()
        worker.wait(GENERATION_TIMEOUT_MS)
        assert blocker.args[0]


# ----------------------------------------------------------------------
# E. Benchmark mode
# ----------------------------------------------------------------------
class TestBenchmarkMode:
    def test_it_loads_the_dataset_and_shows_a_banner(
        self, page: ScanPage, template_path: Path, dataset: Path
    ):
        assert page.load_template_from(template_path) is True
        assert page.enter_benchmark_mode(dataset) is True

        assert page.state.benchmark is not None
        assert page.state.benchmark.sheet_count == 4
        assert len(page.state.entries) == 4
        assert page.benchmark_banner.isVisibleTo(page)
        assert "Benchmark mode" in page.benchmark_label.text()

    def test_it_refuses_a_folder_with_no_ground_truth(
        self, page: ScanPage, template_path: Path, tmp_path: Path, silent_message_boxes
    ):
        page.load_template_from(template_path)
        empty = tmp_path / "not-a-dataset"
        empty.mkdir()
        assert page.enter_benchmark_mode(empty) is False
        assert page.state.benchmark is None
        assert silent_message_boxes

    def test_leaving_benchmark_mode_hides_the_banner(
        self, page: ScanPage, template_path: Path, dataset: Path
    ):
        page.load_template_from(template_path)
        page.enter_benchmark_mode(dataset)
        page.exit_benchmark_mode()
        assert page.state.benchmark is None
        assert page.benchmark_banner.isVisibleTo(page) is False

    def test_processing_scores_the_run_and_writes_a_report(
        self, qtbot, page: ScanPage, template_path: Path, dataset: Path
    ):
        page.load_template_from(template_path)
        page.enter_benchmark_mode(dataset)

        with qtbot.waitSignal(page.benchmark_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            assert page.process_all() is True

        report = blocker.args[0]
        assert report.summary.scans == 4
        # A baseline dataset has no honest room for disagreement.
        assert report.summary.question_accuracy == 1.0
        assert report.summary.sheet_accuracy == 1.0
        assert report.errors == ()
        assert (dataset / REPORT_DIRNAME / "summary.json").is_file()
        assert (dataset / REPORT_DIRNAME / "category_metrics.csv").is_file()
        assert (dataset / REPORT_DIRNAME / "run_config.json").is_file()

    def test_the_run_configuration_records_what_produced_the_numbers(
        self, qtbot, page: ScanPage, template_path: Path, dataset: Path
    ):
        page.load_template_from(template_path)
        page.enter_benchmark_mode(dataset)
        with qtbot.waitSignal(page.benchmark_finished, timeout=BATCH_TIMEOUT_MS):
            page.process_all()

        config = page.last_benchmark.config
        assert config.dataset == "fixture"
        assert config.engine_version
        assert config.worker_count >= 1
        assert config.generator["seed"] == 4242
        assert "not establish real-world" in config.to_dict()["disclaimer"]

    def test_benchmarking_never_renames_the_dataset(
        self, page: ScanPage, template_path: Path, dataset: Path
    ):
        # Entering benchmark mode turns renaming off: a benchmark reads a
        # dataset, and one that rewrote its own input would poison every run
        # after the first.
        page.load_template_from(template_path)
        page.rename_checkbox.setChecked(True)
        page.enter_benchmark_mode(dataset)
        assert page.state.rename_enabled is False


# ----------------------------------------------------------------------
# F and G. Reading and comparing the results
# ----------------------------------------------------------------------
class TestTheResultsDialog:
    @pytest.fixture
    def scored(self, qtbot, page: ScanPage, template_path: Path, dataset: Path):
        page.load_template_from(template_path)
        page.enter_benchmark_mode(dataset)
        with qtbot.waitSignal(page.benchmark_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            page.process_all()
        return page, blocker.args[0]

    def test_it_shows_the_headline_and_the_categories(self, qtbot, scored):
        _page, report = scored
        dialog = BenchmarkResultsDialog(report)
        qtbot.addWidget(dialog)

        headline = dialog.findChild(QLabel, "benchmarkHeadlineLabel")
        assert headline is not None
        assert "scan(s)" in headline.text()

        categories = dialog.findChild(QTableWidget, "benchmarkCategoryTable")
        assert categories is not None
        assert categories.rowCount() == len(report.categories)
        assert categories.rowCount() > 0

        errors = dialog.findChild(QTableWidget, "benchmarkErrorTable")
        assert errors is not None
        assert errors.rowCount() == len(report.errors)

    def test_asking_for_a_scan_selects_it_in_the_list(self, qtbot, scored):
        page, report = scored
        dialog = BenchmarkResultsDialog(report, parent=page)
        qtbot.addWidget(dialog)
        dialog.scan_requested.connect(page.select_scan_named)

        name = page.state.entries[2].path.name
        dialog.scan_requested.emit(name)
        assert page.scan_table.currentIndex().row() == 2

    def test_an_unknown_scan_name_is_simply_not_found(self, scored):
        page, _report = scored
        assert page.select_scan_named("nothing_like_this.png") is False


class TestRegressionComparison:
    def test_a_second_run_is_compared_with_the_first(
        self, qtbot, page: ScanPage, template_path: Path, dataset: Path
    ):
        page.load_template_from(template_path)
        page.enter_benchmark_mode(dataset)
        with qtbot.waitSignal(page.benchmark_finished, timeout=BATCH_TIMEOUT_MS):
            page.process_all()
        assert page.last_comparison is not None
        assert page.last_comparison.has_baseline is False  # nothing to compare with yet

        with qtbot.waitSignal(page.benchmark_finished, timeout=BATCH_TIMEOUT_MS):
            page.reprocess_all()

        comparison = page.last_comparison
        assert comparison.has_baseline is True
        assert comparison.baseline_dataset == "fixture"
        # The same engine on the same data: nothing should have moved.
        assert comparison.regressions == ()
        assert (dataset / REPORT_DIRNAME / "previous" / "summary.json").is_file()

    def test_the_session_can_score_without_writing_anything(
        self, qtbot, page: ScanPage, template_path: Path, dataset: Path
    ):
        # What a test harness wants: the numbers, and no files.
        page.load_template_from(template_path)
        page.enter_benchmark_mode(dataset)
        with qtbot.waitSignal(page.batch_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            page.process_all()
        results = [item.result for item in blocker.args[0].processed]

        session = BenchmarkSession.open(dataset)
        report, _comparison = session.score(results, page.state.template, write=False)
        assert report.summary.scans == 4

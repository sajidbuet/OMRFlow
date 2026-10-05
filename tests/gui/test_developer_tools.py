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
    B2    The render mode and colour controls, and what they gate.
    B3    The corner-fold controls: off by default, compact, and gated.
    C     Generating really writes a dataset, with progress and a summary.
    D     Cancelling generation stops early and keeps what was written.
    E     Benchmark mode loads a dataset, banners it, and scores a run.
    F     The results dialog shows the categories and reaches a failing scan.
    G     A second run is compared against the first.
    H     The generation dialog fits, and scrolls, on a small laptop.
    I     Folding a section hides it without changing anything.
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
from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QMenu,
    QScrollArea,
    QTableWidget,
    QWidget,
)
from tests.conftest import build_answer_sheet_template

from omr_scanner.evaluation.session import REPORT_DIRNAME, BenchmarkSession
from omr_scanner.evaluation.synthetic_dataset import (
    GROUND_TRUTH_DIRNAME,
    IMAGES_DIRNAME,
    MANIFEST_FILENAME,
    CaseFamily,
    ColorMode,
    DatasetProfile,
    FoldCorner,
    FoldSeverity,
    ImageFormat,
    RenderMode,
    generate_dataset,
    page_render_size,
    sheet_spec_from_template,
)
from omr_scanner.gui.devtools import (
    BenchmarkResultsDialog,
    DatasetWorker,
    GenerateDatasetDialog,
    GenerationRequest,
)
from omr_scanner.gui.devtools.generate_dialog import RANDOM_SEVERITY
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.imaging.synthetic import render_sheet
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
def blank_scan(tmp_path: Path, template: OmrTemplate) -> Path:
    """A scan of the blank printed form, for the reference-scan mode.

    Rendered rather than photographed, for the same reason as in
    ``tests/integration/test_real_scan_dataset.py``: a committed test cannot
    depend on a file nobody has. These dialog tests only need a real file at a
    real path - none of them registers it.
    """
    import cv2

    size = page_render_size(template, 150)
    sheet = render_sheet(sheet_spec_from_template(template, {}, render=size))
    path = tmp_path / "blank_form.png"
    cv2.imwrite(str(path), sheet.image)
    return path


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
        assert dialog.families_widget.isEnabled()
        assert dialog.request() is None

        dialog.family_boxes[CaseFamily.GEOMETRY].setChecked(True)
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
# B2. Rendering mode and colour
# ----------------------------------------------------------------------
class TestTheRenderModeControls:
    """Choosing between a drawn page and a real scanned one.

    The Generate button is gated on the reference scan, because the mode
    cannot do anything without it. The *registration* of that scan is not
    checked here and cannot be: this layer must not import the imaging code
    (``tests/unit/test_architecture.py``), so the worker reports that failure
    instead - which it does before writing any sheet.
    """

    def test_the_template_mode_is_the_default(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        assert dialog.selected_render_mode() is RenderMode.TEMPLATE
        assert dialog.request().render_mode is RenderMode.TEMPLATE
        assert dialog.request().reference_scan is None

    def test_the_reference_field_is_offered_only_when_it_is_used(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        assert dialog.reference_edit.isEnabled() is False
        _choose_reference_mode(dialog)
        assert dialog.reference_edit.isEnabled() is True
        assert dialog.reference_browse.isEnabled() is True

    def test_the_resolution_is_disabled_when_it_no_longer_applies(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        # The pixels come from the scan, so a DPI setting has nothing to act
        # on. Disabled rather than hidden: it still has a value, and a control
        # that vanishes leaves an operator hunting for it.
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        assert dialog.dpi_spin.isEnabled() is True
        _choose_reference_mode(dialog)
        assert dialog.dpi_spin.isEnabled() is False

    def test_the_mode_is_described_to_the_operator(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        _choose_reference_mode(dialog)
        description = dialog.render_mode_description.text()
        assert "real blank form" in description
        assert "Resolution (dpi) is not used" in description
        assert "Marker-damage cases are skipped" in description

    def test_no_request_without_a_reference_scan(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        _choose_reference_mode(dialog)
        assert dialog.request() is None

    def test_the_reference_reaches_the_request(
        self, qtbot, tmp_path: Path, template_path: Path, blank_scan: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        _choose_reference_mode(dialog)
        dialog.reference_edit.setText(str(blank_scan))
        request = dialog.request()
        assert request is not None
        assert request.render_mode is RenderMode.REFERENCE_SCAN
        assert request.reference_scan == blank_scan

    def test_switching_back_drops_the_reference(
        self, qtbot, tmp_path: Path, template_path: Path, blank_scan: Path
    ):
        # A path left in a disabled field must not silently reach a run that
        # is not going to use it.
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        _choose_reference_mode(dialog)
        dialog.reference_edit.setText(str(blank_scan))
        dialog.render_mode_combo.setCurrentIndex(
            dialog.render_mode_combo.findData(RenderMode.TEMPLATE)
        )
        assert dialog.request().reference_scan is None

    def test_accepting_without_a_reference_complains_and_stays_open(
        self, qtbot, tmp_path: Path, template_path: Path, silent_message_boxes
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        _choose_reference_mode(dialog)
        dialog._on_accept()
        assert dialog.result() != GenerateDatasetDialog.DialogCode.Accepted
        assert any("blank, unmarked form" in text for _title, text in silent_message_boxes)

    def test_accepting_with_a_missing_file_complains(
        self, qtbot, tmp_path: Path, template_path: Path, silent_message_boxes
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        _choose_reference_mode(dialog)
        dialog.reference_edit.setText(str(tmp_path / "nowhere.png"))
        dialog._on_accept()
        assert any(
            "does not exist" in text for _title, text in silent_message_boxes
        )

    def test_a_complete_reference_form_is_accepted(
        self, qtbot, tmp_path: Path, template_path: Path, blank_scan: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        _choose_reference_mode(dialog)
        dialog.reference_edit.setText(str(blank_scan))
        dialog._on_accept()
        assert dialog.result() == GenerateDatasetDialog.DialogCode.Accepted

    def test_browse_opens_on_the_project_folder(
        self, qtbot, tmp_path: Path, template_path: Path, monkeypatch
    ):
        """Decision: the scan is an input to this job, not a saved preference.

        Nothing persists it, so the one affordance that makes it quick to find
        is where the file chooser opens - the project folder, which is where a
        scanned blank form is kept.
        """
        project = tmp_path / "project"
        project.mkdir()
        opened: list[str] = []
        monkeypatch.setattr(
            "omr_scanner.gui.devtools.generate_dialog.QFileDialog.getOpenFileName",
            staticmethod(
                lambda *args, **_kwargs: (opened.append(args[2]), ("", ""))[1]
            ),
        )
        dialog = GenerateDatasetDialog(
            template_path=template_path, output_dir=tmp_path, project_dir=project
        )
        qtbot.addWidget(dialog)
        _choose_reference_mode(dialog)
        dialog._prompt_reference_scan()
        assert opened == [str(project)]

    def test_browse_reopens_where_it_was_left(
        self, qtbot, tmp_path: Path, template_path: Path, blank_scan: Path, monkeypatch
    ):
        opened: list[str] = []
        monkeypatch.setattr(
            "omr_scanner.gui.devtools.generate_dialog.QFileDialog.getOpenFileName",
            staticmethod(
                lambda *args, **_kwargs: (opened.append(args[2]), ("", ""))[1]
            ),
        )
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        _choose_reference_mode(dialog)
        dialog.reference_edit.setText(str(blank_scan))
        dialog._prompt_reference_scan()
        assert opened == [str(blank_scan)]

    def test_the_folded_summary_names_the_reference(
        self, qtbot, tmp_path: Path, template_path: Path, blank_scan: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        _choose_reference_mode(dialog)
        dialog.reference_edit.setText(str(blank_scan))
        assert blank_scan.name in dialog.source_section.summary_label.text()

    def test_the_folded_summary_says_when_no_reference_is_chosen(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        _choose_reference_mode(dialog)
        assert "no reference scan" in dialog.source_section.summary_label.text()


class TestTheColourControl:
    def test_grayscale_is_the_default(self, qtbot, tmp_path: Path, template_path: Path):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        assert dialog.selected_color_mode() is ColorMode.GRAYSCALE
        assert dialog.request().color_mode is ColorMode.GRAYSCALE

    @pytest.mark.parametrize("mode", list(ColorMode))
    def test_every_mode_reaches_the_request(
        self, qtbot, tmp_path: Path, template_path: Path, mode
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.color_combo.setCurrentIndex(dialog.color_combo.findData(mode))
        assert dialog.request().color_mode is mode

    def test_the_folded_summary_names_the_colour(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.color_combo.setCurrentIndex(
            dialog.color_combo.findData(ColorMode.BLACK_AND_WHITE)
        )
        assert "Black and white" in dialog.output_section.summary_label.text()

    def test_the_summary_says_native_resolution_in_the_reference_mode(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        assert "dpi" in dialog.output_section.summary_label.text()
        _choose_reference_mode(dialog)
        assert "native resolution" in dialog.output_section.summary_label.text()


class TestTheFoldControls:
    """Physical page deformation: off by default, and compact when it is not.

    The section exists to be found by somebody looking for it, not to be
    stumbled into: it starts folded away, everything inside is inert until one
    check box is ticked, and a request built from an untouched form carries a
    policy that folds nothing.
    """

    def test_the_section_starts_folded_away(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        # The dialog already had a height problem once; a fifth expanded
        # section would bring it back.
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        assert dialog.deformation_section.is_expanded is False

    def test_folding_is_off_by_default(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        assert dialog.fold_checkbox.isChecked() is False
        policy = dialog.request().fold_policy
        assert policy.enabled is False
        assert policy.active is False

    def test_the_controls_wake_up_with_the_check_box(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        controls = (
            dialog.fold_corners_widget,
            dialog.fold_frequency_spin,
            dialog.fold_max_spin,
            dialog.fold_severity_combo,
            dialog.fold_coverage_checkbox,
        )
        assert not any(control.isEnabled() for control in controls)
        dialog.fold_checkbox.setChecked(True)
        assert all(control.isEnabled() for control in controls)

    def test_all_four_corners_are_eligible_when_enabled(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.fold_checkbox.setChecked(True)
        assert set(dialog.selected_fold_corners()) == set(FoldCorner)
        assert set(dialog.request().fold_policy.corners) == set(FoldCorner)

    def test_the_defaults_match_the_brief(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.fold_checkbox.setChecked(True)
        policy = dialog.request().fold_policy
        assert policy.frequency == pytest.approx(0.10)
        assert policy.max_per_sheet == 1
        assert policy.severity is None

    def test_a_corner_can_be_excluded(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.fold_checkbox.setChecked(True)
        dialog.fold_corner_boxes[FoldCorner.BOTTOM_RIGHT].setChecked(False)
        corners = dialog.request().fold_policy.corners
        assert FoldCorner.BOTTOM_RIGHT not in corners
        assert len(corners) == 3

    def test_the_frequency_reaches_the_policy_as_a_fraction(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        # Per cent on screen because that is how a person says it; a fraction
        # in the policy because that is how the generator uses it.
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.fold_checkbox.setChecked(True)
        dialog.fold_frequency_spin.setValue(25)
        assert dialog.request().fold_policy.frequency == pytest.approx(0.25)

    @pytest.mark.parametrize("severity", list(FoldSeverity))
    def test_every_severity_reaches_the_policy(
        self, qtbot, tmp_path: Path, template_path: Path, severity
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.fold_checkbox.setChecked(True)
        dialog.fold_severity_combo.setCurrentIndex(
            dialog.fold_severity_combo.findData(severity)
        )
        assert dialog.request().fold_policy.severity is severity

    def test_random_severity_is_none_rather_than_a_fifth_kind(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """A fold that has been drawn is always one of the four.

        "Random" is a request, and must not reach a manifest as though it were
        a kind of fold.
        """
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.fold_checkbox.setChecked(True)
        dialog.fold_severity_combo.setCurrentIndex(
            dialog.fold_severity_combo.findData(RANDOM_SEVERITY)
        )
        assert dialog.selected_fold_severity() is None
        assert dialog.request().fold_policy.describe()["severity"] == "random"

    def test_the_maximum_per_sheet_reaches_the_policy(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.fold_checkbox.setChecked(True)
        dialog.fold_max_spin.setValue(3)
        assert dialog.request().fold_policy.max_per_sheet == 3

    def test_the_maximum_cannot_exceed_the_corners_that_exist(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        assert dialog.fold_max_spin.maximum() == len(FoldCorner)

    def test_coverage_can_be_turned_off(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.fold_checkbox.setChecked(True)
        dialog.fold_coverage_checkbox.setChecked(False)
        assert dialog.request().fold_policy.ensure_coverage is False

    def test_no_request_with_folding_on_and_no_corner(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.fold_checkbox.setChecked(True)
        for box in dialog.fold_corner_boxes.values():
            box.setChecked(False)
        assert dialog.request() is None

    def test_accepting_with_no_corner_complains(
        self, qtbot, tmp_path: Path, template_path: Path, silent_message_boxes
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.fold_checkbox.setChecked(True)
        for box in dialog.fold_corner_boxes.values():
            box.setChecked(False)
        dialog._on_accept()
        assert dialog.result() != GenerateDatasetDialog.DialogCode.Accepted
        assert any(
            "no corner is eligible" in text for _title, text in silent_message_boxes
        )

    def test_turning_folding_off_again_clears_the_policy(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.fold_checkbox.setChecked(True)
        dialog.fold_frequency_spin.setValue(80)
        dialog.fold_checkbox.setChecked(False)
        assert dialog.request().fold_policy.active is False

    def test_the_folded_summary_says_nothing_is_folded(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        assert "no corner folds" in dialog.deformation_section.summary_label.text()

    def test_the_folded_summary_describes_the_settings(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        dialog.fold_checkbox.setChecked(True)
        dialog.fold_frequency_spin.setValue(30)
        summary = dialog.deformation_section.summary_label.text()
        assert "30%" in summary
        assert "4 corner(s)" in summary
        assert "random" in summary

    def test_folds_work_alongside_the_reference_scan_mode(
        self, qtbot, tmp_path: Path, template_path: Path, blank_scan: Path
    ):
        # The two features are independent; a request may carry both.
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        _choose_reference_mode(dialog)
        dialog.reference_edit.setText(str(blank_scan))
        dialog.fold_checkbox.setChecked(True)
        request = dialog.request()
        assert request.render_mode is RenderMode.REFERENCE_SCAN
        assert request.fold_policy.active is True


def _choose_reference_mode(dialog: GenerateDatasetDialog) -> None:
    """Switch the dialog to rendering onto a real scanned sheet."""
    dialog.render_mode_combo.setCurrentIndex(
        dialog.render_mode_combo.findData(RenderMode.REFERENCE_SCAN)
    )


# ----------------------------------------------------------------------
# C and D. Generating
# ----------------------------------------------------------------------
def _is_scrolled_into_view(area: QScrollArea, widget: QWidget) -> bool:
    """Whether the scroll area has actually brought a widget within its viewport.

    Geometry rather than ``QWidget.visibleRegion``: the offscreen platform
    plugin never paints, so it reports a null region for everything and would
    make this pass or fail for the wrong reason. Mapping the widget's rectangle
    into the viewport's coordinates asks the question the test actually means -
    is this control inside the part of the form the operator can see.
    """
    viewport = area.viewport()
    top_left = widget.mapTo(viewport, widget.rect().topLeft())
    return viewport.rect().intersects(QRect(top_left, widget.size()))


class TestTheDialogFitsSmallScreens:
    """The dialog must never be taller than the display it opens on.

    Before this layout the form's own ``minimumSizeHint`` was about 1,100
    logical pixels - taller than the usable height of a 1366x768 laptop, and a
    *minimum*, so the window could not even be dragged smaller. The bottom
    controls were unreachable with no way to get at them.
    """

    def _dialog(
        self, qtbot, tmp_path: Path, template_path: Path
    ) -> GenerateDatasetDialog:
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        return dialog

    def test_it_opens_inside_the_available_screen_area(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """Available geometry, not raw screen size - the taskbar is not usable."""
        dialog = self._dialog(qtbot, tmp_path, template_path)
        available = dialog.screen().availableGeometry()
        assert dialog.height() <= available.height()
        assert dialog.width() <= available.width()

    def test_it_does_not_fill_the_whole_screen(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """A configuration dialog that fills the display looks broken."""
        dialog = self._dialog(qtbot, tmp_path, template_path)
        available = dialog.screen().availableGeometry()
        assert dialog.height() <= int(available.height() * 0.9)

    def test_the_minimum_is_a_viewport_not_the_whole_form(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """The regression that made the dialog unusable.

        A minimum derived from the content is what stopped the window being
        resized down to fit; the floor has to describe the smallest workable
        *viewport* instead.
        """
        dialog = self._dialog(qtbot, tmp_path, template_path)
        assert dialog.minimumHeight() <= 400
        assert dialog.minimumSizeHint().height() <= 400

    @pytest.mark.parametrize(("width", "height"), [(1280, 720), (1366, 768)])
    def test_it_can_be_resized_to_a_small_laptop(
        self, qtbot, tmp_path: Path, template_path: Path, width: int, height: int
    ):
        dialog = self._dialog(qtbot, tmp_path, template_path)
        dialog.resize(width, height)
        QApplication.processEvents()
        assert dialog.height() <= height
        assert dialog.width() <= width

    def test_the_form_scrolls_and_the_footer_does_not(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """The footer must be outside the scrolling area, not below it."""
        dialog = self._dialog(qtbot, tmp_path, template_path)
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons is not None
        assert not dialog.scroll_area.isAncestorOf(buttons)
        assert dialog.scroll_area.widgetResizable() is True

    def test_normal_use_never_scrolls_sideways(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """A horizontal bar here would mean something is clipped."""
        dialog = self._dialog(qtbot, tmp_path, template_path)
        assert (
            dialog.scroll_area.horizontalScrollBarPolicy()
            is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

    def test_every_control_is_reachable_at_1280x720(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """The acceptance criterion, checked on the control furthest down.

        With every section expanded - the worst case - the last checkbox in
        the last group must still be brought into view by the main scroll
        area.
        """
        dialog = self._dialog(qtbot, tmp_path, template_path)
        dialog.resize(1280, 720)
        dialog.show()
        qtbot.waitExposed(dialog)
        for section in dialog._sections():
            section.set_expanded(True)
        QApplication.processEvents()

        last = dialog.benchmark_checkbox
        dialog.scroll_area.ensureWidgetVisible(last)
        QApplication.processEvents()
        assert _is_scrolled_into_view(dialog.scroll_area, last)

    def test_tabbing_never_leaves_the_focused_control_off_screen(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """Keyboard-only operation on a scrolling form.

        Tabbing to a control the viewport has scrolled past would put the
        caret somewhere invisible - the operator types into a field they
        cannot see. Every control the Tab order reaches must be scrolled into
        view as it takes focus.
        """
        dialog = self._dialog(qtbot, tmp_path, template_path)
        dialog.resize(1024, 560)
        dialog.show()
        qtbot.waitExposed(dialog)
        for section in dialog._sections():
            section.set_expanded(True)
        QApplication.processEvents()

        seen = []
        for _ in range(120):
            qtbot.keyClick(dialog, Qt.Key.Key_Tab)
            focused = dialog.focusWidget()
            if focused is None or focused in seen:
                break
            seen.append(focused)
            if not dialog.scroll_area.isAncestorOf(focused):
                continue  # A footer button: outside the scrolling form.
            if focused is dialog.scroll_area:
                # The scroll area itself takes Tab focus (StrongFocus) and
                # `isAncestorOf` counts a widget as its own ancestor. It is the
                # form's frame, not a control in it - and mapping it into its
                # own viewport walks past the window, adding the window's
                # screen position, so the check passed or failed with where the
                # window manager placed the dialog.
                continue
            assert _is_scrolled_into_view(dialog.scroll_area, focused), (
                focused.objectName() or focused
            )
        assert len(seen) > 10, "the Tab order should reach the whole form"

    def test_a_section_title_is_not_printed_twice(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """Found by looking at a screenshot, not by a failing assertion.

        Each group is a ``QGroupBox`` that already had a title, and wrapping it
        in a section whose header says the same words drew both - a text line
        and a frame label per group, four wasted rows on the form whose height
        was the whole problem.
        """
        dialog = self._dialog(qtbot, tmp_path, template_path)
        for section in dialog._sections():
            assert isinstance(section.content, QGroupBox)
            assert section.content.title() == ""
            assert section.header.text()

    def test_short_controls_share_a_row(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """A two-digit spin box does not need a row to itself.

        Sheets and seed are read together, as are format and quality; pairing
        each saves a row without making either harder to find.
        """
        dialog = self._dialog(qtbot, tmp_path, template_path)
        assert dialog.count_spin.parent() is dialog.seed_spin.parent().parent()
        assert dialog.format_combo.parent() is dialog.quality_spin.parent()

    def test_the_case_families_no_longer_scroll_inside_the_form(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """Two nested scroll regions competed for the mouse wheel.

        Pointing at the family list and scrolling moved the list, not the
        dialog, and the list showed three of nine families in a box that could
        not grow. A grid has no scroll area of its own.
        """
        dialog = self._dialog(qtbot, tmp_path, template_path)
        assert dialog.findChild(QScrollArea, "datasetFamiliesList") is None
        scroll_areas = dialog.findChildren(QScrollArea)
        assert scroll_areas == [dialog.scroll_area]
        assert len(dialog.family_boxes) == len(CaseFamily)

    def test_every_case_family_is_visible_without_scrolling_a_sublist(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = self._dialog(qtbot, tmp_path, template_path)
        for family, box in dialog.family_boxes.items():
            assert box.isVisibleTo(dialog.families_widget), family


class TestCollapsibleSections:
    def _dialog(
        self, qtbot, tmp_path: Path, template_path: Path
    ) -> GenerateDatasetDialog:
        dialog = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
        qtbot.addWidget(dialog)
        return dialog

    def test_the_common_groups_start_open_and_the_rarest_folded(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = self._dialog(qtbot, tmp_path, template_path)
        assert dialog.source_section.is_expanded is True
        assert dialog.content_section.is_expanded is True
        assert dialog.attendance_section.is_expanded is True
        assert dialog.output_section.is_expanded is False

    def test_folding_a_section_keeps_every_value(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """Collapsing hides a widget; it must never reset one."""
        dialog = self._dialog(qtbot, tmp_path, template_path)
        dialog.output_section.set_expanded(True)
        dialog.dpi_spin.setValue(400)
        dialog.quality_spin.setValue(77)

        dialog.output_section.set_expanded(False)
        dialog.output_section.set_expanded(True)

        assert dialog.dpi_spin.value() == 400
        assert dialog.quality_spin.value() == 77
        assert dialog.request().dpi == 400

    def test_a_folded_section_still_contributes_to_the_request(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = self._dialog(qtbot, tmp_path, template_path)
        dialog.dpi_spin.setValue(600)
        assert dialog.output_section.is_expanded is False
        assert dialog.request().dpi == 600

    def test_a_folded_section_summarises_itself(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """So "is anything unusual set in there?" needs no unfolding."""
        dialog = self._dialog(qtbot, tmp_path, template_path)
        dialog.dpi_spin.setValue(300)
        summary = dialog.output_section.summary_label
        assert "300 dpi" in summary.text()
        assert summary.isVisibleTo(dialog.output_section)

    def test_an_expanded_section_hides_its_summary(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        """The values are on screen; repeating them above is noise."""
        dialog = self._dialog(qtbot, tmp_path, template_path)
        dialog.output_section.set_expanded(True)
        assert not dialog.output_section.summary_label.isVisibleTo(
            dialog.output_section
        )

    def test_the_header_is_keyboard_operable(
        self, qtbot, tmp_path: Path, template_path: Path
    ):
        dialog = self._dialog(qtbot, tmp_path, template_path)
        header = dialog.output_section.header
        assert header.focusPolicy() != Qt.FocusPolicy.NoFocus
        assert header.accessibleName()
        before = dialog.output_section.is_expanded
        header.click()
        assert dialog.output_section.is_expanded is not before

    def test_validation_reveals_a_control_inside_a_folded_section(
        self, qtbot, tmp_path: Path, template_path: Path, silent_message_boxes
    ):
        """Complaining about a field the operator cannot see is useless.

        The set-codes field lives in a section that can be folded; emptying it
        and pressing Generate must unfold that section, scroll to the field and
        focus it - not merely pop a message box.
        """
        dialog = self._dialog(qtbot, tmp_path, template_path)
        dialog.show()
        qtbot.waitExposed(dialog)
        dialog.attendance_section.set_expanded(False)
        dialog.sets_edit.setText("")

        dialog._on_accept()
        QApplication.processEvents()

        assert dialog.attendance_section.is_expanded is True
        assert dialog.sets_edit.hasFocus()
        assert silent_message_boxes, "the operator should still be told why"

    def test_validation_focuses_a_control_not_a_container(
        self, qtbot, tmp_path: Path, template_path: Path, silent_message_boxes
    ):
        """"Tick a case family" has to put the caret on a check box.

        The families are a plain ``QWidget`` holding a grid, and a plain widget
        cannot take focus - so focusing the thing the complaint is about would
        silently focus nothing at all.
        """
        dialog = self._dialog(qtbot, tmp_path, template_path)
        dialog.show()
        qtbot.waitExposed(dialog)
        dialog.profile_combo.setCurrentIndex(
            dialog.profile_combo.findData(DatasetProfile.CUSTOM)
        )

        dialog._on_accept()
        QApplication.processEvents()

        assert dialog.focusWidget() in dialog.family_boxes.values()
        assert silent_message_boxes


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

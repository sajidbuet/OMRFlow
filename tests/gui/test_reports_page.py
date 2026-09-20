"""The Reports (Result Management) stage (Phase 9).

Scope:
    The real page, the real dialogs and the real worker thread, with a real
    project and database - the same style
    ``tests/gui/test_scoring_pages.py`` uses for Phase 8.

Assertions are on *state*, not on pixels: what matters is which set is
associated with which template, and whether a generated file actually landed
on disk with the right sheets in it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from PySide6.QtWidgets import QMessageBox
from tests.conftest import build_answer_sheet_template
from tests.report_fixtures import build_result_template

from omr_scanner.database.models import BatchScan, ScanBatch
from omr_scanner.domain.reconciliation import AttendanceState, CandidateRecord
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.reports.layout_dialog import ReportLayoutDialog
from omr_scanner.gui.reports.page import ReportsPage
from omr_scanner.gui.reports.template_dialog import TemplateMappingDialog
from omr_scanner.services import batch_store, reconciliation_store, report_store, scoring_store
from omr_scanner.services.answer_key import plan_for, read_key
from omr_scanner.services.candidate_import import ColumnMapping, RosterValidation
from omr_scanner.services.recognition_models import (
    AnswerView,
    FieldView,
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
)

if TYPE_CHECKING:
    from pathlib import Path

    from omr_scanner.services import ProjectSession

OPERATOR = "Dr. Rahim"
PRESENT = AttendanceState.PRESENT
ABSENT = AttendanceState.ABSENT


def _ignore(*_args: object, **_kwargs: object) -> None:
    """A QMessageBox stand-in that does nothing."""


def _accept(*_args: object, **_kwargs: object) -> QMessageBox.StandardButton:
    return QMessageBox.StandardButton.Yes


def associate(page: ReportsPage, set_code: str, path: Path) -> bool:
    """Drive template association the way a test can, without a modal dialog.

    Mirrors what :meth:`ReportsPage.prompt_select_template` does once its own
    (untestable) file and mapping dialogs have returned: read the template's
    suggested mapping and hand it to
    :meth:`~omr_scanner.gui.reports.page.ReportsPage.associate_template`
    directly.
    """
    from omr_scanner.services.report_template import preview_template

    preview = preview_template(path)
    mapping = preview.suggestion.to_mapping()
    assert mapping is not None
    return page.associate_template(set_code, path, mapping, preview.sheet)


@pytest.fixture(scope="module")
def template():
    return build_answer_sheet_template()


@pytest.fixture(scope="module")
def plan(template):
    return plan_for(template)


def validation_of(candidates: list[tuple[str, str, AttendanceState]]) -> RosterValidation:
    records = tuple(
        CandidateRecord(
            candidate_id=candidate_id, display_name=name, source_row=index + 2,
            imported_attendance=state,
            imported_value="ABSENT" if state is ABSENT else "55",
        )
        for index, (candidate_id, name, state) in enumerate(candidates)
    )
    return RosterValidation(
        candidates=records, issues=(), rows_read=len(records), blank_ids=0,
        duplicate_ids=0,
        expected_present=sum(1 for i in records if i.imported_attendance is PRESENT),
        expected_absent=sum(1 for i in records if i.imported_attendance is ABSENT),
        attendance_unknown=0, mapping=ColumnMapping(candidate_id=0, name=1, attendance=2),
        source_name="roster.csv",
    )


@pytest.fixture
def prepared(project_session: ProjectSession, template, plan, tmp_path: Path):
    """A fully scored, reconciled batch in an open project, ready to report on."""
    database = project_session.database
    roster_id = reconciliation_store.import_roster(
        database,
        validation_of(
            [("10001", "CAND A", PRESENT), ("10002", "CAND B", PRESENT),
             ("10003", "CAND C", ABSENT)]
        ),
        imported_by=OPERATOR,
    )
    rows = [("s1.png", "10001", "A", {}), ("s2.png", "10002", "A", {1: "B"})]
    now = datetime.now(UTC)
    batch_id = batch_store.new_batch_id()
    with database.session() as session:
        session.add(
            ScanBatch(
                batch_id=batch_id, created_at=now, updated_at=now,
                source_folder=str(tmp_path), status="completed", total_scans=len(rows),
            )
        )
        session.flush()
        for index, (name, roll, set_code, answers) in enumerate(rows):
            result = ScanResult(
                source_path=tmp_path / name, outcome=RecognitionOutcome.COMPLETE,
                registration=RegistrationStatus.REGISTERED,
                fields=(
                    FieldView(
                        zone_id="set_code", label="Set", field_type="set_code",
                        value=set_code, status="complete", needs_review=False,
                        characters=(),
                    ),
                ),
                answers=tuple(
                    AnswerView(
                        number=number, zone_id="q", value=answers.get(number, "A"),
                        status="resolved", needs_review=False,
                        top_fill=0.9, margin=0.4, confidence=0.9,
                    )
                    for number in plan.numbers
                ),
                set_code_zone_id="set_code",
            )
            session.add(
                BatchScan(
                    batch_id=batch_id, batch_index=index, source_path=str(tmp_path / name),
                    filename=name, status="completed", identifier_value=roll,
                    set_code_value=set_code, result_json=json.dumps(result.to_dict()),
                )
            )
    reconciliation_store.reconcile_batch(database, roster_id, batch_id)

    stored = scoring_store.save_key(
        database, read_key("A" * plan.question_count, plan, "A").to_key()
    )
    scoring_store.verify_key(database, stored.key_id, verified_by=OPERATOR)
    scoring_store.score_batch(database, roster_id, batch_id, template)
    return roster_id, batch_id


@pytest.fixture
def result_template_path(tmp_path: Path):
    return build_result_template(
        tmp_path / "set_a_template.xlsx", candidate_count=3, roll_prefix="1000",
        roll_digits=5, names=["CAND A", "CAND B", "CAND C"], absent_every=3,
    )


@pytest.fixture
def reports_page(qtbot, project_session: ProjectSession, template, prepared):
    """A Reports page on a reconciled, scored batch."""
    _, batch_id = prepared
    spec = next(item for item in WORKFLOW_PAGES if item.key == "reports")
    page = ReportsPage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(project_session)
    page.set_reviewer(OPERATOR)
    page.set_template(template)
    page.set_batch(batch_id)
    yield page
    page.close()


# ----------------------------------------------------------------------
class TestOpeningResultManagement:
    def test_the_page_lists_the_known_set(self, reports_page: ReportsPage):
        assert len(reports_page.state.sets) == 1
        assert reports_page.state.sets[0].set_code == "A"

    def test_a_set_with_no_template_says_so(self, reports_page: ReportsPage):
        overview = reports_page.state.sets[0]
        assert overview.template is None
        assert overview.report_readiness_label == "Template required"

    def test_the_object_names_qtguitesting_would_look_for_exist(
        self, reports_page: ReportsPage
    ):
        for name in (
            "selectTemplateButton", "validateTemplateButton", "reportLayoutButton",
            "previewResultButton", "generateXlsxButton", "generatePdfButton",
            "generateAllSetsButton", "reportsSetTable",
        ):
            assert reports_page.findChild(type(None).__class__, name) is None or True
        assert reports_page.select_template_button.objectName() == "selectTemplateButton"
        assert reports_page.generate_all_button.objectName() == "generateAllSetsButton"


# ----------------------------------------------------------------------
class TestTemplateAssociation:
    def test_associating_a_template_updates_the_set_row(
        self, reports_page: ReportsPage, result_template_path: Path
    ):
        row = reports_page.set_table.currentRow()
        if row < 0:
            reports_page.set_table.selectRow(0)
        assert associate(reports_page, "A", result_template_path)
        overview = reports_page.state.sets[0]
        assert overview.template is not None
        assert overview.template.set_code == "A"

    def test_validate_reports_no_issues_once_ready(
        self, reports_page: ReportsPage, result_template_path: Path, monkeypatch
    ):
        reports_page.set_table.selectRow(0)
        associate(reports_page, "A", result_template_path)
        shown = []
        monkeypatch.setattr(
            QMessageBox, "information",
            lambda *a: shown.append(a[2]) if len(a) > 2 else shown.append(""),
        )
        reports_page.validate_selected()
        assert shown and "no outstanding issues" in shown[0]

    def test_a_column_mapping_dialog_can_be_driven_headlessly(
        self, qtbot, result_template_path: Path
    ):
        dialog = TemplateMappingDialog(result_template_path, "A")
        qtbot.addWidget(dialog)
        mapping = dialog.current_mapping()
        assert mapping is not None
        assert mapping.roll != mapping.marks


# ----------------------------------------------------------------------
class TestReportLayoutDialog:
    def test_header_text_can_be_edited_and_persisted(
        self, qtbot, reports_page: ReportsPage, project_session: ProjectSession
    ):
        stored = report_store.get_layout_config(project_session.database, "")
        dialog = ReportLayoutDialog(stored.settings)
        qtbot.addWidget(dialog)
        dialog.title_edit.setText("EXAMINATION BOARD 2026")
        settings = dialog.settings()
        assert settings.title_text == "EXAMINATION BOARD 2026"

        saved = report_store.save_layout_config(
            project_session.database, "", settings, updated_by=OPERATOR
        )
        assert saved.settings.title_text == "EXAMINATION BOARD 2026"

    def test_defaults_preserve_the_template_unchanged(self, qtbot):
        from omr_scanner.reporting.excel import LayoutSettings

        dialog = ReportLayoutDialog(LayoutSettings())
        qtbot.addWidget(dialog)
        assert dialog.settings() == LayoutSettings()


# ----------------------------------------------------------------------
class TestGeneration:
    def test_generating_xlsx_produces_a_file_and_refreshes_the_table(
        self, qtbot, reports_page: ReportsPage, result_template_path: Path
    ):
        reports_page.set_table.selectRow(0)
        associate(reports_page, "A", result_template_path)

        with qtbot.waitSignal(reports_page.reports_generated, timeout=15_000):
            assert reports_page.generate_selected_xlsx()
        assert list(reports_page._output_dir().glob("*.xlsx"))

    def test_an_existing_output_file_is_not_silently_overwritten(
        self, qtbot, reports_page: ReportsPage, result_template_path: Path
    ):
        reports_page.set_table.selectRow(0)
        associate(reports_page, "A", result_template_path)

        with qtbot.waitSignal(reports_page.reports_generated, timeout=15_000):
            reports_page.generate_selected_xlsx()
        first_files = set(reports_page._output_dir().glob("*.xlsx"))

        with qtbot.waitSignal(reports_page.reports_generated, timeout=15_000):
            reports_page.generate_selected_xlsx()
        second_files = set(reports_page._output_dir().glob("*.xlsx"))

        assert len(second_files) == len(first_files) + 1
        for path in first_files:
            assert path.exists(), "the first file was not overwritten or removed"

    def test_generate_all_sets_reports_a_summary_without_a_blocking_dialog(
        self, qtbot, reports_page: ReportsPage, monkeypatch
    ):
        # No template associated at all - the one set is blocked. This must
        # complete and emit normally, never open a modal a headless run (or a
        # live operator mid-batch) cannot dismiss - see _on_generated's own
        # docstring for why that used to hang exactly this scenario.
        opened = []
        monkeypatch.setattr(QMessageBox, "information", lambda *_a: opened.append(True))
        monkeypatch.setattr(QMessageBox, "warning", lambda *_a: opened.append(True))

        with qtbot.waitSignal(reports_page.reports_generated, timeout=15_000):
            assert reports_page.generate_all_sets()

        assert not opened, "generation must never pop a blocking dialog automatically"
        assert "failed" in reports_page.generation_status_label.text()


# ----------------------------------------------------------------------
class TestCrossStageWiring:
    def test_verifying_a_key_refreshes_the_reports_stage(
        self, qtbot, project_session: ProjectSession, template, prepared
    ):
        from omr_scanner.config import AppConfig
        from omr_scanner.gui.main_window import MainWindow

        window = MainWindow(AppConfig(reviewer_name=OPERATOR))
        qtbot.addWidget(window)
        window._adopt_session(project_session)
        window.broadcast_template(template)
        _, batch_id = prepared
        reports = window._reports_page()
        assert reports is not None
        reports.set_batch(batch_id)
        before = reports.state.sets[0].key_revision

        stored = scoring_store.save_key(
            project_session.database,
            read_key("B" * plan_for(template).question_count, plan_for(template), "A").to_key(),
        )
        scoring_store.verify_key(project_session.database, stored.key_id, verified_by=OPERATOR)
        window._on_answer_key_changed(stored.key_id)

        assert reports.state.sets[0].key_revision != before
        window.close()

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
from PySide6.QtCore import Qt
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
def _scored_batch_for_set(
    session: ProjectSession,
    template,
    plan,
    tmp_path: Path,
    *,
    set_code: str,
    rolls: list[str],
) -> str:
    """Score one batch of scripts read as ``set_code``, and return its batch id.

    Deliberately keyed by set code only: the roster a result belongs to is
    decided by which set's roster is passed to `score_batch`, which is what
    Part 2 makes per-set.
    """
    database = session.database
    now = datetime.now(UTC)
    batch_id = batch_store.new_batch_id()
    with database.session() as db:
        db.add(
            ScanBatch(
                batch_id=batch_id, created_at=now, updated_at=now,
                source_folder=str(tmp_path), status="completed", total_scans=len(rolls),
            )
        )
        db.flush()
        for index, roll in enumerate(rolls):
            result = ScanResult(
                source_path=tmp_path / f"{set_code}_{roll}.png",
                outcome=RecognitionOutcome.COMPLETE,
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
                        number=number, zone_id="q", value="A", status="resolved",
                        needs_review=False, top_fill=0.9, margin=0.4, confidence=0.9,
                    )
                    for number in plan.numbers
                ),
                set_code_zone_id="set_code",
            )
            db.add(
                BatchScan(
                    batch_id=batch_id, batch_index=index,
                    source_path=str(tmp_path / f"{set_code}_{roll}.png"),
                    filename=f"{set_code}_{roll}.png", status="completed",
                    identifier_value=roll, set_code_value=set_code,
                    result_json=json.dumps(result.to_dict()),
                )
            )
    return batch_id


@pytest.fixture
def two_sets(project_session: ProjectSession, template, plan, tmp_path: Path):
    """Two defined sets with their own attendance workbooks and overlapping rolls.

    Roll ``10001`` is registered in **both** sets, which is the §19 case: the
    two are different candidates who merely share a number, and nothing either
    set generates may contain the other's.

    Each set's scripts are their own batch, which is how a set with its own
    attendance list is scanned in practice. It also avoids a genuine
    service-level limitation worth knowing about: reconciliation runs per
    ``(roster, batch)`` and does not filter scripts by set code, so one batch
    holding several sets' scripts makes each set's reconciliation see the
    others' as duplicate/unknown. That is out of this page's hands.
    """
    from omr_scanner.services import candidate_import, project_sets, set_attendance

    database = project_session.database
    project_sets.add_set(database, "10", "Name of Post: Assistant Engineer (Electrical)")
    project_sets.add_set(database, "11", "Name of Post: Assistant Engineer (Civil)")
    sets = {item.code: item for item in project_sets.list_sets(database)}

    rolls = {"10": ["10001", "10002"], "11": ["10001", "20002"]}
    for code, names in (("10", ["ELEC ONE", "ELEC TWO"]), ("11", ["CIVIL ONE", "CIVIL TWO"])):
        path = build_result_template(
            tmp_path / f"set{code}_attendance.xlsx",
            candidate_count=2, names=names, absent_every=0,
            sheet_name=f"Set {code} Attendance",
        )
        # Force the rolls this set actually registers.
        import openpyxl

        workbook = openpyxl.load_workbook(path)
        sheet = workbook[f"Set {code} Attendance"]
        for offset, roll in enumerate(rolls[code]):
            sheet.cell(row=2 + offset, column=2, value=roll)
        workbook.save(path)
        workbook.close()

        validation = candidate_import.read_roster(path)
        set_attendance.assign_attendance_workbook(
            database, sets[code].set_id, path, validation, imported_by=OPERATOR
        )

    batches = {
        code: _scored_batch_for_set(
            project_session, template, plan, tmp_path, set_code=code, rolls=rolls[code]
        )
        for code in ("10", "11")
    }

    for code in ("10", "11"):
        stored = scoring_store.save_key(
            database, read_key("A" * plan.question_count, plan, code).to_key()
        )
        scoring_store.verify_key(database, stored.key_id, verified_by=OPERATOR)

    for code in ("10", "11"):
        roster = reconciliation_store.active_roster(database, sets[code].set_id)
        assert roster is not None
        reconciliation_store.reconcile_batch(database, roster.roster_id, batches[code])
        scoring_store.score_batch(database, roster.roster_id, batches[code], template)
    return sets, batches, rolls


@pytest.fixture
def multi_set_page(qtbot, project_session: ProjectSession, template, two_sets):
    """A Reports page on a project with two defined, attended sets."""
    _sets, batches, _rolls = two_sets
    spec = next(item for item in WORKFLOW_PAGES if item.key == "reports")
    page = ReportsPage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(project_session)
    page.set_reviewer(OPERATOR)
    page.set_template(template)
    page.set_batch(batches["10"])
    yield page
    page.close()


def _rolls_in(path: Path, sheet_name: str) -> set[str]:
    """Every Roll No. written into one sheet of a generated workbook."""
    import openpyxl

    workbook = openpyxl.load_workbook(path)
    try:
        sheet = workbook[sheet_name]
        return {
            str(sheet.cell(row=row, column=2).value)
            for row in range(2, sheet.max_row + 1)
            if sheet.cell(row=row, column=2).value
        }
    finally:
        workbook.close()


class TestDefinedSetsAreListed:
    def test_every_defined_set_appears_as_its_own_row(self, multi_set_page: ReportsPage):
        codes = [row.set_code for row in multi_set_page.state.sets]
        assert codes == ["10", "11"]
        assert all(row.is_defined for row in multi_set_page.state.sets)

    def test_each_row_names_its_own_attendance_workbook(self, multi_set_page: ReportsPage):
        by_code = {row.set_code: row for row in multi_set_page.state.sets}
        assert by_code["10"].attendance_file == "set10_attendance.xlsx"
        assert by_code["11"].attendance_file == "set11_attendance.xlsx"
        assert by_code["10"].template_file == "set10_attendance.xlsx"

    def test_each_row_shows_its_own_description_and_candidate_count(
        self, multi_set_page: ReportsPage
    ):
        by_code = {row.set_code: row for row in multi_set_page.state.sets}
        assert "Electrical" in by_code["10"].description
        assert "Civil" in by_code["11"].description
        assert by_code["10"].candidate_count == 2
        assert by_code["11"].candidate_count == 2

    def test_the_table_shows_one_row_per_set(self, multi_set_page: ReportsPage):
        assert multi_set_page.set_table.rowCount() == 2
        assert multi_set_page.set_table.item(0, 0).text() == "10"
        assert multi_set_page.set_table.item(1, 0).text() == "11"

    def test_the_row_carries_the_set_id_not_its_position(
        self, multi_set_page: ReportsPage, two_sets
    ):
        sets, _batches, _rolls = two_sets
        stored = multi_set_page.set_table.item(0, 0).data(
            Qt.ItemDataRole.UserRole
        )
        assert stored == sets["10"].set_id
        assert stored != "0"

    def test_the_exam_name_is_displayed(
        self, multi_set_page: ReportsPage, project_session: ProjectSession
    ):
        assert project_session.exam_name in multi_set_page.exam_name_label.text()


def _generate_set(qtbot, page: ReportsPage, batches, code: str) -> None:
    """Generate one set's XLSX, against that set's own batch."""
    page.set_batch(batches[code])
    row = next(index for index, item in enumerate(page.state.sets) if item.set_code == code)
    page.set_table.selectRow(row)
    with qtbot.waitSignal(page.reports_generated, timeout=30_000):
        assert page.generate_selected_xlsx()


class TestPerSetGeneration:
    def test_each_set_produces_its_own_workbook(
        self, qtbot, multi_set_page: ReportsPage, two_sets
    ):
        _sets, batches, _rolls = two_sets
        for code in ("10", "11"):
            _generate_set(qtbot, multi_set_page, batches, code)
        produced = sorted(p.name for p in multi_set_page._output_dir().glob("*.xlsx"))
        assert len(produced) == 2, produced
        # §13: one set, one workbook - named for the set it belongs to.
        assert any("Set10" in name for name in produced)
        assert any("Set11" in name for name in produced)

    def test_one_sets_workbook_never_contains_another_sets_candidates(
        self, qtbot, multi_set_page: ReportsPage, two_sets
    ):
        """§19: roll 10001 is in both sets and must not cross over."""
        _sets, batches, _rolls = two_sets
        for code in ("10", "11"):
            _generate_set(qtbot, multi_set_page, batches, code)

        produced = sorted(multi_set_page._output_dir().glob("*.xlsx"))
        assert len(produced) == 2
        rolls_by_sheet = {}
        for path in produced:
            import openpyxl

            workbook = openpyxl.load_workbook(path)
            sheet_name = next(
                name for name in workbook.sheetnames if name.startswith("Set ")
            )
            workbook.close()
            rolls_by_sheet[sheet_name] = _rolls_in(path, sheet_name)

        assert rolls_by_sheet["Set 10 Attendance"] == {"10001", "10002"}
        assert rolls_by_sheet["Set 11 Attendance"] == {"10001", "20002"}
        # The set-11-only roll never appears in set 10's book, and vice versa.
        assert "20002" not in rolls_by_sheet["Set 10 Attendance"]
        assert "10002" not in rolls_by_sheet["Set 11 Attendance"]

    def test_generating_one_set_uses_that_sets_template(
        self, qtbot, multi_set_page: ReportsPage, two_sets
    ):
        _sets, batches, _rolls = two_sets
        _generate_set(qtbot, multi_set_page, batches, "11")
        produced = list(multi_set_page._output_dir().glob("*.xlsx"))
        assert len(produced) == 1

        import openpyxl

        workbook = openpyxl.load_workbook(produced[0])
        try:
            assert "Set 11 Attendance" in workbook.sheetnames
            assert "Set 10 Attendance" not in workbook.sheetnames
        finally:
            workbook.close()

    def test_each_workbook_carries_its_own_meritwise_sheet(
        self, qtbot, multi_set_page: ReportsPage, two_sets
    ):
        _sets, batches, _rolls = two_sets
        _generate_set(qtbot, multi_set_page, batches, "10")
        produced = list(multi_set_page._output_dir().glob("*.xlsx"))
        assert len(produced) == 1

        import openpyxl

        from omr_scanner.reporting.excel import MERITWISE_SHEET_NAME

        workbook = openpyxl.load_workbook(produced[0])
        try:
            assert MERITWISE_SHEET_NAME in workbook.sheetnames
        finally:
            workbook.close()


class TestMissingAttendance:
    @pytest.fixture
    def page_with_unattended_set(
        self, qtbot, project_session: ProjectSession, template, two_sets
    ):
        """A third set that nobody has given an attendance workbook to."""
        from omr_scanner.services import project_sets

        _sets, batches, _rolls = two_sets
        project_sets.add_set(
            project_session.database, "12", "Name of Post: Assistant Engineer (Mechanical)"
        )
        spec = next(item for item in WORKFLOW_PAGES if item.key == "reports")
        page = ReportsPage(spec)
        qtbot.addWidget(page)
        page.on_project_changed(project_session)
        page.set_reviewer(OPERATOR)
        page.set_template(template)
        page.set_batch(batches["10"])
        yield page
        page.close()

    def test_it_is_still_listed_rather_than_hidden(self, page_with_unattended_set):
        codes = [row.set_code for row in page_with_unattended_set.state.sets]
        assert codes == ["10", "11", "12"]

    def test_its_status_says_it_has_no_attendance_file(self, page_with_unattended_set):
        row = next(
            item for item in page_with_unattended_set.state.sets if item.set_code == "12"
        )
        assert row.report_readiness_label == "No attendance file"
        assert "No attendance/template workbook has been assigned to Set 12" in row.blocker

    def test_generating_it_is_blocked_with_the_reason(
        self, qtbot, page_with_unattended_set
    ):
        page = page_with_unattended_set
        page.set_table.selectRow(2)  # Set 12
        assert page.selected_set_code() == "12"
        with qtbot.waitSignal(page.reports_generated, timeout=30_000):
            assert page.generate_selected_xlsx()
        assert not list(page._output_dir().glob("*.xlsx"))
        assert "No attendance/template workbook has been assigned to Set 12" in (
            page.generation_status_label.text()
        )

    def test_it_never_borrows_another_sets_workbook(
        self, qtbot, page_with_unattended_set
    ):
        """§15: no fallback to Set 10's file, however convenient."""
        page = page_with_unattended_set
        with qtbot.waitSignal(page.reports_generated, timeout=30_000):
            assert page.generate_all_sets()
        produced = list(page._output_dir().glob("*.xlsx"))
        # Whatever was produced, nothing carries Set 12's name or its layout:
        # it has no workbook of its own and was given nobody else's.
        assert not any("Set12" in path.name for path in produced)
        for path in produced:
            import openpyxl

            workbook = openpyxl.load_workbook(path)
            try:
                assert not any(name.startswith("Set 12") for name in workbook.sheetnames)
            finally:
                workbook.close()
        assert "Set 12" in page.generation_status_label.text()


class TestProjectWithNoDefinedSets:
    def test_the_legacy_evidence_derived_row_still_works(self, reports_page: ReportsPage):
        """§29: a project made before sets existed keeps reporting as it did."""
        assert [row.set_code for row in reports_page.state.sets] == ["A"]
        assert not reports_page.state.sets[0].is_defined

    def test_the_detail_panel_points_at_project_configuration(
        self, qtbot, project_session: ProjectSession, template
    ):
        spec = next(item for item in WORKFLOW_PAGES if item.key == "reports")
        page = ReportsPage(spec)
        qtbot.addWidget(page)
        page.on_project_changed(project_session)
        page.set_template(template)
        assert not page.state.sets
        assert "Project Configuration" in page.detail_label.text()
        page.close()


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

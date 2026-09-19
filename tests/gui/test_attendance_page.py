"""The Attendance stage: importing a roster and reconciling it (Phase 7).

Scope:
    The real page, a real project, a real database and the real reconciliation
    services. The dialogs that own a native file chooser are never exercised;
    the methods behind them (:meth:`AttendancePage.import_from`,
    :meth:`AttendancePage.save_sample_to`) are, which is where the behaviour
    lives.

Privacy:
    Every identifier and name here is fictional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox, QPushButton

from omr_scanner.config import AppConfig
from omr_scanner.domain.reconciliation import (
    AttendanceState,
    ReconciliationReason,
    ReconciliationStatus,
    ResolutionState,
)
from omr_scanner.gui.attendance.import_dialog import RosterImportDialog
from omr_scanner.gui.attendance.page import AttendancePage
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.services import batch_store, reconciliation_store
from omr_scanner.services.candidate_import import (
    ColumnMapping,
    read_roster,
    sample_template_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from omr_scanner.services import ProjectSession

OPERATOR = "Dr. A. Rahman"

ROSTER_CSV = (
    "Sl.No.,Roll No.,Name,Total (90),Merit\n"
    "1,100001,CANDIDATE A,55,1\n"
    "2,100002,CANDIDATE B,60,2\n"
    "3,100003,CANDIDATE C,ABSENT,---\n"
    "4,100004,CANDIDATE D,,---\n"
    "5,100005,CANDIDATE E, abs ,---\n"
)


@pytest.fixture
def roster_file(tmp_path: Path) -> Path:
    path = tmp_path / "candidates.csv"
    path.write_text(ROSTER_CSV, encoding="utf-8")
    return path


@pytest.fixture
def batch(project_session: ProjectSession, tmp_path: Path) -> str:
    """A batch whose scans carry the acceptance scenario's roll numbers.

    Written straight into `batch_scan`: this module tests the Attendance page,
    and running real recognition for it would make every test ten seconds
    slower to prove something `tests/integration/` already proves.
    """
    from datetime import UTC, datetime

    from omr_scanner.database.models import BatchScan, ScanBatch

    database = project_session.database
    batch_id = batch_store.new_batch_id()
    now = datetime.now(UTC)
    scripts = [
        ("s1.png", "100001"),
        ("s2a.png", "100002"),
        ("s2b.png", "100002"),
        ("s3.png", "100005"),
        ("s4.png", "999999"),
    ]
    with database.session() as session:
        session.add(
            ScanBatch(
                batch_id=batch_id,
                created_at=now,
                updated_at=now,
                source_folder=str(tmp_path),
                status="completed",
                total_scans=len(scripts),
            )
        )
        session.flush()
        for index, (name, identifier) in enumerate(scripts):
            session.add(
                BatchScan(
                    batch_id=batch_id,
                    batch_index=index,
                    source_path=str(tmp_path / name),
                    filename=name,
                    status="completed",
                    identifier_value=identifier,
                    result_json="",
                )
            )
    return batch_id


@pytest.fixture
def page(qtbot, project_session: ProjectSession, batch):
    """An Attendance page on an open project, shut down deterministically."""
    spec = next(item for item in WORKFLOW_PAGES if item.key == "attendance")
    attendance = AttendancePage(spec)
    qtbot.addWidget(attendance)
    attendance.on_project_changed(project_session)
    attendance.set_operator(OPERATOR)
    attendance.set_batch(batch)
    yield attendance
    attendance.close()


def import_roster(qtbot, page: AttendancePage, path: Path) -> None:
    """Import a roster through the page, awaiting the reconciliation."""
    validation = read_roster(path)
    with qtbot.waitSignal(page.reconciled, timeout=10_000):
        assert page.commit_roster(validation) is True


@pytest.fixture
def imported(qtbot, page: AttendancePage, roster_file: Path) -> AttendancePage:
    """A page with the acceptance roster imported and reconciled."""
    import_roster(qtbot, page, roster_file)
    return page


def entries(page: AttendancePage) -> dict[str, object]:
    return {entry.candidate_id: entry for entry in page.state.entries}


# Named stand-ins for the modal dialogs. Named rather than lambdas because a
# `QMessageBox` static method is called positionally with (parent, title, text)
# and the message is the third argument - which is the thing worth asserting.
def _capture(sink: list[str]) -> Callable[..., None]:
    """Return a QMessageBox stand-in that records the message it was given."""

    def recorded(*args: object, **_kwargs: object) -> None:
        sink.append(str(args[2]) if len(args) > 2 else "")

    return recorded


def _ignore(*_args: object, **_kwargs: object) -> None:
    """A QMessageBox stand-in that does nothing."""


def _answer_no(*_args: object, **_kwargs: object) -> QMessageBox.StandardButton:
    """A QMessageBox.question stand-in that declines."""
    return QMessageBox.StandardButton.No


def select(page: AttendancePage, candidate_id: str) -> None:
    """Select the row filed under ``candidate_id``."""
    for row, entry in enumerate(page.state.entries):
        if entry.candidate_id == candidate_id:
            page.table.selectRow(row)
            return
    raise AssertionError(f"{candidate_id} is not in the current table")


# ----------------------------------------------------------------------
class TestEmptyState:
    def test_the_page_says_no_list_has_been_imported(self, page: AttendancePage):
        assert "No candidate list" in page.roster_label.text()

    def test_reconciling_is_unavailable_without_a_roster(self, page: AttendancePage):
        assert page.reconcile_button.isEnabled() is False
        assert page.reconcile() is False

    def test_importing_is_available_with_a_project_open(self, page: AttendancePage):
        assert page.import_button.isEnabled() is True

    def test_the_operator_name_reaches_the_page(self, page: AttendancePage):
        assert OPERATOR in page.operator_label.text()

    def test_no_reviewer_name_is_said_plainly(self, page: AttendancePage):
        page.set_operator("")
        assert "cannot be recorded without a name" in page.operator_label.text()


class TestImport:
    def test_importing_a_roster_makes_it_active(self, imported: AttendancePage):
        assert "candidates.csv" in imported.roster_label.text()
        assert "5 candidate(s)" in imported.roster_label.text()

    def test_importing_reconciles_at_once(self, imported: AttendancePage):
        assert imported.state.entries
        assert "Registered" in imported.summary_label.text()

    def test_the_summary_reports_every_count(self, imported: AttendancePage):
        text = imported.summary_label.text()
        for label in (
            "Registered", "Expected present", "Marked absent", "Scripts",
            "Matched", "Absent confirmed", "Unknown ID", "Duplicate script",
            "Present without script", "Absent with script",
        ):
            assert label in text

    def test_the_summary_says_work_remains(self, imported: AttendancePage):
        assert "still need review" in imported.summary_label.text()

    def test_a_second_import_asks_before_replacing(
        self, qtbot, imported: AttendancePage, tmp_path, monkeypatch
    ):
        asked: list[str] = []

        def refuse(*args: object, **_kwargs: object) -> QMessageBox.StandardButton:
            asked.append(str(args[2]) if len(args) > 2 else "")
            return QMessageBox.StandardButton.No

        monkeypatch.setattr(QMessageBox, "question", refuse)
        other = tmp_path / "other.csv"
        other.write_text("Roll No.,Name\n200001,CANDIDATE Z\n", encoding="utf-8")
        assert imported.import_from(other) is False
        assert asked and "already uses" in asked[0]
        # ...and the original roster is still in force.
        assert "candidates.csv" in imported.roster_label.text()


class TestReconciliationTable:
    def test_the_table_opens_on_the_exceptions(self, imported: AttendancePage):
        # The default filter is the work, not the whole cohort.
        assert imported.status_filter.currentText() == "Exceptions only"
        assert all(e.status.is_exception for e in imported.state.entries)

    def test_every_classification_is_reachable(self, imported: AttendancePage):
        imported.status_filter.setCurrentIndex(0)  # Everything
        found = {entry.status for entry in imported.state.entries}
        assert found == {
            ReconciliationStatus.MATCHED,
            ReconciliationStatus.DUPLICATE_SCRIPT,
            ReconciliationStatus.ABSENT_CONFIRMED,
            ReconciliationStatus.PRESENT_WITHOUT_SCRIPT,
            ReconciliationStatus.ABSENT_WITH_SCRIPT,
            ReconciliationStatus.UNKNOWN_ID,
        }

    def test_the_status_column_uses_operator_language(self, imported: AttendancePage):
        imported.status_filter.setCurrentIndex(0)
        texts = {
            imported.table.item(row, 0).text()
            for row in range(imported.table.rowCount())
        }
        assert "Marked absent but script found" in texts
        assert "Present but no script found" in texts
        # ...and never the enum value.
        assert not any("_" in text for text in texts)

    def test_filtering_by_one_status_narrows_the_table(self, imported: AttendancePage):
        imported.status_filter.setCurrentIndex(
            [imported.status_filter.itemText(i) for i in range(
                imported.status_filter.count()
            )].index("Unknown candidate ID")
        )
        assert [e.candidate_id for e in imported.state.entries] == ["999999"]
        assert imported.table.rowCount() == 1

    def test_searching_by_candidate_id(self, imported: AttendancePage):
        imported.status_filter.setCurrentIndex(0)
        imported.search_box.setText("100004")
        assert [e.candidate_id for e in imported.state.entries] == ["100004"]

    def test_searching_by_name(self, imported: AttendancePage):
        imported.status_filter.setCurrentIndex(0)
        imported.search_box.setText("CANDIDATE D")
        assert [e.candidate_id for e in imported.state.entries] == ["100004"]

    def test_the_row_count_reports_outstanding_work(self, imported: AttendancePage):
        assert "needing review" in imported.table_count_label.text()

    def test_attendance_is_shown_in_words(self, imported: AttendancePage):
        imported.status_filter.setCurrentIndex(0)
        select(imported, "100003")
        row = imported.table.currentRow()
        assert imported.table.item(row, 3).text() == "Marked absent"


class TestCoOccurringIssues:
    def test_a_second_issue_is_shown_beside_the_headline(
        self, qtbot, imported: AttendancePage
    ):
        # Give 100005 (marked absent, one script) a second script, so it is
        # both absent-with-script and duplicated. Driven through the page, so
        # this also covers the route an operator actually takes.
        select(imported, "999999")
        imported.assign_edit.setText("100005")
        imported.reason_combo.setCurrentText(ReconciliationReason.SHEET_SWAPPED.label)
        with qtbot.waitSignal(imported.resolution_recorded, timeout=10_000):
            assert imported.assign_selected_script() is True
        select(imported, "100005")
        row = imported.table.currentRow()
        text = imported.table.item(row, 0).text()
        assert "Marked absent but script found" in text
        assert "Duplicate script" in text, "the second problem must stay visible"

    def test_the_detail_panel_lists_every_issue(
        self, qtbot, imported: AttendancePage
    ):
        database = imported.database
        roster = imported.state.roster
        scan = _scan_id(imported, "s4.png")
        reconciliation_store.assign_script(
            database, roster.roster_id, imported.state.batch_id, scan,
            candidate_id="100005", operator=OPERATOR,
            reason=ReconciliationReason.SHEET_SWAPPED,
        )
        imported.refresh_table()
        select(imported, "100005")
        assert "This entry also has" in imported.detail_label.text()


def _scan_id(page: AttendancePage, filename: str) -> int:
    from sqlalchemy import select as sa_select

    from omr_scanner.database.models import BatchScan

    with page.database.session() as session:
        return session.scalars(
            sa_select(BatchScan.scan_id)
            .where(BatchScan.batch_id == page.state.batch_id)
            .where(BatchScan.filename == filename)
        ).first()


class TestDetailPanel:
    def test_selecting_an_entry_describes_it(self, imported: AttendancePage):
        select(imported, "100004")
        text = imported.detail_label.text()
        assert "Present but no script found" in text
        assert "may be missing" in text

    def test_the_imported_attendance_is_shown_verbatim(self, imported: AttendancePage):
        select(imported, "100005")
        text = imported.detail_label.text()
        assert "Candidate list said" in text
        assert "Marked absent" in text
        # The raw cell, spacing trimmed - so an operator can see what was written.
        assert "cell read 'abs'" in text

    def test_every_script_is_listed(self, imported: AttendancePage):
        select(imported, "100002")
        assert imported.scripts_list.count() == 2
        assert "read as 100002" in imported.scripts_list.item(0).text()

    def test_an_entry_with_no_scripts_lists_none(self, imported: AttendancePage):
        select(imported, "100004")
        assert imported.scripts_list.count() == 0

    def test_a_fresh_entry_says_no_decisions_yet(self, imported: AttendancePage):
        select(imported, "100004")
        assert "No decisions" in imported.history_label.text()


class TestResolution:
    def test_assigning_an_unknown_script_to_a_candidate(
        self, qtbot, imported: AttendancePage
    ):
        select(imported, "999999")
        imported.assign_edit.setText("100004")
        imported.reason_combo.setCurrentText(
            ReconciliationReason.MISREAD_IDENTIFIER.label
        )
        with qtbot.waitSignal(imported.resolution_recorded, timeout=10_000):
            assert imported.assign_selected_script() is True

        imported.status_filter.setCurrentIndex(0)
        found = entries(imported)
        assert found["100004"].status is ReconciliationStatus.MATCHED
        assert "999999" not in found
        # The machine's reading is kept.
        assert found["100004"].scripts[0].script.machine_candidate_id == "999999"

    def test_assigning_to_a_candidate_who_already_has_one_shows_the_duplicate(
        self, qtbot, imported: AttendancePage
    ):
        select(imported, "999999")
        imported.assign_edit.setText("100001")
        imported.reason_combo.setCurrentText(ReconciliationReason.SHEET_SWAPPED.label)
        with qtbot.waitSignal(imported.resolution_recorded, timeout=10_000):
            assert imported.assign_selected_script() is True
        imported.status_filter.setCurrentIndex(0)
        assert entries(imported)["100001"].status is (
            ReconciliationStatus.DUPLICATE_SCRIPT
        )

    def test_assigning_to_an_unregistered_candidate_is_refused(
        self, imported: AttendancePage, monkeypatch
    ):
        warned: list[str] = []
        monkeypatch.setattr(
            QMessageBox,
            "warning",
            _capture(warned),
        )
        select(imported, "999999")
        imported.assign_edit.setText("nobody")
        assert imported.assign_selected_script() is False
        assert warned and "not on the imported candidate list" in warned[0]

    def test_a_decision_without_a_name_is_refused(
        self, imported: AttendancePage, monkeypatch
    ):
        warned: list[str] = []
        monkeypatch.setattr(
            QMessageBox,
            "warning",
            _capture(warned),
        )
        imported.set_operator("")
        select(imported, "999999")
        imported.assign_edit.setText("100004")
        assert imported.assign_selected_script() is False
        assert warned and "Settings" in warned[0]
        assert entries(imported)["999999"].status is ReconciliationStatus.UNKNOWN_ID

    def test_setting_a_duplicate_script_aside(self, qtbot, imported: AttendancePage):
        select(imported, "100002")
        imported.scripts_list.setCurrentRow(1)
        imported.reason_combo.setCurrentText(
            ReconciliationReason.ACCIDENTAL_RESCAN.label
        )
        with qtbot.waitSignal(imported.resolution_recorded, timeout=10_000):
            assert imported.toggle_selected_exclusion() is True

        imported.status_filter.setCurrentIndex(0)
        entry = entries(imported)["100002"]
        assert entry.status is ReconciliationStatus.MATCHED
        assert entry.script_count == 1
        assert len(entry.scripts) == 2, "set aside, never deleted"

    def test_a_set_aside_script_is_still_shown(self, qtbot, imported: AttendancePage):
        select(imported, "100002")
        imported.scripts_list.setCurrentRow(1)
        with qtbot.waitSignal(imported.resolution_recorded, timeout=10_000):
            imported.toggle_selected_exclusion()
        imported.status_filter.setCurrentIndex(0)
        select(imported, "100002")
        texts = [
            imported.scripts_list.item(i).text()
            for i in range(imported.scripts_list.count())
        ]
        assert any("SET ASIDE" in text for text in texts)

    def test_overriding_attendance_keeps_the_imported_value(
        self, qtbot, imported: AttendancePage
    ):
        select(imported, "100005")
        imported.reason_combo.setCurrentText(
            ReconciliationReason.CANDIDATE_ATTENDED.label
        )
        with qtbot.waitSignal(imported.resolution_recorded, timeout=10_000):
            assert imported.toggle_attendance() is True

        imported.status_filter.setCurrentIndex(0)
        entry = entries(imported)["100005"]
        assert entry.effective_attendance is AttendanceState.PRESENT
        assert entry.candidate.imported_attendance is AttendanceState.ABSENT
        assert entry.status is ReconciliationStatus.MATCHED

    def test_an_override_is_shown_beside_what_was_imported(
        self, qtbot, imported: AttendancePage
    ):
        select(imported, "100005")
        with qtbot.waitSignal(imported.resolution_recorded, timeout=10_000):
            imported.toggle_attendance()
        imported.status_filter.setCurrentIndex(0)
        select(imported, "100005")
        row = imported.table.currentRow()
        assert "list said marked absent" in imported.table.item(row, 3).text()

    def test_attendance_cannot_be_overridden_for_an_unknown_script(
        self, imported: AttendancePage, monkeypatch
    ):
        told: list[str] = []
        monkeypatch.setattr(
            QMessageBox,
            "information",
            _capture(told),
        )
        select(imported, "999999")
        assert imported.toggle_attendance() is False
        assert told and "candidate on the imported list" in told[0]

    def test_accepting_an_exception_as_is(self, qtbot, imported: AttendancePage):
        select(imported, "100004")
        imported.reason_combo.setCurrentText(
            ReconciliationReason.SCRIPT_MISSING.label
        )
        with qtbot.waitSignal(imported.resolution_recorded, timeout=10_000):
            assert imported.toggle_dismissed() is True
        imported.status_filter.setCurrentIndex(0)
        entry = entries(imported)["100004"]
        assert entry.resolution is ResolutionState.DISMISSED
        # ...and it keeps its classification rather than looking fixed.
        assert entry.status is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT

    def test_an_accepted_exception_can_be_put_back(
        self, qtbot, imported: AttendancePage
    ):
        select(imported, "100004")
        with qtbot.waitSignal(imported.resolution_recorded, timeout=10_000):
            imported.toggle_dismissed()
        imported.status_filter.setCurrentIndex(0)
        select(imported, "100004")
        assert imported.dismiss_button.text() == "Put Back On The List"
        with qtbot.waitSignal(imported.resolution_recorded, timeout=10_000):
            imported.toggle_dismissed()
        assert entries(imported)["100004"].resolution is ResolutionState.OPEN

    def test_other_without_an_explanation_is_refused(
        self, imported: AttendancePage, monkeypatch
    ):
        warned: list[str] = []
        monkeypatch.setattr(
            QMessageBox,
            "warning",
            _capture(warned),
        )
        select(imported, "100004")
        imported.reason_combo.setCurrentText(ReconciliationReason.OTHER.label)
        imported.reason_text.setText("")
        assert imported.toggle_dismissed() is False
        assert warned and "explanation" in warned[0]

    def test_the_counts_update_after_a_decision(
        self, qtbot, imported: AttendancePage
    ):
        before = imported.summary_label.text()
        select(imported, "100004")
        imported.reason_combo.setCurrentText(
            ReconciliationReason.SCRIPT_MISSING.label
        )
        with qtbot.waitSignal(imported.resolution_recorded, timeout=10_000):
            imported.toggle_dismissed()
        assert imported.summary_label.text() != before
        assert "Accepted as-is <b>1</b>" in imported.summary_label.text()


class TestHistory:
    def test_a_decision_appears_in_the_history(self, qtbot, imported: AttendancePage):
        select(imported, "999999")
        imported.assign_edit.setText("100004")
        imported.reason_combo.setCurrentText(
            ReconciliationReason.MISREAD_IDENTIFIER.label
        )
        with qtbot.waitSignal(imported.resolution_recorded, timeout=10_000):
            imported.assign_selected_script()

        imported.status_filter.setCurrentIndex(0)
        select(imported, "100004")
        text = imported.history_label.text()
        assert "Script assigned" in text
        assert OPERATOR in text
        assert "999999" in text and "100004" in text
        assert "misread" in text.lower()


class TestSampleTemplate:
    def test_saving_the_sample_writes_the_packaged_bytes(
        self, page: AttendancePage, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(QMessageBox, "information", _ignore)
        destination = tmp_path / "sample.xlsx"
        assert page.save_sample_to(destination) is True
        assert destination.read_bytes() == sample_template_bytes()

    def test_the_saved_sample_can_be_imported_straight_back(
        self, qtbot, page: AttendancePage, tmp_path, monkeypatch
    ):
        # The whole point of offering it.
        monkeypatch.setattr(QMessageBox, "information", _ignore)
        destination = tmp_path / "sample.xlsx"
        page.save_sample_to(destination)
        import_roster(qtbot, page, destination)
        assert page.state.roster is not None
        assert page.state.roster.candidate_count > 0

    def test_replacing_an_existing_file_is_confirmed_first(
        self, page: AttendancePage, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            QMessageBox, "question", _answer_no
        )
        destination = tmp_path / "sample.xlsx"
        destination.write_bytes(b"mine")
        assert page.save_sample_to(destination) is False
        assert destination.read_bytes() == b"mine"


class TestImportDialog:
    def test_the_dialog_previews_and_maps_a_csv(
        self, qtbot, roster_file: Path
    ):
        dialog = RosterImportDialog(roster_file)
        qtbot.addWidget(dialog)
        qtbot.waitUntil(lambda: dialog.validation is not None, timeout=10_000)
        assert dialog.preview_table.rowCount() == 5
        assert dialog.current_mapping() == ColumnMapping(
            candidate_id=1, name=2, attendance=3
        )
        assert dialog.import_button.isEnabled() is True
        assert "5 candidate(s)" in dialog.validation_label.text()
        dialog.close()

    def test_a_worksheet_chooser_appears_only_for_a_workbook(
        self, qtbot, roster_file: Path
    ):
        dialog = RosterImportDialog(roster_file)
        qtbot.addWidget(dialog)
        assert dialog.sheet_combo.isVisibleTo(dialog) is False
        dialog.close()

    def test_changing_the_mapping_re_reads_the_file(
        self, qtbot, roster_file: Path
    ):
        dialog = RosterImportDialog(roster_file)
        qtbot.addWidget(dialog)
        qtbot.waitUntil(lambda: dialog.validation is not None, timeout=10_000)
        # Map Sl.No. as the candidate ID instead.
        dialog.id_combo.setCurrentIndex(dialog.id_combo.findData(0))
        qtbot.waitUntil(
            lambda: dialog.validation is not None
            and dialog.validation.mapping.candidate_id == 0,
            timeout=10_000,
        )
        assert [i.candidate_id for i in dialog.validation.candidates] == [
            "1", "2", "3", "4", "5",
        ]
        dialog.close()

    def test_a_duplicate_roster_cannot_be_imported(self, qtbot, tmp_path):
        path = tmp_path / "dupes.csv"
        path.write_text(
            "Roll No.,Name\n100001,CANDIDATE A\n100001,CANDIDATE B\n",
            encoding="utf-8",
        )
        dialog = RosterImportDialog(path)
        qtbot.addWidget(dialog)
        qtbot.waitUntil(
            lambda: "cannot be imported" in dialog.validation_label.text(),
            timeout=10_000,
        )
        assert dialog.import_button.isEnabled() is False
        assert "100001" in dialog.validation_label.text()
        dialog.close()

    def test_an_ambiguous_id_column_asks_the_operator(self, qtbot, tmp_path):
        path = tmp_path / "ambiguous.csv"
        path.write_text(
            "Roll No.,Candidate ID,Name\n1,X-1,A\n2,X-2,B\n", encoding="utf-8"
        )
        dialog = RosterImportDialog(path)
        qtbot.addWidget(dialog)
        assert "More than one column" in dialog.validation_label.text()
        assert dialog.import_button.isEnabled() is False
        dialog.close()

    def test_an_unreadable_file_says_what_is_wrong(self, qtbot, tmp_path):
        path = tmp_path / "broken.xlsx"
        path.write_bytes(b"not a workbook")
        dialog = RosterImportDialog(path)
        qtbot.addWidget(dialog)
        assert "could not be opened" in dialog.validation_label.text()
        assert dialog.import_button.isEnabled() is False
        dialog.close()

    def test_a_roster_with_no_attendance_column_warns(self, qtbot, tmp_path):
        path = tmp_path / "ids_only.csv"
        path.write_text("Roll No.,Name\n100001,CANDIDATE A\n", encoding="utf-8")
        dialog = RosterImportDialog(path)
        qtbot.addWidget(dialog)
        qtbot.waitUntil(lambda: dialog.validation is not None, timeout=10_000)
        assert "No marks/attendance column" in dialog.validation_label.text()
        assert dialog.import_button.isEnabled() is True, "still importable"
        dialog.close()


class TestResponsiveness:
    def test_the_event_loop_keeps_running_while_reconciliation_runs(
        self, qtbot, page: AttendancePage, roster_file: Path
    ):
        # A QTimer that could not tick if reconciliation were happening on the
        # GUI thread is the honest form of "the window stayed responsive".
        ticks: list[int] = []
        heartbeat = QTimer()
        heartbeat.setInterval(5)
        heartbeat.timeout.connect(lambda: ticks.append(1))
        heartbeat.start()
        try:
            import_roster(qtbot, page, roster_file)
        finally:
            heartbeat.stop()
        assert ticks, "the GUI thread was blocked while reconciliation ran"


class TestPersistenceAcrossPages:
    def test_decisions_are_still_there_on_a_fresh_page(
        self, qtbot, imported: AttendancePage, project_session, batch
    ):
        select(imported, "100004")
        imported.reason_combo.setCurrentText(
            ReconciliationReason.SCRIPT_MISSING.label
        )
        with qtbot.waitSignal(imported.resolution_recorded, timeout=10_000):
            imported.toggle_dismissed()

        spec = next(item for item in WORKFLOW_PAGES if item.key == "attendance")
        fresh = AttendancePage(spec)
        qtbot.addWidget(fresh)
        try:
            fresh.on_project_changed(project_session)
            fresh.set_operator(OPERATOR)
            fresh.set_batch(batch)
            fresh.status_filter.setCurrentIndex(0)
            found = {e.candidate_id: e for e in fresh.state.entries}
            assert found["100004"].resolution is ResolutionState.DISMISSED
            assert found["100004"].reviewer == OPERATOR
        finally:
            fresh.close()


class TestWindowIntegration:
    def test_the_attendance_stage_is_no_longer_a_placeholder(self, qtbot, tmp_path):
        window = MainWindow(AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        try:
            page = window._attendance_page()
            assert isinstance(page, AttendancePage)
            assert window.show_page("attendance") is True
        finally:
            window.close()

    def test_the_reviewer_name_reaches_the_page(self, qtbot, tmp_path):
        config = AppConfig().with_reviewer_name("Dr. Configured")
        window = MainWindow(config, config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        try:
            page = window._attendance_page()
            assert page is not None
            assert page.state.operator == "Dr. Configured"
        finally:
            window.close()

    def test_the_window_can_open_a_batch_for_reconciliation(self, qtbot, tmp_path):
        window = MainWindow(AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        try:
            assert window.reconcile_batch("abc") is True
            page = window._attendance_page()
            assert page is not None and page.state.batch_id == "abc"
        finally:
            window.close()


class TestStableObjectNames:
    def test_the_widgets_can_be_found_by_name(self, page: AttendancePage):
        for name in (
            "activeRosterLabel",
            "importRosterButton",
            "downloadSampleTemplateButton",
            "reconcileButton",
            "reconciliationSummaryLabel",
            "reconciliationStatusFilter",
            "reconciliationResolutionFilter",
            "reconciliationSearchBox",
            "reconciliationTable",
            "reconciliationCountLabel",
            "reconciliationDetailLabel",
            "entryScriptsList",
            "assignCandidateEdit",
            "assignScriptButton",
            "excludeScriptButton",
            "overrideAttendanceButton",
            "dismissEntryButton",
            "reconciliationReasonCombo",
            "reconciliationReasonText",
            "reconciliationHistoryLabel",
            "reconciliationOperatorLabel",
        ):
            assert page.findChild(object, name) is not None, name

    def test_the_import_dialog_names_its_widgets(self, qtbot, roster_file: Path):
        dialog = RosterImportDialog(roster_file)
        qtbot.addWidget(dialog)
        try:
            for name in (
                "rosterFileLabel",
                "rosterSheetCombo",
                "candidateIdColumnCombo",
                "candidateNameColumnCombo",
                "attendanceColumnCombo",
                "rosterPreviewTable",
                "rosterValidationLabel",
                "confirmRosterImportButton",
            ):
                assert dialog.findChild(object, name) is not None, name
            assert isinstance(
                dialog.findChild(QPushButton, "confirmRosterImportButton"),
                QPushButton,
            )
        finally:
            dialog.close()

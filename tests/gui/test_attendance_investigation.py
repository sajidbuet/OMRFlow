"""Investigating an attendance exception from the scan itself (Step 6).

Scope:
    The Attendance stage as an operator reconciliation workstation: the two
    contradictions that most often mean a roll number was written or read
    wrongly, the evidence behind them, and a correction that goes through the
    same review ledger the Resolve stage writes.

The sheets are real: rendered, read by the real engine, stored by the real
batch recorder, so the inspector re-reads a genuine scan exactly as it would
for an operator.

    SYN_000001  170501  - its own candidate, present           -> Matched
    SYN_000002  170502  - its own candidate, present           -> Matched
    SYN_000003  170504  - written by 170503; 170504 was absent  -> the case
    SYN_000004  17050_  - last digit left blank                 -> unread ID

    Roster: 170501 P, 170502 P, 170503 P, 170504 ABSENT, 170505 P
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import cv2
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from tests.conftest import build_answer_sheet_template, render_marked_sheet
from tests.gui.test_resolve_page import sheet_marks

from omr_scanner.config import AppConfig
from omr_scanner.config.app_config import (
    MAX_SPLIT_RATIO,
    MIN_SPLIT_RATIO,
    load_app_config,
)
from omr_scanner.domain.reconciliation import ReconciliationStatus
from omr_scanner.domain.review import ConflictType, FieldKind, ReasonCode, ValueSource
from omr_scanner.gui.attendance.page import DEFAULT_SPLIT_RATIO, AttendancePage
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.services import (
    batch_store,
    open_project,
    project_sets,
    reconciliation_store,
    review_store,
)
from omr_scanner.services.batch_processor import process_batch
from omr_scanner.services.candidate_import import read_roster

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.services import ProjectSession

pytestmark = pytest.mark.gui

OPERATOR = "Dr. A. Rahman"
TIMEOUT_MS = 120_000

ROSTER = (
    "Roll No.,Name,Total\n"
    "170501,KARIM A,55\n"
    "170502,RAHIM B,60\n"
    "170503,SALMA C,48\n"
    "170504,NADIA D,ABSENT\n"
    "170505,FARID E,51\n"
)

SHEETS = (
    ("SYN_000001.png", "170501"),
    ("SYN_000002.png", "170502"),
    ("SYN_000003.png", "170504"),
    ("SYN_000004.png", "17050"),
)


@pytest.fixture
def template():
    return build_answer_sheet_template()


@pytest.fixture
def batch(project_session: ProjectSession, template, tmp_path: Path) -> str:
    """Render, read, store and conflict-check the four sheets."""
    scans = tmp_path / "scans"
    scans.mkdir()
    paths = []
    for name, roll in SHEETS:
        path = scans / name
        cv2.imwrite(str(path), render_marked_sheet(template, sheet_marks(roll)))
        paths.append(path)
    database = project_session.database
    batch_id = batch_store.create_batch(
        database, paths, identity=batch_store.BatchIdentity.of(template)
    )
    recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)
    report = process_batch(paths, template, on_result=recorder.record, workers=1)
    recorder.flush()
    batch_store.finalise_batch(database, batch_id)
    ids = batch_store.scan_ids_by_path(database, batch_id)
    for item in report.processed:
        review_store.sync_conflicts(
            database,
            batch_id=batch_id,
            scan_id=ids[item.source_path],
            result=item.result,
            template=template,
        )
    return batch_id


def _page(qtbot, project_session, template, batch) -> AttendancePage:
    spec = next(item for item in WORKFLOW_PAGES if item.key == "attendance")
    page = AttendancePage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(project_session)
    page.set_template(template)
    page.set_operator(OPERATOR)
    page.set_batch(batch)
    return page


@pytest.fixture
def roster_path(tmp_path: Path) -> Path:
    path = tmp_path / "attendance.csv"
    path.write_text(ROSTER, encoding="utf-8")
    return path


@pytest.fixture
def page(qtbot, project_session, template, batch, roster_path):
    """The Attendance page with the roster imported and reconciled."""
    attendance = _page(qtbot, project_session, template, batch)
    with qtbot.waitSignal(attendance.reconciled, timeout=TIMEOUT_MS):
        assert attendance.commit_roster(read_roster(roster_path), source_path=roster_path)
    yield attendance
    attendance.close()


@pytest.fixture
def confirmed(monkeypatch, page: AttendancePage) -> list[dict]:
    """Record every override warning, and confirm it."""
    seen: list[dict] = []

    def confirm(_shape: object, overrides: dict) -> bool:
        seen.append(dict(overrides))
        return True

    monkeypatch.setattr(page.inspector, "confirm_override", confirm)
    return seen


def select(page: AttendancePage, candidate_id: str) -> None:
    for row, entry in enumerate(page.state.entries):
        if entry.candidate_id == candidate_id:
            page.table.selectRow(row)
            return
    raise AssertionError(f"{candidate_id} is not shown")


def status_of(page: AttendancePage, candidate_id: str) -> ReconciliationStatus:
    entries = reconciliation_store.list_entries(
        page.database, page.state.roster.roster_id, page.state.batch_id
    )
    return next(item.status for item in entries if item.candidate_id == candidate_id)


def inspect(qtbot, page: AttendancePage) -> None:
    with qtbot.waitSignal(page.inspector.loaded, timeout=TIMEOUT_MS):
        assert page.inspect_selected_script() is True


def correct_id(qtbot, page: AttendancePage, value: str) -> None:
    page.inspector.editor(FieldKind.IDENTIFIER).setText(value)
    with qtbot.waitSignal(page.reconciled, timeout=TIMEOUT_MS):
        assert page.inspector.apply_correction(FieldKind.IDENTIFIER) is True


class TestTheStatesAreDistinguished:
    def test_the_chips_count_each_kind(self, page: AttendancePage):
        chips = {key: chip.text() for key, chip in page._chips.items()}
        assert chips["matched"] == "Matched  2"
        assert chips["missing"] == "Missing script  2"
        assert chips["absent"] == "Absent + script  1"
        assert chips["unrecognised"] == "Unrecognised  1"
        assert chips["duplicate"] == "Duplicates  0"

    def test_expected_present_with_no_script_is_missing(self, page: AttendancePage):
        assert status_of(page, "170503") is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT
        select(page, "170503")
        row = page.table.currentRow()
        assert page.table.item(row, 0).text() == "⚠ Missing script"
        assert "no script matched" in page.table.item(row, 6).text()

    def test_absent_with_a_script_is_its_own_contradiction(self, page: AttendancePage):
        assert status_of(page, "170504") is ReconciliationStatus.ABSENT_WITH_SCRIPT
        select(page, "170504")
        row = page.table.currentRow()
        assert page.table.item(row, 0).text() == "! Absent but script found"
        assert page.table.item(row, 5).text() == "170504"

    def test_the_problem_is_stated_in_the_detail_panel(self, page: AttendancePage):
        select(page, "170504")
        text = page.detail_label.text()
        assert "NADIA D" in text
        assert "Marked absent" in text
        assert "1 scanned script recognised as 170504" in text
        assert "Another candidate may have filled in this roll number" in text


class TestWhereToLook:
    def test_an_absent_candidates_script_suggests_who_wrote_it(self, page: AttendancePage):
        select(page, "170504")
        assert page.scripts_list.count() == 1
        assert "SYN_000003.png" in page.scripts_list.item(0).text()
        leads = [page.leads_list.item(i).text() for i in range(page.leads_list.count())]
        assert any(text.startswith("170503") and "1 edit away" in text for text in leads)
        # 170505 also differs by one digit; both are suggestions, neither is chosen.
        assert status_of(page, "170503") is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT

    def test_a_missing_script_points_at_the_misfiled_sheet(
        self, qtbot, page: AttendancePage
    ):
        select(page, "170503")
        leads = [page.leads_list.item(i).text() for i in range(page.leads_list.count())]
        assert any("SYN_000003.png" in text and "absent" in text for text in leads)
        row = next(i for i, text in enumerate(leads) if "SYN_000003.png" in text)
        page.leads_list.setCurrentRow(row)
        with qtbot.waitSignal(page.inspector.loaded, timeout=TIMEOUT_MS):
            assert page.follow_selected_lead() is True
        assert page.inspector.effective_value(FieldKind.IDENTIFIER) == "170504"

    def test_nothing_is_reassigned_by_a_lead(self, page: AttendancePage):
        select(page, "170503")
        select(page, "170504")
        assert status_of(page, "170503") is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT
        assert status_of(page, "170504") is ReconciliationStatus.ABSENT_WITH_SCRIPT


class TestInspectingTheScan:
    def test_the_original_scan_and_the_read_sheet_are_shown(
        self, qtbot, page: AttendancePage
    ):
        select(page, "170504")
        inspect(qtbot, page)
        inspector = page.inspector
        assert inspector.view_tabs.currentIndex() == 0, "the original scan first"
        assert inspector.view_tabs.tabText(0) == "Original scan"
        assert inspector.original_view.has_page is True
        assert inspector.read_view.has_page is True
        assert inspector.machine_value(FieldKind.IDENTIFIER) == "170504"
        assert inspector.editor(FieldKind.IDENTIFIER).text() == "170504"
        assert inspector.editor(FieldKind.SET_CODE).text() == "A"

    def test_enter_on_the_table_inspects(self, qtbot, page: AttendancePage):
        select(page, "170504")
        page.table.setFocus()
        with qtbot.waitSignal(page.inspector.loaded, timeout=TIMEOUT_MS):
            QTest.keyClick(page.table, Qt.Key.Key_Return)
        assert page.inspector.scan_id is not None

    def test_ctrl_f_focuses_the_search(self, qtbot, page: AttendancePage):
        from PySide6.QtWidgets import QApplication

        # A real key press, delivered only to an active window - as in the
        # Resolve stage's keyboard tests.
        page.show()
        page.activateWindow()
        page.raise_()
        page.table.setFocus()
        QApplication.processEvents()
        if not page.isActiveWindow():
            pytest.skip("this Qt platform plugin never activates a window")
        QTest.keyClick(page.table, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        assert page.focusWidget() is page.search_box

    def test_without_a_template_the_sheet_is_not_reread(
        self, qtbot, project_session, batch, roster_path
    ):
        attendance = _page(qtbot, project_session, None, batch)
        try:
            attendance.state.template = None
            with qtbot.waitSignal(attendance.reconciled, timeout=TIMEOUT_MS):
                attendance.commit_roster(read_roster(roster_path), source_path=roster_path)
            select(attendance, "170504")
            assert attendance.inspect_selected_script() is False
            assert attendance.inspector.editor(FieldKind.IDENTIFIER).isEnabled() is False
        finally:
            attendance.close()


class TestCorrectingTheWholeStudentId:
    def test_the_script_moves_to_the_candidate_who_wrote_it(
        self, qtbot, page: AttendancePage, confirmed
    ):
        select(page, "170504")
        inspect(qtbot, page)
        correct_id(qtbot, page, "170503")

        # Every position was read confidently, so the change is an override,
        # and the operator was asked first.
        assert confirmed == [{5: ("4", "3")}]
        assert status_of(page, "170503") is ReconciliationStatus.MATCHED
        assert status_of(page, "170504") is ReconciliationStatus.ABSENT_CONFIRMED
        chips = {key: chip.text() for key, chip in page._chips.items()}
        assert chips["matched"] == "Matched  3"
        assert chips["absent"] == "Absent + script  0"
        # ...and the operator is told where it went.
        assert "now filed under <b>170503</b> (Matched)" in page.table_count_label.text()

    def test_downstream_stages_read_the_corrected_id(
        self, qtbot, page: AttendancePage, confirmed
    ):
        select(page, "170504")
        inspect(qtbot, page)
        scan_id = page.inspector.scan_id
        correct_id(qtbot, page, "170503")
        found = review_store.effective_identifiers(page.database, page.state.batch_id)[scan_id]
        assert (found.machine_value, found.value) == ("170504", "170503")
        assert found.source is ValueSource.HUMAN
        scripts = {
            item.scan_id: item
            for item in reconciliation_store.batch_scripts(page.database, page.state.batch_id)
        }
        assert scripts[scan_id].effective_candidate_id == "170503"
        assert scripts[scan_id].machine_candidate_id == "170504"

    def test_the_ledger_keeps_the_machine_value_and_says_why(
        self, qtbot, page: AttendancePage, confirmed
    ):
        select(page, "170504")
        inspect(qtbot, page)
        scan_id = page.inspector.scan_id
        correct_id(qtbot, page, "170503")
        (record,) = [
            item
            for item in review_store.list_conflicts(
                page.database,
                page.state.batch_id,
                filters=review_store.ConflictFilter(scan_id=scan_id, include_withdrawn=True),
            )
            if item.conflict_type is ConflictType.MANUAL_OVERRIDE
        ]
        (event,) = review_store.history_for(page.database, record.conflict_id)
        assert event.machine_value == "4"
        assert (event.previous_value, event.new_value) == ("4", "3")
        assert event.reviewer == OPERATOR
        assert event.reason_code == ReasonCode.WRONG_ID_ENTERED.value
        assert review_store.is_override(event.detail)
        assert "Attendance stage" in event.detail
        assert "170504 (Absent but script found)" in event.detail

    def test_the_selection_does_not_jump_to_the_top(
        self, qtbot, page: AttendancePage, confirmed
    ):
        select(page, "170504")
        before = page.table.currentRow()
        assert before > 0
        inspect(qtbot, page)
        scan_id = page.inspector.scan_id
        correct_id(qtbot, page, "170503")
        # 170504 is settled and has left the exceptions; the row that took its
        # place is selected, and the sheet being inspected stays on screen.
        assert page.table.currentRow() == min(before, len(page.state.entries) - 1)
        assert page.inspector.scan_id == scan_id

    def test_the_correction_survives_reopening_the_project(
        self, qtbot, page: AttendancePage, project_session, confirmed
    ):
        select(page, "170504")
        inspect(qtbot, page)
        scan_id = page.inspector.scan_id
        correct_id(qtbot, page, "170503")
        root = project_session.root
        page.close()
        project_session.close()
        with open_project(root) as reopened:
            found = review_store.effective_identifiers(reopened.database, page.state.batch_id)
            assert found[scan_id].value == "170503"
            assert found[scan_id].machine_value == "170504"

    def test_cancelling_the_override_changes_nothing(
        self, qtbot, monkeypatch, page: AttendancePage
    ):
        monkeypatch.setattr(page.inspector, "confirm_override", lambda *_a: False)
        select(page, "170504")
        inspect(qtbot, page)
        page.inspector.editor(FieldKind.IDENTIFIER).setText("170503")
        assert page.inspector.apply_correction(FieldKind.IDENTIFIER) is False
        assert status_of(page, "170504") is ReconciliationStatus.ABSENT_WITH_SCRIPT
        assert review_store.effective_identifiers(page.database, page.state.batch_id)[
            page.inspector.scan_id
        ].source is ValueSource.MACHINE

    def test_one_undo_puts_it_back(self, qtbot, page: AttendancePage, confirmed):
        select(page, "170504")
        inspect(qtbot, page)
        correct_id(qtbot, page, "170503")
        with qtbot.waitSignal(page.reconciled, timeout=TIMEOUT_MS):
            assert page.inspector.undo_last() is True
        assert status_of(page, "170504") is ReconciliationStatus.ABSENT_WITH_SCRIPT
        assert status_of(page, "170503") is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT

    def test_an_invalid_id_is_refused(self, qtbot, page: AttendancePage):
        select(page, "170504")
        inspect(qtbot, page)
        page.inspector.editor(FieldKind.IDENTIFIER).setText("1705")
        assert page.inspector.apply_button(FieldKind.IDENTIFIER).isEnabled() is False
        assert "6 position" in page.inspector.preview_label.text()


class TestCompletingAnUnreadId:
    def test_an_incomplete_id_is_completed_without_an_override(
        self, qtbot, page: AttendancePage, confirmed
    ):
        page.status_filter.setCurrentIndex(page.status_filter.findText("Unrecognised ID"))
        assert len(page.state.entries) == 1
        page.table.selectRow(0)
        inspect(qtbot, page)
        assert page.inspector.editor(FieldKind.IDENTIFIER).text() == "17050?"
        correct_id(qtbot, page, "170505")
        assert confirmed == [], "the blank position was in dispute; nothing overridden"
        assert status_of(page, "170505") is ReconciliationStatus.MATCHED


class TestFindingThings:
    def test_a_chip_filters_the_table_and_a_second_click_clears_it(
        self, page: AttendancePage
    ):
        page._chips["missing"].click()
        assert {entry.candidate_id for entry in page.state.entries} == {"170503", "170505"}
        assert page._chips["missing"].isChecked() is True
        page._chips["missing"].click()
        assert page.status_filter.currentText() == "Exceptions only"
        assert page._chips["missing"].isChecked() is False

    def test_search_finds_a_recognised_id(self, qtbot, page: AttendancePage, confirmed):
        page.status_filter.setCurrentIndex(0)
        page.search_box.setText("170504")
        assert [entry.candidate_id for entry in page.state.entries] == ["170504"]
        select(page, "170504")
        inspect(qtbot, page)
        correct_id(qtbot, page, "170503")
        # What the machine read still finds the script, under its new owner.
        page.search_box.setText("")
        page.status_filter.setCurrentIndex(0)
        page.search_box.setText("170504")
        found = {entry.candidate_id for entry in page.state.entries}
        assert found == {"170503", "170504"}


class TestSmallScreens:
    @pytest.mark.parametrize("size", [(1920, 1080), (1600, 900), (1366, 768)])
    def test_every_control_needed_to_finish_a_correction_is_reachable(
        self, qtbot, page: AttendancePage, size
    ):
        width, height = size
        # The page gets what the window leaves after its title bar and ribbon.
        page.resize(width - 40, height - 180)
        page.show()
        qtbot.waitExposed(page)
        select(page, "170504")
        inspect(qtbot, page)
        assert page.minimumSizeHint().height() <= height - 180
        for widget in (
            page.inspect_button,
            page.inspector.apply_button(FieldKind.IDENTIFIER),
            page.inspector.editor(FieldKind.IDENTIFIER),
            page.dismiss_button,
            page.attendance_button,
        ):
            page.detail_scroll.ensureWidgetVisible(widget)
            qtbot.wait(20)
            top_left = widget.mapTo(page.detail_scroll.viewport(), widget.rect().topLeft())
            viewport = page.detail_scroll.viewport().rect()
            assert viewport.contains(top_left), (size, widget.objectName())
        assert page.table.isVisible()
        assert page.table.height() > 100, "the exception table stays the main area"


class TestSets:
    @pytest.fixture
    def two_sets(self, qtbot, project_session, template, batch, tmp_path):
        database = project_session.database
        # The sets are coded "A" and "B" because the rendered sheets carry set
        # code A: a set reconciles only the scripts whose set code is its own,
        # so these four sheets belong to the first set and none to the second.
        ten = project_sets.add_set(database, "A", "Assistant Engineer (Electrical)")
        eleven = project_sets.add_set(database, "B", "Assistant Engineer (Civil)")
        attendance = _page(qtbot, project_session, template, batch)
        first = tmp_path / "set10.csv"
        first.write_text(ROSTER, encoding="utf-8")
        second = tmp_path / "set11.csv"
        second.write_text("Roll No.,Name,Total\n170504,OTHER PERSON,40\n", encoding="utf-8")
        for exam_set, path in ((ten, first), (eleven, second)):
            attendance.select_set(exam_set.set_id)
            with qtbot.waitSignal(attendance.reconciled, timeout=TIMEOUT_MS):
                attendance.commit_roster(read_roster(path), source_path=path)
        yield attendance, ten, eleven
        attendance.close()

    def test_a_correction_in_one_set_does_not_touch_the_other(
        self, qtbot, monkeypatch, two_sets
    ):
        page, ten, eleven = two_sets
        monkeypatch.setattr(page.inspector, "confirm_override", lambda *_a: True)
        database = page.database
        roster_11 = reconciliation_store.active_roster(database, eleven.set_id)
        before = reconciliation_store.stored_counts(
            database, roster_11.roster_id, page.state.batch_id
        )
        page.select_set(ten.set_id)
        page.refresh_table()
        select(page, "170504")
        inspect(qtbot, page)
        correct_id(qtbot, page, "170503")

        after = reconciliation_store.stored_counts(
            database, roster_11.roster_id, page.state.batch_id
        )
        assert after == before, "set 11's reconciliation was not recomputed or altered"
        assert reconciliation_store.active_roster(database, eleven.set_id) == roster_11

    def test_switching_sets_clears_the_inspector_and_the_rows(self, qtbot, two_sets):
        page, ten, eleven = two_sets
        page.select_set(ten.set_id)
        page.refresh_table()
        select(page, "170504")
        inspect(qtbot, page)
        page.set_table.selectRow(1)
        assert page.selected_set().set_id == eleven.set_id
        assert page.inspector.scan_id is None
        page.status_filter.setCurrentIndex(0)
        names = {entry.display_name for entry in page.state.entries}
        assert "OTHER PERSON" in names
        assert "NADIA D" not in names


# ----------------------------------------------------------------------
# Next / previous unresolved, and the remembered divider
# ----------------------------------------------------------------------
def _activate(widget) -> None:
    """Give a real key press somewhere to land, or skip on a headless plugin."""
    widget.show()
    widget.activateWindow()
    widget.raise_()
    QApplication.processEvents()
    if not widget.isActiveWindow():
        pytest.skip("this Qt platform plugin never activates a window")


class TestAttendanceNavigation:
    def outstanding(self, page: AttendancePage) -> list[str]:
        return [entry.candidate_id for entry in page.state.entries if entry.needs_attention]

    def test_next_walks_the_rows_needing_review_in_order(self, page: AttendancePage):
        wanted = self.outstanding(page)
        assert len(wanted) == 4
        page.table.clearSelection()
        page.table.setCurrentCell(-1, -1)
        seen = []
        for _ in wanted:
            assert page.select_next_unresolved() is True
            seen.append(page._selected_entry().candidate_id)
        assert seen == wanted

    def test_wrapping_is_announced_not_silent(self, page: AttendancePage):
        wanted = self.outstanding(page)
        select(page, wanted[-1])
        assert page.select_next_unresolved() is True
        assert page._selected_entry().candidate_id == wanted[0]
        assert "continued from the top" in page.navigation_note.text()
        assert page.navigation_note.isVisibleTo(page)
        # An ordinary step clears the note.
        assert page.select_next_unresolved() is True
        assert page.navigation_note.text() == ""

    def test_previous_from_the_first_wraps_to_the_last(self, page: AttendancePage):
        wanted = self.outstanding(page)
        select(page, wanted[0])
        assert page.select_previous_unresolved() is True
        assert page._selected_entry().candidate_id == wanted[-1]
        assert "continued from the end" in page.navigation_note.text()

    def test_it_stays_within_the_current_filter(self, page: AttendancePage):
        page._chips["missing"].click()
        visited = set()
        for _ in range(6):
            page.select_next_unresolved()
            visited.add(page._selected_entry().candidate_id)
        assert visited == {"170503", "170505"}

    def test_a_row_already_dealt_with_is_passed_over(self, qtbot, page: AttendancePage):
        select(page, "170505")
        page.reason_combo.setCurrentIndex(page.reason_combo.findData("script_missing"))
        with qtbot.waitSignal(page.resolution_recorded, timeout=TIMEOUT_MS):
            assert page.toggle_dismissed() is True
        page.resolution_filter.setCurrentIndex(0)
        page.status_filter.setCurrentIndex(0)
        visited = set()
        for _ in range(8):
            page.select_next_unresolved()
            visited.add(page._selected_entry().candidate_id)
        assert "170505" not in visited

    def test_nothing_left_is_said_and_the_selection_stays(self, page: AttendancePage):
        page.status_filter.setCurrentIndex(page.status_filter.findText("Matched"))
        select(page, "170501")
        assert page.select_next_unresolved() is False
        assert page._selected_entry().candidate_id == "170501"
        assert "Nothing in this view" in page.navigation_note.text()

    def test_the_buttons_name_their_shortcuts(self, page: AttendancePage):
        assert "Ctrl+Down" in page.next_unresolved_button.toolTip()
        assert "Ctrl+Up" in page.previous_unresolved_button.toolTip()

    def test_ctrl_down_and_ctrl_up(self, page: AttendancePage):
        wanted = self.outstanding(page)
        select(page, wanted[0])
        _activate(page)
        page.table.setFocus()
        QTest.keyClick(page.table, Qt.Key.Key_Down, Qt.KeyboardModifier.ControlModifier)
        assert page._selected_entry().candidate_id == wanted[1]
        QTest.keyClick(page.table, Qt.Key.Key_Up, Qt.KeyboardModifier.ControlModifier)
        assert page._selected_entry().candidate_id == wanted[0]


class TestTheDividerIsRemembered:
    def test_the_config_clamps_rather_than_rejects(self):
        assert AppConfig().attendance_split_ratio is None
        assert AppConfig().with_attendance_split_ratio(0.5).attendance_split_ratio == 0.5
        assert AppConfig().with_attendance_split_ratio(0.99).attendance_split_ratio == (
            MAX_SPLIT_RATIO
        )
        assert AppConfig().with_attendance_split_ratio(0.01).attendance_split_ratio == (
            MIN_SPLIT_RATIO
        )

    def test_an_older_config_file_still_loads(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"config_version": 1, "reviewer_name": "X"}), "utf-8")
        loaded = load_app_config(path, strict=True)
        assert loaded.reviewer_name == "X"
        assert loaded.attendance_split_ratio is None

    def test_the_default_applies_when_nothing_is_saved(self, qtbot, page: AttendancePage):
        page.resize(1400, 700)
        page.show()
        qtbot.waitExposed(page)
        assert page.split_ratio() == pytest.approx(DEFAULT_SPLIT_RATIO, abs=0.03)

    def test_an_unsuitable_ratio_leaves_the_detail_pane_its_minimum(
        self, qtbot, page: AttendancePage
    ):
        page.set_split_ratio(5.0)
        page.resize(900, 700)
        page.show()
        qtbot.waitExposed(page)
        right = page.work_splitter.sizes()[1]
        assert right >= page.detail_scroll.minimumWidth() - 2

    def test_moving_the_divider_is_saved_and_restored_by_the_window(
        self, qtbot, tmp_path
    ):
        config_path = tmp_path / "config.json"
        window = MainWindow(AppConfig(), config_path=config_path)
        qtbot.addWidget(window)
        try:
            window.resize(1500, 900)
            window.show()
            window.show_page("attendance")
            attendance = window._attendance_page()
            qtbot.waitExposed(attendance)
            total = sum(attendance.work_splitter.sizes())
            attendance.work_splitter.setSizes([int(total * 0.45), total - int(total * 0.45)])
            attendance.work_splitter.splitterMoved.emit(int(total * 0.45), 1)
            with qtbot.waitSignal(attendance.split_ratio_changed, timeout=5_000):
                pass
            qtbot.waitUntil(lambda: config_path.exists(), timeout=5_000)
        finally:
            window.close()

        saved = load_app_config(config_path, strict=True).attendance_split_ratio
        assert saved == pytest.approx(0.45, abs=0.02)

        reopened = MainWindow(load_app_config(config_path), config_path=config_path)
        qtbot.addWidget(reopened)
        try:
            reopened.resize(1500, 900)
            reopened.show()
            reopened.show_page("attendance")
            attendance = reopened._attendance_page()
            qtbot.waitExposed(attendance)
            qtbot.waitUntil(lambda: attendance.split_ratio() is not None, timeout=5_000)
            assert attendance.split_ratio() == pytest.approx(0.45, abs=0.03)
        finally:
            reopened.close()

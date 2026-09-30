# ruff: noqa: F811 - the Attendance page fixtures are imported and requested by name
"""The Attendance stage's disposition controls, on the real page.

Scope:
    Keep This Script, Reject / Exclude, Defer, Restore and Undo Last
    Disposition, driven through :class:`AttendancePage` on a real project and
    database. The acceptance roster has candidate 100002 scanned twice
    (``s2a.png``, ``s2b.png``) and one sheet read as 999999, who is on no list.
    The two modal questions are answered through the page's own seams
    (:meth:`AttendancePage.confirm_keep`, :meth:`AttendancePage.ask_exclusion`)
    - a modal is never ``exec()``-ed in a test.

Privacy:
    Every identifier and name here is fictional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from tests.gui.test_attendance_page import (  # noqa: F401 - fixtures
    OPERATOR,
    batch,
    entries,
    import_roster,
    imported,
    page,
    roster_file,
    select,
)

from omr_scanner.domain.reconciliation import ReconciliationStatus
from omr_scanner.domain.scan_lifecycle import LifecycleState, RejectionReason
from omr_scanner.gui.attendance.page import (
    DEFERRED_SHEETS,
    REJECTED_SHEETS,
    AttendancePage,
    ExclusionDialog,
)
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.services import batch_store, open_project, scan_lifecycle

if TYPE_CHECKING:
    from omr_scanner.services import ProjectSession


def scan_ids(page: AttendancePage) -> dict[str, int]:
    assert page.database is not None and page.state.batch_id is not None
    return {
        path.name: scan_id
        for path, scan_id in batch_store.scan_ids_by_path(
            page.database, page.state.batch_id
        ).items()
    }


def chip_value(page: AttendancePage, key: str) -> str:
    return page._chips[key].text().rsplit("  ", 1)[1]


def show_all(page: AttendancePage) -> None:
    page.status_filter.setCurrentIndex(0)


def select_script(page: AttendancePage, name: str) -> None:
    for row in range(page.scripts_list.count()):
        if page.scripts_list.item(row).text().startswith(name):
            page.scripts_list.setCurrentRow(row)
            return
    raise AssertionError(f"{name} is not listed")


@pytest.fixture
def answers(monkeypatch):
    """Answer the page's two questions; record what was asked."""
    asked: list[str] = []

    def keep(_self, text: str) -> bool:
        asked.append(text)
        return True

    def exclusion(_self, text: str) -> tuple[RejectionReason, str]:
        asked.append(text)
        return RejectionReason.WRONG_DOCUMENT, "Physics paper"

    monkeypatch.setattr(AttendancePage, "confirm_keep", keep)
    monkeypatch.setattr(AttendancePage, "ask_exclusion", exclusion)
    return asked


def ignore_warnings(monkeypatch) -> list[str]:
    warned: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *args, **_kw: warned.append(str(args[2]))
    )
    return warned


# ----------------------------------------------------------------------
class TestDuplicateGroup:
    def test_the_duplicate_row_exposes_both_scans(self, imported: AttendancePage):
        select(imported, "100002")
        assert imported.scripts_list.count() == 2
        assert imported.script_position_label.isVisibleTo(imported)
        assert "Script 1 of 2" in imported.script_position_label.text()
        assert imported.keep_button.isVisibleTo(imported)
        assert imported.keep_button.isEnabled()
        facts = imported.script_facts_label.text()
        assert "s2a.png" in facts and "scan #2" in facts and "100002" in facts
        assert "Active" in facts

    def test_each_copy_can_be_inspected_in_turn(self, imported: AttendancePage):
        ids = scan_ids(imported)
        select(imported, "100002")
        # Selecting the duplicate opens its first copy at once.
        assert imported.inspector.scan_id == ids["s2a.png"]
        assert imported.step_script(1) is True
        assert imported.inspector.scan_id == ids["s2b.png"]
        assert "Script 2 of 2" in imported.script_position_label.text()
        assert "s2b.png" in imported.script_facts_label.text()
        assert imported.next_script_button.isEnabled() is False
        assert imported.step_script(-1) is True
        assert imported.inspector.scan_id == ids["s2a.png"]

    def test_the_keyboard_walks_the_copies(self, qtbot, imported: AttendancePage):
        ids = scan_ids(imported)
        select(imported, "100002")
        imported.scripts_list.setFocus()
        qtbot.keyClick(imported.scripts_list, Qt.Key.Key_Down)
        assert imported.inspector.scan_id == ids["s2b.png"]

    def test_keep_this_script_rejects_the_other_copy(
        self, imported: AttendancePage, answers
    ):
        ids = scan_ids(imported)
        select(imported, "100002")
        select_script(imported, "s2b.png")
        assert imported.keep_selected_script() is True
        asked = answers[0]
        assert "Keep this script for candidate 100002 and reject 1 duplicate copy?" in asked
        keep_part, reject_part = asked.split("REJECT / EXCLUDE:")
        assert "s2b.png" in keep_part and "s2a.png" in reject_part
        assert "scan #3" in keep_part and "scan #2" in reject_part
        database = imported.database
        assert scan_lifecycle.state_of(database, ids["s2a.png"]) is LifecycleState.EXCLUDED
        assert scan_lifecycle.state_of(database, ids["s2b.png"]) is LifecycleState.ACTIVE
        show_all(imported)
        entry = entries(imported)["100002"]
        assert entry.status is ReconciliationStatus.MATCHED
        assert [view.script.scan_id for view in entry.scripts] == [ids["s2b.png"]]
        assert chip_value(imported, "duplicate") == "0"
        assert chip_value(imported, "rejected") == "1"

    def test_declining_the_confirmation_changes_nothing(
        self, imported: AttendancePage, monkeypatch
    ):
        monkeypatch.setattr(AttendancePage, "confirm_keep", lambda _self, _text: False)
        select(imported, "100002")
        assert imported.keep_selected_script() is False
        assert entries(imported)["100002"].status is ReconciliationStatus.DUPLICATE_SCRIPT

    def test_undo_and_restore_bring_the_duplicate_back(
        self, imported: AttendancePage, answers
    ):
        ids = scan_ids(imported)
        select(imported, "100002")
        imported.keep_selected_script()
        assert imported.undo_disposition_button.isVisibleTo(imported)
        assert imported.undo_last_disposition() is True
        assert scan_lifecycle.state_of(imported.database, ids["s2b.png"]) is (
            LifecycleState.ACTIVE
        )
        show_all(imported)
        assert entries(imported)["100002"].status is ReconciliationStatus.DUPLICATE_SCRIPT

    def test_a_single_script_is_never_offered_keep(self, imported: AttendancePage):
        show_all(imported)
        select(imported, "100001")
        assert not imported.keep_button.isVisibleTo(imported)
        assert not imported.script_position_label.isVisibleTo(imported)
        assert imported.defer_button.isVisibleTo(imported)
        assert imported.exclude_sheet_button.isVisibleTo(imported)
        assert not imported.restore_button.isVisibleTo(imported)


class TestUnwantedSheet:
    def test_reject_exclude_removes_the_unknown_id(self, imported: AttendancePage, answers):
        ids = scan_ids(imported)
        select(imported, "999999")
        assert imported.exclude_selected_script() is True
        assert "s4.png" in answers[0] and "999999" in answers[0]
        assert "999999" not in entries(imported)
        assert chip_value(imported, "rejected") == "1"
        case = scan_lifecycle.get_case(imported.database, ids["s4.png"])
        assert case is not None and case.reason is RejectionReason.WRONG_DOCUMENT
        assert case.note == "Physics paper"

    def test_the_rejected_sheet_stays_discoverable_and_restorable(
        self, imported: AttendancePage, answers
    ):
        ids = scan_ids(imported)
        select(imported, "999999")
        imported.exclude_selected_script()
        imported.filter_by_chip("rejected")
        assert imported.status_filter.currentText() == REJECTED_SHEETS
        assert [case.scan_id for case in imported.state.dispositions] == [ids["s4.png"]]
        imported.table.selectRow(0)
        assert imported.inspector.scan_id == ids["s4.png"]
        assert "REJECTED — EXCLUDED FROM RESULTS" in imported.detail_label.text()
        assert imported.restore_button.isVisibleTo(imported)
        assert not imported.defer_button.isVisibleTo(imported)
        assert "Rejected / excluded" in imported.history_label.text()
        assert imported.restore_selected_script() is True
        assert imported.state.dispositions == []
        show_all(imported)
        assert entries(imported)["999999"].status is ReconciliationStatus.UNKNOWN_ID


class TestDefer:
    def test_defer_keeps_the_sheet_and_counts_it(self, imported: AttendancePage):
        ids = scan_ids(imported)
        select(imported, "999999")
        assert imported.defer_selected_script() is True
        assert scan_lifecycle.state_of(imported.database, ids["s4.png"]) is (
            LifecycleState.DEFERRED
        )
        assert chip_value(imported, "deferred") == "1"
        assert "999999" not in entries(imported)
        assert "1 sheet(s) are deferred and will not be included" in (
            imported.summary_label.text()
        )
        imported.filter_by_chip("deferred")
        assert imported.status_filter.currentText() == DEFERRED_SHEETS
        imported.table.selectRow(0)
        assert "DEFERRED" in imported.detail_label.text()
        # From Deferred: decide now, either way.
        assert imported.restore_button.isVisibleTo(imported)
        assert imported.exclude_sheet_button.isVisibleTo(imported)

    def test_restoring_a_deferred_sheet_returns_it_to_review(self, imported: AttendancePage):
        select(imported, "999999")
        imported.defer_selected_script()
        imported.filter_by_chip("deferred")
        imported.table.selectRow(0)
        assert imported.restore_selected_script() is True
        show_all(imported)
        assert entries(imported)["999999"].status is ReconciliationStatus.UNKNOWN_ID
        assert chip_value(imported, "deferred") == "0"

    def test_a_decision_without_a_name_is_refused(self, imported: AttendancePage, monkeypatch):
        warned = ignore_warnings(monkeypatch)
        imported.set_operator("")
        select(imported, "999999")
        assert imported.defer_button.isEnabled() is False
        assert imported.defer_selected_script() is False
        assert warned and "name" in warned[0].lower()


class TestPersistence:
    def test_dispositions_survive_closing_and_reopening(
        self, qtbot, imported: AttendancePage, answers, project_session: ProjectSession
    ):
        ids = scan_ids(imported)
        select(imported, "100002")
        imported.keep_selected_script()
        select(imported, "999999")
        imported.defer_selected_script()
        batch_id = imported.state.batch_id
        root = project_session.root
        imported.on_project_changed(None)
        project_session.close()
        with open_project(root) as reopened:
            spec = next(item for item in WORKFLOW_PAGES if item.key == "attendance")
            again = AttendancePage(spec)
            qtbot.addWidget(again)
            again.on_project_changed(reopened)
            again.set_operator(OPERATOR)
            again.set_batch(batch_id)
            try:
                assert scan_lifecycle.state_of(reopened.database, ids["s2b.png"]) is (
                    LifecycleState.EXCLUDED
                )
                assert chip_value(again, "rejected") == "1"
                assert chip_value(again, "deferred") == "1"
                show_all(again)
                assert entries(again)["100002"].status is ReconciliationStatus.MATCHED
                assert "999999" not in entries(again)
            finally:
                again.close()


class TestExclusionDialog:
    def test_it_offers_only_exclusion_reasons(self, qtbot):
        dialog = ExclusionDialog("Reject this?")
        qtbot.addWidget(dialog)
        labels = [dialog.reason_combo.itemText(i) for i in range(dialog.reason_combo.count())]
        assert "Duplicate scan of a kept script" in labels
        assert "Folded / physically damaged" not in labels
        dialog.note_edit.setText("x")
        assert dialog.values() == (RejectionReason.WRONG_DOCUMENT, "x")


class TestSmallScreens:
    @pytest.mark.parametrize("size", [(1366, 768), (1100, 680)])
    def test_the_disposition_controls_are_reachable_and_unclipped(
        self, qtbot, imported: AttendancePage, size
    ):
        width, height = size
        imported.resize(width - 40, height - 180)
        imported.show()
        qtbot.waitExposed(imported)
        select(imported, "100002")
        qtbot.wait(50)
        for widget in (
            imported.previous_script_button,
            imported.next_script_button,
            imported.inspect_button,
            imported.keep_button,
            imported.exclude_sheet_button,
            imported.defer_button,
        ):
            assert widget.isVisible(), (size, widget.objectName())
            imported.detail_scroll.ensureWidgetVisible(widget)
            qtbot.wait(20)
            viewport = imported.detail_scroll.viewport()
            top_left = widget.mapTo(viewport, widget.rect().topLeft())
            bottom_right = widget.mapTo(viewport, widget.rect().bottomRight())
            assert viewport.rect().contains(top_left), (size, widget.objectName())
            assert bottom_right.x() <= viewport.width(), (size, widget.objectName())
            assert widget.width() >= widget.minimumSizeHint().width(), (
                size, widget.objectName(),
            )
        for key in ("rejected", "deferred"):
            chip = imported._chips[key]
            assert chip.isVisible() and chip.width() >= chip.sizeHint().width() - 2
        assert imported.table.height() > 100

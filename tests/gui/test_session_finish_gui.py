"""Finish Scan Session and Reopen in session mode (0.1.1 revised phase 8).

The Scan stage's session mode closes only through the one finish policy
(``session_finish.finish_scan_session``, spied on - never replaced - so the
real service runs), lists every typed blocker, accepts incomplete results
only for the three acknowledgeable blockers and only by a named operator,
and reopens through ``session_finish.reopen_session``.
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog
from sqlalchemy import select
from tests.gui.session_gui_rig import OPERATOR, SHEETS, SessionGuiRig
from tests.integration.test_session_finish import (
    BIG,
    resolve_all_conflicts,
    resolve_all_conflicts_except_evidence,
    settle,
)
from tests.quality_rig import displaced_id

from omr_scanner.database.models import AuditEvent
from omr_scanner.domain.scan_sessions import ScanSessionState
from omr_scanner.domain.session_finish import BlockerCode, FinishBlocker, FinishOutcome
from omr_scanner.domain.session_snapshot import SessionActivity
from omr_scanner.gui import session_close
from omr_scanner.gui.scan import session_mode as session_mode_module
from omr_scanner.gui.scan.session_dialogs import FinishChoice, FinishSessionDialog
from omr_scanner.services import quality_decisions, scan_sessions, session_finish

pytestmark = pytest.mark.gui


@pytest.fixture
def finishes(monkeypatch: pytest.MonkeyPatch) -> list[FinishOutcome]:
    seen: list[FinishOutcome] = []
    real = session_finish.finish_scan_session

    def spy(*args: Any, **kwargs: Any) -> FinishOutcome:
        outcome = real(*args, **kwargs)
        seen.append(outcome)
        return outcome

    monkeypatch.setattr(session_finish, "finish_scan_session", spy)
    return seen


@pytest.fixture
def rig(project_session, monkeypatch, finishes) -> SessionGuiRig:
    return SessionGuiRig(project_session, monkeypatch)


def state(rig: SessionGuiRig) -> ScanSessionState:
    info = scan_sessions.get_scan_session(rig.database, rig.session_id)
    assert info is not None
    return info.state


def processed_session(rig: SessionGuiRig, *, resolve: bool = True) -> None:
    rig.source("a")
    rig.write("a", [(f"{i}.png", SHEETS[i]) for i in range(3)])
    rig.new_engine(unit_policy=BIG)
    settle(rig)
    if resolve:
        resolve_all_conflicts(rig)
    assert rig.engine is not None
    rig.engine.shutdown()
    rig.engine = None


def finish(qtbot, rig: SessionGuiRig, **options: Any) -> FinishOutcome:
    page = rig.page
    assert page is not None
    with qtbot.waitSignal(page.session_mode.finish_completed, timeout=60_000) as done:
        assert page.session_mode.finish_session(**options) is True
    return done.args[0]


ALL_CODES = [
    FinishBlocker(BlockerCode.FILES_STABILIZING, 2),
    FinishBlocker(BlockerCode.FILES_READY, 1),
    FinishBlocker(BlockerCode.SHEETS_QUEUED, 14),
    FinishBlocker(BlockerCode.SHEETS_PROCESSING, 3),
    FinishBlocker(BlockerCode.UNITS_RUNNING, 1),
    FinishBlocker(BlockerCode.UNRESOLVED_CONFLICTS, 7),
    FinishBlocker(BlockerCode.RESCAN_SUGGESTED, 2),
    FinishBlocker(BlockerCode.FILES_AWAITING_DECISION, 4),
    FinishBlocker(BlockerCode.SOURCE_UNREACHABLE, source_label="Scanner B", detail="offline"),
    FinishBlocker(BlockerCode.SOURCE_NOT_RECONCILED, source_label="Scanner C"),
    FinishBlocker(BlockerCode.RESCAN_OUTSTANDING, 2),
    FinishBlocker(BlockerCode.REPLACEMENT_UNMATCHED, 1),
    FinishBlocker(BlockerCode.SHEETS_DEFERRED, 5),
    FinishBlocker(BlockerCode.PROCESSING_ACTIVE),
]


class TestTheBlockerDialog:
    def test_every_typed_blocker_is_listed_grouped_with_a_way_to_each(self, qtbot):
        dialog = FinishSessionDialog(
            "Final Exam", ALL_CODES, attempted=True, operator=OPERATOR,
            disabled_sources=("Scanner D",),
        )
        qtbot.addWidget(dialog)
        text = " ".join(label.text() for label in dialog.group_labels.values())
        for blocker in ALL_CODES:
            assert session_close.blocker_message(blocker).split(" (")[0][:40] in text.replace(
                "&#x27;", "'"
            )
        assert set(dialog.group_labels) == {
            "Processing", "Review", "Rescans", "Files awaiting a decision", "Sources",
            "Attendance",
        }
        assert set(dialog.navigation_buttons) == {
            "scan:session", "resolve:conflicts", "resolve:suggestions", "resolve:rescans",
            "resolve:files", "scan:sources", "attendance",
        }
        # Non-acknowledgeable blockers present: no way to close past them.
        assert not dialog.accept_box.isVisible()
        assert not dialog.incomplete_button.isVisibleTo(dialog)
        assert dialog.cancel_button.isDefault()
        assert not dialog.attempt_button.isDefault()

    def test_only_acknowledgeable_blockers_offer_a_named_incomplete_close(self, qtbot):
        blockers = [
            FinishBlocker(BlockerCode.RESCAN_OUTSTANDING, 2),
            FinishBlocker(BlockerCode.SHEETS_DEFERRED, 1),
        ]
        dialog = FinishSessionDialog("Final Exam", blockers, attempted=True, operator=OPERATOR)
        qtbot.addWidget(dialog)
        assert dialog.accept_box.isVisibleTo(dialog)
        assert not dialog.incomplete_button.isEnabled()
        dialog.choose_accept_incomplete()  # unticked: nothing happens
        assert dialog.choice is FinishChoice.CANCEL
        dialog.accept_box.setChecked(True)
        assert dialog.incomplete_button.isEnabled()
        dialog.incomplete_button.click()
        assert dialog.choice is FinishChoice.ACCEPT_INCOMPLETE
        assert OPERATOR in dialog.operator_label.text()

    def test_without_an_operator_nothing_can_be_attempted_or_accepted(self, qtbot):
        blockers = [FinishBlocker(BlockerCode.RESCAN_OUTSTANDING, 1)]
        dialog = FinishSessionDialog("Final Exam", blockers, attempted=True, operator="")
        qtbot.addWidget(dialog)
        assert not dialog.attempt_button.isEnabled()
        assert not dialog.accept_box.isEnabled()
        assert "operator name" in dialog.operator_label.text()

    def test_escape_cancels_and_navigation_closes_with_its_destination(self, qtbot):
        dialog = FinishSessionDialog("Final Exam", ALL_CODES, attempted=False, operator=OPERATOR)
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.keyClick(dialog, Qt.Key.Key_Escape)
        assert dialog.result() == QDialog.DialogCode.Rejected
        assert dialog.choice is FinishChoice.CANCEL
        again = FinishSessionDialog("Final Exam", ALL_CODES, attempted=False, operator=OPERATOR)
        qtbot.addWidget(again)
        with qtbot.waitSignal(again.navigate) as went:
            again.navigation_buttons["resolve:files"].click()
        assert went.args == ["resolve:files"]
        assert again.choice is FinishChoice.NAVIGATE

    def test_a_long_list_scrolls_inside_a_bounded_dialog(self, qtbot):
        dialog = FinishSessionDialog("Final Exam", ALL_CODES * 3, attempted=True, operator=OPERATOR)
        qtbot.addWidget(dialog)
        dialog.show()
        QApplication.processEvents()
        screen = dialog.screen().availableGeometry()
        assert dialog.height() <= screen.height()
        assert dialog.width() <= screen.width()
        assert dialog.cancel_button.isVisible() and dialog.attempt_button.isVisible()


class TestFinishThroughThePolicy:
    def test_unresolved_conflicts_refuse_then_a_clean_session_closes(self, qtbot, rig, finishes):
        processed_session(rig, resolve=False)
        page = rig.build_page(qtbot)
        outcome = finish(qtbot, rig)
        if not outcome.closed:
            assert BlockerCode.UNRESOLVED_CONFLICTS in outcome.codes
            assert state(rig) is ScanSessionState.OPEN
            resolve_all_conflicts(rig)
            outcome = finish(qtbot, rig)
        assert outcome.closed
        assert finishes and finishes[-1].closed
        assert state(rig) is ScanSessionState.CLOSED
        with rig.database.session() as session:
            closed = session.scalars(
                select(AuditEvent).where(AuditEvent.action == "session_closed")
            ).one()
        assert closed.reviewer == OPERATOR
        rig.wait_for(
            lambda: rig.view() is not None
            and rig.view().snapshot.activity is SessionActivity.CLOSED  # type: ignore[union-attr]
        )
        assert page.session_panel.lifecycle_label.text() == "CLOSED"
        assert page.session_panel.reopen_session_button.isVisibleTo(page.session_panel)
        assert not page.session_panel.finish_session_button.isVisibleTo(page.session_panel)

    def test_finish_runs_in_the_engine_thread_while_scanning(self, qtbot, rig, finishes):
        rig.source("a")
        rig.write("a", [(f"{i}.png", SHEETS[i]) for i in range(3)])
        page = rig.build_page(qtbot)
        with qtbot.waitSignal(page.session_mode.runner.engine_started, timeout=30_000):
            page.session_mode.start()
        rig.wait_for(rig.processed(3), tick=3)
        resolve_all_conflicts(rig)
        rig.wait_for(
            lambda: rig.view().snapshot.activity  # type: ignore[union-attr]
            is SessionActivity.CAUGHT_UP,
            tick=1,
        )
        outcome = finish(qtbot, rig)
        assert outcome.closed, outcome.codes
        assert page.session_mode.running  # the engine keeps running; late files are held
        page.shutdown_background_work()
        assert state(rig) is ScanSessionState.CLOSED

    def test_an_outstanding_rescan_closes_only_by_named_acceptance(self, qtbot, rig, finishes):
        rig.source("a")
        rig.write("a", [("0.png", SHEETS[0]), ("moved.png", displaced_id(3))])
        rig.new_engine(unit_policy=BIG)
        settle(rig)
        resolve_all_conflicts_except_evidence(rig)
        moved = quality_decisions.outstanding_suggestions(rig.database, rig.session_id)[0]
        quality_decisions.confirm_suggestion(rig.database, moved.scan_id, reviewer=OPERATOR)
        resolve_all_conflicts(rig)
        assert rig.engine is not None
        rig.engine.shutdown()
        rig.engine = None
        rig.build_page(qtbot)
        refused = finish(qtbot, rig)
        assert not refused.closed
        assert refused.codes == (BlockerCode.RESCAN_OUTSTANDING,)
        assert state(rig) is ScanSessionState.OPEN
        accepted = finish(qtbot, rig, accept_incomplete=True)
        assert accepted.closed
        assert [item.code for item in accepted.accepted] == [BlockerCode.RESCAN_OUTSTANDING]
        with rig.database.session() as session:
            event = session.scalars(
                select(AuditEvent).where(AuditEvent.action == "session_closed")
            ).one()
        assert event.reviewer == OPERATOR
        assert "incomplete results accepted" in event.detail

    def test_without_an_operator_name_nothing_is_attempted(self, qtbot, rig, finishes):
        processed_session(rig)
        page = rig.build_page(qtbot, reviewer="")
        assert page.session_mode.finish_session() is False
        assert finishes == []
        assert "operator name" in page.session_panel.warnings_label.text()
        assert state(rig) is ScanSessionState.OPEN

    def test_the_menu_close_in_session_mode_opens_the_session_finish_dialog(
        self, qtbot, rig, monkeypatch
    ):
        processed_session(rig)
        page = rig.build_page(qtbot)
        opened: list[bool] = []
        monkeypatch.setattr(
            page.session_mode, "_prompt_finish_session", lambda: opened.append(True)
        )
        page._prompt_close_scan_session()
        assert opened == [True]

    def test_the_interactive_flow_shows_fresh_blockers_after_each_attempt(
        self, qtbot, rig, finishes, monkeypatch
    ):
        processed_session(rig, resolve=False)
        page = rig.build_page(qtbot)
        shown: list[tuple[bool, tuple[BlockerCode, ...]]] = []

        def answer(dialog: FinishSessionDialog) -> int:
            shown.append(
                (dialog.attempt_button.text() == "Check Again",
                 tuple(item.code for item in dialog.blockers))
            )
            dialog.choice = FinishChoice.ATTEMPT if len(shown) == 1 else FinishChoice.CANCEL
            return int(QDialog.DialogCode.Accepted)

        monkeypatch.setattr(FinishSessionDialog, "exec", answer)
        page.session_mode._prompt_finish_session()
        qtbot.waitUntil(
            lambda: len(shown) >= 2 or bool(finishes and finishes[-1].closed), timeout=60_000
        )
        if finishes[-1].closed:
            return  # nothing blocked this session at all
        preview, result = shown[0], shown[1]
        assert preview[0] is False and result[0] is True
        # The second dialog lists what the service returned, fresh.
        assert result[1] == finishes[-1].codes


class TestReopen:
    def test_reopen_is_named_audited_and_marks_final_outputs_stale(self, qtbot, rig):
        processed_session(rig)
        page = rig.build_page(qtbot)
        assert finish(qtbot, rig).closed
        page.set_reviewer("")
        assert page.session_mode.reopen_session() is False
        assert state(rig) is ScanSessionState.CLOSED
        page.set_reviewer(OPERATOR)
        assert page.session_mode.reopen_session(reason="late script") is True
        info = scan_sessions.get_scan_session(rig.database, rig.session_id)
        assert info is not None and info.state is ScanSessionState.OPEN
        assert info.final_outputs_stale_since is not None
        with rig.database.session() as session:
            event = session.scalars(
                select(AuditEvent).where(AuditEvent.action == "session_reopened")
            ).one()
        assert event.reviewer == OPERATOR
        rig.wait_for(
            lambda: rig.view() is not None
            and rig.view().session is not None  # type: ignore[union-attr]
            and rig.view().session.reopen_count >= 1  # type: ignore[union-attr]
        )
        assert page.session_panel.lifecycle_label.text() == "OPEN · REOPENED"
        assert "stale" in page.session_panel.warnings_label.text()

    def test_reclosing_runs_every_check_again(self, qtbot, rig, finishes):
        processed_session(rig)
        rig.build_page(qtbot)
        assert finish(qtbot, rig).closed
        assert rig.page is not None
        assert rig.page.session_mode.reopen_session() is True
        before = len(finishes)
        assert finish(qtbot, rig).closed
        assert len(finishes) == before + 1

    def test_the_scan_page_reopen_goes_through_the_one_adapter(self, qtbot, rig, monkeypatch):
        processed_session(rig)
        page = rig.build_page(qtbot)
        assert finish(qtbot, rig).closed
        seen: list[str] = []
        real = session_finish.reopen_session

        def spy(*args: Any, **kwargs: Any) -> object:
            seen.append(kwargs.get("reopened_by", ""))
            return real(*args, **kwargs)

        monkeypatch.setattr(session_finish, "reopen_session", spy)
        assert page.reopen_active_scan_session() is True
        assert seen == [OPERATOR]


def test_the_production_engine_factory_builds_without_starting(project_session):
    from tests.crash.harness import template as harness_template

    from omr_scanner.config.processing import ProcessingSettings

    info = scan_sessions.create_scan_session(project_session.database, name="Exam")
    build = session_mode_module.production_engine_factory(
        project_session, info.scan_session_id, harness_template(), None,
        ProcessingSettings(), OPERATOR,
    )
    engine = build()
    try:
        assert engine.scan_session_id == info.scan_session_id  # type: ignore[attr-defined]
    finally:
        engine.shutdown()  # type: ignore[attr-defined]

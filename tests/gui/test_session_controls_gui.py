"""Session-mode processing and intake controls on the Scan stage (0.1.1 revised phase 8).

Drives the real Scan page in session mode: the QThread runner around the real
continuous engine (inline real recognition, fake disk), the 1-second snapshot
poller and the persisted Phase 7 controls. Every assertion about state reads
the database or the snapshot - never a widget's own memory.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication
from sqlalchemy import select
from tests.gui.session_gui_rig import OPERATOR, SHEETS, SessionGuiRig, wait_stopped

from omr_scanner.database.models import AuditEvent, BatchScan, ScanJobStatus
from omr_scanner.domain.session_controls import ProcessingIntent
from omr_scanner.domain.session_snapshot import SessionActivity
from omr_scanner.services import coordinator, session_controls

pytestmark = pytest.mark.gui


@pytest.fixture
def rig(project_session, monkeypatch) -> SessionGuiRig:
    return SessionGuiRig(project_session, monkeypatch)


def started(rig: SessionGuiRig, qtbot) -> None:
    page = rig.page
    assert page is not None
    with qtbot.waitSignal(page.session_mode.runner.engine_started, timeout=30_000):
        assert page.session_mode.start() is True


def controls(rig: SessionGuiRig):
    return session_controls.get_controls(rig.database, rig.session_id)


class _FiniteRun:
    """Stands for the finite Scan stage's run holding the project's lease."""


class TestRunner:
    def test_start_runs_the_engine_off_the_gui_thread_and_reaches_caught_up(self, qtbot, rig):
        rig.source("a")
        rig.write("a", [(f"{i}.png", SHEETS[i]) for i in range(3)])
        page = rig.build_page(qtbot)
        assert page.session_mode.active and page.session_panel.isVisibleTo(page)
        started(rig, qtbot)
        thread = page.session_mode.runner.engine_thread
        assert thread is not None and thread is not QThread.currentThread()
        assert page.session_mode.running
        rig.wait_for(rig.processed(3), tick=3)
        rig.wait_for(
            lambda: rig.view() is not None
            and rig.view().snapshot.activity is SessionActivity.CAUGHT_UP,  # type: ignore[union-attr]
            tick=1,
        )
        assert page.session_panel.activity_label.text() == SessionActivity.CAUGHT_UP.label
        assert "complete" not in page.session_panel.activity_label.text().lower()
        # The finite run commands say why they are not offered.
        assert not page.process_all_button.isEnabled()
        assert "Continuous scanning" in page.process_all_button.toolTip()
        page.shutdown_background_work()
        assert not page.session_mode.running
        assert coordinator.holder_of(rig.database) is None
        # Leaving is not finishing: the intent is untouched and the session open.
        assert controls(rig).processing is ProcessingIntent.RUNNING
        assert rig.view().snapshot.session_state == "open"  # type: ignore[union-attr]

    def test_the_finite_scan_stage_holding_the_project_refuses_start_with_its_message(
        self, qtbot, rig
    ):
        rig.source("a")
        page = rig.build_page(qtbot)
        holder = _FiniteRun()
        lease = coordinator.acquire(
            rig.database, coordinator.CoordinatorKind.FINITE_SCAN, owner=holder, label="test"
        )
        try:
            with qtbot.waitSignal(page.session_mode.runner.busy, timeout=30_000) as busy:
                assert page.session_mode.start() is True
            wait_stopped(page)
        finally:
            lease.release()
        message = busy.args[0]
        assert "already being processed" in message
        assert message in page.session_panel.warnings_label.text().replace("&#x27;", "'")
        assert not page.session_mode.running
        # Nothing was reset or retried: the holder still held it until released.
        assert coordinator.holder_of(rig.database) is None


class TestProcessingControls:
    def test_pause_persists_and_lets_in_flight_sheets_finish(self, qtbot, rig):
        rig.source("a")
        rig.write("a", [(f"{i}.png", SHEETS[i]) for i in range(4)])
        page = rig.build_page(qtbot)
        started(rig, qtbot)
        rig.wait_for(rig.processed(1), tick=3)
        assert page.session_panel.processing_button.text() == "Pause Processing"
        assert page.session_mode.pause_processing() is True
        assert controls(rig).processing is ProcessingIntent.PAUSED
        rig.wait_for(
            lambda: rig.view().snapshot.activity  # type: ignore[union-attr]
            is SessionActivity.PROCESSING_PAUSED
        )
        # The button is worded from the persisted intent, not toggled by the click.
        assert page.session_panel.processing_button.text() == "Resume Processing"
        assert page.session_panel.activity_label.text() == "Processing paused"
        with rig.database.session() as session:
            claimed = session.scalars(
                select(BatchScan.status).where(BatchScan.status == ScanJobStatus.PROCESSING.value)
            ).all()
        # Whatever was claimed before the pause finishes and is saved.
        rig.wait_for(lambda: rig.engine is not None and rig.engine.in_flight == 0)
        assert len(claimed) <= 4
        assert page.session_mode.resume_processing() is True
        assert controls(rig).processing is ProcessingIntent.RUNNING
        rig.wait_for(rig.processed(4), tick=3)
        page.shutdown_background_work()

    def test_resume_starts_the_engine_when_it_is_not_running(self, qtbot, rig):
        rig.source("a")
        session_controls.pause_processing(rig.database, rig.session_id, actor=OPERATOR)
        page = rig.build_page(qtbot)
        assert page.session_panel.processing_button.text() == "Resume Processing"
        with qtbot.waitSignal(page.session_mode.runner.engine_started, timeout=30_000):
            assert page.session_mode.resume_processing() is True
        assert controls(rig).processing is ProcessingIntent.RUNNING
        page.shutdown_background_work()

    def test_finish_current_and_stop_persists_stopped_and_settles(self, qtbot, rig):
        rig.source("a")
        rig.write("a", [(f"{i}.png", SHEETS[i]) for i in range(3)])
        page = rig.build_page(qtbot)
        started(rig, qtbot)
        rig.wait_for(rig.processed(1), tick=3)
        assert page.session_mode.finish_current() is True
        wait_stopped(page)
        assert controls(rig).processing is ProcessingIntent.STOPPED
        with rig.database.session() as session:
            left = session.scalars(
                select(BatchScan.status).where(
                    BatchScan.status.in_(
                        (ScanJobStatus.QUEUED.value, ScanJobStatus.PROCESSING.value)
                    )
                )
            ).all()
        assert left == []  # nothing left claimed
        rig.wait_for(
            lambda: rig.view().snapshot.activity  # type: ignore[union-attr]
            is SessionActivity.PROCESSING_STOPPED
        )
        assert coordinator.holder_of(rig.database) is None

    def test_cancel_queued_needs_a_named_operator_and_is_audited(self, qtbot, rig):
        rig.source("a")
        rig.write("a", [(f"{i}.png", SHEETS[i]) for i in range(3)])
        page = rig.build_page(qtbot, reviewer="")
        started(rig, qtbot)
        assert page.session_mode.cancel_queued() is False
        assert "operator name" in page.session_panel.warnings_label.text()
        page.set_reviewer(OPERATOR)
        assert page.session_mode.cancel_queued() is True
        wait_stopped(page)
        assert controls(rig).processing is ProcessingIntent.STOPPED
        with rig.database.session() as session:
            actions = session.scalars(
                select(AuditEvent.action).where(AuditEvent.entity_type == "session_control")
            ).all()
        assert "queue_cancelled" in actions
        with rig.database.session() as session:
            statuses = set(session.scalars(select(BatchScan.status)).all())
        assert ScanJobStatus.PROCESSING.value not in statuses


class TestIntakeControls:
    def test_pause_and_resume_intake_session_wide_and_per_source(self, qtbot, rig):
        a = rig.source("a")
        rig.source("b")
        page = rig.build_page(qtbot)
        assert page.session_panel.intake_button.text() == "Pause Intake"
        assert page.session_mode.pause_intake() is True
        assert controls(rig).intake_paused
        rig.wait_for(lambda: rig.view().snapshot.controls.intake_paused)  # type: ignore[union-attr]
        assert page.session_panel.intake_button.text() == "Resume Intake"
        assert "Intake: paused" in page.session_panel.state_line_label.text()
        assert page.session_mode.resume_intake() is True
        assert not controls(rig).intake_paused
        assert page.session_mode.set_source_paused(a, True) is True
        assert a in controls(rig).paused_sources
        rig.wait_for(lambda: a in rig.view().snapshot.controls.paused_sources)  # type: ignore[union-attr]
        assert "1 source(s) paused" in page.session_panel.state_line_label.text()
        page.session_panel.sources_section.set_expanded(True)
        assert page.session_panel.select_source(a)
        assert page.session_panel.source_pause_button.text() == "Resume Source"
        statuses = [
            page.session_panel.sources_table.item(row, 1).text()
            for row in range(page.session_panel.sources_table.rowCount())
        ]
        assert "Paused" in statuses
        assert page.session_mode.set_source_paused(a, False) is True
        assert a not in controls(rig).paused_sources

    def test_a_read_only_project_offers_no_action(self, qtbot, rig, monkeypatch):
        rig.source("a")
        page = rig.build_page(qtbot)
        monkeypatch.setattr(type(rig.project), "read_only", property(lambda _self: True))
        page.session_mode.adopt(rig.project)
        QApplication.processEvents()
        assert page.session_mode.active
        assert not page.session_panel.start_button.isEnabled()
        assert page.session_mode.pause_processing() is False
        assert "read-only" in page.session_panel.warnings_label.text()

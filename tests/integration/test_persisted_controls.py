"""Operator controls persist and the engine obeys them (0.1.1 revised phase 7).

Intake (global and per source) and recognition (running / paused / stopped)
are operator **intent**, stored in the project and read by the engine on every
step and after every restart - never reset to "running" by a restart, which
is what revised phase 6's in-memory flags did. *Finish current and stop* and
*Cancel queued work* are distinct, persisted first, and leave nothing claimed.
"""

from __future__ import annotations

from collections import deque

import pytest
from sqlalchemy import func, select
from tests.engine_rig import OPTIONS, EngineRig, KillAt, Killed, readable_sheets

from omr_scanner.database.models import AuditEvent, BatchScan, IntakeFile, ScanJobStatus
from omr_scanner.domain.processing import EngineLimits, EngineState
from omr_scanner.domain.session_controls import CONTROL_ENTITY, ProcessingIntent
from omr_scanner.domain.session_snapshot import SessionActivity
from omr_scanner.services import intake as intake_service
from omr_scanner.services import session_controls, session_snapshot
from omr_scanner.services.recognition_pool import InlineRecogniser
from omr_scanner.services.review_store import ReviewError


@pytest.fixture
def rig(project_session):
    return EngineRig(project_session)


def statuses(rig: EngineRig) -> dict[str, int]:
    with rig.database.session() as session:
        return {
            str(status): int(count)
            for status, count in session.execute(
                select(BatchScan.status, func.count()).group_by(BatchScan.status)
            ).all()
        }


def ledger_rows(rig: EngineRig, source: str | None = None) -> int:
    with rig.database.session() as session:
        statement = select(func.count()).select_from(IntakeFile)
        if source is not None:
            statement = statement.where(IntakeFile.source_id == source)
        return int(session.scalar(statement) or 0)


def control_events(rig: EngineRig) -> list[tuple[str, str, str]]:
    with rig.database.session() as session:
        return [
            (row.action, row.reviewer, row.new_value)
            for row in session.scalars(
                select(AuditEvent)
                .where(AuditEvent.entity_type == CONTROL_ENTITY)
                .order_by(AuditEvent.event_id)
            ).all()
        ]


def restart(rig: EngineRig, **options: object) -> None:
    """A clean application stop and start: the in-memory engine is gone."""
    if rig.engine is not None:
        rig.engine.shutdown()
    rig.new_engine(**options)  # type: ignore[arg-type]


def with_ten_ready(rig: EngineRig) -> None:
    rig.source("a")
    rig.write("a", [(f"{i:06d}.png", data) for i, data in enumerate(readable_sheets(12)[:10])])
    rig.new_engine(limits=EngineLimits(max_in_flight=4, claim_window=4))
    rig.make_ready()
    assert rig.engine is not None
    assert rig.engine.form_units()


class TestIntakePause:
    def test_global_intake_pause_survives_restart_and_loses_nothing(self, rig):
        rig.source("a")
        session_controls.set_intake_paused(rig.database, rig.session_id, True, actor="op")
        rig.write("a", [("000001.png", readable_sheets(12)[0])])
        rig.new_engine()
        rig.make_ready()
        assert ledger_rows(rig) == 0, "a paused session's sources are not listed"
        restart(rig)
        rig.make_ready()
        assert ledger_rows(rig) == 0
        assert rig.engine is not None and rig.engine.status().intake_paused
        session_controls.set_intake_paused(rig.database, rig.session_id, False, actor="op")
        rig.make_ready()
        rig.run()
        assert statuses(rig).get(ScanJobStatus.COMPLETED.value, 0) + statuses(rig).get(
            ScanJobStatus.WARNING.value, 0
        ) == 1
        assert [item[0] for item in control_events(rig)] == ["intake_paused", "intake_resumed"]

    def test_a_paused_source_is_skipped_kept_and_never_called_unreachable(self, rig):
        a = rig.source("a")
        b = rig.source("b")
        session_controls.set_source_paused(rig.database, a, True, actor="op")
        sheets = readable_sheets(12)
        rig.write("a", [("000001.png", sheets[0])])
        rig.write("b", [("000002.png", sheets[1])])
        rig.new_engine()
        rig.make_ready()
        rig.run()
        assert ledger_rows(rig, a) == 0 and ledger_rows(rig, b) == 1
        snapshot = session_snapshot.take_snapshot(rig.database, rig.session_id, now=rig.clock())
        by_id = {item.source_id: item for item in snapshot.sources}
        assert by_id[a].intake_paused and not by_id[b].intake_paused
        assert snapshot.unreachable_sources == ()
        assert snapshot.activity is SessionActivity.INTAKE_PAUSED
        assert not snapshot.caught_up
        restart(rig)
        rig.make_ready()
        assert ledger_rows(rig, a) == 0
        session_controls.set_source_paused(rig.database, a, False, actor="op")
        rig.make_ready()
        rig.run()
        assert ledger_rows(rig, a) == 1
        assert intake_service.get_source(rig.database, a).reachability.value == "online"


class TestProcessingPause:
    def test_pause_lets_in_flight_sheets_finish_and_commit_and_survives_restart(self, rig):
        with_ten_ready(rig)
        engine = rig.engine
        assert engine is not None
        engine.step()  # claims and submits four
        assert engine.in_flight == 4 and statuses(rig)["processing"] == 4
        session_controls.pause_processing(rig.database, rig.session_id, actor="op")
        for _ in range(10):
            engine.step()
        counts = statuses(rig)
        assert engine.in_flight == 0
        assert counts.get("processing", 0) == 0
        assert counts.get("completed", 0) + counts.get("warning", 0) == 4  # recorded
        assert counts.get("pending", 0) == 6  # nothing new claimed
        status = engine.status()
        assert status.scheduling_paused and status.processing_intent is ProcessingIntent.PAUSED

        restart(rig, limits=EngineLimits(max_in_flight=4, claim_window=4))
        assert rig.engine is not None
        report_controls = session_controls.get_controls(rig.database, rig.session_id)
        assert report_controls.processing is ProcessingIntent.PAUSED
        rig.run()
        assert statuses(rig).get("pending", 0) == 6, "a paused session must not resume itself"
        snapshot = session_snapshot.take_snapshot(rig.database, rig.session_id, now=rig.clock())
        assert snapshot.activity is SessionActivity.PROCESSING_PAUSED

        session_controls.resume_processing(rig.database, rig.session_id, actor="op")
        rig.run()
        assert statuses(rig).get("pending", 0) == 0
        assert [item[0] for item in control_events(rig)] == [
            "processing_paused", "processing_resumed",
        ]

    def test_running_intent_is_restored_after_a_crash(self, rig):
        with_ten_ready(rig)
        rig.engine._hooks = KillAt("committed", 1)  # type: ignore[union-attr]
        with pytest.raises(Killed):
            rig.run()
        assert rig.engine is not None and rig.engine.state is EngineState.FAULTED
        rig.new_engine()  # restart: recovery, then the stored intent (running)
        rig.run()
        counts = statuses(rig)
        assert counts.get("pending", 0) == counts.get("processing", 0) == 0


class TestStopPolicies:
    def test_finish_current_and_stop_settles_current_work_and_stays_stopped(self, rig):
        with_ten_ready(rig)
        engine = rig.engine
        assert engine is not None
        engine.step()
        assert engine.in_flight == 4
        status = engine.finish_current_and_stop(actor="op", reason="end of shift")
        counts = statuses(rig)
        assert status.state is EngineState.STOPPED
        assert counts.get("processing", 0) == 0, "a clean stop leaves nothing claimed"
        assert counts.get("completed", 0) + counts.get("warning", 0) == 4
        assert counts.get("pending", 0) == 6
        assert ("finish_requested", "op", "stopped") in control_events(rig)
        rig.new_engine()
        rig.run()
        assert statuses(rig).get("pending", 0) == 6, "stopped is not resumed by a restart"
        snapshot = session_snapshot.take_snapshot(rig.database, rig.session_id, now=rig.clock())
        assert snapshot.activity is SessionActivity.PROCESSING_STOPPED

    def test_a_crash_while_finishing_comes_back_stopped(self, rig):
        with_ten_ready(rig)
        engine = rig.engine
        assert engine is not None
        engine.step()
        engine._hooks = KillAt("committed", 2)  # killed while current work settles
        with pytest.raises(Killed):
            engine.finish_current_and_stop(actor="op")
        rig.new_engine()
        rig.run()
        counts = statuses(rig)
        assert counts.get("processing", 0) == 0
        assert counts.get("completed", 0) + counts.get("warning", 0) == 2
        assert session_controls.get_controls(
            rig.database, rig.session_id
        ).processing is ProcessingIntent.STOPPED

    def test_cancel_queued_is_named_withdraws_what_has_not_started_and_keeps_the_rest(
        self, rig
    ):
        class TwoInWorkers(InlineRecogniser):
            """The first two submitted sheets are inside workers and cannot be withdrawn."""

            def cancel_queued(self) -> list[int]:
                kept = list(self._queue)[:2]
                dropped = [ticket for ticket, _path in list(self._queue)[2:]]
                self._queue = deque(kept)
                return dropped

        rig.source("a")
        rig.write("a", [(f"{i:06d}.png", d) for i, d in enumerate(readable_sheets(12)[:10])])
        engine = rig.new_engine(
            limits=EngineLimits(max_in_flight=4, claim_window=4),
            recogniser=TwoInWorkers(rig.template, options=OPTIONS, recognise=rig.recognise),
        )
        rig.make_ready()
        engine.form_units()
        engine.step()
        assert engine.in_flight == 4
        with pytest.raises(ReviewError):
            engine.cancel_queued_and_stop(actor="  ")
        assert engine.state is EngineState.RUNNING  # refused: nothing happened
        assert session_controls.get_controls(
            rig.database, rig.session_id
        ).processing is ProcessingIntent.RUNNING
        status = engine.cancel_queued_and_stop(actor="Operator", reason="wrong folder")
        counts = statuses(rig)
        assert status.state is EngineState.STOPPED
        assert counts.get("processing", 0) == 0
        assert counts.get("completed", 0) + counts.get("warning", 0) == 2  # running ones recorded
        assert counts.get("pending", 0) == 8  # withdrawn, durable, resumable
        assert ("queue_cancelled", "Operator", "stopped") in control_events(rig)
        rig.new_engine()
        rig.run()
        assert statuses(rig).get("pending", 0) == 8

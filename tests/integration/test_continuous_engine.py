"""The continuous engine end to end: intake -> finite units -> durable sheets (revised phase 6).

Real project database, real intake service on a fake filesystem and clock, the
stress dataset's real sheets, real recognition results (replayed per content,
see :mod:`tests.engine_rig`). Kill and restart cases are in
``test_engine_recovery.py``; bounds in ``test_engine_backpressure.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from tests.engine_rig import EngineRig, digest, durable_view, readable_sheets, sheet_bytes

from omr_scanner.database.models import BatchScan, BatchStatus, ScanBatch, ScanJobStatus
from omr_scanner.domain.processing import EngineState, UnitPolicy
from omr_scanner.services import scan_sessions


@pytest.fixture
def rig(project_session):
    return EngineRig(project_session)


def _batches(rig: EngineRig) -> list[scan_sessions.BatchInfo]:
    return list(scan_sessions.batches_of(rig.database, rig.session_id))


class TestBasicFlow:
    def test_two_sources_become_sealed_units_and_every_sheet_is_read_once(self, rig):
        sheets = readable_sheets(24)[:20]
        rig.source("a")
        rig.source("b")
        rig.write("a", [(f"{i:06d}.png", data) for i, data in enumerate(sheets[:10])])
        rig.write("b", [(f"{i:06d}.png", data) for i, data in enumerate(sheets[10:])])
        engine = rig.new_engine()
        rig.make_ready()
        rig.run()

        batches = _batches(rig)
        assert len(batches) == 2
        assert all(item.sealed_at is not None for item in batches)
        assert {item.status for item in batches} <= {
            BatchStatus.COMPLETED.value,
            BatchStatus.COMPLETED_WITH_ERRORS.value,
        }
        with rig.database.session() as session:
            sources = set(session.scalars(select(ScanBatch.source_id)).all())
            statuses = session.scalars(select(BatchScan.status)).all()
        assert sources == set(rig.sources.values())
        terminal = {ScanJobStatus.COMPLETED.value, ScanJobStatus.WARNING.value,
                    ScanJobStatus.FAILED.value, ScanJobStatus.DUPLICATE.value}
        assert set(statuses) <= terminal
        unique = {digest(item) for item in sheets}
        assert all(rig.recognise.calls[key] <= 1 for key in unique)
        status = engine.status()
        assert status.caught_up
        assert status.in_flight == 0
        assert status.state is EngineState.RUNNING
        view = durable_view(rig.database, rig.session_id)
        assert not view["pending"]
        engine.shutdown()
        assert engine.state is EngineState.STOPPED


class TestMultiSourceScenario:
    def test_three_sources_arrivals_late_duplicate_and_restart(self, rig):
        """§29: three sources, arrivals during processing, a late duplicate, a restart.

        A1 and B1 register; A2 arrives while B1 is read; C comes later; a later
        sheet repeats a Student ID; the engine is stopped with work left; a new
        engine finishes the same session.
        """
        from tests.engine_rig import KillAt, Killed

        from omr_scanner.domain.review import ConflictType

        sheets = readable_sheets(48)[:40]
        # Index 18 repeats index 17's Student ID on a different image.
        a1, b1, a2, c1 = sheets[:8], sheets[8:16], sheets[16:17], sheets[19:30]
        late = [sheets[17], sheets[18]]
        policy = UnitPolicy(max_unit_size=8, trickle_seconds=0)
        rig.source("a")
        rig.source("b")
        rig.write("a", [(f"{i:06d}.png", d) for i, d in enumerate(a1)])
        rig.write("b", [(f"{i:06d}.png", d) for i, d in enumerate(b1)])
        engine = rig.new_engine(unit_policy=policy)
        rig.make_ready()
        assert len(engine.form_units()) == 1  # one unit per pass: A1 (oldest first)
        engine.step()
        assert len(engine.form_units()) == 1  # B1
        for _ in range(10):
            engine.step()
        # A2 arrives while B1 is being read.
        rig.write("a", [("000100.png", a2[0]), ("000101.png", late[0])])
        rig.make_ready()
        engine.form_units()
        engine.step()
        rig.source("c")
        rig.write("c", [(f"c{i:06d}.png", d) for i, d in enumerate(c1)])
        rig.write("c", [("late.png", late[1])])
        rig.make_ready()
        # Stop (a kill) while work remains.
        engine._hooks = KillAt("committed", 3)  # injected mid-run
        with pytest.raises(Killed):
            rig.run()
        at_stop = _batches(rig)
        assert len(at_stop) >= 3

        rig.new_engine(unit_policy=policy)
        rig.make_ready()
        rig.run()
        rig.engine.shutdown()  # type: ignore[union-attr]
        batches = _batches(rig)
        assert [item.batch_id for item in batches][: len(at_stop)] == [
            item.batch_id for item in at_stop
        ]
        assert all(item.sealed_at is not None for item in batches)
        with rig.database.session() as session:
            by_source = {
                str(source): count
                for source, count in session.execute(
                    select(ScanBatch.source_id, func.count()).group_by(ScanBatch.source_id)
                ).all()
            }
            members = session.execute(select(BatchScan.batch_id, BatchScan.content_sha256)).all()
        assert set(by_source) == set(rig.sources.values())
        # Every file exactly once; units distinct; nothing counted twice.
        written = {digest(item) for item in [*a1, *b1, *a2, *c1, *late]}
        contents = [sha for _batch, sha in members]
        assert set(contents) <= written
        assert len(contents) == len(set(contents))
        view = durable_view(rig.database, rig.session_id)
        assert not view["pending"]
        assert written == set(view["results"]) | set(view["unread_duplicates"])
        duplicates = {
            item[0] for item in view["conflicts"]
            if item[1] == ConflictType.IDENTIFIER_DUPLICATE.value
        }
        assert {digest(late[0]), digest(late[1])} <= duplicates
        assert len(view["effective"]) == len(set(view["effective"]))

    def test_status_snapshot_and_idempotent_start(self, rig):
        sheets = readable_sheets(12)[:6]
        rig.source("a")
        rig.write("a", [(f"{i:06d}.png", d) for i, d in enumerate(sheets)])
        engine = rig.new_engine()
        before = engine.status()
        assert before.state is EngineState.RUNNING
        assert before.registered == 0
        assert before.caught_up  # nothing known yet: caught up as of now
        rig.make_ready()
        assert engine.status().ready_intake == 6
        engine.form_units()
        status = engine.status()
        assert status.ready_intake == 0 and status.pending == 6 and status.units_registered == 1
        assert not status.caught_up
        rig.run()
        status = engine.status()
        assert status.caught_up
        assert status.completed + status.failed == 6
        assert status.sheets_committed == 6 and status.in_flight == 0
        with pytest.raises(Exception, match="already"):
            engine.start()
        # A second engine's recovery over a finished session changes nothing.
        snapshot = durable_view(rig.database, rig.session_id)
        rig.new_engine()
        assert durable_view(rig.database, rig.session_id) == snapshot


class TestRunLoop:
    def test_the_thin_loop_runs_on_a_thread_and_stops_cleanly(self, rig):
        import threading

        sheets = readable_sheets(12)[:6]
        rig.source("a")
        rig.write("a", [(f"{i:06d}.png", d) for i, d in enumerate(sheets)])
        engine = rig.new_engine(start=False)
        stop = threading.Event()
        result: dict = {}

        def advance(seconds: float) -> None:
            rig.clock.advance(6)  # the injected clock moves; no real waiting
            if engine.status().caught_up and engine.status().completed + engine.status().failed:
                stop.set()

        thread = threading.Thread(
            target=lambda: result.setdefault(
                "status", engine.run(stop.is_set, idle_sleep=0, wait=0, sleeper=advance)
            )
        )
        thread.start()
        thread.join(timeout=120)
        assert not thread.is_alive()
        assert result["status"].state is EngineState.STOPPED
        assert result["status"].completed + result["status"].failed == 6

    def test_idle_steps_do_not_log_at_info(self, rig, caplog):
        import logging

        engine = rig.new_engine()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="omr_scanner"):
            for _ in range(200):
                engine.poll_intake()
                engine.form_units()
                engine.step()
        assert not [record for record in caplog.records if record.levelno >= logging.INFO]


class TestContinuousArrival:
    def test_new_files_form_a_new_unit_and_the_sealed_unit_never_grows(self, rig):
        sheets = readable_sheets(30)
        rig.source("a")
        rig.write("a", [(f"{i:06d}.png", data) for i, data in enumerate(sheets[:5])])
        engine = rig.new_engine(unit_policy=UnitPolicy(max_unit_size=50, trickle_seconds=0))
        rig.make_ready()
        engine.form_units()
        first = _batches(rig)
        assert len(first) == 1
        engine.step()
        rig.write("a", [(f"{i:06d}.png", data) for i, data in enumerate(sheets[5:9], start=5)])
        rig.make_ready()
        rig.run()
        batches = _batches(rig)
        assert len(batches) == 2
        assert batches[0].batch_id == first[0].batch_id
        assert batches[0].total_scans == 5
        assert batches[1].total_scans == 4
        status = engine.status()
        assert status.caught_up
        assert status.state is EngineState.RUNNING

    def test_caught_up_is_not_session_complete(self, rig):
        rig.source("a")
        rig.write("a", [("000001.png", sheet_bytes(2)[0])])
        engine = rig.new_engine()
        rig.make_ready()
        rig.run()
        assert engine.status().caught_up
        info = scan_sessions.get_scan_session(rig.database, rig.session_id)
        assert info is not None and info.state.value == "open"

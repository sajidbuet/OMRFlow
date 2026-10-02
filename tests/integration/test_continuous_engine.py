"""The continuous engine end to end: intake -> finite units -> durable sheets (revised phase 6).

Real project database, real intake service on a fake filesystem and clock, the
stress dataset's real sheets, real recognition results (replayed per content,
see :mod:`tests.engine_rig`). Kill and restart cases are in
``test_engine_recovery.py``; bounds in ``test_engine_backpressure.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
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

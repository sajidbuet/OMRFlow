"""One processing coordinator per project (0.1.1 revised phase 7, ARCHITECTURE_NOTES §13.1).

The finite Scan stage's run and the continuous engine each take the project's
coordinator lease before touching processing state; the second gets a typed
:class:`CoordinatorBusyError` and changes nothing - in particular the engine's
recovery never returns the claims of a finite run that is still reading. The
lease is released on normal completion, on shutdown and when an exception
escapes; an owner that no longer exists cannot keep it. (A real process kill
is in ``tests/crash/test_phase7_kills.py``.)
"""

from __future__ import annotations

import gc

import pytest
from sqlalchemy import select
from tests.engine_rig import EngineRig, KillAt, Killed, readable_sheets

from omr_scanner.database.models import BatchScan, BatchStatus, ScanBatch, ScanJobStatus
from omr_scanner.domain.processing import EngineState
from omr_scanner.services import batch_store, coordinator, scan_sessions
from omr_scanner.services.coordinator import CoordinatorBusyError, CoordinatorKind


@pytest.fixture
def rig(project_session):
    return EngineRig(project_session)


class Owner:
    """Stands in for a coordinator object (the Scan page, an engine)."""


def finite_run_in_progress(
    rig: EngineRig, tmp_path
) -> tuple[str, Owner, coordinator.CoordinatorLease]:
    """What the Scan stage holds mid-run: the lease, a running batch, queued rows."""
    paths = []
    for index, data in enumerate(readable_sheets(12)[:3]):
        path = tmp_path / f"finite{index}.png"
        path.write_bytes(data)
        paths.append(path)
    owner = Owner()
    lease = coordinator.acquire(rig.database, CoordinatorKind.FINITE_SCAN, owner=owner)
    batch = scan_sessions.start_batch(
        rig.database, paths, identity=batch_store.BatchIdentity.of(rig.template),
        started_by="op", scan_session_id=rig.session_id,
    )
    batch_store.mark_queued(rig.database, batch, paths)
    batch_store.set_batch_status(rig.database, batch, BatchStatus.RUNNING)
    return batch, owner, lease


def rows(rig: EngineRig, batch: str) -> list[str]:
    with rig.database.session() as session:
        return [
            str(item)
            for item in session.scalars(
                select(BatchScan.status).where(BatchScan.batch_id == batch)
            ).all()
        ]


def batch_status(rig: EngineRig, batch: str) -> str:
    with rig.database.session() as session:
        return str(session.get(ScanBatch, batch).status)  # type: ignore[union-attr]


def test_a_running_engine_refuses_the_finite_scan_stage(rig):
    rig.new_engine()
    with pytest.raises(CoordinatorBusyError) as refused:
        coordinator.acquire(rig.database, CoordinatorKind.FINITE_SCAN, owner=Owner())
    assert refused.value.holder.kind is CoordinatorKind.CONTINUOUS_ENGINE
    assert refused.value.requested is CoordinatorKind.FINITE_SCAN
    assert "continuous processing" in refused.value.user_message
    assert coordinator.holder_of(rig.database).kind is CoordinatorKind.CONTINUOUS_ENGINE  # type: ignore[union-attr]


def test_a_finite_run_refuses_the_engine_and_its_recovery_touches_nothing(rig, tmp_path):
    batch, owner, lease = finite_run_in_progress(rig, tmp_path)
    engine = rig.new_engine(start=False)
    with pytest.raises(CoordinatorBusyError) as refused:
        engine.start()
    assert refused.value.holder.kind is CoordinatorKind.FINITE_SCAN
    assert engine.state is EngineState.NEW
    # The engine's recovery would have returned these claims to pending.
    assert rows(rig, batch) == [ScanJobStatus.QUEUED.value] * 3
    assert batch_status(rig, batch) == BatchStatus.RUNNING.value
    # The finite run ends (its own settling would follow); the project is free.
    lease.release()
    del owner
    engine.start()
    assert rows(rig, batch) == [ScanJobStatus.PENDING.value] * 3  # now recovery's to repair
    rig.run()
    assert set(rows(rig, batch)) <= {"completed", "warning", "failed"}


def test_a_second_engine_is_refused_and_a_clean_stop_frees_the_project(rig):
    first = rig.new_engine()
    with pytest.raises(CoordinatorBusyError):
        rig.new_engine()
    first.shutdown()
    assert coordinator.holder_of(rig.database) is None
    lease = coordinator.acquire(rig.database, CoordinatorKind.FINITE_SCAN, owner=rig)
    lease.release()
    lease.release()  # idempotent
    rig.new_engine()


def test_an_exception_escaping_the_engine_releases_the_project(rig):
    rig.source("a")
    rig.write("a", [(f"{i:06d}.png", d) for i, d in enumerate(readable_sheets(12)[:4])])
    engine = rig.new_engine(hooks=KillAt("committed", 1))
    rig.make_ready()
    with pytest.raises(Killed):
        rig.run()
    assert engine.state is EngineState.FAULTED
    assert coordinator.holder_of(rig.database) is None
    restarted = rig.new_engine()  # the project is not poisoned
    assert restarted.state is EngineState.RUNNING
    rig.run()


def test_an_owner_that_no_longer_exists_cannot_keep_the_project(rig):
    owner = Owner()
    coordinator.acquire(rig.database, CoordinatorKind.FINITE_SCAN, owner=owner)
    assert coordinator.holder_of(rig.database) is not None
    del owner
    gc.collect()
    assert coordinator.holder_of(rig.database) is None
    rig.new_engine()


def test_the_lease_is_per_project(rig, workspace):
    from omr_scanner.services import create_project

    other = create_project(workspace, "Another examination")
    try:
        rig.new_engine()
        lease = coordinator.acquire(other.database, CoordinatorKind.FINITE_SCAN, owner=other)
        lease.release()
    finally:
        other.close()


def test_a_finished_session_close_is_refused_while_the_engine_processes(rig):
    from omr_scanner.domain.session_finish import BlockerCode
    from omr_scanner.services import session_finish

    rig.new_engine()
    outcome = session_finish.finish_scan_session(
        rig.database, rig.session_id, closed_by="op"
    )
    assert not outcome.closed
    assert outcome.codes[0] is BlockerCode.PROCESSING_ACTIVE

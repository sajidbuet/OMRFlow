"""The engine with real worker processes (``spawn``, as on Windows) - revised phase 6.

* the warm pool reads several finite units without restarting, and gives the
  same durable outcome as reading in-process;
* a worker process killed by the operating system mid-run breaks the pool;
  its sheets are retried on a fresh pool, nothing is lost or failed for it;
* recycling replaces the pool between drains;
* no worker process outlives the engine.

Real recognition here (no replay): the workers read the project copies.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import psutil
import pytest
from sqlalchemy import select
from tests.engine_rig import OPTIONS, EngineRig, durable_view, readable_sheets

from omr_scanner.database.models import BatchScan, ScanJobStatus
from omr_scanner.domain.processing import EngineLimits, UnitPolicy
from omr_scanner.services import create_project
from omr_scanner.services.recognition_pool import ProcessRecogniser

SHEETS = 12


def _workers() -> list[psutil.Process]:
    try:
        return [
            child
            for child in psutil.Process().children(recursive=True)
            if "python" in child.name().lower()
        ]
    except psutil.Error:
        return []


def _wait_for_no_workers(timeout: float = 20.0) -> list[psutil.Process]:
    deadline = time.monotonic() + timeout
    left = _workers()
    while left and time.monotonic() < deadline:
        time.sleep(0.1)
        left = [item for item in _workers() if item.is_running()]
    return left


@pytest.fixture
def projects(workspace) -> Iterator[list]:
    opened: list = []
    yield opened
    for project in opened:
        project.close()


def _rig(projects, workspace, name: str) -> EngineRig:
    project = create_project(workspace, name)
    projects.append(project)
    rig = EngineRig(project)
    sheets = readable_sheets(16)[:SHEETS]
    rig.source("a")
    rig.write("a", [(f"{i:06d}.png", data) for i, data in enumerate(sheets)])
    return rig


def _run(rig: EngineRig, recogniser: ProcessRecogniser, *, kill_a_worker: bool = False) -> None:
    engine = rig.new_engine(
        recogniser=recogniser,
        limits=EngineLimits(max_in_flight=4, claim_window=4),
        unit_policy=UnitPolicy(max_unit_size=4, trickle_seconds=0),
    )
    rig.make_ready()
    killed = False
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        engine.form_units()
        report = engine.step(wait=0.2)
        if kill_a_worker and not killed and engine.in_flight:
            workers = _workers()
            if workers:
                workers[0].kill()
                killed = True
        if report.idle and not engine.in_flight and engine.status().caught_up:
            break
    else:
        raise AssertionError("the engine did not catch up")
    if kill_a_worker:
        assert killed
    engine.shutdown()


def test_warm_pool_matches_in_process_reading(projects, workspace):
    inline = _rig(projects, workspace, "Inline")
    engine = inline.new_engine(unit_policy=UnitPolicy(max_unit_size=4, trickle_seconds=0))
    inline.make_ready()
    inline.run()
    engine.shutdown()
    expected = durable_view(inline.database, inline.session_id)

    pooled = _rig(projects, workspace, "Pool")
    recogniser = ProcessRecogniser(pooled.template, workers=2, options=OPTIONS)
    _run(pooled, recogniser)
    assert durable_view(pooled.database, pooled.session_id) == expected
    assert recogniser.pools_started == 1, "one warm pool across three finite units"
    assert _wait_for_no_workers() == []


def test_a_killed_worker_process_loses_no_sheet(projects, workspace):
    rig = _rig(projects, workspace, "Lost")
    recogniser = ProcessRecogniser(rig.template, workers=2, options=OPTIONS)
    _run(rig, recogniser, kill_a_worker=True)
    with rig.database.session() as session:
        rows = session.execute(
            select(BatchScan.status, BatchScan.attempt_count, BatchScan.error_message)
        ).all()
    assert len(rows) == SHEETS
    assert all(status != ScanJobStatus.PENDING.value for status, _a, _m in rows)
    assert all(attempts == 1 for _s, attempts, _m in rows)
    assert not any("worker process failed" in message for _s, _a, message in rows)
    assert recogniser.pools_started >= 2
    assert _wait_for_no_workers() == []


def test_recycling_replaces_the_pool_between_drains(projects, workspace):
    rig = _rig(projects, workspace, "Recycle")
    recogniser = ProcessRecogniser(rig.template, workers=2, options=OPTIONS, recycle_after=2)
    _run(rig, recogniser)
    assert recogniser.pools_started >= 3
    with rig.database.session() as session:
        statuses = set(session.scalars(select(BatchScan.status)).all())
    assert ScanJobStatus.PENDING.value not in statuses
    assert ScanJobStatus.PROCESSING.value not in statuses
    assert _wait_for_no_workers() == []

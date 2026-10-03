"""Phase 7 writers and readers at once (ADR-0009's strategy, extended).

While the engine's coordinator thread reconciles intake, registers units,
commits results (each with its quality decision) and runs the incremental
duplicate pass, an operator thread answers suggested rescans and conflicts
and changes controls, and a GUI-like poller takes session snapshots. Asserted:
no ``database is locked``, every operator action commits, operator latency far
below the busy timeout, every snapshot partitions, and the coordinator lease
refuses a second coordinator throughout.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections.abc import Callable

from sqlalchemy import select
from tests.engine_rig import EngineRig, readable_sheets
from tests.quality_rig import blank_page, displaced_id

from omr_scanner.database.engine import BUSY_TIMEOUT_MS
from omr_scanner.database.models import ReviewConflict
from omr_scanner.domain.processing import EngineLimits, UnitPolicy
from omr_scanner.domain.review import ConflictState, ReasonCode
from omr_scanner.services import (
    coordinator,
    quality_decisions,
    review_store,
    session_controls,
    session_snapshot,
)


def test_operator_actions_snapshots_and_the_engine_run_together(project_session):
    rig = EngineRig(project_session)
    sheets = readable_sheets(70)
    rig.source("a")
    spare = rig.source("b")
    rig.write("a", [(f"{i:06d}.png", data) for i, data in enumerate(sheets[:20])])
    rig.write("a", [(f"blank{i}.png", blank_page(i)) for i in range(4)])
    rig.write("a", [(f"moved{i}.png", displaced_id(i)) for i in range(2)])
    engine = rig.new_engine(
        limits=EngineLimits(max_in_flight=4, claim_window=4, max_commit_group=1),
        unit_policy=UnitPolicy(max_unit_size=5, trickle_seconds=0),
    )
    rig.make_ready()
    rig.run()
    suggestions = [
        item.scan_id
        for item in quality_decisions.outstanding_suggestions(rig.database, rig.session_id)
    ]
    with rig.database.session() as session:
        conflicts = list(
            session.scalars(
                select(ReviewConflict.conflict_id).where(
                    ReviewConflict.state == ConflictState.OPEN.value
                )
            ).all()
        )
    assert len(suggestions) >= 4 and conflicts

    # More sheets arrive while the operator and the poller work.
    rig.write("a", [(f"late{i:06d}.png", data) for i, data in enumerate(sheets[20:60])])
    errors: list[BaseException] = []
    operator_latency: list[float] = []
    snapshot_latency: list[float] = []
    partitions_ok: list[bool] = []
    refused: list[bool] = []
    stop = threading.Event()

    def timed(action: Callable[..., object], *args: object, **kwargs: object) -> None:
        started = time.perf_counter()
        action(*args, **kwargs)
        operator_latency.append(time.perf_counter() - started)

    def operator() -> None:
        try:
            for index, scan_id in enumerate(suggestions):
                if index % 2:
                    timed(quality_decisions.dismiss_suggestion, rig.database, scan_id,
                          reviewer="Operator")
                else:
                    timed(quality_decisions.confirm_suggestion, rig.database, scan_id,
                          reviewer="Operator")
            while not stop.is_set():
                for conflict_id in conflicts[:5]:
                    timed(review_store.accept_machine_value, rig.database, conflict_id,
                          reviewer="Operator", reason=ReasonCode.MACHINE_CONFIRMED)
                    timed(review_store.reopen, rig.database, conflict_id, reviewer="Operator")
                timed(session_controls.set_source_paused, rig.database, spare, True,
                      actor="Operator")
                timed(session_controls.set_source_paused, rig.database, spare, False,
                      actor="Operator")
                try:
                    coordinator.acquire(
                        rig.database, coordinator.CoordinatorKind.FINITE_SCAN, owner=stop
                    ).release()
                    refused.append(False)
                except coordinator.CoordinatorBusyError:
                    refused.append(True)
        except BaseException as exc:
            errors.append(exc)

    def poller() -> None:
        try:
            while not stop.is_set():
                started = time.perf_counter()
                snapshot = session_snapshot.take_snapshot(rig.database, rig.session_id)
                snapshot_latency.append(time.perf_counter() - started)
                partitions_ok.append(snapshot.partitions)
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=operator, name="operator"),
        threading.Thread(target=poller, name="snapshot-poller"),
    ]
    for thread in threads:
        thread.start()
    try:
        rig.make_ready()
        rig.run()
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=60)
    engine.shutdown()

    assert not errors, errors
    assert engine.status().last_error == ""
    assert operator_latency and snapshot_latency
    assert all(partitions_ok), "every snapshot must partition, even mid-commit"
    assert refused and all(refused), "the coordinator lease held throughout"
    assert quality_decisions.count_outstanding(rig.database, rig.session_id) == 0
    final = session_snapshot.take_snapshot(rig.database, rig.session_id)
    assert final.partitions and final.partition.queued == final.partition.processing == 0
    worst = max(operator_latency)
    print(
        f"\nOperator actions during processing: {len(operator_latency)}, "
        f"median {statistics.median(operator_latency) * 1000:.1f} ms, "
        f"worst {worst * 1000:.1f} ms; snapshots: {len(snapshot_latency)}, "
        f"median {statistics.median(snapshot_latency) * 1000:.1f} ms, "
        f"worst {max(snapshot_latency) * 1000:.1f} ms (busy_timeout {BUSY_TIMEOUT_MS} ms)"
    )
    assert worst < BUSY_TIMEOUT_MS / 1000 / 2

"""Concurrent writers: the engine and a Resolve decision at the same time (ADR-0009).

The writer strategy is one coordinator per project for processing (the
engine's thread: intake reconciliation, registration, claims, results, unit
passes) plus SQLite's ``busy_timeout`` as the safety net for the one other
writer a live session has - an operator's Resolve decision on the GUI thread.
This test runs both at once on one project and asserts that neither ever sees
``database is locked``, that every decision commits with its audit event, and
that a decision's latency stays far below the timeout.
"""

from __future__ import annotations

import statistics
import threading
import time

from sqlalchemy import func, select, text
from tests.engine_rig import EngineRig, readable_sheets

from omr_scanner.database.engine import BUSY_TIMEOUT_MS
from omr_scanner.database.models import AuditEvent, ReviewConflict, ScanJobStatus
from omr_scanner.domain.processing import EngineLimits, UnitPolicy
from omr_scanner.domain.review import ConflictState, ReasonCode
from omr_scanner.services import review_store


def test_busy_timeout_is_set_on_every_connection(project_session):
    with project_session.database.session() as session:
        assert session.execute(text("PRAGMA busy_timeout")).scalar_one() == BUSY_TIMEOUT_MS


def test_engine_and_resolve_decisions_write_concurrently(project_session):
    rig = EngineRig(project_session)
    sheets = readable_sheets(70)[:60]
    rig.source("a")
    rig.write("a", [(f"{i:06d}.png", data) for i, data in enumerate(sheets[:20])])
    engine = rig.new_engine(
        limits=EngineLimits(max_in_flight=4, claim_window=4, max_commit_group=1),
        unit_policy=UnitPolicy(max_unit_size=5, trickle_seconds=0),
    )
    rig.make_ready()
    rig.run()
    with rig.database.session() as session:
        targets = list(
            session.scalars(
                select(ReviewConflict.conflict_id).where(
                    ReviewConflict.state == ConflictState.OPEN.value
                )
            ).all()
        )
    assert targets, "the first sheets raised no conflict to decide"

    # More sheets arrive on a second source while the operator works.
    rig.source("b")
    rig.write("b", [(f"{i:06d}.png", data) for i, data in enumerate(sheets[20:], start=20)])
    errors: list[BaseException] = []
    latencies: list[float] = []
    accepted = 0
    stop = threading.Event()

    def operator() -> None:
        nonlocal accepted
        try:
            while not stop.is_set():
                for conflict_id in targets:
                    started = time.perf_counter()
                    review_store.accept_machine_value(
                        rig.database, conflict_id, reviewer="Reviewer",
                        reason=ReasonCode.MACHINE_CONFIRMED,
                    )
                    latencies.append(time.perf_counter() - started)
                    accepted += 1
                    started = time.perf_counter()
                    review_store.reopen(rig.database, conflict_id, reviewer="Reviewer")
                    latencies.append(time.perf_counter() - started)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    thread = threading.Thread(target=operator, name="resolve-operator")
    thread.start()
    try:
        rig.make_ready()
        rig.run()
    finally:
        stop.set()
        thread.join(timeout=60)
    engine.shutdown()

    assert not errors, errors
    assert engine.status().last_error == ""
    assert accepted > 0
    with rig.database.session() as session:
        logged = session.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "accepted")
        )
        unfinished = session.scalar(
            select(func.count()).select_from(review_store.BatchScan).where(
                review_store.BatchScan.status.in_(
                    [ScanJobStatus.PENDING.value, ScanJobStatus.PROCESSING.value]
                )
            )
        )
    assert logged == accepted
    assert unfinished == 0
    worst = max(latencies)
    print(
        f"\nResolve decisions during processing: {len(latencies)}, "
        f"median {statistics.median(latencies) * 1000:.1f} ms, "
        f"worst {worst * 1000:.1f} ms (busy_timeout {BUSY_TIMEOUT_MS} ms)"
    )
    assert worst < BUSY_TIMEOUT_MS / 1000 / 2

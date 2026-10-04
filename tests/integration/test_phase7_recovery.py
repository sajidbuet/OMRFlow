"""Restart semantics added in revised phase 7, and the worker-lost retry-state audit.

Revised phase 6 keeps the per-sheet "worker process died" retry counter
(``EngineLimits.infrastructure_retries``) in memory, so a restart resets it.
Decision recorded here (``PHASE_G_HANDOFF.md`` §8): **intentionally
ephemeral**, because these tests show that resetting it

* only grants a sheet up to ``1 + infrastructure_retries`` submissions per
  process start - a bounded retry *opportunity*; nothing restarts the engine by
  itself, so there is no automatic loop;
* never produces a second committed result, a second conflict, a duplicate
  audit event or a changed effective set - a result can only be written onto a
  row still claimed (``claimed_only``), and a committed row is terminal;
* never bypasses a correctness rule: the placeholder result of a lost worker is
  a *software* fault (quality decision ``retry_processing``), never a rescan.

And the restart sequence: no session, batch or supersession is created; the
persisted intent and the quality decisions are restored, not re-derived.
"""

from __future__ import annotations

from sqlalchemy import func, select
from tests.engine_rig import EngineRig, readable_sheets, structure
from tests.integration.test_engine_backpressure import (
    HeldRecogniser,
    engine_with,
    register_units,
)

from omr_scanner.database.models import AuditEvent, BatchScan, ReviewConflict, ScanJobStatus
from omr_scanner.domain.processing import EngineLimits
from omr_scanner.domain.quality_decision import QualityDecision
from omr_scanner.domain.session_controls import ProcessingIntent
from omr_scanner.services import quality_decisions, session_controls, session_population

LIMITS = EngineLimits(max_in_flight=4, claim_window=4, infrastructure_retries=1)


def test_a_restart_resets_only_the_retry_opportunity_never_the_result(project_session, tmp_path):
    rig = EngineRig(project_session)
    register_units(rig, units=1, size=4, tmp=tmp_path)

    # First process: the target's worker dies once; its retry budget is spent.
    first = HeldRecogniser()
    engine = engine_with(rig, first, LIMITS)
    engine.step()
    target = first.queue[0][0]
    first.lose = {target}
    first.release(1)
    engine.step()  # lost -> released and claimed again (budget 1 of 1 used)
    engine.shutdown(drain=False)  # the application stops; the counter is gone
    assert first.submissions == 4 + 1

    # Second process: the counter starts again - one more retry is granted.
    second = HeldRecogniser()
    engine = engine_with(rig, second, LIMITS)
    engine.step()
    assert second.queue[0][0] == target
    second.lose = {target}
    second.release(1)
    engine.step()  # lost again -> retried again (the reset's only effect)
    second.lose = {target}
    second.release()
    while engine.step().claimed or engine.in_flight:
        second.release()
    engine.shutdown()
    assert second.submissions == 4 + 1  # bounded: 1 + infrastructure_retries per start

    with rig.database.session() as session:
        rows = session.scalars(select(BatchScan)).all()
        conflicts = session.scalar(
            select(func.count()).select_from(ReviewConflict).where(
                ReviewConflict.scan_id == target
            )
        )
        events = session.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.scan_id == target)
        )
    row = next(item for item in rows if item.scan_id == target)
    assert row.status == ScanJobStatus.FAILED.value
    assert all(item.attempt_count == 1 for item in rows), "one committed result per sheet"
    assert conflicts == 1  # its one sheet-level conflict, never duplicated
    assert events == 1  # that conflict's single 'detected' event
    decision = quality_decisions.decision_of(rig.database, target)
    assert decision is not None and decision.decision is QualityDecision.RETRY_PROCESSING
    assert decision.suggested_reason is None  # a software fault is never a rescan
    population = session_population.session_population(rig.database, rig.session_id)
    assert len(population.effective) == 4


def test_a_committed_sheet_is_never_claimed_again_whatever_the_restarts(
    project_session, tmp_path
):
    rig = EngineRig(project_session)
    register_units(rig, units=1, size=3, tmp=tmp_path)
    for start in range(3):
        held = HeldRecogniser()
        held.release()
        engine = engine_with(rig, held, LIMITS)
        while engine.step().claimed or engine.in_flight:
            pass
        engine.shutdown()
        assert held.submissions == (3 if start == 0 else 0)
    with rig.database.session() as session:
        assert set(session.scalars(select(BatchScan.attempt_count)).all()) == {1}


def test_the_restart_sequence_creates_nothing_and_restores_intent(project_session):
    rig = EngineRig(project_session)
    rig.source("a")
    rig.write("a", [(f"{i}.png", data) for i, data in enumerate(readable_sheets(12)[:6])])
    rig.new_engine()
    rig.make_ready()
    rig.run()
    session_controls.pause_processing(rig.database, rig.session_id, actor="op")
    before = structure(rig.database, rig.session_id)
    decisions = quality_decisions.decisions_by_scan(rig.database, range(1, 50))
    assert len(decisions) == 6
    for _ in range(3):
        rig.engine.shutdown()  # type: ignore[union-attr]
        engine = rig.new_engine(start=False)
        report = engine.start()
        assert report.controls is not None
        assert report.controls.processing is ProcessingIntent.PAUSED
        assert report.decisions_backfilled == 0
        assert report.scan_recovery.scans_returned == 0
        assert structure(rig.database, rig.session_id) == before
        assert quality_decisions.decisions_by_scan(rig.database, range(1, 50)) == decisions

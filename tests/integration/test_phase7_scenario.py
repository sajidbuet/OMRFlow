"""Phase 7 synthetic validation: quality, rescans and controls across three scanners.

A deterministic headless scenario of several hundred real synthetic sheets
(the stress dataset's generator, seed 42) read by the real recognition engine
through the continuous engine: three watched sources, many finite units, clean
sheets, simulated damage (blank pages, displaced and erased identifier blocks -
``tests/quality_rig.py``), duplicate Student IDs across units and sources, a
kill and restarts, rejections confirmed from suggestions, rescans arriving at
**other** scanners, a confirmed replacement undone, and intake continuing
afterwards.

This is revised phase 7's automated validation. It is **not** the phase 9
synthetic qualification campaign (>= 10,000 sheets) and not a real-scanner run.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from tests.engine_rig import EngineRig, KillAt, Killed, digest, readable_sheets
from tests.quality_rig import blank_page, displaced_id, erased_id, rescan_of, roll_of

from omr_scanner.database.models import AuditEvent, BatchScan, ReviewConflict, ScanRejection
from omr_scanner.domain.processing import EngineLimits, UnitPolicy
from omr_scanner.domain.quality_decision import QualityDecision
from omr_scanner.domain.review import ConflictType
from omr_scanner.domain.scan_lifecycle import LifecycleAction, LifecycleState, RejectionReason
from omr_scanner.services import (
    quality_decisions,
    scan_lifecycle,
    session_population,
    session_snapshot,
)

UNIT = UnitPolicy(max_unit_size=40, trickle_seconds=0)
LIMITS = EngineLimits(max_in_flight=8, claim_window=8)
DAMAGED = (3, 4, 5, 6)  # identifier displaced; the clean originals are never written
ERASED = (8, 9)
SOURCES = "abc"


@pytest.fixture
def rig(project_session):
    return EngineRig(project_session)


def write_round_robin(rig: EngineRig, items: list[tuple[str, bytes]], offset: int = 0) -> None:
    for index, (name, data) in enumerate(items):
        rig.write(SOURCES[(index + offset) % len(SOURCES)], [(name, data)])


def run(rig: EngineRig) -> None:
    if rig.engine is None:
        rig.new_engine(limits=LIMITS, unit_policy=UNIT)
    rig.make_ready()
    rig.run()


def restart(rig: EngineRig) -> None:
    if rig.engine is not None:
        rig.engine.shutdown()
    rig.new_engine(limits=LIMITS, unit_policy=UNIT)


def scan_with(rig: EngineRig, data: bytes) -> int:
    with rig.database.session() as session:
        return int(
            session.scalars(
                select(BatchScan.scan_id).where(BatchScan.content_sha256 == digest(data))
            ).one()
        )


def test_three_scanners_quality_rescans_restarts_and_continued_intake(rig):
    sheets = readable_sheets(340)
    excluded = set(DAMAGED) | set(ERASED)
    clean_first = [(f"w1-{i:04d}.png", data) for i, data in enumerate(sheets[:200])
                   if i not in excluded]
    damage = (
        [(f"blank{i}.png", blank_page(i)) for i in range(3)]
        + [(f"moved{k}.png", displaced_id(k)) for k in DAMAGED]
        + [(f"erased{k}.png", erased_id(k)) for k in ERASED]
    )
    for name in SOURCES:
        rig.source(name)

    # --- wave 1: 200 sheets over three scanners, several finite units -------
    write_round_robin(rig, clean_first + damage)
    run(rig)
    with rig.database.session() as session:
        units = session.scalars(select(BatchScan.batch_id).distinct()).all()
        assert session.scalar(select(ScanRejection.scan_id).where(
            ScanRejection.state == LifecycleState.REJECTED_PENDING_RESCAN.value)) is None
    assert len(units) >= 5
    suggested = {
        item.scan_id: item.suggested_reason
        for item in quality_decisions.outstanding_suggestions(
            rig.database, rig.session_id, limit=1000
        )
    }
    moved = {k: scan_with(rig, displaced_id(k)) for k in DAMAGED}
    blanks = [scan_with(rig, blank_page(i)) for i in range(3)]
    erased = [scan_with(rig, erased_id(k)) for k in ERASED]
    assert {suggested[scan] for scan in moved.values()} == {RejectionReason.FOLDED}
    assert {suggested[scan] for scan in blanks} == {RejectionReason.REGISTRATION}
    assert {suggested[scan] for scan in erased} == {RejectionReason.ID_UNREADABLE}
    # Every suggestion rests on stored evidence the pinned policy maps to a rescan.
    for scan, decision in quality_decisions.decisions_by_scan(rig.database, suggested).items():
        assert decision.decision is QualityDecision.RESCAN_REQUIRED, scan
    effective_before = len(
        session_population.session_population(rig.database, rig.session_id).effective
    )

    # --- operator: confirm the four displaced sheets, dismiss one blank -----
    for k, scan in moved.items():
        quality_decisions.confirm_suggestion(
            rig.database, scan, reviewer="Operator", declared_candidate_id=roll_of(sheets[k])
        )
    quality_decisions.dismiss_suggestion(rig.database, blanks[0], reviewer="Operator")
    population = session_population.session_population(rig.database, rig.session_id)
    assert len(population.effective) == effective_before - 4

    # --- wave 2: 100 more sheets and three rescans at other scanners; a kill -
    rig.clock.advance((datetime.now(UTC) - rig.clock()).total_seconds() + 60)
    rescans = [(f"rescan{k}.png", rescan_of(k)) for k in DAMAGED[:3]]
    wave2 = [(f"w2-{i:04d}.png", data) for i, data in enumerate(sheets[200:300], start=200)]
    write_round_robin(rig, wave2 + rescans, offset=1)
    rig.engine._hooks = KillAt("committed", 30)  # type: ignore[union-attr]
    rig.make_ready()
    with pytest.raises(Killed):
        rig.run()
    restart(rig)
    run(rig)

    candidates = scan_lifecycle.session_possible_rescans(rig.database, rig.session_id)
    replacements = {k: scan_with(rig, rescan_of(k)) for k in DAMAGED[:3]}
    for k, replacement in replacements.items():
        [candidate] = candidates[moved[k]]
        assert candidate.scan_id == replacement and candidate.arrived_after_rejection
        assert candidate.source_label.startswith("Scanner ")
    assert candidates[moved[DAMAGED[3]]] == ()  # its rescan never arrived
    # Suggestion is not confirmation: nothing is linked yet.
    with rig.database.session() as session:
        assert session.scalar(select(ScanRejection.scan_id).where(
            ScanRejection.replacement_scan_id.is_not(None))) is None

    # --- confirm the three replacements, undo one ---------------------------
    for k, replacement in replacements.items():
        scan_lifecycle.confirm_replacement(
            rig.database, moved[k], replacement, reviewer="Operator"
        )
    undone = DAMAGED[2]
    scan_lifecycle.remove_replacement(rig.database, moved[undone], reviewer="Operator")

    def check_lineage() -> None:
        population = session_population.session_population(rig.database, rig.session_id)
        for k in DAMAGED[:2]:
            assert replacements[k] in population.effective
            assert moved[k] not in population.effective
            assert scan_lifecycle.state_of(rig.database, moved[k]) is (
                LifecycleState.SUPERSEDED_BY_REPLACEMENT
            )
            assert [item.action for item in scan_lifecycle.lifecycle_history(
                rig.database, moved[k])] == [LifecycleAction.REJECTED, LifecycleAction.REPLACED]
        assert scan_lifecycle.state_of(rig.database, moved[undone]) is (
            LifecycleState.REJECTED_PENDING_RESCAN
        )
        assert [item.action for item in scan_lifecycle.lifecycle_history(
            rig.database, moved[undone])] == [
            LifecycleAction.REJECTED, LifecycleAction.REPLACED,
            LifecycleAction.REPLACEMENT_REMOVED,
        ]
        assert replacements[undone] in population.effective  # an ordinary active scan again

    check_lineage()
    for _ in range(2):  # lineage and counts survive restarts
        restart(rig)
        check_lineage()

    # --- wave 3: intake continues after all of it ----------------------------
    wave3 = [(f"w3-{i:04d}.png", data) for i, data in enumerate(sheets[300:340], start=300)]
    write_round_robin(rig, wave3, offset=2)
    run(rig)
    check_lineage()

    # --- the final state, checked independently -----------------------------
    written = [data for _name, data in clean_first + damage + wave2 + rescans + wave3]
    unique = {digest(data) for data in written}
    population = session_population.session_population(rig.database, rig.session_id)
    # Each distinct image is one script, minus the four rejected originals
    # (two replaced, one awaiting a rescan after its undo, one never rescanned).
    assert len(population.effective) == len(unique) - 4
    with rig.database.session() as session:
        assert all(session.get(BatchScan, scan) is not None for scan in moved.values())
        rejected_by = session.execute(
            select(AuditEvent.reviewer).where(
                AuditEvent.action == LifecycleAction.REJECTED.value
            )
        ).all()
        duplicates = session.execute(
            select(ReviewConflict.scan_id, ReviewConflict.batch_id).where(
                ReviewConflict.conflict_type == ConflictType.IDENTIFIER_DUPLICATE.value
            )
        ).all()
    assert [row[0] for row in rejected_by] == ["Operator"] * 4, "no automatic rejection"
    assert len({batch for _scan, batch in duplicates}) >= 2, (
        "duplicate Student IDs must be found across finite units"
    )
    snapshot = session_snapshot.take_snapshot(rig.database, rig.session_id, now=rig.clock())
    assert snapshot.partitions
    assert snapshot.partition.queued == snapshot.partition.processing == 0
    assert (snapshot.rescans.done, snapshot.rescans.total) == (
        2, 2 + 2 + snapshot.outstanding_suggestions
    )
    assert snapshot.recognition.fraction == 1.0
    print(
        f"\nPhase 7 scenario: {len(written)} files written, {len(unique)} distinct images, "
        f"{len(population.effective)} effective scripts, "
        f"{len(set(population.batch_ids))} units, "
        f"partition {snapshot.partition.as_dict()}"
    )

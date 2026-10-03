"""Finish scan session, close and reopen (0.1.1 revised phase 7, ACCEPTANCE_CRITERIA §3).

*Finish* runs a final reconciliation of every enabled source, then returns
**every** blocker as a typed code - never only the first - or closes the
session in one audited transaction. Files arriving afterwards are held, never
added. *Reopen* is named and audited, stales final outputs, and re-closing runs
every check again.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.engine_rig import EngineRig, readable_sheets, sheet_bytes
from tests.quality_rig import blank_page, displaced_id, rescan_of, roll_of

from omr_scanner.database.models import AuditEvent, ScanBatch
from omr_scanner.domain.intake import IntakeState
from omr_scanner.domain.processing import EngineLimits, UnitPolicy
from omr_scanner.domain.review import ReasonCode
from omr_scanner.domain.scan_lifecycle import RejectionReason
from omr_scanner.domain.scan_sessions import SESSION_ENTITY, ScanSessionState
from omr_scanner.domain.session_finish import BlockerCode, IncompleteAcceptance
from omr_scanner.domain.session_snapshot import SessionActivity
from omr_scanner.services import intake as intake_service
from omr_scanner.services import (
    intake_decisions,
    open_project,
    quality_decisions,
    report_store,
    review_store,
    scan_lifecycle,
    scan_sessions,
    session_finish,
    session_snapshot,
)
from omr_scanner.services.review_store import ReviewError

SHEETS = readable_sheets(48)
BIG = UnitPolicy(max_unit_size=50, trickle_seconds=0)


@pytest.fixture
def rig(project_session):
    return EngineRig(project_session)


def finish(rig: EngineRig, **options: object):  # type: ignore[no-untyped-def]
    assert rig.engine is not None
    return rig.engine.finish_session(closed_by="Operator", **options)  # type: ignore[arg-type]


def settle(rig: EngineRig) -> None:
    if rig.engine is None:
        rig.new_engine(unit_policy=BIG)
    rig.make_ready()
    rig.run()


def resolve_all_conflicts(rig: EngineRig) -> None:
    from omr_scanner.database.models import ReviewConflict

    with rig.database.session() as session:
        ids = session.scalars(
            select(ReviewConflict.conflict_id).where(ReviewConflict.state == "open")
        ).all()
    for conflict in ids:
        review_store.accept_machine_value(
            rig.database, conflict, reviewer="Operator", reason=ReasonCode.MACHINE_CONFIRMED
        )


def test_every_blocker_is_returned_together(rig):
    a, b, c = rig.source("a"), rig.source("b"), rig.source("c")
    rig.write("a", [("17.png", SHEETS[17]), ("18.png", SHEETS[18]), ("blank.png", blank_page()),
                    ("moved7.png", displaced_id(7)), ("moved8.png", displaced_id(8)),
                    ("broken.png", sheet_bytes(36)[30]), ("defer.png", SHEETS[2])])
    settle(rig)
    for _ in range(4):  # the malformed file exhausts its decode attempts: unreadable
        rig.clock.advance(60)
        rig.engine.poll_intake(force=True)  # type: ignore[union-attr]
    ids = {
        item.suggested_reason: item.scan_id
        for item in quality_decisions.outstanding_suggestions(rig.database, rig.session_id)
    }
    moved = [
        item.scan_id
        for item in quality_decisions.outstanding_suggestions(rig.database, rig.session_id)
        if item.suggested_reason is RejectionReason.FOLDED
    ]
    assert len(moved) == 2 and RejectionReason.REGISTRATION in ids
    # Two rejected sheets: one rescan still to come, one already here (unmatched).
    quality_decisions.confirm_suggestion(rig.database, moved[0], reviewer="Operator",
                                         declared_candidate_id=roll_of(SHEETS[7]))
    quality_decisions.confirm_suggestion(rig.database, moved[1], reviewer="Operator",
                                         declared_candidate_id=roll_of(SHEETS[8]))
    rig.write("c", [("rescan8.png", rescan_of(8))])
    settle(rig)
    from omr_scanner.services import session_population

    population = session_population.session_population(rig.database, rig.session_id)
    deferred = next(
        scan for scan in population.effective
        if scan not in quality_decisions.decisions_by_scan(rig.database, [scan])
        or quality_decisions.decision_of(rig.database, scan).decision.value == "accept"  # type: ignore[union-attr]
    )
    scan_lifecycle.defer_scan(rig.database, deferred, reviewer="Operator")
    # Work still moving: a ready file, a unit claimed and in flight, a file still settling.
    rig.engine.shutdown()  # type: ignore[union-attr]
    rig.new_engine(unit_policy=BIG, limits=EngineLimits(max_in_flight=1, claim_window=1))
    rig.write("a", [("queued1.png", SHEETS[20]), ("queued2.png", SHEETS[21])])
    rig.make_ready()
    assert rig.engine.form_units()  # type: ignore[union-attr]
    rig.engine.step()  # type: ignore[union-attr]  # one claimed, one queued
    rig.write("b", [("ready.png", SHEETS[22])])
    rig.engine.poll_intake(force=True)  # type: ignore[union-attr]
    rig.clock.advance(6)
    rig.engine.poll_intake(force=True)  # type: ignore[union-attr]
    rig.write("b", [("settling.png", SHEETS[23])])
    # Scanner C goes offline; a fourth, disabled scanner is listed by name.
    rig.fs.unreachable.add(rig.root("c"))
    d = rig.source("d")
    intake_service.set_source_enabled(rig.database, d, False, actor="Operator")

    outcome = finish(rig)
    assert not outcome.closed
    codes = {item.code: item for item in outcome.blockers}
    assert set(codes) == {
        BlockerCode.FILES_STABILIZING, BlockerCode.FILES_READY, BlockerCode.SHEETS_QUEUED,
        BlockerCode.SHEETS_PROCESSING, BlockerCode.UNITS_RUNNING,
        BlockerCode.UNRESOLVED_CONFLICTS, BlockerCode.RESCAN_OUTSTANDING,
        BlockerCode.REPLACEMENT_UNMATCHED, BlockerCode.RESCAN_SUGGESTED,
        BlockerCode.FILES_AWAITING_DECISION, BlockerCode.SHEETS_DEFERRED,
        BlockerCode.SOURCE_UNREACHABLE,
    }
    assert codes[BlockerCode.RESCAN_OUTSTANDING].count == 1
    assert codes[BlockerCode.REPLACEMENT_UNMATCHED].count == 1
    assert codes[BlockerCode.RESCAN_SUGGESTED].count == 1
    assert codes[BlockerCode.FILES_AWAITING_DECISION].count == 1
    assert codes[BlockerCode.SOURCE_UNREACHABLE].source_id == c
    assert outcome.disabled_sources == ("Scanner d",)
    assert {item.source_id for item in outcome.sources} == {a, b, c}
    assert scan_sessions.get_scan_session(rig.database, rig.session_id).state is (  # type: ignore[union-attr]
        ScanSessionState.OPEN
    )
    # An incomplete-results acceptance does not reach non-acknowledgeable blockers.
    again = finish(rig, acknowledge=IncompleteAcceptance("Operator", "deadline"))
    assert not again.closed


@pytest.mark.parametrize(
    "case",
    ["stabilizing", "ready", "suggested", "held", "unreachable", "conflict", "deferred"],
)
def test_each_blocker_alone_blocks_and_is_the_only_one(rig, case):
    rig.source("a")
    rig.write("a", [("0.png", SHEETS[0])])
    settle(rig)
    resolve_all_conflicts(rig)
    expected: BlockerCode
    if case == "stabilizing":
        rig.write("a", [("late.png", SHEETS[1])])
        expected = BlockerCode.FILES_STABILIZING
    elif case == "ready":
        rig.write("a", [("late.png", SHEETS[1])])
        session_controls_pause(rig)
        rig.make_ready()
        expected = BlockerCode.FILES_READY
    elif case == "suggested":
        rig.write("a", [("blank.png", blank_page())])
        settle(rig)
        resolve_all_conflicts_except_evidence(rig)
        # The suggestion's evidence conflict is the same question, still open:
        # both are reported until an operator answers it (either way).
        outcome = finish(rig)
        assert outcome.codes == (BlockerCode.UNRESOLVED_CONFLICTS, BlockerCode.RESCAN_SUGGESTED)
        return
    elif case == "held":
        rig.write("a", [("broken.png", sheet_bytes(36)[30])])
        for _ in range(5):
            rig.clock.advance(60)
            rig.engine.poll_intake(force=True)  # type: ignore[union-attr]
        expected = BlockerCode.FILES_AWAITING_DECISION
    elif case == "unreachable":
        rig.fs.unreachable.add(rig.root("a"))
        expected = BlockerCode.SOURCE_UNREACHABLE
    elif case == "conflict":
        rig.write("a", [("17.png", SHEETS[17]), ("18.png", SHEETS[18])])
        settle(rig)
        expected = BlockerCode.UNRESOLVED_CONFLICTS
    else:
        from omr_scanner.services import session_population

        scan = next(iter(session_population.session_population(
            rig.database, rig.session_id).effective))
        scan_lifecycle.defer_scan(rig.database, scan, reviewer="Operator")
        expected = BlockerCode.SHEETS_DEFERRED
    outcome = finish(rig)
    assert not outcome.closed
    assert outcome.codes == (expected,)


def session_controls_pause(rig: EngineRig) -> None:
    from omr_scanner.services import session_controls

    session_controls.pause_processing(rig.database, rig.session_id, actor="Operator")


def resolve_all_conflicts_except_evidence(rig: EngineRig) -> None:
    from omr_scanner.database.models import ReviewConflict

    with rig.database.session() as session:
        ids = session.scalars(
            select(ReviewConflict.conflict_id)
            .where(ReviewConflict.state == "open")
            .where(ReviewConflict.conflict_type.not_in(quality_decisions.EVIDENCE_CONFLICTS))
        ).all()
    for conflict in ids:
        review_store.accept_machine_value(
            rig.database, conflict, reviewer="Operator", reason=ReasonCode.MACHINE_CONFIRMED
        )


class TestCloseAndReopen:
    def test_a_clean_session_closes_audited_sealed_and_later_files_are_held(self, rig):
        rig.source("a")
        rig.write("a", [(f"{i}.png", SHEETS[i]) for i in range(4)])
        settle(rig)
        resolve_all_conflicts(rig)
        with pytest.raises(ReviewError):
            rig.engine.finish_session(closed_by=" ")  # type: ignore[union-attr]
        outcome = finish(rig, reason="exam over")
        assert outcome.closed and outcome.blockers == ()
        info = scan_sessions.get_scan_session(rig.database, rig.session_id)
        assert info is not None and info.state is ScanSessionState.CLOSED
        assert info.closed_by == "Operator"
        with rig.database.session() as session:
            assert all(
                item is not None
                for item in session.scalars(
                    select(ScanBatch.sealed_at).where(ScanBatch.scan_session_id == rig.session_id)
                ).all()
            )
            closed = session.scalars(
                select(AuditEvent)
                .where(AuditEvent.entity_type == SESSION_ENTITY)
                .where(AuditEvent.action == "session_closed")
            ).all()
        assert [(item.reviewer, item.reason_text) for item in closed] == [("Operator", "exam over")]
        # A file arriving now is held for an operator - never added.
        rig.write("a", [("late.png", SHEETS[30])])
        rig.make_ready()
        rig.run()
        snapshot = session_snapshot.take_snapshot(rig.database, rig.session_id, now=rig.clock())
        assert snapshot.activity is SessionActivity.CLOSED and not snapshot.caught_up
        assert snapshot.partition.held == 1 and snapshot.partitions
        pending = intake_decisions.pending_decisions(rig.database, rig.session_id)
        assert [(item.file_name, item.state, item.source_label) for item in pending] == [
            ("late.png", IntakeState.HELD, "Scanner a")
        ]
        assert pending[0].arrived_at is not None and "closed" in pending[0].why
        # Finishing a closed session is refused, by code.
        assert finish(rig).codes == (BlockerCode.SESSION_NOT_OPEN,)

        # Reopen (named, audited); release the held file; it is processed.
        with pytest.raises(ReviewError):
            session_finish.reopen_session(rig.database, rig.session_id, reopened_by="")
        reopened = session_finish.reopen_session(
            rig.database, rig.session_id, reopened_by="Supervisor", reason="late script"
        )
        assert reopened.state is ScanSessionState.OPEN
        assert reopened.final_outputs_stale_since is not None
        intake_decisions.decide_file(
            rig.database, pending[0].intake_file_id, intake_decisions.FileDecision.RELEASE,
            reviewer="Supervisor",
        )
        rig.make_ready()
        rig.run()
        after = session_snapshot.take_snapshot(rig.database, rig.session_id, now=rig.clock())
        assert after.partition.held == 0 and after.recognition.done == 5
        # Re-closing runs every check again.
        resolve_all_conflicts(rig)
        assert finish(rig).closed

    def test_incomplete_results_only_by_an_audited_named_acceptance(self, rig):
        rig.source("a")
        rig.write("a", [("0.png", SHEETS[0]), ("moved.png", displaced_id(12))])
        settle(rig)
        resolve_all_conflicts_except_evidence(rig)
        moved = quality_decisions.outstanding_suggestions(rig.database, rig.session_id)[0]
        quality_decisions.confirm_suggestion(rig.database, moved.scan_id, reviewer="Operator")
        resolve_all_conflicts(rig)
        refused = finish(rig)
        assert refused.codes == (BlockerCode.RESCAN_OUTSTANDING,)
        assert refused.blockers[0].acknowledgeable
        with pytest.raises(ReviewError):
            finish(rig, acknowledge=IncompleteAcceptance(" ", "no name"))
        closed = finish(rig, acknowledge=IncompleteAcceptance("Supervisor", "candidate absent"))
        assert closed.closed
        assert [item.code for item in closed.accepted] == [BlockerCode.RESCAN_OUTSTANDING]
        with rig.database.session() as session:
            event = session.scalars(
                select(AuditEvent).where(AuditEvent.action == "session_closed")
            ).one()
        assert event.reviewer == "Supervisor"
        assert "incomplete results accepted" in event.detail
        assert "candidate absent" in event.reason_text


def test_reopen_stales_a_final_export_and_reclosing_does_not_revive_it(tmp_path):
    """On the schema-15 fixture closed and exported by the previous build."""
    import json

    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "schema15"
    provenance = json.loads((fixtures / "PROVENANCE.json").read_text(encoding="utf-8"))
    root = tmp_path / "finite"
    shutil.copytree(fixtures / "finite", root)
    with open_project(root) as session:
        database = session.database
        batch = provenance["finite_batch"]
        scan_session_id = provenance["finite_session"]
        assert report_store.final_export_status(database, batch, "A").state == "current"
        session_finish.reopen_session(
            database, scan_session_id, reopened_by="Supervisor", reason="re-mark"
        )
        assert report_store.final_export_status(database, batch, "A").state == "stale"
        outcome = session_finish.finish_scan_session(
            database, scan_session_id, closed_by="Supervisor"
        )
        assert outcome.closed, outcome.blockers
        assert report_store.final_export_status(database, batch, "A").state == "stale"
        with database.session() as db:
            actions = [
                row.action
                for row in db.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.entity_type == SESSION_ENTITY)
                    .where(AuditEvent.entity_id == scan_session_id)
                    .order_by(AuditEvent.event_id)
                ).all()
            ]
        assert actions[-2:] == ["session_reopened", "session_closed"]

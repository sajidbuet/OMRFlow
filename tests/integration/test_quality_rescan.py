"""Quality decisions feed Reject & Rescan as suggestions (0.1.1 revised phase 7).

The continuous engine reads real sheets (``tests/quality_rig.py``: a blank page,
a displaced and an erased identifier block, clean sheets) through the real
work unit; each read sheet's decision commits with its result. A
``RESCAN_REQUIRED`` decision is only a suggestion: nothing is rejected until a
named operator confirms it through ``reject_scan``; dismissal and Resolve's own
decision on the evidence conflict are answers too. Rescans are suggested from
any source of the session and confirmed by the operator; lineage, undo and the
effective count survive restarts.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from tests.engine_rig import EngineRig, readable_sheets
from tests.quality_rig import blank_page, displaced_id, erased_id, rescan_of, roll_of

from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    ReviewConflict,
    ScanQualityDecision,
    ScanRejection,
)
from omr_scanner.domain.quality_decision import (
    DEFAULT_POLICY,
    QualityDecision,
    QualityReason,
)
from omr_scanner.domain.review import ConflictType, ReasonCode
from omr_scanner.domain.scan_lifecycle import LifecycleAction, LifecycleState, RejectionReason
from omr_scanner.services import (
    quality_decisions,
    review_store,
    scan_lifecycle,
    session_population,
    session_snapshot,
)
from omr_scanner.services.review_store import ReviewError


@pytest.fixture
def rig(project_session):
    return EngineRig(project_session)


def run_all(rig: EngineRig) -> None:
    if rig.engine is None:
        rig.new_engine()
    rig.make_ready()
    rig.run()


def scan_of(rig: EngineRig, name: str) -> int:
    with rig.database.session() as session:
        found = session.scalars(
            select(BatchScan.scan_id).where(BatchScan.source_path.like(f"%{name}%"))
        ).all()
    if not found:
        # Watched files are stored under their content-addressed copy name.
        raise AssertionError(f"no scan for {name}")
    return int(found[0])


def scan_with(rig: EngineRig, data: bytes) -> int:
    from tests.engine_rig import digest

    with rig.database.session() as session:
        return int(
            session.scalars(
                select(BatchScan.scan_id).where(BatchScan.content_sha256 == digest(data))
            ).one()
        )


def damaged_session(rig: EngineRig) -> dict[str, int]:
    """Clean sheets 0-3 plus each damage case, over two sources; read to the end."""
    sheets = readable_sheets(48)
    rig.source("a")
    rig.source("b")
    rig.write("a", [("c0.png", sheets[0]), ("c1.png", sheets[1]), ("blank.png", blank_page())])
    rig.write("b", [("c2.png", sheets[2]), ("moved.png", displaced_id(5)),
                    ("erased.png", erased_id(6))])
    run_all(rig)
    return {
        "clean": scan_with(rig, sheets[0]),
        "blank": scan_with(rig, blank_page()),
        "moved": scan_with(rig, displaced_id(5)),
        "erased": scan_with(rig, erased_id(6)),
    }


class TestDecisionsInTheWorkUnit:
    def test_every_read_sheet_has_its_decision_and_nothing_is_rejected(self, rig):
        ids = damaged_session(rig)
        decisions = quality_decisions.decisions_by_scan(rig.database, ids.values())
        assert decisions[ids["clean"]].decision is QualityDecision.ACCEPT
        blank = decisions[ids["blank"]]
        assert blank.decision is QualityDecision.RESCAN_REQUIRED
        assert blank.reasons == (QualityReason.REGISTRATION_FAILED,)
        assert blank.suggested_reason is RejectionReason.REGISTRATION
        moved = decisions[ids["moved"]]
        assert moved.decision is QualityDecision.RESCAN_REQUIRED
        assert moved.reasons == (QualityReason.QUALITY_UNUSABLE,)
        assert moved.suggested_reason is RejectionReason.FOLDED
        assert decisions[ids["erased"]].suggested_reason is RejectionReason.ID_UNREADABLE
        assert {item.policy_fingerprint for item in decisions.values()} == {
            DEFAULT_POLICY.fingerprint
        }
        with rig.database.session() as session:
            read = session.scalar(
                select(func.count()).select_from(BatchScan)
                .where(BatchScan.status.in_(["completed", "warning", "failed"]))
            )
            stored = session.scalar(select(func.count()).select_from(ScanQualityDecision))
            rejections = session.scalar(select(func.count()).select_from(ScanRejection))
        assert stored == read == 6
        assert rejections == 0, "a quality decision must never reject anything"
        # A suggestion never changes the population: all six still count.
        population = session_population.session_population(rig.database, rig.session_id)
        assert len(population.effective) == 6
        assert quality_decisions.count_outstanding(rig.database, rig.session_id) == 3
        listed = quality_decisions.outstanding_suggestions(rig.database, rig.session_id)
        assert {item.scan_id for item in listed} == {ids["blank"], ids["moved"], ids["erased"]}
        assert all(item.source_id for item in listed)

    def test_the_session_pins_its_policy_against_a_later_default(self, rig, monkeypatch):
        sheets = readable_sheets(48)
        rig.source("a")
        rig.write("a", [("c0.png", sheets[0])])
        run_all(rig)
        pinned = quality_decisions.pinned_policy(rig.database, rig.session_id)
        assert pinned == DEFAULT_POLICY
        # The application's default changes after scanning began ...
        rules = dict(DEFAULT_POLICY.rules)
        rules[QualityReason.ALIGNMENT_WARNING] = QualityDecision.RESCAN_REQUIRED
        stricter = dataclasses.replace(DEFAULT_POLICY, version=99, rules=rules)
        monkeypatch.setattr(quality_decisions, "DEFAULT_POLICY", stricter)
        rig.write("a", [("blank.png", blank_page())])
        run_all(rig)
        # ... and this session keeps interpreting with the policy it pinned.
        fingerprints = {
            item.policy_fingerprint
            for item in quality_decisions.decisions_by_scan(
                rig.database, range(1, 100)
            ).values()
        }
        assert fingerprints == {DEFAULT_POLICY.fingerprint}
        assert quality_decisions.pinned_policy(rig.database, rig.session_id) == DEFAULT_POLICY

    def test_decisions_missing_after_a_recognition_only_write_are_backfilled_on_start(self, rig):
        ids = damaged_session(rig)
        rig.engine.shutdown()  # type: ignore[union-attr]
        with rig.database.session() as session:
            session.query(ScanQualityDecision).delete()
        rig.new_engine()  # the restart sequence derives them from stored results
        decisions = quality_decisions.decisions_by_scan(rig.database, ids.values())
        assert decisions[ids["blank"]].decision is QualityDecision.RESCAN_REQUIRED
        assert decisions[ids["clean"]].decision is QualityDecision.ACCEPT


class TestOperatorAnswers:
    def test_dismissal_keeps_the_sheet_is_audited_and_is_final(self, rig):
        ids = damaged_session(rig)
        with pytest.raises(ReviewError):
            quality_decisions.dismiss_suggestion(rig.database, ids["moved"], reviewer=" ")
        quality_decisions.dismiss_suggestion(
            rig.database, ids["moved"], reviewer="Operator", note="looks fine on paper"
        )
        assert quality_decisions.count_outstanding(rig.database, rig.session_id) == 2
        with pytest.raises(quality_decisions.QualityError):
            quality_decisions.dismiss_suggestion(rig.database, ids["moved"], reviewer="Operator")
        assert scan_lifecycle.state_of(rig.database, ids["moved"]) is LifecycleState.ACTIVE
        with rig.database.session() as session:
            events = session.scalars(
                select(AuditEvent).where(AuditEvent.entity_type == quality_decisions.QUALITY_ENTITY)
            ).all()
        assert [(item.action, item.reviewer, item.entity_id) for item in events] == [
            ("quality_dismissed", "Operator", str(ids["moved"]))
        ]
        # A restart re-derives nothing: the answer stands.
        rig.engine.shutdown()  # type: ignore[union-attr]
        rig.new_engine()
        assert quality_decisions.count_outstanding(rig.database, rig.session_id) == 2

    def test_confirmation_is_the_existing_rejection(self, rig):
        ids = damaged_session(rig)
        before = len(session_population.session_population(rig.database, rig.session_id).effective)
        case = quality_decisions.confirm_suggestion(
            rig.database, ids["blank"], reviewer="Operator"
        )
        assert case.state is LifecycleState.REJECTED_PENDING_RESCAN
        assert case.reason is RejectionReason.REGISTRATION
        assert "Confirmed suggested rescan" in case.note
        history = scan_lifecycle.lifecycle_history(rig.database, ids["blank"])
        assert [item.action for item in history] == [LifecycleAction.REJECTED]
        assert history[0].reviewer == "Operator"
        after = session_population.session_population(rig.database, rig.session_id)
        assert len(after.effective) == before - 1
        assert quality_decisions.count_outstanding(rig.database, rig.session_id) == 2
        with pytest.raises(quality_decisions.QualityError):
            quality_decisions.confirm_suggestion(rig.database, ids["blank"], reviewer="Operator")
        # Undoing the rejection makes the suggestion outstanding again: the
        # evidence still says rescan, and nobody has answered it.
        scan_lifecycle.undo_reject(rig.database, ids["blank"], reviewer="Operator")
        assert quality_decisions.count_outstanding(rig.database, rig.session_id) == 3

    def test_an_operator_choice_of_reason_wins(self, rig):
        ids = damaged_session(rig)
        case = quality_decisions.confirm_suggestion(
            rig.database, ids["moved"], reviewer="Operator", reason=RejectionReason.SKEW,
        )
        assert case.reason is RejectionReason.SKEW

    def test_resolving_the_evidence_conflict_in_resolve_answers_the_suggestion(self, rig):
        ids = damaged_session(rig)
        with rig.database.session() as session:
            conflict = session.scalars(
                select(ReviewConflict.conflict_id)
                .where(ReviewConflict.scan_id == ids["blank"])
                .where(ReviewConflict.conflict_type == ConflictType.REGISTRATION_FAILED.value)
            ).one()
        review_store.accept_machine_value(
            rig.database, conflict, reviewer="Operator", reason=ReasonCode.MACHINE_CONFIRMED,
        )
        outstanding = {
            item.scan_id
            for item in quality_decisions.outstanding_suggestions(rig.database, rig.session_id)
        }
        assert ids["blank"] not in outstanding
        assert scan_lifecycle.state_of(rig.database, ids["blank"]) is LifecycleState.ACTIVE
        # Reopening that conflict makes the suggestion outstanding again.
        review_store.reopen(rig.database, conflict, reviewer="Operator")
        assert ids["blank"] in {
            item.scan_id
            for item in quality_decisions.outstanding_suggestions(rig.database, rig.session_id)
        }


class TestRescanFromAnotherSource:
    def test_cross_source_rescan_is_suggested_confirmed_undone_and_survives_restarts(self, rig):
        sheets = readable_sheets(48)
        rig.source("a")
        rig.source("b")
        rig.write("a", [("c0.png", sheets[0]), ("moved.png", displaced_id(7))])
        rig.write("b", [("c1.png", sheets[1])])
        run_all(rig)
        original = scan_with(rig, displaced_id(7))
        roll = roll_of(sheets[7])
        quality_decisions.confirm_suggestion(
            rig.database, original, reviewer="Operator", declared_candidate_id=roll,
        )
        # Nothing yet: the rescan has not arrived.
        assert scan_lifecycle.session_possible_rescans(rig.database, rig.session_id) == {
            original: ()
        }
        # Scanner C, later, scans the same paper again. (The rig's intake runs
        # on a fake clock; the rejection was stamped with real time.)
        rig.clock.advance((datetime.now(UTC) - rig.clock()).total_seconds() + 60)
        rig.source("c")
        rig.write("c", [("again.png", rescan_of(7))])
        run_all(rig)
        replacement = scan_with(rig, rescan_of(7))
        suggested = scan_lifecycle.session_possible_rescans(rig.database, rig.session_id)
        [candidate] = suggested[original]
        assert candidate.scan_id == replacement
        assert candidate.source_label == "Scanner c"
        assert candidate.arrived_after_rejection is True
        assert candidate.candidate_id == roll
        # Suggestion is not confirmation: still outstanding until confirmed.
        assert scan_lifecycle.state_of(rig.database, original) is (
            LifecycleState.REJECTED_PENDING_RESCAN
        )
        counted_before = len(
            session_population.session_population(rig.database, rig.session_id).effective
        )
        scan_lifecycle.confirm_replacement(
            rig.database, original, replacement, reviewer="Operator"
        )
        population = session_population.session_population(rig.database, rig.session_id)
        assert replacement in population.effective and original not in population.effective
        assert len(population.effective) == counted_before  # the rescan counts once
        with rig.database.session() as session:
            assert session.get(BatchScan, original) is not None  # original kept
        snapshot = session_snapshot.take_snapshot(rig.database, rig.session_id)
        assert snapshot.partitions
        assert (snapshot.rescans.done, snapshot.rescans.total) == (1, 1)

        # Restart twice: lineage and counts are durable.
        for _ in range(2):
            rig.engine.shutdown()  # type: ignore[union-attr]
            rig.new_engine()
            again = session_population.session_population(rig.database, rig.session_id)
            assert again.effective == population.effective
            assert scan_lifecycle.state_of(rig.database, original) is (
                LifecycleState.SUPERSEDED_BY_REPLACEMENT
            )

        # Undo the replacement: the previous lifecycle is restored.
        scan_lifecycle.remove_replacement(rig.database, original, reviewer="Operator")
        assert scan_lifecycle.state_of(rig.database, original) is (
            LifecycleState.REJECTED_PENDING_RESCAN
        )
        assert scan_lifecycle.state_of(rig.database, replacement) is LifecycleState.ACTIVE
        actions = [item.action for item in scan_lifecycle.lifecycle_history(rig.database, original)]
        assert actions == [
            LifecycleAction.REJECTED, LifecycleAction.REPLACED, LifecycleAction.REPLACEMENT_REMOVED
        ]
        # ... and it can be confirmed again; continued intake changes nothing.
        scan_lifecycle.confirm_replacement(
            rig.database, original, replacement, reviewer="Operator"
        )
        rig.write("a", [("late.png", sheets[9])])
        run_all(rig)
        final = session_population.session_population(rig.database, rig.session_id)
        assert replacement in final.effective and original not in final.effective
        assert len(final.effective) == counted_before + 1

    def test_reimported_rejected_bytes_are_never_suggested_as_the_rescan(self, rig):
        sheets = readable_sheets(48)
        rig.source("a")
        rig.source("b")
        rig.write("a", [("c0.png", sheets[0]), ("moved.png", displaced_id(8))])
        run_all(rig)
        original = scan_with(rig, displaced_id(8))
        quality_decisions.confirm_suggestion(
            rig.database, original, reviewer="Operator",
            declared_candidate_id=roll_of(sheets[8]),
        )
        rig.write("b", [("same-file.png", displaced_id(8))])
        run_all(rig)
        assert scan_lifecycle.session_possible_rescans(rig.database, rig.session_id) == {
            original: ()
        }
        with rig.database.session() as session:
            states = session.scalars(select(ScanRejection.state)).all()
        assert LifecycleState.REJECTED_PENDING_RESCAN.value in states
        # The same bytes again are either an exact duplicate or a re-import -
        # never a second script and never a replacement.
        population = session_population.session_population(rig.database, rig.session_id)
        assert len(population.effective) == 1

"""Attendance dispositions - Keep This Script, Reject / Exclude, Defer, Restore.

Scope:
    The services behind the Attendance stage's disposition controls, end to
    end on the Reject & Rescan world (one batch holding Sets 1, 2 and 3, real
    files, content hashes, verified keys and scoring). Every state is traced
    through reconciliation -> duplicate detection -> scoring -> Results ->
    report readiness -> reopening the project, because that is where an
    exclusion that one screen forgot would show.

    The dispositions reuse the Reject & Rescan lifecycle table; nothing here
    deletes a row or a file.

Privacy:
    Every identifier and name here is fictional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from tests.integration.test_reject_and_rescan import (
    OPERATOR,
    TEMPLATE,
    World,
    build_world,
    lifecycle_events,
    reject,
)
from tests.integration.test_reject_and_rescan_followup import add_batch, duplicates

from omr_scanner.database.models import BatchScan, ScanRejection
from omr_scanner.domain.reconciliation import ReconciliationStatus, ResolutionState
from omr_scanner.domain.reporting import ReadinessIssueKind
from omr_scanner.domain.scan_lifecycle import (
    EXCLUSION_REASONS,
    RESCAN_REASONS,
    LifecycleAction,
    LifecycleState,
    RejectionReason,
)
from omr_scanner.domain.scoring import BlockReason, ResultStatus
from omr_scanner.services import (
    create_project,
    open_project,
    reconciliation_store,
    report_store,
    scan_lifecycle,
    scoring_store,
)
from omr_scanner.services.review_store import ReviewError
from omr_scanner.services.scan_lifecycle import LifecycleError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def world(workspace: Path, tmp_path: Path) -> Iterator[World]:
    session = create_project(workspace, "Attendance Dispositions")
    try:
        yield build_world(session, tmp_path)
    finally:
        if not session.is_closed:
            session.close()


def duplicate_of(
    world: World, name: str = "s2a_again.png", *, content: bytes | None = None
) -> int:
    """A second scan of candidate 200121's sheet (Set 2), read the same way.

    Reconciled at once, as the Attendance stage does after a batch.
    """
    scan_id = world.add_processed_scan(name, "200121", "2", 14, content=content)
    world.reconcile("2")
    return scan_id


def unknown_sheet(world: World) -> int:
    """A sheet of Set 2 read as a roll number nobody on the list has."""
    scan_id = world.add_processed_scan("s2_stray.png", "209999", "2", 3)
    world.reconcile("2")
    return scan_id


def state(world: World, scan_id: int) -> LifecycleState:
    return scan_lifecycle.state_of(world.database, scan_id)


def keep(world: World, kept: int, *others: int) -> None:
    scan_lifecycle.keep_script(
        world.database, kept, list(others), reviewer=OPERATOR, candidate_id="200121"
    )


def exclude(
    world: World, scan_id: int, reason: RejectionReason = RejectionReason.ACCIDENTAL_SCAN,
    note: str = "",
) -> None:
    scan_lifecycle.exclude_scan(
        world.database, scan_id, reviewer=OPERATOR, reason=reason, note=note
    )


def defer(world: World, scan_id: int) -> None:
    scan_lifecycle.defer_scan(world.database, scan_id, reviewer=OPERATOR, note="Ask invigilator")


def restore(world: World, scan_id: int) -> None:
    scan_lifecycle.restore_scan(world.database, scan_id, reviewer=OPERATOR)


def stored_entries(world: World, code: str) -> dict[str, object]:
    """The stored reconciliation, as every later stage reads it - not re-run."""
    return {
        entry.candidate_id: entry
        for entry in reconciliation_store.list_entries(
            world.database, world.rosters[code], world.batch_id
        )
    }


def open_duplicates(world: World) -> set[int]:
    return {
        scan_id
        for scan_id, found in duplicates(world, world.batch_id).items()
        if found.needs_attention
    }


# ----------------------------------------------------------------------
# Vocabulary
# ----------------------------------------------------------------------
class TestVocabulary:
    def test_only_active_is_ever_result_eligible(self):
        assert not LifecycleState.EXCLUDED.is_result_eligible
        assert not LifecycleState.DEFERRED.is_result_eligible
        assert LifecycleState.EXCLUDED.is_disposition and LifecycleState.DEFERRED.is_disposition
        # Neither is a rescan still to fetch; a deferral is a decision still owed.
        assert not LifecycleState.EXCLUDED.is_outstanding
        assert not LifecycleState.DEFERRED.is_outstanding
        assert LifecycleState.DEFERRED.awaits_decision
        assert not LifecycleState.EXCLUDED.awaits_decision

    def test_the_two_reason_lists_do_not_leak_into_each_other(self):
        assert RejectionReason.DUPLICATE not in RESCAN_REASONS
        assert RejectionReason.FOLDED not in EXCLUSION_REASONS
        assert RejectionReason.OTHER in RESCAN_REASONS
        assert RejectionReason.OTHER in EXCLUSION_REASONS

    def test_every_new_action_fits_the_ledger_column(self):
        for action in LifecycleAction:
            assert len(action.value) <= 20, action


# ----------------------------------------------------------------------
# 1-8. Duplicate scripts
# ----------------------------------------------------------------------
class TestDuplicates:
    def test_two_copies_of_one_script_are_a_duplicate(self, world):
        second = duplicate_of(world)
        entry = world.reconcile("2")["200121"]
        assert entry.status is ReconciliationStatus.DUPLICATE_SCRIPT
        assert {view.script.scan_id for view in entry.scripts} == {
            world.ids["s2a.png"], second
        }
        assert {world.ids["s2a.png"], second} <= open_duplicates(world)

    def test_keeping_the_first_rejects_the_second(self, world):
        first, second = world.ids["s2a.png"], duplicate_of(world)
        keep(world, first, second)
        assert state(world, first) is LifecycleState.ACTIVE
        assert state(world, second) is LifecycleState.EXCLUDED
        entry = stored_entries(world, "2")["200121"]
        assert entry.status is ReconciliationStatus.MATCHED
        assert [view.script.scan_id for view in entry.scripts] == [first]
        assert not open_duplicates(world), "the Resolve duplicate is withdrawn too"
        case = scan_lifecycle.get_case(world.database, second)
        assert case is not None and case.reason is RejectionReason.DUPLICATE

    def test_keeping_the_second_rejects_the_first(self, world):
        first, second = world.ids["s2a.png"], duplicate_of(world)
        keep(world, second, first)
        assert state(world, first) is LifecycleState.EXCLUDED
        assert state(world, second) is LifecycleState.ACTIVE
        entry = stored_entries(world, "2")["200121"]
        assert entry.status is ReconciliationStatus.MATCHED
        assert [view.script.scan_id for view in entry.scripts] == [second]

    def test_three_copies_keep_one_and_exclude_the_other_two(self, world):
        first = world.ids["s2a.png"]
        second = duplicate_of(world)
        third = duplicate_of(world, "s2a_third.png")
        assert world.reconcile("2")["200121"].script_count == 3
        keep(world, second, first, third)
        assert [state(world, item) for item in (first, second, third)] == [
            LifecycleState.EXCLUDED, LifecycleState.ACTIVE, LifecycleState.EXCLUDED,
        ]
        assert stored_entries(world, "2")["200121"].status is ReconciliationStatus.MATCHED
        kept_events = [event.action for event in lifecycle_events(world, "s2a_again.png")]
        assert kept_events == [LifecycleAction.KEPT_CANONICAL.value]
        for name in ("s2a.png", "s2a_third.png"):
            events = lifecycle_events(world, name)
            assert [event.action for event in events] == [LifecycleAction.EXCLUDED.value]
            assert events[0].reason_code == RejectionReason.DUPLICATE.value
            assert events[0].previous_value == "active" and events[0].new_value == "excluded"
            assert "200121" in events[0].detail and "s2a_again.png" in events[0].detail

    def test_one_refusal_leaves_every_copy_as_it_was(self, world):
        first, second = world.ids["s2a.png"], duplicate_of(world)
        third = duplicate_of(world, "s2a_third.png")
        defer(world, third)  # not active: keep() may exclude it, but not keep it
        with pytest.raises(LifecycleError):
            keep(world, third, first, second)
        assert state(world, first) is LifecycleState.ACTIVE
        assert state(world, second) is LifecycleState.ACTIVE

    def test_the_rejected_copy_is_never_scored(self, world):
        first, second = world.ids["s2a.png"], duplicate_of(world)
        blocked = world.score("2")["200121"]
        assert blocked.status is ResultStatus.BLOCKED  # two scripts, none chosen
        keep(world, second, first)
        results = world.score("2")
        assert results["200121"].status is ResultStatus.SCORED
        assert results["200121"].scan_id == second
        assert all(item.scan_id != first for item in results.values())

    def test_the_rejected_copy_is_in_no_report(self, world):
        first, second = world.ids["s2a.png"], duplicate_of(world)
        keep(world, first, second)
        world.score("2")
        inputs = report_store.gather_set_inputs(
            world.database, world.rosters["2"], world.batch_id, TEMPLATE, "2"
        )
        assert inputs.results_by_candidate["200121"].scan_id == first

    def test_the_rejected_copy_does_not_count_as_a_script(self, world):
        before = reconciliation_store.script_scope(
            world.database, world.rosters["2"], world.batch_id
        )
        first, second = world.ids["s2a.png"], duplicate_of(world)
        keep(world, first, second)
        counts = reconciliation_store.stored_counts(
            world.database, world.rosters["2"], world.batch_id
        )
        assert counts is not None
        assert counts.duplicate_script == 0
        scope = reconciliation_store.script_scope(
            world.database, world.rosters["2"], world.batch_id
        )
        assert scope.in_set == before.in_set
        assert scope.excluded == 1
        assert scope.superseded == before.superseded, "an exclusion is not a supersession"

    def test_restoring_the_rejected_copy_raises_the_duplicate_again(self, world):
        first, second = world.ids["s2a.png"], duplicate_of(world)
        keep(world, first, second)
        restore(world, second)
        assert state(world, second) is LifecycleState.ACTIVE
        entry = stored_entries(world, "2")["200121"]
        assert entry.status is ReconciliationStatus.DUPLICATE_SCRIPT
        assert {first, second} <= open_duplicates(world)
        events = [event.action for event in lifecycle_events(world, "s2a_again.png")]
        assert events == [LifecycleAction.EXCLUDED.value, LifecycleAction.RESTORED.value]

    def test_restore_then_keep_the_other_copy(self, world):
        """Kept the poorer scan by mistake: restore it, keep the better one."""
        first, second = world.ids["s2a.png"], duplicate_of(world)
        keep(world, first, second)
        restore(world, second)
        keep(world, second, first)
        assert state(world, first) is LifecycleState.EXCLUDED
        assert state(world, second) is LifecycleState.ACTIVE
        assert world.score("2")["200121"].scan_id == second

    def test_an_identical_file_imported_twice_keeps_its_kept_copy(self, world):
        """The kept copy has the excluded copy's bytes, and must stay active."""
        content = (world.scans_dir / "s2a.png").read_bytes()
        second = duplicate_of(world, content=content)
        keep(world, world.ids["s2a.png"], second)
        world.coordinator_passes()  # what runs after the next batch
        assert state(world, world.ids["s2a.png"]) is LifecycleState.ACTIVE
        assert state(world, second) is LifecycleState.EXCLUDED

    def test_nothing_is_deleted(self, world):
        second = duplicate_of(world)
        path = world.scans_dir / "s2a_again.png"
        before = path.read_bytes()
        keep(world, world.ids["s2a.png"], second)
        assert path.read_bytes() == before
        with world.database.session() as session:
            row = session.get(BatchScan, second)
            assert row is not None and row.result_json

    def test_keep_needs_a_named_reviewer_and_a_real_other_copy(self, world):
        first, second = world.ids["s2a.png"], duplicate_of(world)
        with pytest.raises(ReviewError):
            scan_lifecycle.keep_script(world.database, first, [second], reviewer=" ")
        with pytest.raises(LifecycleError):
            scan_lifecycle.keep_script(world.database, first, [], reviewer=OPERATOR)
        with pytest.raises(LifecycleError):
            scan_lifecycle.keep_script(world.database, first, [first], reviewer=OPERATOR)
        assert state(world, second) is LifecycleState.ACTIVE


# ----------------------------------------------------------------------
# 9-11. An unwanted or undecided sheet
# ----------------------------------------------------------------------
class TestUnwantedSheet:
    def test_an_unknown_sheet_rejected_leaves_no_exception(self, world):
        stray = unknown_sheet(world)
        assert world.reconcile("2")["209999"].status is ReconciliationStatus.UNKNOWN_ID
        exclude(world, stray, RejectionReason.WRONG_DOCUMENT, note="Physics paper")
        entries = stored_entries(world, "2")
        assert "209999" not in entries
        cases = scan_lifecycle.list_dispositions(world.database, world.batch_id)
        assert [case.scan_id for case in cases] == [stray]
        assert cases[0].state is LifecycleState.EXCLUDED
        assert cases[0].recognised_candidate_id == "209999"

    def test_the_rejected_sheet_is_removed_from_results(self, world):
        stray = unknown_sheet(world)
        assert world.score("2")["209999"].status is ResultStatus.BLOCKED
        exclude(world, stray)
        # Not shown even before recalculating: the result describes nobody now.
        assert "209999" not in world.results("2")
        world.score("2")
        assert "209999" not in world.results("2")

    def test_a_scored_candidate_is_never_pruned(self, world):
        stray = unknown_sheet(world)
        exclude(world, stray)
        before = world.score("2")["200121"]
        assert before.status is ResultStatus.SCORED
        assert world.score("2")["200121"].final_score == before.final_score

    def test_deferring_keeps_the_sheet_and_excludes_it_from_scoring(self, world):
        stray = unknown_sheet(world)
        defer(world, stray)
        assert state(world, stray) is LifecycleState.DEFERRED
        assert stray in scan_lifecycle.ineligible_scan_ids(world.database, world.batch_id)
        assert "209999" not in stored_entries(world, "2")
        assert "209999" not in world.score("2")
        counts = scan_lifecycle.count_cases(world.database, world.batch_id)
        assert counts.deferred == 1 and counts.outstanding == 0 and counts.total == 0
        scope = reconciliation_store.script_scope(
            world.database, world.rosters["2"], world.batch_id
        )
        assert scope.deferred == 1
        with world.database.session() as session:
            assert session.get(BatchScan, stray) is not None

    def test_a_deferred_sheet_is_reported_as_incomplete(self, world):
        stray = unknown_sheet(world)
        defer(world, stray)
        readiness = report_store.check_readiness(
            world.database, world.rosters["2"], world.batch_id, TEMPLATE, "2",
            for_final_export=True,
        )
        deferred = readiness.by_kind(ReadinessIssueKind.SHEET_DEFERRED)
        assert len(deferred) == 1 and "s2_stray.png" in deferred[0].message
        assert ReadinessIssueKind.SHEET_DEFERRED.is_acknowledgeable
        assert readiness.incomplete_count == readiness.outstanding_rescans + 1

    def test_restoring_a_deferred_sheet_returns_it_to_review(self, world):
        stray = unknown_sheet(world)
        defer(world, stray)
        restore(world, stray)
        assert state(world, stray) is LifecycleState.ACTIVE
        assert stored_entries(world, "2")["209999"].status is ReconciliationStatus.UNKNOWN_ID
        actions = [event.action for event in lifecycle_events(world, "s2_stray.png")]
        assert actions == [LifecycleAction.DEFERRED.value, LifecycleAction.RESTORED.value]

    def test_a_deferred_sheet_can_then_be_rejected(self, world):
        stray = unknown_sheet(world)
        defer(world, stray)
        exclude(world, stray, RejectionReason.BLANK_UNUSABLE)
        assert state(world, stray) is LifecycleState.EXCLUDED
        actions = [event.action for event in lifecycle_events(world, "s2_stray.png")]
        assert actions == [
            LifecycleAction.DEFERRED.value, LifecycleAction.DISPOSITION_CHANGED.value,
        ]

    def test_the_refusals(self, world):
        stray = unknown_sheet(world)
        with pytest.raises(LifecycleError):
            exclude(world, stray, RejectionReason.OTHER)  # a note is required
        with pytest.raises(ReviewError):
            scan_lifecycle.defer_scan(world.database, stray, reviewer="")
        with pytest.raises(LifecycleError):
            restore(world, stray)  # nothing to restore
        exclude(world, stray)
        with pytest.raises(LifecycleError):
            exclude(world, stray)
        with pytest.raises(LifecycleError):
            defer(world, stray)  # restore first
        assert state(world, stray) is LifecycleState.EXCLUDED


class TestDeferredRegisteredCandidate:
    def test_a_candidates_only_script_deferred_is_not_a_missing_script(self, world):
        before = reconciliation_store.stored_counts(
            world.database, world.rosters["2"], world.batch_id
        )
        assert before is not None
        defer(world, world.ids["s2a.png"])
        entry = stored_entries(world, "2")["200121"]
        assert entry.status is ReconciliationStatus.SCRIPT_DEFERRED
        assert entry.resolution is ResolutionState.DEFERRED
        assert not entry.needs_attention, "a disposition: the operator may move on"
        assert entry.script_count == 0
        assert [view.script.deferred for view in entry.scripts] == [True]
        counts = reconciliation_store.stored_counts(
            world.database, world.rosters["2"], world.batch_id
        )
        assert counts is not None
        # 200123's own missing script is unrelated and unchanged.
        assert counts.present_without_script == before.present_without_script
        assert counts.script_deferred == 1 and counts.deferred == 1
        assert counts.outstanding_count == before.outstanding_count

    def test_it_is_not_scored_and_says_why(self, world):
        defer(world, world.ids["s2a.png"])
        result = world.score("2")["200121"]
        assert result.status is ResultStatus.BLOCKED
        assert BlockReason.NOT_RECONCILED in {item.reason for item in result.blocks}
        readiness = report_store.check_readiness(
            world.database, world.rosters["2"], world.batch_id, TEMPLATE, "2",
            for_final_export=True,
        )
        assert any(item.roll == "200121" for item in readiness.by_kind(
            ReadinessIssueKind.SHEET_DEFERRED
        ))

    def test_resolves_undo_reject_restores_a_disposition_too(self, world):
        defer(world, world.ids["s2a.png"])
        scan_lifecycle.undo_reject(world.database, world.ids["s2a.png"], reviewer=OPERATOR)
        assert state(world, world.ids["s2a.png"]) is LifecycleState.ACTIVE
        assert stored_entries(world, "2")["200121"].status is ReconciliationStatus.MATCHED


# ----------------------------------------------------------------------
# 12. Persistence
# ----------------------------------------------------------------------
class TestPersistence:
    def test_every_disposition_survives_reopening(self, world):
        first, second = world.ids["s2a.png"], duplicate_of(world)
        stray = unknown_sheet(world)
        keep(world, first, second)
        defer(world, stray)
        root, roster, batch = world.session.root, world.rosters["2"], world.batch_id
        world.session.close()
        with open_project(root) as reopened:
            database = reopened.database
            assert scan_lifecycle.state_of(database, first) is LifecycleState.ACTIVE
            assert scan_lifecycle.state_of(database, second) is LifecycleState.EXCLUDED
            assert scan_lifecycle.state_of(database, stray) is LifecycleState.DEFERRED
            reconciliation_store.reconcile_batch(database, roster, batch)
            entries = {
                entry.candidate_id: entry
                for entry in reconciliation_store.list_entries(database, roster, batch)
            }
            assert entries["200121"].status is ReconciliationStatus.MATCHED
            assert "209999" not in entries
            scoring_store.score_batch(database, roster, batch, TEMPLATE, computed_by=OPERATOR)
            results = {
                item.candidate_id: item
                for item in scoring_store.list_results(database, roster, batch, TEMPLATE)
            }
            assert results["200121"].scan_id == first
            assert "209999" not in results

    def test_no_schema_change_and_no_row_is_deleted(self, world):
        second = duplicate_of(world)
        stray = unknown_sheet(world)
        with world.database.session() as session:
            scans = session.scalar(select(func.count()).select_from(BatchScan))
        keep(world, world.ids["s2a.png"], second)
        exclude(world, stray)
        restore(world, stray)
        with world.database.session() as session:
            assert session.scalar(select(func.count()).select_from(BatchScan)) == scans
            rows = session.scalars(select(ScanRejection)).all()
            # The restored sheet's row rests as 'active' - the record outlives the undo.
            assert {row.scan_id: row.state for row in rows} == {
                second: "excluded", stray: "active",
            }


# ----------------------------------------------------------------------
# 13-15. Reject & Rescan is not regressed
# ----------------------------------------------------------------------
class TestRejectAndRescanStillHolds:
    def test_a_rejected_original_and_its_rescan_are_not_a_duplicate(self, world):
        reject(world, "s2b.png")
        rescan = world.add_processed_scan("s2b_rescan.png", "200122", "2", 15)
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], rescan, reviewer=OPERATOR
        )
        entry = world.reconcile("2")["200122"]
        assert entry.status is ReconciliationStatus.MATCHED
        assert ReconciliationStatus.DUPLICATE_SCRIPT not in {issue.status for issue in entry.issues}
        assert rescan not in open_duplicates(world)

    def test_a_confirmed_rescan_cannot_be_rejected_or_deferred(self, world):
        reject(world, "s2b.png")
        rescan = world.add_processed_scan("s2b_rescan.png", "200122", "2", 15)
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], rescan, reviewer=OPERATOR
        )
        with pytest.raises(LifecycleError, match="replacement"):
            exclude(world, rescan)
        with pytest.raises(LifecycleError):
            defer(world, rescan)
        assert state(world, rescan) is LifecycleState.ACTIVE
        assert state(world, world.ids["s2b.png"]) is LifecycleState.SUPERSEDED_BY_REPLACEMENT

    def test_a_duplicate_of_a_rescan_is_settled_without_breaking_the_link(self, world):
        reject(world, "s2b.png")
        rescan = world.add_processed_scan("s2b_rescan.png", "200122", "2", 15)
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], rescan, reviewer=OPERATOR
        )
        again = world.add_processed_scan("s2b_rescan_again.png", "200122", "2", 15)
        assert world.reconcile("2")["200122"].status is ReconciliationStatus.DUPLICATE_SCRIPT
        with pytest.raises(LifecycleError):
            keep(world, again, rescan)  # would strand the link
        keep(world, rescan, again)
        case = scan_lifecycle.get_case(world.database, world.ids["s2b.png"])
        assert case is not None and case.replacement_scan_id == rescan
        assert stored_entries(world, "2")["200122"].status is ReconciliationStatus.MATCHED
        assert world.score("2")["200122"].scan_id == rescan

    def test_a_disposition_is_not_a_rescan_case(self, world):
        stray = unknown_sheet(world)
        exclude(world, stray)
        assert scan_lifecycle.list_cases(world.database, world.batch_id) == ()
        assert scan_lifecycle.replacement_candidates(world.database, stray) == ()
        assert scan_lifecycle.plan_purge(world.database, world.session.root).eligible == ()

    def test_a_rescan_is_still_undone_on_resolve_not_restored_here(self, world):
        reject(world, "s2b.png")
        with pytest.raises(LifecycleError) as refused:
            restore(world, world.ids["s2b.png"])
        assert "Resolve" in (refused.value.user_message or "")

    def test_cross_batch_replacement_still_counts_once(self, world):
        reject(world, "s2b.png")
        later = add_batch(world, [("SCN_200122.tif", "200122", "2", 15)])
        rescan = world.ids["SCN_200122.tif"]
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], rescan, reviewer=OPERATOR
        )
        # An accidental duplicate in the original batch is settled there.
        stray_copy = world.add_processed_scan("s2b_copy.png", "200122", "2", 15)
        entry = world.reconcile("2")["200122"]
        assert entry.status is ReconciliationStatus.DUPLICATE_SCRIPT
        keep(world, rescan, stray_copy)
        assert stored_entries(world, "2")["200122"].status is ReconciliationStatus.MATCHED
        assert world.score("2")["200122"].scan_id == rescan
        # And the rescan still counts nowhere else.
        reconciliation_store.reconcile_batch(world.database, world.rosters["2"], later)
        assert "200122" in {
            entry.candidate_id
            for entry in reconciliation_store.list_entries(
                world.database, world.rosters["2"], later
            )
            if entry.script_count == 0
        }
        assert state(world, stray_copy) is LifecycleState.EXCLUDED

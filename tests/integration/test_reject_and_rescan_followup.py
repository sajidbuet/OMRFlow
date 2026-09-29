"""Reject & Rescan hardening: cross-batch, clean sheets, duplicates after Undo.

Scope:
    The same real-project world as ``test_reject_and_rescan.py`` - three sets
    in one batch, attendance workbooks, verified keys - plus a **second
    batch** holding rescans, as happens when a sheet rejected in one run is
    rescanned days later on another scanner. Every assertion is on the
    services and the database.

Privacy:
    Every identifier here is fictional.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import openpyxl
import pytest
from sqlalchemy import select
from tests.integration.test_reject_and_rescan import (
    HEADER_ROW,
    OPERATOR,
    SCRIPTS,
    TEMPLATE,
    World,
    build_world,
    lifecycle_events,
    make_result,
    reject,
)

from omr_scanner.database.migrations import SCHEMA_VERSION
from omr_scanner.database.models import AuditEvent, BatchScan, ReviewConflict, ScanBatch
from omr_scanner.domain.reconciliation import ReconciliationStatus
from omr_scanner.domain.review import ConflictState, ConflictType, ReviewAction
from omr_scanner.domain.scan_lifecycle import FileState, LifecycleState, PurgeMode, RejectionReason
from omr_scanner.domain.scoring import ResultStatus, StaleReason
from omr_scanner.reporting import excel as rx
from omr_scanner.services import (
    batch_store,
    create_project,
    open_project,
    reconciliation_store,
    report_store,
    review_store,
    scan_lifecycle,
    scan_provenance,
    scoring_store,
)
from omr_scanner.services.reconciliation_leads import owner_leads
from omr_scanner.services.scan_lifecycle import LifecycleError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def world(workspace: Path, tmp_path: Path) -> Iterator[World]:
    session = create_project(workspace, "Reject and Rescan Follow-up")
    try:
        yield build_world(session, tmp_path)
    finally:
        if not session.is_closed:
            session.close()


def add_batch(
    world: World, rows: list[tuple[str, str, str, int]], *, content: dict[str, bytes] | None = None
) -> str:
    """A second batch of read scans - another run, another scanner."""
    scans_dir = world.scans_dir / "rescans"
    scans_dir.mkdir(exist_ok=True)
    now = datetime.now(UTC)
    batch_id = batch_store.new_batch_id()
    with world.database.session() as session:
        session.add(
            ScanBatch(
                batch_id=batch_id, created_at=now, updated_at=now,
                source_folder=str(scans_dir), status="completed", total_scans=len(rows),
            )
        )
        session.flush()
        for index, (name, roll, code, correct) in enumerate(rows):
            path = scans_dir / name
            path.write_bytes((content or {}).get(name, f"rescan:{name}".encode()))
            session.add(
                BatchScan(
                    batch_id=batch_id, batch_index=index, source_path=str(path),
                    filename=name, status="completed", identifier_value=roll,
                    set_code_value=code,
                    result_json=json.dumps(make_result(path, roll, code, correct).to_dict()),
                )
            )
    scan_provenance.compute_hashes_for_batch(world.database, batch_id)
    for path, scan_id in batch_store.scan_ids_by_path(world.database, batch_id).items():
        world.ids[path.name] = scan_id
    # What the Scan stage runs after every batch.
    scan_lifecycle.sync_reimports(world.database, batch_id)
    review_store.sync_duplicate_identifiers(world.database, batch_id)
    review_store.sync_undefined_set_codes(world.database, batch_id)
    return batch_id


def entries_of(world: World, code: str, batch_id: str) -> dict[str, object]:
    reconciliation_store.reconcile_batch(world.database, world.rosters[code], batch_id)
    return {
        entry.candidate_id: entry
        for entry in reconciliation_store.list_entries(
            world.database, world.rosters[code], batch_id
        )
    }


def duplicates(world: World, batch_id: str) -> dict[int, ConflictState]:
    """Every duplicate-ID record of a batch in the working queue, by scan."""
    return {
        item.scan_id: item.state
        for item in review_store.list_conflicts(
            world.database, batch_id,
            filters=review_store.ConflictFilter(include_withdrawn=True),
        )
        if item.conflict_type is ConflictType.IDENTIFIER_DUPLICATE
    }


# ----------------------------------------------------------------------
# A. Cross-batch replacement
# ----------------------------------------------------------------------
class TestCrossBatchReplacement:
    def test_a_rescan_in_another_batch_is_offered_and_not_linked(self, world):
        reject(world, "s2b.png")
        later = add_batch(world, [("SCN_000913.tif", "200122", "2", 20)])
        found = scan_lifecycle.replacement_candidates(world.database, world.ids["s2b.png"])
        assert [item.scan_id for item in found] == [world.ids["SCN_000913.tif"]]
        assert found[0].other_batch is True
        assert found[0].batch_id == later
        assert later[:8] in found[0].batch_label
        assert found[0].set_code_agrees is True
        # Offered, never linked.
        assert scan_lifecycle.state_of(world.database, world.ids["s2b.png"]) is (
            LifecycleState.REJECTED_PENDING_RESCAN
        )
        assert [event.action for event in lifecycle_events(world, "s2b.png")] == ["rejected"]

    def test_confirming_counts_only_the_rescan_in_the_originals_batch(self, world):
        reject(world, "s2b.png")
        later = add_batch(world, [("SCN_000913.tif", "200122", "2", 20)])
        rescan = world.ids["SCN_000913.tif"]
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], rescan, reviewer=OPERATOR
        )
        entry = world.reconcile("2")["200122"]
        assert entry.status is ReconciliationStatus.MATCHED
        assert [view.script.scan_id for view in entry.scripts] == [rescan]
        result = world.score("2")["200122"]
        assert result.status is ResultStatus.SCORED
        assert result.scan_id == rescan
        assert result.correct_count == 20  # the rescan's answers, from its own batch
        assert scan_lifecycle.state_of(world.database, world.ids["s2b.png"]) is (
            LifecycleState.SUPERSEDED_BY_REPLACEMENT
        )
        # Counted once: the rescan's own batch leaves it out.
        in_later = entries_of(world, "2", later)
        assert all(
            view.script.scan_id != rescan
            for item in in_later.values()
            for view in item.scripts
        )
        scope = reconciliation_store.script_scope(
            world.database, world.rosters["2"], later
        )
        assert scope.counted_elsewhere == 1 and scope.in_set == 0

    def test_set_scoping_still_holds(self, world):
        before = {code: set(world.reconcile(code)) for code in "13"}
        reject(world, "s2b.png")
        add_batch(world, [("SCN_000913.tif", "200122", "2", 20)])
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], world.ids["SCN_000913.tif"],
            reviewer=OPERATOR,
        )
        assert {code: set(world.reconcile(code)) for code in "13"} == before
        scope = reconciliation_store.script_scope(
            world.database, world.rosters["2"], world.batch_id
        )
        assert scope.in_set == 3  # s2a, s2c and the adopted rescan

    def test_a_rescan_of_another_set_is_not_counted_in_this_one(self, world):
        reject(world, "s2b.png")
        # Read as set 3 - its effective set code decides, even adopted.
        add_batch(world, [("SCN_000914.tif", "200122", "3", 20)])
        rescan = world.ids["SCN_000914.tif"]
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], rescan, reviewer=OPERATOR
        )
        entry = world.reconcile("2")["200122"]
        assert entry.script_count == 0
        assert rescan not in {
            view.script.scan_id for view in entry.scripts
        }

    def test_the_relationship_persists_after_reopening(self, world):
        reject(world, "s2b.png")
        later = add_batch(world, [("SCN_000913.tif", "200122", "2", 20)])
        rescan = world.ids["SCN_000913.tif"]
        original = world.ids["s2b.png"]
        scan_lifecycle.confirm_replacement(world.database, original, rescan, reviewer=OPERATOR)
        root, batch_id, roster = world.session.root, world.batch_id, world.rosters["2"]
        world.session.close()
        with open_project(root) as reopened:
            case = scan_lifecycle.get_case(reopened.database, original)
            assert case is not None
            assert case.replacement_scan_id == rescan
            assert case.replacement_batch_id == later
            reconciliation_store.reconcile_batch(reopened.database, roster, batch_id)
            entry = {
                item.candidate_id: item
                for item in reconciliation_store.list_entries(reopened.database, roster, batch_id)
            }["200122"]
            assert [view.script.scan_id for view in entry.scripts] == [rescan]

    def test_the_link_is_audited_under_each_scans_own_batch(self, world):
        reject(world, "s2b.png")
        later = add_batch(world, [("SCN_000913.tif", "200122", "2", 20)])
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], world.ids["SCN_000913.tif"],
            reviewer=OPERATOR,
        )
        original = lifecycle_events(world, "s2b.png")[-1]
        linked = lifecycle_events(world, "SCN_000913.tif")[-1]
        assert original.action == "replaced" and original.batch_id == world.batch_id
        assert linked.action == "linked_replacement" and linked.batch_id == later
        assert f"from batch {later[:8]}" in original.detail

    def test_suggestion_semantics_hold_across_batches(self, world):
        reject(world, "s2b.png")
        add_batch(world, [("SCN_000913.tif", "200122", "2", 20)])
        entries = world.reconcile("2")
        # Before confirmation 200122 has no valid script.
        assert entries["200122"].status is ReconciliationStatus.RESCAN_REQUIRED
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], world.ids["SCN_000913.tif"],
            reviewer=OPERATOR,
        )
        entries = world.reconcile("2")
        unknown = entries["20123"]
        leads = [lead.candidate_id for lead in owner_leads(unknown, list(entries.values()))]
        assert "200123" in leads
        # After it: the confirmed replacement is a valid script.
        assert "200122" not in leads

    def test_a_scan_that_is_not_in_this_project_is_refused(self, world, workspace):
        reject(world, "s2b.png")
        with create_project(workspace, "Some Other Project") as other:
            elsewhere = batch_store.create_batch(
                other.database, [workspace / "x.png"], identity=batch_store.BatchIdentity()
            )
            foreign = max(batch_store.scan_ids_by_path(other.database, elsewhere).values())
        # The project boundary is the database: an id with no row here is
        # refused, and nothing in this project can name another's scan.
        unknown = foreign + 10_000
        with pytest.raises(LifecycleError):
            scan_lifecycle.confirm_replacement(
                world.database, world.ids["s2b.png"], unknown, reviewer=OPERATOR
            )

    def test_the_rejected_bytes_in_another_batch_are_not_a_rescan(self, world):
        reject(world, "s2b.png")
        add_batch(
            world, [("copy_of_s2b.png", "200122", "2", 15)],
            content={"copy_of_s2b.png": b"scan:s2b.png"},
        )
        copy = world.ids["copy_of_s2b.png"]
        assert scan_lifecycle.state_of(world.database, copy) is (
            LifecycleState.REIMPORT_OF_REJECTED
        )
        assert scan_lifecycle.replacement_candidates(
            world.database, world.ids["s2b.png"]
        ) == ()
        assert copy not in {
            item.scan_id
            for item in scan_lifecycle.association_choices(world.database, world.ids["s2b.png"])
        }
        with pytest.raises(LifecycleError):
            scan_lifecycle.confirm_replacement(
                world.database, world.ids["s2b.png"], copy, reviewer=OPERATOR
            )
        assert world.reconcile("2")["200122"].status is ReconciliationStatus.RESCAN_REQUIRED

    def test_a_replacement_cannot_serve_two_originals_across_batches(self, world):
        reject(world, "s2b.png")
        reject(world, "s_blur.png")
        add_batch(world, [("SCN_000913.tif", "200122", "2", 20)])
        rescan = world.ids["SCN_000913.tif"]
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], rescan, reviewer=OPERATOR
        )
        assert rescan not in {
            item.scan_id
            for item in scan_lifecycle.association_choices(
                world.database, world.ids["s_blur.png"]
            )
        }
        with pytest.raises(LifecycleError):
            scan_lifecycle.confirm_replacement(
                world.database, world.ids["s_blur.png"], rescan, reviewer=OPERATOR
            )

    def test_an_unknown_id_case_is_linked_by_hand_from_another_batch(self, world):
        reject(world, "s_blur.png")
        add_batch(
            world,
            [("DSC_0001.jpg", "300122", "3", 9), ("DSC_0002.jpg", "300121", "3", 9)],
        )
        # Project-wide, and searchable in SQL.
        everything = scan_lifecycle.association_choices(world.database, world.ids["s_blur.png"])
        assert {world.ids["DSC_0001.jpg"], world.ids["s1a.png"]} <= {
            item.scan_id for item in everything
        }
        narrowed = scan_lifecycle.association_choices(
            world.database, world.ids["s_blur.png"], search="DSC_0001"
        )
        assert [item.scan_id for item in narrowed] == [world.ids["DSC_0001.jpg"]]
        assert narrowed[0].other_batch is True
        capped = scan_lifecycle.association_choices(
            world.database, world.ids["s_blur.png"], limit=2
        )
        assert len(capped) == 2
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s_blur.png"], world.ids["DSC_0001.jpg"],
            reviewer=OPERATOR,
        )
        assert scan_lifecycle.state_of(world.database, world.ids["s_blur.png"]) is (
            LifecycleState.SUPERSEDED_BY_REPLACEMENT
        )

    def test_removing_a_cross_batch_link_returns_the_rescan_to_its_own_batch(self, world):
        reject(world, "s2b.png")
        later = add_batch(world, [("SCN_000913.tif", "200122", "2", 20)])
        rescan = world.ids["SCN_000913.tif"]
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], rescan, reviewer=OPERATOR
        )
        world.reconcile("2")
        scan_lifecycle.remove_replacement(
            world.database, world.ids["s2b.png"], reviewer=OPERATOR
        )
        assert world.reconcile("2")["200122"].status is ReconciliationStatus.RESCAN_REQUIRED
        assert rescan in {
            view.script.scan_id
            for item in entries_of(world, "2", later).values()
            for view in item.scripts
        }
        removed = lifecycle_events(world, "SCN_000913.tif")[-1]
        assert removed.action == "replacement_removed" and removed.batch_id == later

    def test_a_mark_from_the_original_goes_stale_after_a_cross_batch_link(self, world):
        assert world.score("2")["200122"].status is ResultStatus.SCORED
        reject(world, "s2b.png")
        add_batch(world, [("SCN_000913.tif", "200122", "2", 20)])
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], world.ids["SCN_000913.tif"],
            reviewer=OPERATOR,
        )
        stale = world.results("2")["200122"]
        assert StaleReason.RECONCILIATION in stale.stale_reasons

    def test_no_schema_change_was_needed(self):
        # The replacement link was scan-to-scan from the start; cross-batch
        # support is service logic only. Later migrations exist (12 is
        # answer-key provenance), but none of them touches rejection or
        # replacement.
        from omr_scanner.database.migrations import MIGRATIONS

        assert SCHEMA_VERSION >= 11
        assert MIGRATIONS[10].version == 11
        later = " ".join(item.description.lower() for item in MIGRATIONS[11:])
        assert "reject" not in later and "rescan" not in later and "scan_" not in later


# ----------------------------------------------------------------------
# B. Clean sheets (service side; the view is in tests/gui)
# ----------------------------------------------------------------------
class TestProcessedSheets:
    def test_every_read_sheet_is_listed_with_its_conflict_count(self, world):
        sheets = {
            sheet.filename: sheet
            for sheet in scan_lifecycle.processed_sheets(world.database, world.batch_id)
        }
        assert set(sheets) == {name for name, *_rest in SCRIPTS}
        assert sheets["s1a.png"].open_conflicts == 0
        assert sheets["s_x.png"].open_conflicts == 1  # set code not a defined set
        assert all(sheet.state is LifecycleState.ACTIVE for sheet in sheets.values())

    def test_the_list_is_searched_and_paged_in_sql(self, world):
        found = scan_lifecycle.processed_sheets(world.database, world.batch_id, search="s3")
        assert [sheet.filename for sheet in found] == ["s3a.png", "s3b.png"]
        page = scan_lifecycle.processed_sheets(world.database, world.batch_id, limit=2, offset=2)
        assert [sheet.filename for sheet in page] == ["s2a.png", "s2b.png"]

    def test_rejecting_a_clean_sheet_is_the_ordinary_rejection(self, world):
        assert review_store.list_conflicts(
            world.database, world.batch_id,
            filters=review_store.ConflictFilter(scan_id=world.ids["s3b.png"]),
        ) == ()
        before = review_store.count_conflicts(world.database, world.batch_id)
        reject(world, "s3b.png", reason=RejectionReason.WRONG_DOCUMENT)
        assert world.reconcile("3")["300122"].status is ReconciliationStatus.RESCAN_REQUIRED
        assert scan_lifecycle.count_cases(world.database, world.batch_id).outstanding == 1
        listed = {
            sheet.filename: sheet
            for sheet in scan_lifecycle.processed_sheets(world.database, world.batch_id)
        }
        assert listed["s3b.png"].state is LifecycleState.REJECTED_PENDING_RESCAN
        # Unresolved counts are those of conflicts, and unchanged.
        after = review_store.count_conflicts(world.database, world.batch_id)
        assert (after.total, after.unresolved) == (before.total, before.unresolved)


# ----------------------------------------------------------------------
# C. Duplicate state after lifecycle changes
# ----------------------------------------------------------------------
class TestDuplicateReconstruction:
    def _duplicate_pair(self, world: World) -> tuple[int, int]:
        partner = world.ids["s2a.png"]
        dup = world.add_processed_scan("s2a_dup.png", "200121", "2", 3)
        found = duplicates(world, world.batch_id)
        assert found == {partner: ConflictState.OPEN, dup: ConflictState.OPEN}
        return partner, dup

    def test_undo_reject_raises_the_partners_duplicate_again(self, world):
        partner, dup = self._duplicate_pair(world)
        reject(world, "s2a_dup.png")
        # The partner's record is withdrawn; the rejected scan's is hidden.
        assert duplicates(world, world.batch_id) == {partner: ConflictState.WITHDRAWN}
        scan_lifecycle.undo_reject(world.database, dup, reviewer=OPERATOR)
        assert duplicates(world, world.batch_id) == {
            partner: ConflictState.OPEN,
            dup: ConflictState.OPEN,
        }
        assert review_store.count_conflicts(world.database, world.batch_id).by_type.get(
            ConflictType.IDENTIFIER_DUPLICATE.value
        ) == 2

    def test_the_state_equals_a_fresh_detection(self, world):
        partner, dup = self._duplicate_pair(world)
        reject(world, "s2a_dup.png")
        scan_lifecycle.undo_reject(world.database, dup, reviewer=OPERATOR)
        after_undo = duplicates(world, world.batch_id)
        # Running the canonical engine again changes nothing.
        review_store.sync_duplicate_identifiers(world.database, world.batch_id)
        assert duplicates(world, world.batch_id) == after_undo
        with world.database.session() as session:
            record = session.scalars(
                select(ReviewConflict)
                .where(ReviewConflict.scan_id == partner)
                .where(ReviewConflict.conflict_type == ConflictType.IDENTIFIER_DUPLICATE.value)
            ).one()
            assert set(record.related_scan_ids.split(",")) == {str(dup)}

    def test_the_re_raise_is_a_machine_event_and_not_undoable(self, world):
        partner, dup = self._duplicate_pair(world)
        reject(world, "s2a_dup.png")
        scan_lifecycle.undo_reject(world.database, dup, reviewer=OPERATOR)
        with world.database.session() as session:
            record = session.scalars(
                select(ReviewConflict)
                .where(ReviewConflict.scan_id == partner)
                .where(ReviewConflict.conflict_type == ConflictType.IDENTIFIER_DUPLICATE.value)
            ).one()
            events = session.scalars(
                select(AuditEvent)
                .where(AuditEvent.conflict_id == record.conflict_id)
                .order_by(AuditEvent.event_id)
            ).all()
        assert [event.action for event in events] == ["detected", "withdrawn", "redetected"]
        assert events[-1].reviewer == ""
        history = review_store.history_for(world.database, record.conflict_id)
        assert review_store.recompute_state(history) is ConflictState.OPEN
        assert review_store.last_decision(world.database, world.batch_id) is None
        assert ReviewAction.REDETECTED.is_command and not ReviewAction.REDETECTED.is_human

    def test_attendance_and_scoring_agree_with_resolve(self, world):
        _partner, dup = self._duplicate_pair(world)
        reject(world, "s2a_dup.png")
        assert world.reconcile("2")["200121"].status is ReconciliationStatus.MATCHED
        scan_lifecycle.undo_reject(world.database, dup, reviewer=OPERATOR)
        assert world.reconcile("2")["200121"].status is ReconciliationStatus.DUPLICATE_SCRIPT
        result = world.score("2")["200121"]
        assert result.status is ResultStatus.BLOCKED

    def test_a_decided_duplicate_is_never_reopened_by_the_machine(self, world):
        partner, dup = self._duplicate_pair(world)
        record = next(
            item
            for item in review_store.list_conflicts(world.database, world.batch_id)
            if item.scan_id == partner and item.conflict_type is ConflictType.IDENTIFIER_DUPLICATE
        )
        review_store.accept_machine_value(world.database, record.conflict_id, reviewer=OPERATOR)
        reject(world, "s2a_dup.png")
        scan_lifecycle.undo_reject(world.database, dup, reviewer=OPERATOR)
        assert duplicates(world, world.batch_id)[partner] is ConflictState.RESOLVED

    def test_removing_a_link_recomputes_duplicates_in_both_batches(self, world):
        reject(world, "s2b.png")
        later = add_batch(
            world,
            [("SCN_000913.tif", "200122", "2", 20), ("SCN_000914.tif", "200122", "2", 21)],
        )
        first, second = world.ids["SCN_000913.tif"], world.ids["SCN_000914.tif"]
        # Two active scans with one ID in batch B: detected, as always.
        assert duplicates(world, later) == {
            first: ConflictState.OPEN, second: ConflictState.OPEN,
        }
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], first, reviewer=OPERATOR
        )
        scan_lifecycle.remove_replacement(
            world.database, world.ids["s2b.png"], reviewer=OPERATOR
        )
        after = duplicates(world, later)
        review_store.sync_duplicate_identifiers(world.database, later)
        assert duplicates(world, later) == after
        assert set(after) == {first, second}

    def test_the_restored_state_survives_reopen_and_reprocessing(self, world):
        partner, dup = self._duplicate_pair(world)
        reject(world, "s2a_dup.png")
        scan_lifecycle.undo_reject(world.database, dup, reviewer=OPERATOR)
        expected = duplicates(world, world.batch_id)
        root, batch_id = world.session.root, world.batch_id
        world.session.close()
        with open_project(root) as reopened:
            assert {
                item.scan_id: item.state
                for item in review_store.list_conflicts(
                    reopened.database, batch_id,
                    filters=review_store.ConflictFilter(include_withdrawn=True),
                )
                if item.conflict_type is ConflictType.IDENTIFIER_DUPLICATE
            } == expected
            # A reprocessing pass ends, as every run does, with the canonical
            # duplicate pass - which finds nothing to change.
            batch_store.mark_for_reprocessing(
                reopened.database, batch_id, [partner, dup], reason="retry"
            )
            review_store.sync_duplicate_identifiers(reopened.database, batch_id)
            assert {
                item.scan_id: item.state
                for item in review_store.list_conflicts(
                    reopened.database, batch_id,
                    filters=review_store.ConflictFilter(include_withdrawn=True),
                )
                if item.conflict_type is ConflictType.IDENTIFIER_DUPLICATE
            } == expected

    def test_every_lifecycle_change_leaves_a_fresh_detection_unchanged(self, world):
        """After each transition, re-running the engine is a no-op."""
        dup = world.add_processed_scan("s2b_dup.png", "200122", "2", 4)

        def steady() -> None:
            now = duplicates(world, world.batch_id)
            review_store.sync_duplicate_identifiers(world.database, world.batch_id)
            assert duplicates(world, world.batch_id) == now

        reject(world, "s2b.png")
        steady()
        rescan = world.add_processed_scan("s2b_rescan.png", "200122", "2", 20)
        steady()
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], rescan, reviewer=OPERATOR
        )
        steady()
        scan_lifecycle.remove_replacement(world.database, world.ids["s2b.png"], reviewer=OPERATOR)
        steady()
        scan_lifecycle.undo_reject(world.database, world.ids["s2b.png"], reviewer=OPERATOR)
        steady()
        assert {world.ids["s2b.png"], dup, rescan} <= set(duplicates(world, world.batch_id))
        # s2b, its duplicate and the former rescan are all active: every one
        # carries an open duplicate record, as a fresh detection would give.
        assert all(
            state is ConflictState.OPEN
            for scan_id, state in duplicates(world, world.batch_id).items()
            if scan_id in {world.ids["s2b.png"], dup, rescan}
        )


# ----------------------------------------------------------------------
# Exactly once: a cross-batch replacement in results and exports
# ----------------------------------------------------------------------
def replace_across_batches(world: World, original: str, roll: str, code: str, correct: int):
    """Reject ``original`` in batch A; read and confirm its rescan in batch B."""
    reject(world, original)
    later = add_batch(world, [(f"SCN_{roll}.tif", roll, code, correct)])
    rescan = world.ids[f"SCN_{roll}.tif"]
    scan_lifecycle.confirm_replacement(
        world.database, world.ids[original], rescan, reviewer=OPERATOR
    )
    return later, rescan


def results_in(world: World, code: str, batch_id: str) -> list:
    scoring_store.score_batch(
        world.database, world.rosters[code], batch_id, TEMPLATE, computed_by=OPERATOR
    )
    return list(
        scoring_store.list_results(world.database, world.rosters[code], batch_id, TEMPLATE)
    )


def sheet_rows(sheet) -> dict[str, list[tuple[object, ...]]]:
    """Every data row of a result sheet, by Roll No. (column 2)."""
    rows: dict[str, list[tuple[object, ...]]] = {}
    for values in sheet.iter_rows(min_row=HEADER_ROW + 1, values_only=True):
        if len(values) > 1 and values[1] not in (None, ""):
            rows.setdefault(str(values[1]), []).append(values)
    return rows


class TestCountedExactlyOnce:
    """Physical batch B, academic batch A: the replacement counts once, in A."""

    def test_only_the_replacement_contributes_and_only_once(self, world):
        before = {item.candidate_id: item for item in results_in(world, "3", world.batch_id)}
        assert before["300122"].scan_id == world.ids["s3b.png"]
        later, rescan = replace_across_batches(world, "s3b.png", "300122", "3", 20)
        in_a = results_in(world, "3", world.batch_id)
        in_b = results_in(world, "3", later)
        # One result per candidate in A, as before: nobody gained or lost a row.
        assert sorted(item.candidate_id for item in in_a) == sorted(before)
        mine = [item for item in in_a if item.candidate_id == "300122"]
        assert len(mine) == 1
        assert mine[0].status is ResultStatus.SCORED
        assert mine[0].scan_id == rescan and mine[0].correct_count == 20
        # The original contributes nothing, anywhere.
        original = world.ids["s3b.png"]
        assert all(item.scan_id != original for item in in_a + in_b)
        # Batch B, where the rescan physically is, never scores it.
        assert all(item.scan_id != rescan for item in in_b)
        assert not any(
            item.status is ResultStatus.SCORED and item.candidate_id == "300122" for item in in_b
        )
        # Its answers are loadable there, but no entry of B files it - which is
        # what scoring marks from.
        assert all(
            view.script.scan_id != rescan
            for item in entries_of(world, "3", later).values()
            for view in item.scripts
        )
        # Across both batches, one scored mark for the student.
        scored = [
            item for item in in_a + in_b
            if item.candidate_id == "300122" and item.status is ResultStatus.SCORED
        ]
        assert len(scored) == 1
        # The set's totals are the same count of scored candidates as before.
        assert sum(item.status is ResultStatus.SCORED for item in in_a) == sum(
            item.status is ResultStatus.SCORED for item in before.values()
        )

    def test_the_export_lists_the_student_once_with_the_rescans_mark(self, world, tmp_path):
        _later, rescan = replace_across_batches(world, "s3b.png", "300122", "3", 20)
        results = {item.candidate_id: item for item in results_in(world, "3", world.batch_id)}
        outcome = report_store.generate_for_set(
            world.database, world.set_ids["3"], world.batch_id, TEMPLATE,
            project_name="RR", output_dir=tmp_path / "out", computed_by=OPERATOR,
        )
        assert outcome.ok, (outcome.status, outcome.warnings)
        sheet_name = report_store.resolve_set_sources(
            world.database, world.set_ids["3"]
        ).association.sheet_name
        workbook = openpyxl.load_workbook(outcome.output_path)
        try:
            rollwise = sheet_rows(workbook[sheet_name])
            meritwise = sheet_rows(workbook[rx.MERITWISE_SHEET_NAME])
        finally:
            workbook.close()
        for rows in (rollwise, meritwise):
            assert sorted(rows) == ["300121", "300122"]
            assert all(len(found) == 1 for found in rows.values())
        mark = results["300122"].final_score
        assert results["300122"].scan_id == rescan
        assert rollwise["300122"][0][3] == pytest.approx(float(mark))

    def test_reopening_changes_nothing(self, world):
        later, rescan = replace_across_batches(world, "s3b.png", "300122", "3", 20)
        results_in(world, "3", world.batch_id)
        root, batch_a, roster = world.session.root, world.batch_id, world.rosters["3"]
        world.session.close()
        with open_project(root) as reopened:
            for batch in (batch_a, later):
                scoring_store.score_batch(
                    reopened.database, roster, batch, TEMPLATE, computed_by=OPERATOR
                )
            in_a = scoring_store.list_results(reopened.database, roster, batch_a, TEMPLATE)
            in_b = scoring_store.list_results(reopened.database, roster, later, TEMPLATE)
            scored = [
                item for item in (*in_a, *in_b)
                if item.candidate_id == "300122" and item.status is ResultStatus.SCORED
            ]
            assert [(item.scan_id, item.correct_count) for item in scored] == [(rescan, 20)]
            assert [item.candidate_id for item in in_a].count("300122") == 1


# ----------------------------------------------------------------------
# Undo Reject once the original's image is gone
# ----------------------------------------------------------------------
class TestImageGoneUndoSafety:
    def _purge(self, world: World, mode: PurgeMode) -> tuple[int, int]:
        _later, rescan = replace_across_batches(world, "s2b.png", "200122", "2", 20)
        outcome = scan_lifecycle.execute_purge(
            world.database, world.session.root, mode=mode, reviewer=OPERATOR
        )
        assert outcome.processed == 1
        return world.ids["s2b.png"], rescan

    def _refused_everywhere(self, world: World, original: int, rescan: int, where: str) -> None:
        events_before = len(lifecycle_events(world, "s2b.png"))
        with pytest.raises(LifecycleError) as undo:
            scan_lifecycle.undo_reject(world.database, original, reviewer=OPERATOR)
        assert "Original image is no longer available" in undo.value.user_message
        assert where in undo.value.user_message
        assert "cannot be undone" in undo.value.user_message
        # Not sent to "remove the link first" - that is refused too, and says so.
        assert "Remove the replacement link" not in undo.value.user_message
        with pytest.raises(LifecycleError) as unlink:
            scan_lifecycle.remove_replacement(world.database, original, reviewer=OPERATOR)
        assert unlink.value.user_message == undo.value.user_message
        # Nothing was written, and the replacement is untouched.
        assert len(lifecycle_events(world, "s2b.png")) == events_before
        case = scan_lifecycle.get_case(world.database, original)
        assert case is not None
        assert case.state is LifecycleState.SUPERSEDED_BY_REPLACEMENT
        assert case.replacement_scan_id == rescan
        assert scan_lifecycle.state_of(world.database, rescan) is LifecycleState.ACTIVE
        result = world.score("2")["200122"]
        assert result.status is ResultStatus.SCORED and result.scan_id == rescan

    def test_a_quarantined_original_cannot_be_un_rejected(self, world):
        original, rescan = self._purge(world, PurgeMode.QUARANTINE)
        assert scan_lifecycle.get_case(world.database, original).file_state is (
            FileState.QUARANTINED
        )
        self._refused_everywhere(world, original, rescan, "quarantine")

    def test_a_purged_original_cannot_be_un_rejected(self, world):
        original, rescan = self._purge(world, PurgeMode.DELETE)
        assert scan_lifecycle.get_case(world.database, original).file_state is FileState.PURGED
        self._refused_everywhere(world, original, rescan, "permanently deleted")

    def test_the_note_is_empty_while_the_image_is_present(self):
        assert FileState.PRESENT.unavailable_note == ""
        assert FileState.QUARANTINED.unavailable_note.startswith(
            "Original image is no longer available"
        )

"""Bounded duplicate-ID re-derivation after a Student ID changes (0.1.1 phase 4).

Scope:
    :func:`omr_scanner.services.review_store.sync_duplicate_identifiers_for`,
    which every ordinary path (a Resolve decision, a lifecycle change, a batch
    finishing) uses instead of regrouping the whole session. Each case checks
    the stored duplicate records, and most also check that a **full rebuild**
    afterwards changes nothing - i.e. that the bounded pass reached exactly the
    state the rebuild would - and that untouched groups were never written.

Privacy:
    Every identifier here is fictional.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from tests.integration.test_reject_and_rescan import OPERATOR, build_world, make_result, reject
from tests.integration.test_session_population import SessionWorld

from omr_scanner.database.models import AuditEvent, BatchScan, ReviewConflict, ScanBatch
from omr_scanner.domain.review import (
    ConflictState,
    ConflictType,
    FieldKind,
    MachineObservation,
    ReasonCode,
)
from omr_scanner.services import (
    batch_store,
    create_project,
    open_project,
    review_store,
    scan_lifecycle,
    scan_sessions,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def sw(workspace: Path, tmp_path: Path) -> Iterator[SessionWorld]:
    session = create_project(workspace, "Bounded Duplicates")
    try:
        yield SessionWorld(build_world(session, tmp_path))
    finally:
        if not session.is_closed:
            session.close()


def correct(sw: SessionWorld, name: str, value: str) -> review_store.FieldEdit:
    """A reviewer's whole-Student-ID correction of one sheet; returns the edit."""
    scan_id = sw.ids[name]
    batch = scan_lifecycle.batch_of(sw.database, scan_id)
    assert batch is not None
    previous = review_store.effective_identifiers(sw.database, batch)[scan_id].value
    return review_store.correct_field(
        sw.database, batch_id=batch, scan_id=scan_id, zone_id="roll_number",
        values=dict(enumerate(value)), display_value=value, field_label="Roll",
        reviewer=OPERATOR, reason=ReasonCode.MISCLASSIFICATION,
        overrides={
            index: MachineObservation(value=char, status="complete", confidence=0.9)
            for index, char in enumerate(previous)
        },
        field_kind=FieldKind.IDENTIFIER, previous_value=previous,
    )


def records(database) -> dict[int, tuple[str, str, str]]:
    """``scan -> (state, value, related)`` of every duplicate-ID record."""
    with database.session() as session:
        return {
            row.scan_id: (row.state, row.machine_value, row.related_scan_ids)
            for row in session.scalars(
                select(ReviewConflict).where(
                    ReviewConflict.conflict_type == ConflictType.IDENTIFIER_DUPLICATE.value
                )
            ).all()
        }


def live(database) -> dict[int, set[int]]:
    """``scan -> related scans`` of the duplicate records still in play."""
    return {
        scan: {int(item) for item in related.split(",") if item}
        for scan, (state, _value, related) in records(database).items()
        if state != ConflictState.WITHDRAWN.value
    }


def audit_count(database) -> int:
    with database.session() as session:
        return int(session.scalar(select(func.count()).select_from(AuditEvent)) or 0)


def assert_rebuild_changes_nothing(sw: SessionWorld) -> None:
    """The bounded pass already reached the full rebuild's state."""
    before, events = records(sw.database), audit_count(sw.database)
    review_store.sync_duplicate_identifiers(sw.database, sw.batches[0])
    assert records(sw.database) == before
    assert audit_count(sw.database) == events


class TestBoundedGroups:
    def test_1_unique_to_another_unique(self, sw):
        sw.add_batch([("d1.tif", "100555", "1", 10)])
        correct(sw, "d1.tif", "100556")
        assert sw.ids["d1.tif"] not in live(sw.database)
        assert_rebuild_changes_nothing(sw)

    def test_2_unique_to_an_existing_id_makes_a_cross_batch_duplicate(self, sw):
        sw.add_batch([("d1.tif", "100555", "1", 10)])
        correct(sw, "d1.tif", "100121")
        assert live(sw.database) == {
            sw.ids["s1a.png"]: {sw.ids["d1.tif"]},
            sw.ids["d1.tif"]: {sw.ids["s1a.png"]},
        }
        assert_rebuild_changes_nothing(sw)

    def test_3_duplicate_to_unique_withdraws_the_untouched_partner(self, sw):
        sw.add_batch([("d1.tif", "100121", "1", 10)])
        assert sw.ids["s1a.png"] in live(sw.database)
        correct(sw, "d1.tif", "100559")
        state, _value, _related = records(sw.database)[sw.ids["s1a.png"]]
        assert state == ConflictState.WITHDRAWN.value
        assert_rebuild_changes_nothing(sw)

    def test_4_a_group_of_two_grows_to_three(self, sw):
        sw.add_batch([("d1.tif", "100121", "1", 10)])
        sw.add_batch([("d2.tif", "100777", "1", 11)])
        correct(sw, "d2.tif", "100121")
        group = {sw.ids[name] for name in ("s1a.png", "d1.tif", "d2.tif")}
        found = live(sw.database)
        assert {scan for scan in found if scan in group} == group
        for scan in group:
            assert found[scan] == group - {scan}
        assert_rebuild_changes_nothing(sw)

    def test_5_a_group_of_three_shrinks_to_two(self, sw):
        sw.add_batch([("d1.tif", "100121", "1", 10)])
        sw.add_batch([("d2.tif", "100121", "1", 11)])
        correct(sw, "d2.tif", "100778")
        found = live(sw.database)
        assert found[sw.ids["s1a.png"]] == {sw.ids["d1.tif"]}
        assert found[sw.ids["d1.tif"]] == {sw.ids["s1a.png"]}
        assert_rebuild_changes_nothing(sw)

    def test_6_undoing_the_correction_restores_the_group(self, sw):
        later = sw.add_batch([("d1.tif", "100121", "1", 10)])
        before = live(sw.database)
        edit = correct(sw, "d1.tif", "100559")
        assert sw.ids["s1a.png"] not in live(sw.database)
        review_store.undo_field_edit(
            sw.database, batch_id=later, group=edit.group, reviewer=OPERATOR
        )
        assert live(sw.database) == before
        assert_rebuild_changes_nothing(sw)

    def test_8_a_rejected_sheet_leaves_its_group(self, sw):
        sw.add_batch([("d1.tif", "100121", "1", 10)])
        reject(sw.world, "d1.tif")
        state, _value, _related = records(sw.database)[sw.ids["s1a.png"]]
        assert state == ConflictState.WITHDRAWN.value
        # The rejected sheet's own record is kept as it was, hidden.
        assert records(sw.database)[sw.ids["d1.tif"]][0] != ConflictState.WITHDRAWN.value
        assert_rebuild_changes_nothing(sw)

    def test_9_a_restored_sheet_enters_its_group_again(self, sw):
        sw.add_batch([("d1.tif", "100121", "1", 10)])
        reject(sw.world, "d1.tif")
        scan_lifecycle.undo_reject(sw.database, sw.ids["d1.tif"], reviewer=OPERATOR)
        assert {sw.ids["s1a.png"], sw.ids["d1.tif"]} <= set(live(sw.database))
        assert_rebuild_changes_nothing(sw)

    def test_8b_a_superseded_original_leaves_and_its_rescan_is_no_duplicate(self, sw):
        sw.add_batch([("d1.tif", "100121", "1", 10)])
        scan_lifecycle.reject_scan(
            sw.database, sw.ids["s1a.png"], reviewer=OPERATOR,
            reason=scan_lifecycle.RejectionReason.FOLDED,
        )
        sw.add_batch([("r1.tif", "100121", "1", 20)])
        scan_lifecycle.confirm_replacement(
            sw.database, sw.ids["s1a.png"], sw.ids["r1.tif"], reviewer=OPERATOR
        )
        # The rescan and d1 are the two effective sheets reading 100121.
        assert live(sw.database).get(sw.ids["r1.tif"]) == {sw.ids["d1.tif"]}
        # The superseded original is in nobody's group any more.
        assert all(sw.ids["s1a.png"] not in related for related in live(sw.database).values())
        assert_rebuild_changes_nothing(sw)

    def test_10_reopening_after_edits_is_identical(self, sw):
        sw.add_batch([("d1.tif", "100121", "1", 10)])
        sw.add_batch([("d2.tif", "100777", "1", 11)])
        correct(sw, "d2.tif", "100121")
        correct(sw, "d1.tif", "100555")
        before, events = records(sw.database), audit_count(sw.database)
        root = sw.world.session.root
        sw.world.session.close()
        with open_project(root) as reopened:
            assert records(reopened.database) == before
            assert audit_count(reopened.database) == events

    def test_11_untouched_groups_receive_no_writes(self, sw):
        sw.add_batch([("x1.tif", "100900", "1", 10), ("x2.tif", "100900", "1", 11)])
        sw.add_batch([("d1.tif", "100555", "1", 12)])
        with sw.database.session() as session:
            untouched = {
                row.scan_id: (row.updated_at, row.state, row.related_scan_ids)
                for row in session.scalars(
                    select(ReviewConflict).where(
                        ReviewConflict.scan_id.in_([sw.ids["x1.tif"], sw.ids["x2.tif"]])
                    )
                ).all()
            }
        events = audit_count(sw.database)
        correct(sw, "d1.tif", "100121")
        with sw.database.session() as session:
            after = {
                row.scan_id: (row.updated_at, row.state, row.related_scan_ids)
                for row in session.scalars(
                    select(ReviewConflict).where(
                        ReviewConflict.scan_id.in_([sw.ids["x1.tif"], sw.ids["x2.tif"]])
                    )
                ).all()
            }
            touched_scans = {
                int(item)
                for item in session.scalars(
                    select(AuditEvent.scan_id).where(AuditEvent.event_id > events)
                ).all()
                if item is not None
            }
        assert after == untouched
        assert not touched_scans & {sw.ids["x1.tif"], sw.ids["x2.tif"]}

    def test_12_repeating_the_bounded_pass_writes_nothing(self, sw):
        sw.add_batch([("d1.tif", "100121", "1", 10)])
        correct(sw, "d1.tif", "100555")
        before, events = records(sw.database), audit_count(sw.database)
        for _ in range(3):
            review_store.sync_duplicate_identifiers_for(
                sw.database, [sw.ids["d1.tif"], sw.ids["s1a.png"]]
            )
        assert records(sw.database) == before
        assert audit_count(sw.database) == events

    def test_7_another_session_is_never_in_reach(self, sw):
        other = scan_sessions.create_scan_session(sw.database, name="Other", activate=False)
        sw.add_batch([("o1.tif", "100555", "1", 10)], scan_session_id=other.scan_session_id)
        sw.add_batch([("d1.tif", "100777", "1", 10)])
        correct(sw, "d1.tif", "100555")
        assert sw.ids["o1.tif"] not in live(sw.database)
        assert sw.ids["d1.tif"] not in live(sw.database)


# ----------------------------------------------------------------------
# Instrumented: one change in a large session visits only its groups
# ----------------------------------------------------------------------
def _bulk_batch(sw: SessionWorld, rolls: list[str]) -> str:
    """Thousands of read sheets written directly (no files are needed)."""
    moment = datetime.now(UTC)
    batch_id = batch_store.new_batch_id()
    folder = sw.world.scans_dir / "bulk"
    template = json.dumps(make_result(folder / "x.tif", "000000", "1", 10).to_dict())
    with sw.database.session() as session:
        session.add(
            ScanBatch(
                batch_id=batch_id, created_at=moment, updated_at=moment,
                source_folder=str(folder), status="completed", total_scans=len(rolls),
                scan_session_id=sw.scan_session_id, sealed_at=moment,
            )
        )
        session.flush()
        session.add_all(
            BatchScan(
                batch_id=batch_id, batch_index=index,
                source_path=str(folder / f"b{index:06d}.tif"), filename=f"b{index:06d}.tif",
                status="completed", identifier_value=roll, set_code_value="1",
                result_json=template,
            )
            for index, roll in enumerate(rolls)
        )
    sw.batches.append(batch_id)
    return batch_id


def test_one_correction_in_a_large_session_touches_only_its_groups(sw, monkeypatch):
    size = 6_000
    # 60 duplicate pairs spread through the batch; everyone else unique.
    rolls = [f"{500000 + (index // 2 if index < 120 else index):06d}" for index in range(size)]
    _bulk_batch(sw, rolls)
    review_store.sync_duplicate_identifiers(sw.database, sw.batches[0])  # the full rebuild
    pairs_before = live(sw.database)
    assert len(pairs_before) == 120

    seen: list[int] = []
    real = review_store.detect_duplicate_identifiers

    def spy(identifiers):
        seen.append(len(identifiers))
        return real(identifiers)

    monkeypatch.setattr(review_store, "detect_duplicate_identifiers", spy)
    events = audit_count(sw.database)
    # d1 joins pair 500003 (a third member) from another batch.
    sw.add_batch([("d1.tif", "100555", "1", 10)])
    seen.clear()
    correct(sw, "d1.tif", "500003")
    assert seen and max(seen) <= 5, seen  # the 500003 group (3) - never the 6,000
    scope = review_store.sync_duplicate_identifiers_for(sw.database, [sw.ids["d1.tif"]])
    assert scope.candidates < 20 and scope.records <= 5, scope
    after = live(sw.database)
    changed = {
        scan for scan in set(after) | set(pairs_before) if after.get(scan) != pairs_before.get(scan)
    }
    third = sw.ids["d1.tif"]
    assert third in changed and len(changed) == 3  # d1 and the pair it joined
    # Only those three sheets' records were written (plus d1's own decision).
    with sw.database.session() as session:
        written = {
            int(item)
            for item in session.scalars(
                select(AuditEvent.scan_id).where(AuditEvent.event_id > events)
            ).all()
            if item is not None
        }
    assert written <= changed

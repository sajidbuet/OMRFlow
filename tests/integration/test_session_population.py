"""0.1.1 phase 4: one scan session, several batches, one effective population.

Scope:
    The real-project world of ``test_reject_and_rescan.py`` - three sets, one
    batch, attendance workbooks, verified keys - placed in a scan session, and
    joined by further batches of the same session. Every assertion is on the
    services and the database, and on *identity sets* (which scans, which
    candidates), not row counts alone.

    * A - dispositions and the population key;
    * B - the duplicate-ID matrix across batches;
    * C - Resolve reads the session;
    * D/E/F - Attendance, scoring and Results over the session;
    * H - reopening changes nothing;
    * I - another scan session changes nothing;
    * Project Health on corrupted lineage.

Privacy:
    Every identifier here is fictional.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select, update
from tests.integration.test_reject_and_rescan import (
    OPERATOR,
    TEMPLATE,
    World,
    build_world,
    make_result,
    reject,
)

from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    ReconciliationRun,
    ReviewConflict,
    ScanBatch,
    ScanRejection,
)
from omr_scanner.domain.review import (
    ConflictState,
    ConflictType,
    FieldKind,
    MachineObservation,
    ReasonCode,
)
from omr_scanner.domain.scan_lifecycle import LifecycleState, RejectionReason
from omr_scanner.domain.session_population import SheetDisposition
from omr_scanner.services import (
    batch_store,
    create_project,
    open_project,
    project_health,
    reconciliation_store,
    review_store,
    scan_lifecycle,
    scan_provenance,
    scan_sessions,
    scoring_store,
    session_population,
)
from omr_scanner.services.scan_lifecycle import LifecycleError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

D = SheetDisposition


class SessionWorld:
    """The rescan world, its first batch in a scan session, and helpers."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.scan_session_id = scan_sessions.create_scan_session(
            world.database, name="Midterm", created_by=OPERATOR
        ).scan_session_id
        self._clock = datetime.now(UTC)
        with world.database.session() as session:
            session.execute(
                update(ScanBatch)
                .where(ScanBatch.batch_id == world.batch_id)
                .values(
                    scan_session_id=self.scan_session_id,
                    sealed_at=self._clock,
                    created_at=self._clock,
                )
            )
        self.batches = [world.batch_id]

    @property
    def database(self):
        return self.world.database

    @property
    def ids(self) -> dict[str, int]:
        return self.world.ids

    def add_batch(
        self,
        rows: list[tuple[str, str, str, int]],
        *,
        scan_session_id: str | None = None,
        status: str = "completed",
    ) -> str:
        """Another sealed batch of the session (or of ``scan_session_id``)."""
        scans_dir = self.world.scans_dir / "later"
        scans_dir.mkdir(exist_ok=True)
        self._clock += timedelta(seconds=1)
        batch_id = batch_store.new_batch_id()
        with self.database.session() as session:
            session.add(
                ScanBatch(
                    batch_id=batch_id, created_at=self._clock, updated_at=self._clock,
                    source_folder=str(scans_dir), status="completed", total_scans=len(rows),
                    scan_session_id=scan_session_id or self.scan_session_id,
                    sealed_at=self._clock,
                )
            )
            session.flush()
            for index, (name, roll, code, correct) in enumerate(rows):
                path = scans_dir / name
                path.write_bytes(f"later:{name}".encode())
                session.add(
                    BatchScan(
                        batch_id=batch_id, batch_index=index, source_path=str(path),
                        filename=name, status=status, identifier_value=roll,
                        set_code_value=code,
                        result_json=json.dumps(
                            make_result(path, roll, code, correct).to_dict()
                        ),
                    )
                )
        scan_provenance.compute_hashes_for_batch(self.database, batch_id)
        for path, scan_id in batch_store.scan_ids_by_path(self.database, batch_id).items():
            self.ids[path.name] = scan_id
        # What the Scan stage runs after every batch.
        scan_lifecycle.sync_reimports(self.database, batch_id)
        review_store.sync_duplicate_identifiers(self.database, batch_id)
        review_store.sync_undefined_set_codes(self.database, batch_id)
        if scan_session_id is None:
            self.batches.append(batch_id)
        return batch_id

    def population(self, batch_id: str | None = None):
        return session_population.population(self.database, batch_id or self.batches[0])

    def duplicated(self) -> set[int]:
        """Scans holding a live duplicate-ID record, anywhere in the project."""
        with self.database.session() as session:
            return {
                int(item)
                for item in session.scalars(
                    select(ReviewConflict.scan_id)
                    .where(
                        ReviewConflict.conflict_type
                        == ConflictType.IDENTIFIER_DUPLICATE.value
                    )
                    .where(ReviewConflict.state != ConflictState.WITHDRAWN.value)
                ).all()
            }

    def scripts(self, code: str, batch_id: str | None = None) -> dict[str, set[int]]:
        """``candidate -> scan ids`` filed under each entry of one set's roster."""
        roster = self.world.rosters[code]
        batch = batch_id or self.batches[-1]
        reconciliation_store.reconcile_batch(self.database, roster, batch)
        return {
            entry.candidate_id: {view.script.scan_id for view in entry.scripts}
            for entry in reconciliation_store.list_entries(self.database, roster, batch)
        }

    def scored(self, code: str, batch_id: str | None = None) -> dict[str, int | None]:
        roster = self.world.rosters[code]
        batch = batch_id or self.batches[-1]
        reconciliation_store.reconcile_batch(self.database, roster, batch)
        scoring_store.score_batch(self.database, roster, batch, TEMPLATE, computed_by=OPERATOR)
        return {
            item.candidate_id: item.scan_id
            for item in scoring_store.list_results(self.database, roster, batch, TEMPLATE)
        }

    def roll_field(self, name: str, value: str, previous: str) -> None:
        """A reviewer's correction of one sheet's whole Student ID."""
        scan_id = self.ids[name]
        batch = scan_lifecycle.batch_of(self.database, scan_id)
        assert batch is not None
        review_store.correct_field(
            self.database, batch_id=batch, scan_id=scan_id, zone_id="roll_number",
            values=dict(enumerate(value)), display_value=value, field_label="Roll",
            reviewer=OPERATOR, reason=ReasonCode.MISCLASSIFICATION,
            overrides={
                index: MachineObservation(value=char, status="complete", confidence=0.9)
                for index, char in enumerate(previous)
            },
            field_kind=FieldKind.IDENTIFIER, previous_value=previous,
        )


@pytest.fixture
def sw(workspace: Path, tmp_path: Path) -> Iterator[SessionWorld]:
    session = create_project(workspace, "Session Population")
    try:
        yield SessionWorld(build_world(session, tmp_path))
    finally:
        if not session.is_closed:
            session.close()


def chain(sw: SessionWorld) -> tuple[int, int, int]:
    """s2b rejected, rescanned in batch 2, that rescan rejected, rescanned in batch 3."""
    a = sw.ids["s2b.png"]
    reject(sw.world, "s2b.png")
    sw.add_batch([("R1.tif", "200122", "2", 20)])
    b = sw.ids["R1.tif"]
    scan_lifecycle.confirm_replacement(sw.database, a, b, reviewer=OPERATOR)
    scan_lifecycle.reject_scan(
        sw.database, b, reviewer=OPERATOR, reason=RejectionReason.FOLDED
    )
    sw.add_batch([("R2.tif", "200122", "2", 30)])
    c = sw.ids["R2.tif"]
    scan_lifecycle.confirm_replacement(sw.database, b, c, reviewer=OPERATOR)
    return a, b, c


# ----------------------------------------------------------------------
# A. Dispositions and the population key
# ----------------------------------------------------------------------
class TestDispositions:
    def test_every_sheet_of_the_session_has_exactly_one_disposition(self, sw):
        reject(sw.world, "s1a.png")
        scan_lifecycle.exclude_scan(
            sw.database, sw.ids["s3a.png"], reviewer=OPERATOR, reason=RejectionReason.FOLDED
        )
        scan_lifecycle.defer_scan(sw.database, sw.ids["s3b.png"], reviewer=OPERATOR)
        second = sw.add_batch([("b2a.tif", "100777", "1", 10)])
        sw.add_batch([("b3a.tif", "100778", "1", 10)], status="failed")
        sw.add_batch([("b4a.tif", "100779", "1", 10)], status="pending")
        population = sw.population()
        with sw.database.session() as session:
            every = set(
                session.scalars(
                    select(BatchScan.scan_id).where(BatchScan.batch_id.in_(sw.batches))
                ).all()
            )
        assert set(population.dispositions) == every
        assert population.dispositions[sw.ids["s1a.png"]] is D.REJECTED_PENDING_RESCAN
        assert population.dispositions[sw.ids["s3a.png"]] is D.EXCLUDED
        assert population.dispositions[sw.ids["s3b.png"]] is D.DEFERRED
        assert population.dispositions[sw.ids["b2a.tif"]] is D.EFFECTIVE
        assert population.dispositions[sw.ids["b3a.tif"]] is D.EFFECTIVE_UNREADABLE
        assert population.dispositions[sw.ids["b4a.tif"]] is D.NOT_READ
        assert population.batch_of[sw.ids["b2a.tif"]] == second
        assert sw.ids["s1a.png"] in population.listed
        assert sw.ids["s1a.png"] not in population.effective
        assert sw.ids["b4a.tif"] not in population.reconciled

    def test_every_batch_of_the_session_names_the_same_population(self, sw):
        sw.add_batch([("b2a.tif", "100777", "1", 10)])
        sw.add_batch([("b3a.tif", "100778", "1", 10)])
        first = sw.population(sw.batches[0])
        for batch in sw.batches[1:]:
            other = sw.population(batch)
            assert other.key_batch_id == first.key_batch_id == sw.batches[0]
            assert other.effective == first.effective
            assert other.dispositions == first.dispositions
        assert (
            scan_sessions.downstream_batch_id(sw.database) == sw.batches[0]
        ), "the active session's key is the batch already holding its reconciliation"

    def test_the_key_does_not_move_when_batches_are_added(self, sw):
        key = session_population.population_key(sw.database, sw.batches[0])
        for index in range(3):
            sw.add_batch([(f"k{index}.tif", f"10090{index}", "1", 10)])
            assert session_population.population_key(sw.database, sw.batches[-1]) == key

    def test_a_three_generation_lineage_counts_only_its_newest_sheet(self, sw):
        a, b, c = chain(sw)
        population = sw.population()
        assert population.dispositions[a] is D.SUPERSEDED_BY_REPLACEMENT
        assert population.dispositions[b] is D.SUPERSEDED_BY_REPLACEMENT
        assert population.dispositions[c] is D.EFFECTIVE
        assert population.lineage_root[c] == a
        assert population.lineage_root[b] == a
        assert sw.scripts("2")["200122"] == {c}
        assert sw.scored("2")["200122"] == c
        # History: every generation's recognition row is still there.
        with sw.database.session() as session:
            assert session.scalar(
                select(func.count()).select_from(BatchScan).where(
                    BatchScan.scan_id.in_([a, b, c])
                )
            ) == 3

    def test_rejecting_the_newest_replacement_counts_no_generation(self, sw):
        a, b, c = chain(sw)
        scan_lifecycle.reject_scan(
            sw.database, c, reviewer=OPERATOR, reason=RejectionReason.FOLDED
        )
        population = sw.population()
        assert {population.dispositions[item] for item in (a, b)} == {
            D.SUPERSEDED_BY_REPLACEMENT
        }
        assert population.dispositions[c] is D.REJECTED_PENDING_RESCAN
        assert not {a, b, c} & population.effective
        assert sw.scripts("2")["200122"] <= {c}  # listed as outstanding, never scored
        assert sw.scored("2").get("200122") not in {a, b, c}
        outstanding = scan_lifecycle.outstanding_for_set(sw.database, sw.batches[-1], "2")
        assert [item.scan_id for item in outstanding] == [c]

    def test_undoing_a_rejection_restores_the_sheet(self, sw):
        reject(sw.world, "s1a.png")
        assert sw.ids["s1a.png"] not in sw.population().effective
        scan_lifecycle.undo_reject(sw.database, sw.ids["s1a.png"], reviewer=OPERATOR)
        assert sw.population().dispositions[sw.ids["s1a.png"]] is D.EFFECTIVE

    def test_a_replacement_from_another_session_is_refused(self, sw):
        reject(sw.world, "s2b.png")
        other = scan_sessions.create_scan_session(sw.database, name="Final", activate=False)
        sw.add_batch(
            [("X1.tif", "200122", "2", 20)], scan_session_id=other.scan_session_id
        )
        with pytest.raises(LifecycleError):
            scan_lifecycle.confirm_replacement(
                sw.database, sw.ids["s2b.png"], sw.ids["X1.tif"], reviewer=OPERATOR
            )
        assert scan_lifecycle.state_of(sw.database, sw.ids["s2b.png"]) is (
            LifecycleState.REJECTED_PENDING_RESCAN
        )

    def test_the_population_is_rebuilt_from_the_database_alone(self, sw):
        chain(sw)
        before = sw.population()
        # E9: no image is read to rebuild it - every file can be gone.
        for path in sw.world.scans_dir.rglob("*"):
            if path.is_file():
                path.unlink()
        after = sw.population()
        assert after.dispositions == before.dispositions
        assert after.effective == before.effective


# ----------------------------------------------------------------------
# B. Duplicate IDs across the session
# ----------------------------------------------------------------------
class TestDuplicateMatrix:
    def test_1_within_one_batch(self, sw):
        sw.add_batch([("d1.tif", "100555", "1", 10), ("d2.tif", "100555", "1", 11)])
        assert {sw.ids["d1.tif"], sw.ids["d2.tif"]} <= sw.duplicated()

    def test_2_across_two_batches(self, sw):
        later = sw.add_batch([("d1.tif", "100121", "1", 10)])
        assert {sw.ids["s1a.png"], sw.ids["d1.tif"]} <= sw.duplicated()
        with sw.database.session() as session:
            rows = {
                row.scan_id: row.batch_id
                for row in session.scalars(
                    select(ReviewConflict).where(
                        ReviewConflict.conflict_type
                        == ConflictType.IDENTIFIER_DUPLICATE.value
                    )
                ).all()
            }
        # Each record lives in the batch its sheet was read into.
        assert rows[sw.ids["s1a.png"]] == sw.batches[0]
        assert rows[sw.ids["d1.tif"]] == later

    def test_3_across_three_batches(self, sw):
        sw.add_batch([("d1.tif", "100121", "1", 10)])
        sw.add_batch([("d2.tif", "100121", "1", 11)])
        group = {sw.ids[name] for name in ("s1a.png", "d1.tif", "d2.tif")}
        assert group <= sw.duplicated()
        with sw.database.session() as session:
            related = session.scalars(
                select(ReviewConflict.related_scan_ids).where(
                    ReviewConflict.scan_id == sw.ids["s1a.png"],
                    ReviewConflict.conflict_type == ConflictType.IDENTIFIER_DUPLICATE.value,
                )
            ).one()
        assert {int(item) for item in related.split(",")} == group - {sw.ids["s1a.png"]}

    def test_4_a_confirmed_cross_batch_rescan_is_no_duplicate(self, sw):
        reject(sw.world, "s2b.png")
        sw.add_batch([("R1.tif", "200122", "2", 20)])
        scan_lifecycle.confirm_replacement(
            sw.database, sw.ids["s2b.png"], sw.ids["R1.tif"], reviewer=OPERATOR
        )
        assert not {sw.ids["s2b.png"], sw.ids["R1.tif"]} & sw.duplicated()

    def test_5_a_rejected_original_and_its_unlinked_rescan_are_no_duplicate(self, sw):
        reject(sw.world, "s2b.png")
        sw.add_batch([("R1.tif", "200122", "2", 20)])
        # Not linked yet: the rejected sheet does not count, so nothing collides.
        assert not {sw.ids["s2b.png"], sw.ids["R1.tif"]} & sw.duplicated()

    def test_6_an_excluded_original_and_an_independent_duplicate(self, sw):
        scan_lifecycle.exclude_scan(
            sw.database, sw.ids["s1a.png"], reviewer=OPERATOR, reason=RejectionReason.FOLDED
        )
        sw.add_batch([("d1.tif", "100121", "1", 10)])
        sw.add_batch([("d2.tif", "100121", "1", 11)])
        flagged = sw.duplicated()
        assert {sw.ids["d1.tif"], sw.ids["d2.tif"]} <= flagged

    def test_7_and_8_a_deferred_sheet_then_restored(self, sw):
        scan_lifecycle.defer_scan(sw.database, sw.ids["s1a.png"], reviewer=OPERATOR)
        sw.add_batch([("d1.tif", "100121", "1", 10)])
        assert sw.ids["d1.tif"] not in sw.duplicated()
        scan_lifecycle.restore_scan(sw.database, sw.ids["s1a.png"], reviewer=OPERATOR)
        assert {sw.ids["s1a.png"], sw.ids["d1.tif"]} <= sw.duplicated()

    def test_9_a_correction_creates_a_cross_batch_duplicate(self, sw):
        sw.add_batch([("d1.tif", "100129", "1", 10)])
        assert sw.ids["d1.tif"] not in sw.duplicated()
        sw.roll_field("d1.tif", "100121", "100129")
        assert {sw.ids["s1a.png"], sw.ids["d1.tif"]} <= sw.duplicated()

    def test_10_a_correction_clears_a_cross_batch_duplicate(self, sw):
        sw.add_batch([("d1.tif", "100121", "1", 10)])
        assert {sw.ids["s1a.png"], sw.ids["d1.tif"]} <= sw.duplicated()
        sw.roll_field("d1.tif", "100129", "100121")
        assert not {sw.ids["s1a.png"], sw.ids["d1.tif"]} & sw.duplicated()

    def test_11_reopening_gives_the_same_duplicate_state(self, sw):
        sw.add_batch([("d1.tif", "100121", "1", 10)])
        sw.add_batch([("d2.tif", "100555", "1", 10), ("d3.tif", "100555", "1", 11)])

        def snapshot(database) -> tuple[list[tuple], int]:
            with database.session() as session:
                return sorted(
                    (row.conflict_id, row.scan_id, row.batch_id, row.state, row.related_scan_ids)
                    for row in session.scalars(
                        select(ReviewConflict).where(
                            ReviewConflict.conflict_type
                            == ConflictType.IDENTIFIER_DUPLICATE.value
                        )
                    ).all()
                ), session.scalar(select(func.count()).select_from(AuditEvent))

        before = snapshot(sw.database)
        root = sw.world.session.root
        sw.world.session.close()
        for _ in range(2):
            with open_project(root) as reopened:
                assert snapshot(reopened.database) == before

    def test_12_reconciling_again_is_idempotent(self, sw):
        sw.add_batch([("d1.tif", "100121", "1", 10)])
        with sw.database.session() as session:
            events = session.scalar(select(func.count()).select_from(AuditEvent))
            rows = session.scalar(select(func.count()).select_from(ReviewConflict))
        for batch in sw.batches:
            review_store.sync_duplicate_identifiers(sw.database, batch)
        with sw.database.session() as session:
            assert session.scalar(select(func.count()).select_from(AuditEvent)) == events
            assert session.scalar(select(func.count()).select_from(ReviewConflict)) == rows


# ----------------------------------------------------------------------
# C. Resolve reads the session
# ----------------------------------------------------------------------
class TestResolve:
    def test_the_session_queue_holds_every_batchs_conflicts(self, sw):
        later = sw.add_batch([("d1.tif", "100121", "1", 10)])
        wide = {
            item.scan_id
            for item in review_store.list_conflicts(sw.database, sw.batches[0], session_wide=True)
        }
        assert sw.ids["d1.tif"] in wide and sw.ids["s1a.png"] in wide
        assert wide == {
            item.scan_id
            for item in review_store.list_conflicts(sw.database, later, session_wide=True)
        }
        narrow = {
            item.scan_id for item in review_store.list_conflicts(sw.database, sw.batches[0])
        }
        assert sw.ids["d1.tif"] not in narrow
        # Without rescans the session's count is its batches' counts added up.
        wide_counts = review_store.count_conflicts(sw.database, later, session_wide=True)
        per_batch = [review_store.count_conflicts(sw.database, item) for item in sw.batches]
        assert wide_counts.total == sum(item.total for item in per_batch)
        assert wide_counts.open_count == sum(item.open_count for item in per_batch)

    def test_a_superseded_sheets_conflicts_leave_the_queue(self, sw):
        a, b, _c = chain(sw)
        queue = {
            item.scan_id
            for item in review_store.list_conflicts(sw.database, sw.batches[0], session_wide=True)
        }
        assert not {a, b} & queue


# ----------------------------------------------------------------------
# D/E/F. Attendance, scoring and Results over the session
# ----------------------------------------------------------------------
class TestDownstream:
    def test_attendance_and_scoring_use_the_whole_session(self, sw):
        reject(sw.world, "s1b.png")
        sw.add_batch([("R1b.tif", "100122", "1", 20)])
        scan_lifecycle.confirm_replacement(
            sw.database, sw.ids["s1b.png"], sw.ids["R1b.tif"], reviewer=OPERATOR
        )
        sw.add_batch([("late3.tif", "300122", "3", 9)])
        expected = {
            "100121": {sw.ids["s1a.png"]},
            "100122": {sw.ids["R1b.tif"]},
        }
        for batch in sw.batches:
            assert sw.scripts("1", batch) == expected
            assert sw.scored("1", batch) == {
                candidate: next(iter(scans)) for candidate, scans in expected.items()
            }
        # The later batch's set-3 sheet joins set 3 as a second script.
        assert sw.scripts("3")["300122"] == {sw.ids["s3b.png"], sw.ids["late3.tif"]}

    def test_every_downstream_stage_sees_the_same_scans(self, sw):
        a, b, c = chain(sw)
        sw.add_batch([("late1.tif", "100555", "1", 10)])
        population = sw.population()
        filed: set[int] = set()
        for code in "123":
            for scans in sw.scripts(code).values():
                filed |= scans
        assert filed <= population.reconciled
        scored = {
            scan_id
            for code in "123"
            for scan_id in sw.scored(code).values()
            if scan_id is not None
        }
        assert scored <= population.effective
        assert c in scored and not {a, b} & scored


class TestReadiness:
    def test_a_final_export_waits_for_every_batch_of_the_session(self, sw):
        from omr_scanner.domain.reporting import ReadinessIssueKind
        from omr_scanner.services import report_store

        def unread_issues(batch: str) -> list[str]:
            report = report_store.check_readiness(
                sw.database, sw.world.rosters["1"], batch, TEMPLATE, "1",
                for_final_export=True,
            )
            return [
                item.message
                for item in report.issues
                if item.kind is ReadinessIssueKind.SCORING_INCOMPLETE
                and "not been read yet" in item.message
            ]

        sw.scored("1")
        assert unread_issues(sw.batches[0]) == []
        later = sw.add_batch([("p1.tif", "100777", "1", 10)], status="pending")
        assert unread_issues(sw.batches[0]) == unread_issues(later)
        assert len(unread_issues(later)) == 1 and "1 sheet(s)" in unread_issues(later)[0]


# ----------------------------------------------------------------------
# I. Another scan session changes nothing
# ----------------------------------------------------------------------
class TestIsolation:
    def test_overlapping_ids_in_another_session_are_not_duplicates(self, sw):
        before = sw.scripts("1")
        other = scan_sessions.create_scan_session(sw.database, name="Final", activate=False)
        foreign = sw.add_batch(
            [("f1.tif", "100121", "1", 10), ("f2.tif", "100122", "1", 11)],
            scan_session_id=other.scan_session_id,
        )
        assert not {sw.ids["f1.tif"], sw.ids["f2.tif"], sw.ids["s1a.png"]} & sw.duplicated()
        assert sw.scripts("1", sw.batches[0]) == before
        mine, theirs = sw.population(), sw.population(foreign)
        assert not set(mine.dispositions) & set(theirs.dispositions)
        assert theirs.key_batch_id == foreign != mine.key_batch_id


# ----------------------------------------------------------------------
# Project Health
# ----------------------------------------------------------------------
def codes(sw: SessionWorld) -> set[str]:
    report = project_health.full_check(sw.database, sw.world.session.root)
    return {issue.code for issue in report.issues}


LINEAGE_CODES = {
    "LIFECYCLE_BATCH_MISMATCH",
    "DANGLING_REPLACEMENT",
    "CONTRADICTORY_REPLACEMENT_LINK",
    "CROSS_SESSION_REPLACEMENT",
    "RESCAN_LINEAGE_CYCLE",
    "SESSION_DOWNSTREAM_SPLIT",
}


class TestHealth:
    def test_a_valid_session_raises_no_lineage_finding(self, sw):
        chain(sw)
        assert not codes(sw) & LINEAGE_CODES

    def test_a_cycle_is_reported_and_not_repaired(self, sw):
        a, b, c = chain(sw)
        scan_lifecycle.reject_scan(sw.database, c, reviewer=OPERATOR, reason=RejectionReason.FOLDED)
        with sw.database.session() as session:
            session.execute(
                update(ScanRejection)
                .where(ScanRejection.scan_id == c)
                .values(state=LifecycleState.SUPERSEDED_BY_REPLACEMENT.value, replacement_scan_id=a)
            )
        assert "RESCAN_LINEAGE_CYCLE" in codes(sw)
        population = sw.population()  # never raises, never rewrites
        assert not {a, b, c} & population.effective
        with sw.database.session() as session:
            assert session.scalar(
                select(ScanRejection.replacement_scan_id).where(ScanRejection.scan_id == c)
            ) == a

    def test_a_dangling_and_a_contradictory_link(self, sw):
        a, b, _c = chain(sw)
        with sw.database.session() as session:
            session.execute(
                update(ScanRejection)
                .where(ScanRejection.scan_id == a)
                .values(replacement_scan_id=None)
            )
            session.execute(
                update(ScanRejection)
                .where(ScanRejection.scan_id == b)
                .values(state=LifecycleState.REJECTED_PENDING_RESCAN.value)
            )
        found = codes(sw)
        assert {"DANGLING_REPLACEMENT", "CONTRADICTORY_REPLACEMENT_LINK"} <= found

    def test_a_record_filed_under_the_wrong_batch(self, sw):
        reject(sw.world, "s1a.png")
        later = sw.add_batch([("x.tif", "100999", "1", 10)])
        with sw.database.session() as session:
            session.execute(
                update(ScanRejection)
                .where(ScanRejection.scan_id == sw.ids["s1a.png"])
                .values(batch_id=later)
            )
        assert "LIFECYCLE_BATCH_MISMATCH" in codes(sw)

    def test_a_legacy_cross_session_link_is_a_warning(self, sw):
        reject(sw.world, "s2b.png")
        later = sw.add_batch([("R1.tif", "200122", "2", 20)])
        scan_lifecycle.confirm_replacement(
            sw.database, sw.ids["s2b.png"], sw.ids["R1.tif"], reviewer=OPERATOR
        )
        other = scan_sessions.create_scan_session(sw.database, name="Final", activate=False)
        with sw.database.session() as session:
            session.execute(
                update(ScanBatch)
                .where(ScanBatch.batch_id == later)
                .values(scan_session_id=other.scan_session_id)
            )
        assert "CROSS_SESSION_REPLACEMENT" in codes(sw)
        # Honoured through the lineage root: counted in the original's session.
        assert sw.ids["R1.tif"] in sw.population(sw.batches[0]).effective
        assert sw.population(later).dispositions[sw.ids["R1.tif"]] is (
            D.COUNTED_IN_OTHER_SESSION
        )

    def test_downstream_state_split_across_batches(self, sw):
        later = sw.add_batch([("x.tif", "100999", "1", 10)])
        with sw.database.session() as session:
            run = session.scalars(select(ReconciliationRun).limit(1)).one()
            run.batch_id = later
        assert "SESSION_DOWNSTREAM_SPLIT" in codes(sw)

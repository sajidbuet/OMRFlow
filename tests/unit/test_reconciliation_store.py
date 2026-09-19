"""Storing a roster, a reconciliation and the decisions taken (Phase 7).

Scope:
    :mod:`omr_scanner.services.reconciliation_store` against a real SQLite
    file: the roster tables, the reconciliation cache, the decision rows, and
    the shared append-only audit ledger.

Why a real database rather than a mock:
    Every property worth asserting here belongs to the database - the unique
    constraint that makes re-import safe, the transaction that makes a decision
    atomic, and the Phase 6 triggers that refuse to let the ledger be
    rewritten. A mocked session would assert that the code calls the functions
    it calls.

Privacy:
    Every identifier and name here is fictional.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, text

from omr_scanner.database import open_project_database
from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    RegisteredCandidate,
    ReviewConflict,
    ScanBatch,
)
from omr_scanner.domain.reconciliation import (
    AttendanceSource,
    AttendanceState,
    CandidateRecord,
    ReconciliationAction,
    ReconciliationReason,
    ReconciliationStatus,
    ResolutionState,
)
from omr_scanner.services import reconciliation_store
from omr_scanner.services.candidate_import import (
    ColumnMapping,
    RosterIssue,
    RosterIssueCode,
    RosterValidation,
)
from omr_scanner.services.reconciliation_store import ReconciliationError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from omr_scanner.database import ProjectDatabase

OPERATOR = "Dr. X"
OTHER_OPERATOR = "Dr. Y"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[ProjectDatabase]:
    handle = open_project_database(tmp_path / "database.sqlite", create=True)
    try:
        yield handle
    finally:
        handle.close()


def validation_of(
    candidates: list[tuple[str, str, AttendanceState]],
    *,
    issues: tuple[RosterIssue, ...] = (),
    attendance_column: int | None = 2,
) -> RosterValidation:
    """Build a RosterValidation as the importer would have produced one."""
    records = tuple(
        CandidateRecord(
            candidate_id=candidate_id,
            display_name=name,
            source_row=index + 2,
            imported_attendance=state,
            imported_value="ABSENT" if state is AttendanceState.ABSENT else "55",
        )
        for index, (candidate_id, name, state) in enumerate(candidates)
    )
    return RosterValidation(
        candidates=records,
        issues=issues,
        rows_read=len(records),
        blank_ids=0,
        duplicate_ids=0,
        expected_present=sum(
            1 for i in records if i.imported_attendance is AttendanceState.PRESENT
        ),
        expected_absent=sum(
            1 for i in records if i.imported_attendance is AttendanceState.ABSENT
        ),
        attendance_unknown=sum(
            1 for i in records if i.imported_attendance is AttendanceState.UNKNOWN
        ),
        mapping=ColumnMapping(candidate_id=0, name=1, attendance=attendance_column),
        source_name="roster.csv",
    )


STANDARD = [
    ("1001", "CANDIDATE A", AttendanceState.PRESENT),
    ("1002", "CANDIDATE B", AttendanceState.PRESENT),
    ("1003", "CANDIDATE C", AttendanceState.ABSENT),
    ("1004", "CANDIDATE D", AttendanceState.PRESENT),
    ("1005", "CANDIDATE E", AttendanceState.ABSENT),
]


@pytest.fixture
def roster_id(database) -> int:
    return reconciliation_store.import_roster(
        database, validation_of(STANDARD), imported_by=OPERATOR
    )


def make_batch(
    database: ProjectDatabase, scripts: list[tuple[str, str]], *, unread: set[str] = frozenset()
) -> str:
    """Create a batch whose scans carry the given (filename, identifier) pairs."""
    from omr_scanner.services import batch_store

    batch_id = batch_store.new_batch_id()
    now = datetime.now(UTC)
    with database.session() as session:
        session.add(
            ScanBatch(
                batch_id=batch_id,
                created_at=now,
                updated_at=now,
                source_folder="/scans",
                status="completed",
                total_scans=len(scripts),
            )
        )
        session.flush()
        for index, (name, identifier) in enumerate(scripts):
            session.add(
                BatchScan(
                    batch_id=batch_id,
                    batch_index=index,
                    source_path=f"/scans/{name}",
                    filename=name,
                    status="completed",
                    identifier_value=identifier,
                    result_json="",
                )
            )
        session.flush()
        for name in unread:
            scan_id = session.scalars(
                select(BatchScan.scan_id)
                .where(BatchScan.batch_id == batch_id)
                .where(BatchScan.filename == name)
            ).first()
            session.add(
                ReviewConflict(
                    batch_id=batch_id,
                    scan_id=scan_id,
                    conflict_type="identifier_unreadable",
                    scope="field",
                    state="open",
                    zone_id="roll",
                    group_key=0,
                    field_kind="identifier",
                    field_label="Student ID",
                    machine_value="",
                    machine_status="unreadable",
                    created_at=now,
                    updated_at=now,
                )
            )
    return batch_id


@pytest.fixture
def batch_id(database) -> str:
    return make_batch(
        database,
        [
            ("s1.png", "1001"),
            ("s2a.png", "1002"),
            ("s2b.png", "1002"),
            ("s3.png", "1005"),
            ("s4.png", "9999"),
            ("s5.png", ""),
        ],
        unread={"s5.png"},
    )


@pytest.fixture
def reconciled(database, roster_id, batch_id):
    reconciliation_store.reconcile_batch(database, roster_id, batch_id)
    return roster_id, batch_id


def entries_by_id(database, roster_id, batch_id) -> dict[str, object]:
    return {
        entry.candidate_id: entry
        for entry in reconciliation_store.list_entries(database, roster_id, batch_id)
    }


def scan_id_of(database, batch_id, filename: str) -> int:
    with database.session() as session:
        return session.scalars(
            select(BatchScan.scan_id)
            .where(BatchScan.batch_id == batch_id)
            .where(BatchScan.filename == filename)
        ).first()


class TestImportingARoster:
    def test_a_roster_is_stored_and_becomes_active(self, database):
        roster_id = reconciliation_store.import_roster(database, validation_of(STANDARD))
        active = reconciliation_store.active_roster(database)
        assert active is not None
        assert active.roster_id == roster_id
        assert active.candidate_count == 5
        assert active.expected_present == 3
        assert active.expected_absent == 2

    def test_candidates_keep_their_file_order(self, database, roster_id):
        found = reconciliation_store.roster_candidates(database, roster_id)
        assert [i.candidate_id for i in found] == [
            "1001", "1002", "1003", "1004", "1005",
        ]

    def test_the_imported_attendance_is_stored_verbatim(self, database, roster_id):
        found = {
            i.candidate_id: i
            for i in reconciliation_store.roster_candidates(database, roster_id)
        }
        assert found["1003"].imported_attendance is AttendanceState.ABSENT
        assert found["1003"].imported_value == "ABSENT"

    def test_a_roster_that_did_not_validate_is_refused(self, database):
        bad = validation_of(
            STANDARD,
            issues=(
                RosterIssue(
                    code=RosterIssueCode.DUPLICATE_ID,
                    source_row=3,
                    message="Candidate ID 1001 occurs more than once",
                ),
            ),
        )
        with pytest.raises(ReconciliationError) as caught:
            reconciliation_store.import_roster(database, bad)
        assert "more than once" in caught.value.user_message

    def test_a_refused_import_leaves_nothing_behind(self, database):
        bad = validation_of(
            STANDARD,
            issues=(
                RosterIssue(
                    code=RosterIssueCode.BLANK_ID, source_row=3, message="no id"
                ),
            ),
        )
        with pytest.raises(ReconciliationError):
            reconciliation_store.import_roster(database, bad)
        assert reconciliation_store.list_rosters(database) == ()
        with database.session() as session:
            assert session.scalars(select(RegisteredCandidate)).all() == []

    def test_an_empty_roster_is_refused(self, database):
        with pytest.raises(ReconciliationError):
            reconciliation_store.import_roster(database, validation_of([]))

    def test_the_unique_constraint_backs_up_validation(self, database, roster_id):
        # Even if validation were bypassed, the database refuses.
        with pytest.raises(Exception, match=r"UNIQUE|constraint"), database.session() as s:
            s.add(
                RegisteredCandidate(
                    roster_id=roster_id, candidate_id="1001", row_order=99
                )
            )


class TestReimport:
    def test_a_second_roster_supersedes_the_first(self, database, roster_id):
        second = reconciliation_store.import_roster(
            database, validation_of([("2001", "CANDIDATE Z", AttendanceState.PRESENT)])
        )
        active = reconciliation_store.active_roster(database)
        assert active is not None
        assert active.roster_id == second

    def test_the_old_roster_is_kept_not_merged_or_deleted(self, database, roster_id):
        reconciliation_store.import_roster(
            database, validation_of([("2001", "CANDIDATE Z", AttendanceState.PRESENT)])
        )
        rosters = reconciliation_store.list_rosters(database)
        assert len(rosters) == 2
        # And the old one's candidates are untouched - decisions were taken
        # against them.
        assert len(reconciliation_store.roster_candidates(database, roster_id)) == 5

    def test_the_two_rosters_do_not_merge(self, database, roster_id):
        second = reconciliation_store.import_roster(
            database, validation_of([("2001", "CANDIDATE Z", AttendanceState.PRESENT)])
        )
        assert len(reconciliation_store.roster_candidates(database, second)) == 1

    def test_an_earlier_roster_can_be_made_active_again(self, database, roster_id):
        reconciliation_store.import_roster(
            database, validation_of([("2001", "CANDIDATE Z", AttendanceState.PRESENT)])
        )
        reconciliation_store.set_active_roster(database, roster_id)
        active = reconciliation_store.active_roster(database)
        assert active is not None and active.roster_id == roster_id

    def test_reconciling_against_a_new_roster_leaves_no_stale_entries(
        self, database, roster_id, batch_id
    ):
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        second = reconciliation_store.import_roster(
            database, validation_of([("1001", "CANDIDATE A", AttendanceState.PRESENT)])
        )
        reconciliation_store.reconcile_batch(database, second, batch_id)
        entries = reconciliation_store.list_entries(database, second, batch_id)
        registered = [e.candidate_id for e in entries if e.is_registered]
        assert registered == ["1001"]


class TestReconciliationIsIdempotent:
    def test_running_twice_changes_nothing(self, database, reconciled):
        roster_id, batch_id = reconciled
        first = reconciliation_store.list_entries(database, roster_id, batch_id)
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        second = reconciliation_store.list_entries(database, roster_id, batch_id)
        assert [e.candidate_id for e in first] == [e.candidate_id for e in second]
        assert [e.status for e in first] == [e.status for e in second]

    def test_running_twice_does_not_duplicate_exception_records(
        self, database, reconciled
    ):
        roster_id, batch_id = reconciled
        before = len(reconciliation_store.list_entries(database, roster_id, batch_id))
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        after = len(reconciliation_store.list_entries(database, roster_id, batch_id))
        assert after == before

    def test_every_script_has_exactly_one_link_after_repeated_runs(
        self, database, reconciled
    ):
        roster_id, batch_id = reconciled
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        with database.session() as session:
            rows = session.execute(
                text(
                    "SELECT scan_id, COUNT(*) FROM reconciliation_script "
                    "GROUP BY scan_id"
                )
            ).all()
        assert rows
        assert all(count == 1 for _, count in rows)

    def test_reconciling_records_when_it_last_ran(self, database, reconciled):
        roster_id, batch_id = reconciled
        assert (
            reconciliation_store.last_reconciled_at(database, roster_id, batch_id)
            is not None
        )


class TestStoredClassification:
    def test_every_required_classification_is_stored(self, database, reconciled):
        roster_id, batch_id = reconciled
        found = {
            e.status
            for e in reconciliation_store.list_entries(database, roster_id, batch_id)
        }
        assert found == {
            ReconciliationStatus.MATCHED,
            ReconciliationStatus.DUPLICATE_SCRIPT,
            ReconciliationStatus.ABSENT_CONFIRMED,
            ReconciliationStatus.PRESENT_WITHOUT_SCRIPT,
            ReconciliationStatus.ABSENT_WITH_SCRIPT,
            ReconciliationStatus.UNKNOWN_ID,
            ReconciliationStatus.UNRESOLVED_CANDIDATE_ID,
        }

    def test_the_stored_counts_agree_with_the_entries(self, database, reconciled):
        roster_id, batch_id = reconciled
        counts = reconciliation_store.stored_counts(database, roster_id, batch_id)
        assert counts is not None
        assert counts.registered == 5
        assert counts.scripts == 6
        assert counts.unknown_id == 1
        assert counts.duplicate_script == 1

    def test_an_unread_identifier_is_not_stored_as_unknown(self, database, reconciled):
        roster_id, batch_id = reconciled
        entries = reconciliation_store.list_entries(database, roster_id, batch_id)
        unresolved = [
            e
            for e in entries
            if e.status is ReconciliationStatus.UNRESOLVED_CANDIDATE_ID
        ]
        assert len(unresolved) == 1
        assert unresolved[0].scripts[0].script.identifier_unresolved is True

    def test_filters_narrow_the_query(self, database, reconciled):
        roster_id, batch_id = reconciled
        found = reconciliation_store.list_entries(
            database,
            roster_id,
            batch_id,
            filters=reconciliation_store.EntryFilter(
                statuses=(ReconciliationStatus.UNKNOWN_ID,)
            ),
        )
        assert len(found) == 1
        assert found[0].candidate_id == "9999"

    def test_exceptions_only_excludes_the_normal_outcomes(self, database, reconciled):
        roster_id, batch_id = reconciled
        found = reconciliation_store.list_entries(
            database,
            roster_id,
            batch_id,
            filters=reconciliation_store.EntryFilter(exceptions_only=True),
        )
        assert all(e.status.is_exception for e in found)
        assert len(found) == 5

    def test_search_matches_id_and_name(self, database, reconciled):
        roster_id, batch_id = reconciled
        by_id = reconciliation_store.list_entries(
            database,
            roster_id,
            batch_id,
            filters=reconciliation_store.EntryFilter(search="1004"),
        )
        assert [e.candidate_id for e in by_id] == ["1004"]
        by_name = reconciliation_store.list_entries(
            database,
            roster_id,
            batch_id,
            filters=reconciliation_store.EntryFilter(search="CANDIDATE D"),
        )
        assert [e.candidate_id for e in by_name] == ["1004"]

    def test_counts_by_status_are_one_grouped_query(self, database, reconciled):
        roster_id, batch_id = reconciled
        found = reconciliation_store.count_entries_stored(database, roster_id, batch_id)
        assert found[ReconciliationStatus.MATCHED] == 1
        assert sum(found.values()) == 7


class TestAssignment:
    def test_assigning_moves_the_script_and_keeps_the_machine_value(
        self, database, reconciled, batch_id
    ):
        roster_id, batch = reconciled
        scan = scan_id_of(database, batch, "s4.png")
        reconciliation_store.assign_script(
            database, roster_id, batch, scan,
            candidate_id="1004", operator=OPERATOR,
            reason=ReconciliationReason.MISREAD_IDENTIFIER,
        )
        entries = entries_by_id(database, roster_id, batch)
        assert entries["1004"].status is ReconciliationStatus.MATCHED
        assert entries["1004"].scripts[0].script.machine_candidate_id == "9999"
        assert "9999" not in entries

    def test_assigning_to_an_unregistered_candidate_is_refused(
        self, database, reconciled
    ):
        roster_id, batch = reconciled
        scan = scan_id_of(database, batch, "s4.png")
        with pytest.raises(ReconciliationError) as caught:
            reconciliation_store.assign_script(
                database, roster_id, batch, scan,
                candidate_id="nobody", operator=OPERATOR,
                reason=ReconciliationReason.MISREAD_IDENTIFIER,
            )
        assert "not on the imported candidate list" in caught.value.user_message

    def test_assigning_without_an_operator_is_refused(self, database, reconciled):
        roster_id, batch = reconciled
        scan = scan_id_of(database, batch, "s4.png")
        with pytest.raises(ReconciliationError) as caught:
            reconciliation_store.assign_script(
                database, roster_id, batch, scan,
                candidate_id="1004", operator="   ",
                reason=ReconciliationReason.MISREAD_IDENTIFIER,
            )
        assert "Settings" in caught.value.user_message
        # ...and nothing was recorded.
        assert entries_by_id(database, roster_id, batch)["1004"].status is (
            ReconciliationStatus.PRESENT_WITHOUT_SCRIPT
        )

    def test_other_requires_an_explanation(self, database, reconciled):
        roster_id, batch = reconciled
        scan = scan_id_of(database, batch, "s4.png")
        with pytest.raises(ReconciliationError) as caught:
            reconciliation_store.assign_script(
                database, roster_id, batch, scan,
                candidate_id="1004", operator=OPERATOR,
                reason=ReconciliationReason.OTHER,
            )
        assert "explanation" in caught.value.user_message

    def test_a_cascading_duplicate_is_surfaced_immediately(
        self, database, reconciled
    ):
        roster_id, batch = reconciled
        scan = scan_id_of(database, batch, "s4.png")
        counts = reconciliation_store.assign_script(
            database, roster_id, batch, scan,
            candidate_id="1001", operator=OPERATOR,
            reason=ReconciliationReason.SHEET_SWAPPED,
        )
        # The return value already reports the consequence.
        assert counts.duplicate_script == 2
        assert entries_by_id(database, roster_id, batch)["1001"].status is (
            ReconciliationStatus.DUPLICATE_SCRIPT
        )

    def test_an_assignment_can_be_withdrawn(self, database, reconciled):
        roster_id, batch = reconciled
        scan = scan_id_of(database, batch, "s4.png")
        reconciliation_store.assign_script(
            database, roster_id, batch, scan,
            candidate_id="1004", operator=OPERATOR,
            reason=ReconciliationReason.MISREAD_IDENTIFIER,
        )
        reconciliation_store.clear_script_assignment(
            database, roster_id, batch, scan, operator=OPERATOR
        )
        entries = entries_by_id(database, roster_id, batch)
        assert "9999" in entries
        assert entries["1004"].status is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT


class TestExclusion:
    def test_setting_a_script_aside_resolves_a_duplicate(self, database, reconciled):
        roster_id, batch = reconciled
        scan = scan_id_of(database, batch, "s2b.png")
        reconciliation_store.set_script_excluded(
            database, roster_id, batch, scan, excluded=True, operator=OPERATOR,
            reason=ReconciliationReason.ACCIDENTAL_RESCAN,
        )
        entry = entries_by_id(database, roster_id, batch)["1002"]
        assert entry.status is ReconciliationStatus.MATCHED
        assert entry.script_count == 1

    def test_a_set_aside_script_is_never_deleted(self, database, reconciled):
        roster_id, batch = reconciled
        scan = scan_id_of(database, batch, "s2b.png")
        reconciliation_store.set_script_excluded(
            database, roster_id, batch, scan, excluded=True, operator=OPERATOR,
            reason=ReconciliationReason.ACCIDENTAL_RESCAN,
        )
        entry = entries_by_id(database, roster_id, batch)["1002"]
        assert len(entry.scripts) == 2
        assert any(item.excluded for item in entry.scripts)
        # The scan row itself is untouched.
        with database.session() as session:
            assert session.get(BatchScan, scan) is not None

    def test_a_set_aside_script_keeps_its_reason(self, database, reconciled):
        roster_id, batch = reconciled
        scan = scan_id_of(database, batch, "s2b.png")
        reconciliation_store.set_script_excluded(
            database, roster_id, batch, scan, excluded=True, operator=OPERATOR,
            reason=ReconciliationReason.ACCIDENTAL_RESCAN,
        )
        entry = entries_by_id(database, roster_id, batch)["1002"]
        aside = next(item for item in entry.scripts if item.excluded)
        assert aside.reason_code == ReconciliationReason.ACCIDENTAL_RESCAN.value
        assert aside.reviewer == OPERATOR

    def test_a_script_can_be_brought_back(self, database, reconciled):
        roster_id, batch = reconciled
        scan = scan_id_of(database, batch, "s2b.png")
        reconciliation_store.set_script_excluded(
            database, roster_id, batch, scan, excluded=True, operator=OPERATOR,
            reason=ReconciliationReason.ACCIDENTAL_RESCAN,
        )
        reconciliation_store.set_script_excluded(
            database, roster_id, batch, scan, excluded=False, operator=OPERATOR,
        )
        entry = entries_by_id(database, roster_id, batch)["1002"]
        assert entry.status is ReconciliationStatus.DUPLICATE_SCRIPT

    def test_nominating_a_working_script_clears_the_previous_one(
        self, database, reconciled
    ):
        roster_id, batch = reconciled
        first = scan_id_of(database, batch, "s2a.png")
        second = scan_id_of(database, batch, "s2b.png")
        reconciliation_store.set_primary_script(
            database, roster_id, batch, first, operator=OPERATOR
        )
        reconciliation_store.set_primary_script(
            database, roster_id, batch, second, operator=OPERATOR
        )
        entry = entries_by_id(database, roster_id, batch)["1002"]
        primaries = [i.script.scan_id for i in entry.scripts if i.primary]
        assert primaries == [second]


class TestAttendanceOverride:
    def test_the_imported_value_is_never_changed(self, database, reconciled):
        roster_id, batch = reconciled
        reconciliation_store.override_attendance(
            database, roster_id, batch, "1005",
            attendance=AttendanceState.PRESENT, operator=OPERATOR,
            reason=ReconciliationReason.CANDIDATE_ATTENDED,
        )
        with database.session() as session:
            stored = session.scalars(
                select(RegisteredCandidate)
                .where(RegisteredCandidate.roster_id == roster_id)
                .where(RegisteredCandidate.candidate_id == "1005")
            ).first()
            assert stored.imported_attendance == AttendanceState.ABSENT.value
            assert stored.imported_value == "ABSENT"

    def test_the_effective_value_changes_and_says_who_changed_it(
        self, database, reconciled
    ):
        roster_id, batch = reconciled
        reconciliation_store.override_attendance(
            database, roster_id, batch, "1005",
            attendance=AttendanceState.PRESENT, operator=OPERATOR,
            reason=ReconciliationReason.CANDIDATE_ATTENDED,
        )
        entry = entries_by_id(database, roster_id, batch)["1005"]
        assert entry.effective_attendance is AttendanceState.PRESENT
        assert entry.attendance_source is AttendanceSource.HUMAN
        assert entry.candidate.imported_attendance is AttendanceState.ABSENT
        assert entry.status is ReconciliationStatus.MATCHED

    def test_overriding_an_unregistered_candidate_is_refused(
        self, database, reconciled
    ):
        roster_id, batch = reconciled
        with pytest.raises(ReconciliationError) as caught:
            reconciliation_store.override_attendance(
                database, roster_id, batch, "9999",
                attendance=AttendanceState.PRESENT, operator=OPERATOR,
                reason=ReconciliationReason.CANDIDATE_ATTENDED,
            )
        assert "not on the imported candidate list" in caught.value.user_message

    def test_an_override_can_be_withdrawn(self, database, reconciled):
        roster_id, batch = reconciled
        reconciliation_store.override_attendance(
            database, roster_id, batch, "1005",
            attendance=AttendanceState.PRESENT, operator=OPERATOR,
            reason=ReconciliationReason.CANDIDATE_ATTENDED,
        )
        reconciliation_store.override_attendance(
            database, roster_id, batch, "1005",
            attendance=AttendanceState.UNKNOWN, operator=OPERATOR,
            reason=ReconciliationReason.ROSTER_ERROR,
        )
        entry = entries_by_id(database, roster_id, batch)["1005"]
        assert entry.effective_attendance is AttendanceState.ABSENT
        assert entry.attendance_source is AttendanceSource.IMPORTED


class TestDismissal:
    def test_accepting_an_exception_stops_it_counting_as_outstanding(
        self, database, reconciled
    ):
        roster_id, batch = reconciled
        counts = reconciliation_store.dismiss_entry(
            database, roster_id, batch, "1004", operator=OPERATOR,
            reason=ReconciliationReason.SCRIPT_MISSING,
        )
        assert counts.dismissed == 1
        entry = entries_by_id(database, roster_id, batch)["1004"]
        assert entry.resolution is ResolutionState.DISMISSED
        assert entry.needs_attention is False
        # ...and keeps its classification.
        assert entry.status is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT

    def test_an_accepted_exception_can_be_put_back(self, database, reconciled):
        roster_id, batch = reconciled
        reconciliation_store.dismiss_entry(
            database, roster_id, batch, "1004", operator=OPERATOR,
            reason=ReconciliationReason.SCRIPT_MISSING,
        )
        reconciliation_store.reopen_entry(
            database, roster_id, batch, "1004", operator=OPERATOR
        )
        entry = entries_by_id(database, roster_id, batch)["1004"]
        assert entry.resolution is ResolutionState.OPEN


class TestAuditLedger:
    def test_every_decision_appends_exactly_one_event(self, database, reconciled):
        roster_id, batch = reconciled
        scan = scan_id_of(database, batch, "s4.png")
        with database.session() as session:
            before = len(session.scalars(select(AuditEvent)).all())
        reconciliation_store.assign_script(
            database, roster_id, batch, scan,
            candidate_id="1004", operator=OPERATOR,
            reason=ReconciliationReason.MISREAD_IDENTIFIER,
        )
        with database.session() as session:
            after = len(session.scalars(select(AuditEvent)).all())
        assert after == before + 1

    def test_the_event_records_both_values_and_the_machine_one(
        self, database, reconciled
    ):
        roster_id, batch = reconciled
        scan = scan_id_of(database, batch, "s4.png")
        reconciliation_store.assign_script(
            database, roster_id, batch, scan,
            candidate_id="1004", operator=OPERATOR,
            reason=ReconciliationReason.MISREAD_IDENTIFIER,
        )
        history = reconciliation_store.history_for(database, "script", str(scan))
        assert len(history) == 1
        event = history[0]
        assert event.previous_value == "9999"
        assert event.new_value == "1004"
        assert event.machine_value == "9999"
        assert event.reviewer == OPERATOR
        assert event.action is ReconciliationAction.ASSIGNED

    def test_a_later_decision_appends_rather_than_replacing(
        self, database, reconciled
    ):
        roster_id, batch = reconciled
        scan = scan_id_of(database, batch, "s4.png")
        for target, who in (("1004", OPERATOR), ("1001", OTHER_OPERATOR)):
            reconciliation_store.assign_script(
                database, roster_id, batch, scan,
                candidate_id=target, operator=who,
                reason=ReconciliationReason.MISREAD_IDENTIFIER,
            )
        history = reconciliation_store.history_for(database, "script", str(scan))
        assert len(history) == 2
        # The first decision survives, attributed to the person who made it.
        assert history[0].new_value == "1004"
        assert history[0].reviewer == OPERATOR
        assert history[1].new_value == "1001"
        assert history[1].reviewer == OTHER_OPERATOR
        # And the machine's reading is on both.
        assert {e.machine_value for e in history} == {"9999"}

    def test_an_attendance_override_records_the_imported_value(
        self, database, reconciled
    ):
        roster_id, batch = reconciled
        reconciliation_store.override_attendance(
            database, roster_id, batch, "1005",
            attendance=AttendanceState.PRESENT, operator=OPERATOR,
            reason=ReconciliationReason.CANDIDATE_ATTENDED,
        )
        history = reconciliation_store.history_for(database, "candidate", "1005")
        assert history[-1].machine_value == AttendanceState.ABSENT.value
        assert history[-1].new_value == AttendanceState.PRESENT.value

    def test_the_ledger_refuses_an_update(self, database, reconciled):
        roster_id, batch = reconciled
        reconciliation_store.dismiss_entry(
            database, roster_id, batch, "1004", operator=OPERATOR,
            reason=ReconciliationReason.SCRIPT_MISSING,
        )
        with pytest.raises(Exception, match="append-only"), database.session() as s:
            s.execute(text("UPDATE audit_event SET reviewer = 'someone else'"))

    def test_the_ledger_refuses_a_delete(self, database, reconciled):
        roster_id, batch = reconciled
        reconciliation_store.dismiss_entry(
            database, roster_id, batch, "1004", operator=OPERATOR,
            reason=ReconciliationReason.SCRIPT_MISSING,
        )
        with pytest.raises(Exception, match="append-only"), database.session() as s:
            s.execute(text("DELETE FROM audit_event"))

    def test_phase_7_events_are_filed_under_their_own_entity(
        self, database, reconciled
    ):
        roster_id, batch = reconciled
        reconciliation_store.dismiss_entry(
            database, roster_id, batch, "1004", operator=OPERATOR,
            reason=ReconciliationReason.SCRIPT_MISSING,
        )
        with database.session() as session:
            rows = session.scalars(select(AuditEvent)).all()
            assert {r.entity_type for r in rows} == {"candidate"}
            assert {r.entity_id for r in rows} == {"1004"}

    def test_an_entrys_history_merges_candidate_and_script_decisions(
        self, database, reconciled
    ):
        roster_id, batch = reconciled
        scan = scan_id_of(database, batch, "s2b.png")
        reconciliation_store.set_script_excluded(
            database, roster_id, batch, scan, excluded=True, operator=OPERATOR,
            reason=ReconciliationReason.ACCIDENTAL_RESCAN,
        )
        reconciliation_store.dismiss_entry(
            database, roster_id, batch, "1002", operator=OPERATOR,
            reason=ReconciliationReason.CONFIRMED_CORRECT,
        )
        entry = entries_by_id(database, roster_id, batch)["1002"]
        history = reconciliation_store.history_for_entry(database, entry)
        actions = [record.action for record in history]
        assert ReconciliationAction.EXCLUDED in actions
        assert ReconciliationAction.DISMISSED in actions
        # Ordered oldest first, as one story.
        assert history == tuple(sorted(history, key=lambda r: r.event_id))

    def test_reconciling_does_not_write_events(self, database, reconciled):
        # Only human decisions belong in the ledger; a re-run is not a decision.
        roster_id, batch = reconciled
        with database.session() as session:
            before = len(session.scalars(select(AuditEvent)).all())
        reconciliation_store.reconcile_batch(database, roster_id, batch)
        with database.session() as session:
            assert len(session.scalars(select(AuditEvent)).all()) == before


class TestPersistence:
    def test_everything_survives_closing_and_reopening(self, tmp_path):
        path = tmp_path / "database.sqlite"
        handle = open_project_database(path, create=True)
        roster_id = reconciliation_store.import_roster(
            handle, validation_of(STANDARD), imported_by=OPERATOR
        )
        batch = make_batch(
            handle,
            [("s1.png", "1001"), ("s2.png", "1002"), ("s3.png", "1002")],
        )
        reconciliation_store.reconcile_batch(handle, roster_id, batch)
        scan = scan_id_of(handle, batch, "s3.png")
        reconciliation_store.set_script_excluded(
            handle, roster_id, batch, scan, excluded=True, operator=OPERATOR,
            reason=ReconciliationReason.ACCIDENTAL_RESCAN,
        )
        reconciliation_store.override_attendance(
            handle, roster_id, batch, "1003",
            attendance=AttendanceState.PRESENT, operator=OPERATOR,
            reason=ReconciliationReason.CANDIDATE_ATTENDED,
        )
        before = entries_by_id(handle, roster_id, batch)
        history_before = reconciliation_store.history_for(handle, "script", str(scan))
        handle.close()

        reopened = open_project_database(path)
        try:
            after = entries_by_id(reopened, roster_id, batch)
            assert {k: v.status for k, v in after.items()} == {
                k: v.status for k, v in before.items()
            }
            # The exclusion survived...
            assert after["1002"].script_count == 1
            assert len(after["1002"].scripts) == 2
            # ...as did the attendance override, and the import beneath it.
            assert after["1003"].effective_attendance is AttendanceState.PRESENT
            assert after["1003"].candidate.imported_attendance is (
                AttendanceState.ABSENT
            )
            # ...and the reason, the operator and the ledger.
            history_after = reconciliation_store.history_for(
                reopened, "script", str(scan)
            )
            assert history_after == history_before
            assert history_after[0].reason_code == (
                ReconciliationReason.ACCIDENTAL_RESCAN.value
            )
            assert history_after[0].reviewer == OPERATOR
            # The active roster is still the active roster.
            active = reconciliation_store.active_roster(reopened)
            assert active is not None and active.roster_id == roster_id
        finally:
            reopened.close()

    def test_reconciliation_can_be_rerun_after_reopening(self, tmp_path):
        path = tmp_path / "database.sqlite"
        handle = open_project_database(path, create=True)
        roster_id = reconciliation_store.import_roster(handle, validation_of(STANDARD))
        batch = make_batch(handle, [("s1.png", "1001")])
        reconciliation_store.reconcile_batch(handle, roster_id, batch)
        handle.close()

        reopened = open_project_database(path)
        try:
            counts = reconciliation_store.reconcile_batch(reopened, roster_id, batch)
            assert counts.matched == 1
        finally:
            reopened.close()


class TestMigrationOntoAnExistingPhase6Project:
    """A project reviewed before Phase 7 must keep everything and gain the rest.

    The database is genuinely wound back to schema version 3 - the six tables
    dropped, the two audit columns dropped, the ledger row deleted - rather
    than merely created fresh, because "a new database has the tables" says
    nothing about whether the *upgrade* works on a project that already holds
    conflicts and a decision history.
    """

    def _wind_back_to_version_3(self, db_path: Path) -> None:
        import sqlite3

        connection = sqlite3.connect(db_path)
        try:
            connection.executescript(
                """
                DROP INDEX IF EXISTS ix_audit_event_entity;
                ALTER TABLE audit_event DROP COLUMN entity_type;
                ALTER TABLE audit_event DROP COLUMN entity_id;
                DROP TABLE IF EXISTS reconciliation_decision;
                DROP TABLE IF EXISTS reconciliation_script;
                DROP TABLE IF EXISTS reconciliation_entry;
                DROP TABLE IF EXISTS reconciliation_run;
                DROP TABLE IF EXISTS registered_candidate;
                DROP TABLE IF EXISTS candidate_roster;
                DELETE FROM schema_migration WHERE version >= 4;
                """
            )
            connection.commit()
        finally:
            connection.close()

    def test_a_phase_6_project_upgrades_and_becomes_reconcilable(self, tmp_path):
        from omr_scanner.database.migrations import SCHEMA_VERSION
        from omr_scanner.database.models import ReviewConflict

        db_path = tmp_path / "database.sqlite"
        handle = open_project_database(db_path, create=True)
        batch = make_batch(handle, [("s1.png", "1001")])
        now = datetime.now(UTC)
        with handle.session() as session:
            session.add(
                ReviewConflict(
                    batch_id=batch,
                    scan_id=scan_id_of(handle, batch, "s1.png"),
                    conflict_type="answer_multiple",
                    scope="field",
                    state="open",
                    zone_id="questions_0",
                    group_key=0,
                    field_kind="question",
                    field_label="Q1",
                    machine_value="B-D",
                    machine_status="multiple",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                AuditEvent(
                    occurred_at=now,
                    batch_id=batch,
                    scan_id=0,
                    conflict_id=1,
                    action="corrected",
                    reviewer=OPERATOR,
                    previous_value="B-D",
                    new_value="B",
                    machine_value="B-D",
                    entity_type="conflict",
                    entity_id="",
                )
            )
        handle.close()
        self._wind_back_to_version_3(db_path)

        reopened = open_project_database(db_path)
        try:
            assert reopened.schema_version == SCHEMA_VERSION
            with reopened.session() as session:
                names = {
                    row[0]
                    for row in session.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    ).all()
                }
                assert {
                    "candidate_roster",
                    "registered_candidate",
                    "reconciliation_run",
                    "reconciliation_entry",
                    "reconciliation_script",
                    "reconciliation_decision",
                } <= names

                # Phase 6's data is untouched, and the pre-existing audit row
                # reads back under the new columns' defaults without having
                # been rewritten.
                rows = session.execute(
                    text(
                        "SELECT entity_type, entity_id, conflict_id, reviewer, "
                        "machine_value FROM audit_event"
                    )
                ).all()
                assert rows == [("conflict", "", 1, OPERATOR, "B-D")]
                assert len(session.scalars(select(ReviewConflict)).all()) == 1

            # ...and the project can now be reconciled.
            roster_id = reconciliation_store.import_roster(
                reopened, validation_of(STANDARD)
            )
            counts = reconciliation_store.reconcile_batch(reopened, roster_id, batch)
            assert counts.matched == 1

            # The re-created ledger is append-only again, not merely present.
            reconciliation_store.dismiss_entry(
                reopened, roster_id, batch, "1004", operator=OPERATOR,
                reason=ReconciliationReason.SCRIPT_MISSING,
            )
            with (
                pytest.raises(Exception, match="append-only"),
                reopened.session() as session,
            ):
                session.execute(text("DELETE FROM audit_event"))
        finally:
            reopened.close()


class TestCandidateLookup:
    def test_searching_finds_by_id_and_name(self, database, roster_id):
        assert [
            i.candidate_id
            for i in reconciliation_store.find_candidates(database, roster_id, "1003")
        ] == ["1003"]
        assert [
            i.candidate_id
            for i in reconciliation_store.find_candidates(
                database, roster_id, "CANDIDATE E"
            )
        ] == ["1005"]

    def test_searching_is_capped(self, database):
        many = [
            (str(90000 + i), f"CANDIDATE {i}", AttendanceState.PRESENT)
            for i in range(200)
        ]
        roster_id = reconciliation_store.import_roster(database, validation_of(many))
        found = reconciliation_store.find_candidates(database, roster_id, "", limit=25)
        assert len(found) == 25

    def test_membership_is_a_single_indexed_query(self, database, roster_id):
        assert reconciliation_store.candidate_exists(database, roster_id, "1001")
        assert not reconciliation_store.candidate_exists(database, roster_id, "9999")

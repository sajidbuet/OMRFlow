"""Candidates, scripts and reconciliation, end to end (Phase 7).

Scope:
    The real recognition engine, real rendered sheets, a real project database,
    the real Phase 6 review services and the real reconciliation services.
    Nothing here is hand-built, because the claim Phase 7 makes - "every script
    maps to one candidate or to an explicit reviewable exception" - is a claim
    about what happens when those pieces are wired together.

The acceptance scenario the phase brief specifies:

    ========= =============================== ==========================
    Candidate Situation                        Expected
    ========= =============================== ==========================
    10001     expected present, one script     matched
    10002     expected present, two scripts    duplicate script
    10003     ABSENT, no script                absent, confirmed
    10004     expected present, no script      present without script
    10005     ABSENT, one script               absent with script
    99999     no registered candidate          unknown ID
    (unread)  identifier awaiting review       candidate ID not resolved
    ========= =============================== ==========================

Why the sheets are rendered rather than faked:
    The roll numbers under test are the ones the engine genuinely read off a
    page. A fixture author's idea of what recognition returns is exactly the
    assumption that makes an integration test worthless.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import pytest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.database import open_project_database
from omr_scanner.domain.reconciliation import (
    AttendanceSource,
    AttendanceState,
    ReconciliationAction,
    ReconciliationReason,
    ReconciliationStatus,
    ResolutionState,
)
from omr_scanner.domain.review import ReasonCode
from omr_scanner.services import batch_store, reconciliation_store, review_store
from omr_scanner.services.batch_processor import BatchOptions, process_batch
from omr_scanner.services.candidate_import import read_roster

if TYPE_CHECKING:
    from collections.abc import Iterator

    from omr_scanner.database import ProjectDatabase

OPERATOR = "Dr. Rahman"
SECOND_OPERATOR = "Dr. Haque"


@pytest.fixture(scope="module")
def template():
    return build_answer_sheet_template()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[ProjectDatabase]:
    handle = open_project_database(tmp_path / "database.sqlite", create=True)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def scans_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "scans"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def sheet_marks(roll: str, **overrides: object) -> dict:
    """Standard marks for a sheet carrying ``roll``."""
    marks = {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: "A"},
        "questions_0": dict.fromkeys(range(10), "B"),
        "questions_1": dict.fromkeys(range(10), "C"),
    }
    marks.update(overrides)
    return marks


@pytest.fixture
def make_scan(scans_dir: Path, template):
    def make(name: str, marks: dict) -> Path:
        path = scans_dir / name
        cv2.imwrite(str(path), render_marked_sheet(template, marks))
        return path

    return make


@pytest.fixture
def roster_file(tmp_path: Path) -> Path:
    """The brief's roster, as a marks sheet an examination office would hold."""
    path = tmp_path / "candidates.csv"
    path.write_text(
        "Sl.No.,Roll No.,Name,Total (90),Merit\n"
        "1,100001,CANDIDATE A,55,1\n"
        "2,100002,CANDIDATE B,60,2\n"
        "3,100003,CANDIDATE C,ABSENT,---\n"
        "4,100004,CANDIDATE D,,---\n"
        "5,100005,CANDIDATE E, abs ,---\n",
        encoding="utf-8",
    )
    return path


def run_batch(database, template, paths: list[Path]) -> str:
    """Process a batch the way the Scan page does, and detect its conflicts."""
    batch_id = batch_store.create_batch(
        database, paths, identity=batch_store.BatchIdentity.of(template)
    )
    recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)
    report = process_batch(
        paths, template, options=BatchOptions(), on_result=recorder.record, workers=1
    )
    recorder.flush()
    batch_store.finalise_batch(database, batch_id)

    ids = batch_store.scan_ids_by_path(database, batch_id)
    for item in report.processed:
        review_store.sync_conflicts(
            database,
            batch_id=batch_id,
            scan_id=ids[item.source_path],
            result=item.result,
            template=template,
        )
    review_store.sync_duplicate_identifiers(database, batch_id)
    return batch_id


@pytest.fixture
def prepared(database, template, make_scan, roster_file):
    """The full acceptance scenario, reconciled once."""
    paths = [
        make_scan("s_10001.png", sheet_marks("100001")),
        make_scan("s_10002a.png", sheet_marks("100002")),
        make_scan("s_10002b.png", sheet_marks("100002")),
        make_scan("s_10005.png", sheet_marks("100005")),
        make_scan("s_99999.png", sheet_marks("999999")),
        # A sheet whose roll number cannot be read: the second column carries
        # two marks, so the engine reports the identifier as unresolved rather
        # than inventing one.
        make_scan(
            "s_unread.png",
            sheet_marks("100001", roll_number={0: "1", 1: ["0", "7"], 2: "0",
                                               3: "0", 4: "0", 5: "1"}),
        ),
    ]
    batch_id = run_batch(database, template, paths)
    roster_id = reconciliation_store.import_roster(
        database, read_roster(roster_file), imported_by=OPERATOR
    )
    reconciliation_store.reconcile_batch(database, roster_id, batch_id)
    return roster_id, batch_id, paths


def entries_by_id(database, roster_id, batch_id) -> dict:
    return {
        entry.candidate_id: entry
        for entry in reconciliation_store.list_entries(database, roster_id, batch_id)
    }


def scan_of(database, batch_id, filename: str) -> int:
    from sqlalchemy import select

    from omr_scanner.database.models import BatchScan

    with database.session() as session:
        return session.scalars(
            select(BatchScan.scan_id)
            .where(BatchScan.batch_id == batch_id)
            .where(BatchScan.filename == filename)
        ).first()


# ----------------------------------------------------------------------
# The required acceptance scenario
# ----------------------------------------------------------------------
class TestAcceptanceScenario:
    def test_the_roster_reads_both_absence_spellings(self, roster_file):
        found = read_roster(roster_file)
        absent = {
            i.candidate_id
            for i in found.candidates
            if i.imported_attendance is AttendanceState.ABSENT
        }
        # "ABSENT" and " abs " both, and the blank marks cell is not one.
        assert absent == {"100003", "100005"}
        assert found.expected_present == 3

    def test_every_required_classification_is_produced(self, database, prepared):
        roster_id, batch_id, _ = prepared
        entries = entries_by_id(database, roster_id, batch_id)
        assert entries["100001"].status is ReconciliationStatus.MATCHED
        assert entries["100002"].status is ReconciliationStatus.DUPLICATE_SCRIPT
        assert entries["100003"].status is ReconciliationStatus.ABSENT_CONFIRMED
        assert entries["100004"].status is (
            ReconciliationStatus.PRESENT_WITHOUT_SCRIPT
        )
        assert entries["100005"].status is ReconciliationStatus.ABSENT_WITH_SCRIPT
        assert entries["999999"].status is ReconciliationStatus.UNKNOWN_ID

    def test_an_unread_identifier_is_its_own_state(self, database, prepared):
        roster_id, batch_id, _ = prepared
        unresolved = [
            entry
            for entry in reconciliation_store.list_entries(
                database, roster_id, batch_id
            )
            if entry.status is ReconciliationStatus.UNRESOLVED_CANDIDATE_ID
        ]
        assert len(unresolved) == 1, "the deliberately ambiguous sheet"
        assert unresolved[0].scripts[0].script.identifier_unresolved is True

    def test_every_script_is_accounted_for(self, database, prepared):
        roster_id, batch_id, paths = prepared
        seen = [
            view.script.scan_id
            for entry in reconciliation_store.list_entries(
                database, roster_id, batch_id
            )
            for view in entry.scripts
        ]
        assert len(seen) == len(paths)
        assert len(set(seen)) == len(paths)

    def test_the_summary_counts_agree_with_the_entries(self, database, prepared):
        roster_id, batch_id, paths = prepared
        counts = reconciliation_store.stored_counts(database, roster_id, batch_id)
        assert counts is not None
        assert counts.registered == 5
        assert counts.scripts == len(paths)
        assert counts.matched == 1
        assert counts.absent_confirmed == 1
        assert counts.unknown_id == 1
        assert counts.duplicate_script == 1
        assert counts.present_without_script == 1
        assert counts.absent_with_script == 1
        assert counts.unresolved_candidate_id == 1
        assert counts.is_clear is False


# ----------------------------------------------------------------------
# Resolving, and what survives it
# ----------------------------------------------------------------------
class TestResolvingTheExceptions:
    def test_assigning_the_unknown_script_keeps_what_the_machine_read(
        self, database, prepared
    ):
        roster_id, batch_id, _ = prepared
        scan = scan_of(database, batch_id, "s_99999.png")
        reconciliation_store.assign_script(
            database, roster_id, batch_id, scan,
            candidate_id="100004", operator=OPERATOR,
            reason=ReconciliationReason.MISREAD_IDENTIFIER,
        )
        entries = entries_by_id(database, roster_id, batch_id)
        assert entries["100004"].status is ReconciliationStatus.MATCHED
        moved = entries["100004"].scripts[0]
        # The engine's reading is intact...
        assert moved.script.machine_candidate_id == "999999"
        # ...and the entry it used to be is gone, because the script moved.
        assert "999999" not in entries

    def test_a_cascading_duplicate_is_never_hidden(self, database, prepared):
        roster_id, batch_id, _ = prepared
        scan = scan_of(database, batch_id, "s_99999.png")
        counts = reconciliation_store.assign_script(
            database, roster_id, batch_id, scan,
            candidate_id="100001", operator=OPERATOR,
            reason=ReconciliationReason.SHEET_SWAPPED,
        )
        assert counts.duplicate_script == 2
        entry = entries_by_id(database, roster_id, batch_id)["100001"]
        assert entry.status is ReconciliationStatus.DUPLICATE_SCRIPT
        assert entry.resolution is ResolutionState.OPEN

    def test_setting_a_duplicate_aside_keeps_the_script(self, database, prepared):
        roster_id, batch_id, _ = prepared
        scan = scan_of(database, batch_id, "s_10002b.png")
        reconciliation_store.set_script_excluded(
            database, roster_id, batch_id, scan, excluded=True, operator=OPERATOR,
            reason=ReconciliationReason.ACCIDENTAL_RESCAN,
        )
        entry = entries_by_id(database, roster_id, batch_id)["100002"]
        assert entry.status is ReconciliationStatus.MATCHED
        assert entry.script_count == 1
        assert len(entry.scripts) == 2, "set aside, not deleted"
        with database.session() as session:
            from omr_scanner.database.models import BatchScan

            assert session.get(BatchScan, scan) is not None

    def test_overriding_attendance_keeps_the_imported_value(
        self, database, prepared
    ):
        roster_id, batch_id, _ = prepared
        reconciliation_store.override_attendance(
            database, roster_id, batch_id, "100005",
            attendance=AttendanceState.PRESENT, operator=OPERATOR,
            reason=ReconciliationReason.CANDIDATE_ATTENDED,
        )
        entry = entries_by_id(database, roster_id, batch_id)["100005"]
        assert entry.status is ReconciliationStatus.MATCHED
        assert entry.effective_attendance is AttendanceState.PRESENT
        assert entry.attendance_source is AttendanceSource.HUMAN
        # The three histories remain distinguishable.
        assert entry.candidate.imported_attendance is AttendanceState.ABSENT
        assert entry.candidate.imported_value == "abs"

    def test_resolving_everything_clears_the_outstanding_count(
        self, database, prepared
    ):
        roster_id, batch_id, _ = prepared
        reconciliation_store.assign_script(
            database, roster_id, batch_id,
            scan_of(database, batch_id, "s_99999.png"),
            candidate_id="100004", operator=OPERATOR,
            reason=ReconciliationReason.MISREAD_IDENTIFIER,
        )
        reconciliation_store.set_script_excluded(
            database, roster_id, batch_id,
            scan_of(database, batch_id, "s_10002b.png"),
            excluded=True, operator=OPERATOR,
            reason=ReconciliationReason.ACCIDENTAL_RESCAN,
        )
        reconciliation_store.override_attendance(
            database, roster_id, batch_id, "100005",
            attendance=AttendanceState.PRESENT, operator=OPERATOR,
            reason=ReconciliationReason.CANDIDATE_ATTENDED,
        )
        counts = reconciliation_store.dismiss_entry(
            database, roster_id, batch_id,
            entries_by_id(database, roster_id, batch_id)["100001"].candidate_id,
            operator=OPERATOR, reason=ReconciliationReason.CONFIRMED_CORRECT,
        )
        # Only the unread identifier is left - it belongs on the Resolve stage.
        assert counts.unknown_id == 0
        assert counts.duplicate_script == 0
        assert counts.absent_with_script == 0
        assert counts.present_without_script == 0
        assert counts.unresolved_candidate_id == 1


# ----------------------------------------------------------------------
# Phase 6 is what says who a script belongs to
# ----------------------------------------------------------------------
class TestPhase6Integration:
    def test_an_unread_identifier_becomes_known_once_it_is_reviewed(
        self, database, prepared, template
    ):
        roster_id, batch_id, _ = prepared
        scan = scan_of(database, batch_id, "s_unread.png")

        # Resolve the Phase 6 conflict the way the Resolve stage does.
        conflicts = [
            item
            for item in review_store.list_conflicts(database, batch_id)
            if item.scan_id == scan and item.field.kind.value == "identifier"
        ]
        assert conflicts, "the sheet really does carry an identifier conflict"
        review_store.correct_value(
            database,
            conflicts[0].conflict_id,
            value="0",
            reviewer=SECOND_OPERATOR,
            reason=ReasonCode.DOMINANT_MARK,
        )

        counts = reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        assert counts.unresolved_candidate_id == 0

        entry = entries_by_id(database, roster_id, batch_id)["100001"]
        assert entry.status is ReconciliationStatus.DUPLICATE_SCRIPT
        corrected = next(
            view for view in entry.scripts if view.script.scan_id == scan
        )
        # Phase 6's correction is what placed it, and the machine's reading is
        # still on the record.
        assert corrected.script.effective_candidate_id == "100001"
        assert corrected.script.machine_candidate_id != "100001"
        assert corrected.script.corrected_by_human is True

    def test_reconciliation_follows_a_phase_6_correction(self, database, prepared):
        """A corrected roll number moves the script, and keeps the original.

        Phase 6 raises a duplicate-identifier conflict for the two sheets that
        both read ``100002``; correcting one of them to ``100004`` is the whole
        chain the brief describes - machine value, Phase 6 correction, effective
        value, reconciliation - exercised with the real services.
        """
        roster_id, batch_id, _ = prepared
        scan = scan_of(database, batch_id, "s_10002b.png")

        before = entries_by_id(database, roster_id, batch_id)
        assert before["100002"].status is ReconciliationStatus.DUPLICATE_SCRIPT
        assert before["100004"].status is (
            ReconciliationStatus.PRESENT_WITHOUT_SCRIPT
        )

        duplicate = next(
            item
            for item in review_store.list_conflicts(database, batch_id)
            if item.scan_id == scan
            and item.conflict_type.value == "identifier_duplicate"
        )
        review_store.correct_value(
            database,
            duplicate.conflict_id,
            value="100004",
            reviewer=SECOND_OPERATOR,
            reason=ReasonCode.MISCLASSIFICATION,
        )
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)

        after = entries_by_id(database, roster_id, batch_id)
        # The correction placed the script, with no Phase 7 decision at all.
        assert after["100004"].status is ReconciliationStatus.MATCHED
        assert after["100002"].status is ReconciliationStatus.MATCHED
        moved = after["100004"].scripts[0]
        assert moved.script.effective_candidate_id == "100004"
        # ...and what the engine actually read is still on the record.
        assert moved.script.machine_candidate_id == "100002"
        assert moved.script.corrected_by_human is True

    def test_a_duplicate_identifier_conflict_does_not_make_a_script_unresolved(
        self, database, prepared
    ):
        # Phase 6 raises IDENTIFIER_DUPLICATE for the two 100002 sheets. That is
        # a perfectly legible ID, and calling such a script "not yet resolved"
        # would hide the duplication Phase 7 exists to report.
        roster_id, batch_id, _ = prepared
        entry = entries_by_id(database, roster_id, batch_id)["100002"]
        assert entry.status is ReconciliationStatus.DUPLICATE_SCRIPT
        assert all(
            view.script.identifier_unresolved is False for view in entry.scripts
        )


# ----------------------------------------------------------------------
# Audit, persistence and the untouched original
# ----------------------------------------------------------------------
class TestAuditAndPersistence:
    def test_every_decision_is_audited_with_both_values(self, database, prepared):
        roster_id, batch_id, _ = prepared
        scan = scan_of(database, batch_id, "s_99999.png")
        reconciliation_store.assign_script(
            database, roster_id, batch_id, scan,
            candidate_id="100004", operator=OPERATOR,
            reason=ReconciliationReason.MISREAD_IDENTIFIER,
        )
        history = reconciliation_store.history_for(database, "script", str(scan))
        assert len(history) == 1
        assert history[0].action is ReconciliationAction.ASSIGNED
        assert history[0].previous_value == "999999"
        assert history[0].new_value == "100004"
        assert history[0].machine_value == "999999"
        assert history[0].reviewer == OPERATOR
        assert history[0].reason == ReconciliationReason.MISREAD_IDENTIFIER.label

    def test_a_second_decision_appends_and_keeps_the_first(
        self, database, prepared
    ):
        roster_id, batch_id, _ = prepared
        scan = scan_of(database, batch_id, "s_99999.png")
        reconciliation_store.assign_script(
            database, roster_id, batch_id, scan,
            candidate_id="100004", operator=OPERATOR,
            reason=ReconciliationReason.MISREAD_IDENTIFIER,
        )
        reconciliation_store.assign_script(
            database, roster_id, batch_id, scan,
            candidate_id="100003", operator=SECOND_OPERATOR,
            reason=ReconciliationReason.SHEET_SWAPPED,
        )
        history = reconciliation_store.history_for(database, "script", str(scan))
        assert [item.new_value for item in history] == ["100004", "100003"]
        assert [item.reviewer for item in history] == [OPERATOR, SECOND_OPERATOR]

    def test_everything_survives_closing_and_reopening_the_project(
        self, tmp_path, database, prepared
    ):
        roster_id, batch_id, _ = prepared
        reconciliation_store.assign_script(
            database, roster_id, batch_id,
            scan_of(database, batch_id, "s_99999.png"),
            candidate_id="100004", operator=OPERATOR,
            reason=ReconciliationReason.MISREAD_IDENTIFIER,
        )
        reconciliation_store.override_attendance(
            database, roster_id, batch_id, "100005",
            attendance=AttendanceState.PRESENT, operator=OPERATOR,
            reason=ReconciliationReason.CANDIDATE_ATTENDED,
        )
        before = entries_by_id(database, roster_id, batch_id)
        database.close()

        reopened = open_project_database(tmp_path / "database.sqlite")
        try:
            after = entries_by_id(reopened, roster_id, batch_id)
            assert {k: v.status for k, v in after.items()} == {
                k: v.status for k, v in before.items()
            }
            assert after["100004"].scripts[0].script.machine_candidate_id == "999999"
            assert after["100005"].effective_attendance is AttendanceState.PRESENT
            assert after["100005"].candidate.imported_attendance is (
                AttendanceState.ABSENT
            )
            active = reconciliation_store.active_roster(reopened)
            assert active is not None and active.roster_id == roster_id
            assert reconciliation_store.history_for(
                reopened, "candidate", "100005"
            )
        finally:
            reopened.close()

    def test_reconciliation_never_modifies_a_source_scan(
        self, database, prepared
    ):
        # Phase 5's invariant, still in force two phases later.
        roster_id, batch_id, paths = prepared
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        reconciliation_store.set_script_excluded(
            database, roster_id, batch_id,
            scan_of(database, batch_id, "s_10002b.png"),
            excluded=True, operator=OPERATOR,
            reason=ReconciliationReason.ACCIDENTAL_RESCAN,
        )
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        assert after == before

    def test_reconciliation_never_modifies_the_roster_file(
        self, database, prepared, roster_file
    ):
        roster_id, batch_id, _ = prepared
        before = hashlib.sha256(roster_file.read_bytes()).hexdigest()
        reconciliation_store.override_attendance(
            database, roster_id, batch_id, "100005",
            attendance=AttendanceState.PRESENT, operator=OPERATOR,
            reason=ReconciliationReason.CANDIDATE_ATTENDED,
        )
        assert hashlib.sha256(roster_file.read_bytes()).hexdigest() == before


# ----------------------------------------------------------------------
# Phase 5 is untouched
# ----------------------------------------------------------------------
class TestPhase5IsIntact:
    def test_a_multi_worker_batch_reconciles_identically(
        self, tmp_path, template, make_scan, roster_file
    ):
        """Reconciliation must not have cost the batch its worker pool.

        Run the same sheets on one worker and on four, reconcile both, and
        compare - asserting the reported worker count so the comparison cannot
        quietly be between two sequential runs.
        """
        paths = [
            make_scan("m1.png", sheet_marks("100001")),
            make_scan("m2.png", sheet_marks("100002")),
            make_scan("m3.png", sheet_marks("100002")),
            make_scan("m4.png", sheet_marks("999999")),
        ]
        signatures = []
        for workers in (1, 4):
            handle = open_project_database(
                tmp_path / f"workers_{workers}.sqlite", create=True
            )
            try:
                batch_id = batch_store.create_batch(
                    handle, paths, identity=batch_store.BatchIdentity.of(template)
                )
                recorder = batch_store.BatchRecorder(
                    database=handle, batch_id=batch_id
                )
                report = process_batch(
                    paths, template, options=BatchOptions(),
                    on_result=recorder.record, workers=workers,
                )
                recorder.flush()
                batch_store.finalise_batch(handle, batch_id)
                assert report.worker_count == workers

                ids = batch_store.scan_ids_by_path(handle, batch_id)
                for item in report.processed:
                    review_store.sync_conflicts(
                        handle, batch_id=batch_id,
                        scan_id=ids[item.source_path], result=item.result,
                        template=template,
                    )
                review_store.sync_duplicate_identifiers(handle, batch_id)
                roster_id = reconciliation_store.import_roster(
                    handle, read_roster(roster_file)
                )
                reconciliation_store.reconcile_batch(handle, roster_id, batch_id)
                signatures.append(
                    sorted(
                        (entry.candidate_id, entry.status.value, entry.script_count)
                        for entry in reconciliation_store.list_entries(
                            handle, roster_id, batch_id
                        )
                    )
                )
            finally:
                handle.close()
        assert signatures[0] == signatures[1]

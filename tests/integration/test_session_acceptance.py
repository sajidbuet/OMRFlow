"""0.1.1 phase 4 acceptance: one scan session, three batches, 100+ scripts.

Scope:
    One deterministic examination - three sets of 36 candidates, read in three
    batches of one scan session - containing every case the phase brief (§24)
    names: clean candidates, a duplicate Student ID within a batch and one
    across batches, a rejected sheet, an excluded sheet, a deferred sheet, a
    cross-batch rescan, a two-generation rescan, a corrected Student ID, an
    unresolved identity, an absent candidate whose script exists, and a present
    candidate whose script is missing.

    The expected population is written out by hand below and compared, as
    *identity sets*, with what each stage holds: the canonical effective set,
    Attendance, scoring, Results, the report inputs and the generated workbook.
    Reopening the project must change none of it (E8), and no image is read to
    rebuild it (E9).

Privacy:
    Every identifier here is fictional.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import openpyxl
import pytest
from sqlalchemy import func, select
from tests.integration.test_reject_and_rescan import (
    HEADER_ROW,
    HEADERS,
    OPERATOR,
    PLAN,
    TEMPLATE,
    make_result,
)

from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    ReviewConflict,
    ScanBatch,
    ScanSession,
)
from omr_scanner.domain.review import (
    ConflictState,
    ConflictType,
    FieldKind,
    MachineObservation,
    ReasonCode,
)
from omr_scanner.domain.scan_lifecycle import RejectionReason
from omr_scanner.domain.scoring import ResultStatus
from omr_scanner.domain.session_population import SheetDisposition
from omr_scanner.services import (
    batch_store,
    candidate_import,
    create_project,
    open_project,
    project_sets,
    reconciliation_store,
    report_store,
    review_store,
    scan_lifecycle,
    scan_provenance,
    scan_sessions,
    scoring_store,
    session_population,
    set_attendance,
)
from omr_scanner.services.answer_key import read_key

if TYPE_CHECKING:
    from pathlib import Path

    from omr_scanner.database.engine import ProjectDatabase

D = SheetDisposition
SETS = "123"
CANDIDATES = 36


def roll(code: str, number: int) -> str:
    return f"{code}00{number:03d}"


ABSENT_WITH_SCRIPT = roll("1", 36)
MISSING_SCRIPT = roll("2", 36)


def batch_of_number(number: int) -> int:
    """Candidates 1-12 sit in batch 0, 13-24 in batch 1, 25-36 in batch 2."""
    return (number - 1) // 12


def write_roster(path: Path, code: str) -> Path:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = f"Set {code}"
    for column, header in enumerate(HEADERS, start=1):
        sheet.cell(row=HEADER_ROW, column=column, value=header)
    for offset in range(CANDIDATES):
        candidate = roll(code, offset + 1)
        row = HEADER_ROW + 1 + offset
        sheet.cell(row=row, column=1, value=offset + 1)
        sheet.cell(row=row, column=2, value=candidate)
        sheet.cell(row=row, column=3, value=f"CANDIDATE {candidate}")
        if candidate == ABSENT_WITH_SCRIPT:
            sheet.cell(row=row, column=4, value="ABSENT")
    workbook.save(path)
    workbook.close()
    return path


class Exam:
    """The examination's project, its batches, and a scan id per file name."""

    def __init__(self, session, tmp_path: Path) -> None:
        self.session = session
        self.ids: dict[str, int] = {}
        self.rosters: dict[str, int] = {}
        self.set_ids: dict[str, str] = {}
        for code in SETS:
            self.set_ids[code] = project_sets.add_set(
                self.database, code, f"Paper {code}"
            ).set_id
            path = write_roster(tmp_path / f"set{code}.xlsx", code)
            assignment = set_attendance.assign_attendance_workbook(
                self.database, self.set_ids[code], path, candidate_import.read_roster(path),
                imported_by=OPERATOR,
            )
            assert assignment.template_adopted, assignment.template_blocker
            self.rosters[code] = assignment.roster_id
            stored = scoring_store.save_key(
                self.database, read_key("A" * PLAN.question_count, PLAN, code).to_key()
            )
            scoring_store.verify_key(self.database, stored.key_id, verified_by=OPERATOR)
        self.scan_session_id = scan_sessions.create_scan_session(
            self.database, name="Final examination", created_by=OPERATOR
        ).scan_session_id
        self.batches: list[str] = []
        self._clock = datetime.now(UTC)

    @property
    def database(self) -> ProjectDatabase:
        return self.session.database

    def read_batch(self, rows: list[tuple[str, str, str, int]]) -> str:
        """One sealed batch of the session, and the Scan stage's passes after it."""
        folder = self.session.project.layout.scans_original_dir / f"batch{len(self.batches)}"
        folder.mkdir(parents=True, exist_ok=True)
        self._clock += timedelta(seconds=1)
        batch_id = batch_store.new_batch_id()
        with self.database.session() as session:
            session.add(
                ScanBatch(
                    batch_id=batch_id, created_at=self._clock, updated_at=self._clock,
                    source_folder=str(folder), status="completed", total_scans=len(rows),
                    scan_session_id=self.scan_session_id, sealed_at=self._clock,
                )
            )
            session.flush()
            for index, (name, identifier, code, correct) in enumerate(rows):
                path = folder / name
                path.write_bytes(f"{batch_id}:{name}".encode())
                session.add(
                    BatchScan(
                        batch_id=batch_id, batch_index=index, source_path=str(path),
                        filename=name, status="completed", identifier_value=identifier,
                        set_code_value=code,
                        result_json=json.dumps(
                            make_result(path, identifier, code, correct).to_dict()
                        ),
                    )
                )
        scan_provenance.compute_hashes_for_batch(self.database, batch_id)
        for path, scan_id in batch_store.scan_ids_by_path(self.database, batch_id).items():
            self.ids[path.name] = scan_id
        scan_lifecycle.sync_reimports(self.database, batch_id)
        review_store.sync_duplicate_identifiers(self.database, batch_id)
        review_store.sync_undefined_set_codes(self.database, batch_id)
        self.batches.append(batch_id)
        return batch_id

    def correct_roll(self, name: str, value: str, previous: str) -> None:
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


def sheet(code: str, number: int) -> str:
    return f"s{code}_{number:03d}.tif"


def correct_of(code: str, number: int) -> int:
    """Questions answered correctly: distinct per script, so a mark names its sheet."""
    return (int(code) * 7 + number) % (PLAN.question_count - 1) + 1


def build(exam: Exam) -> None:
    rows: list[list[tuple[str, str, str, int]]] = [[], [], []]
    for code in SETS:
        for number in range(1, CANDIDATES + 1):
            candidate = roll(code, number)
            if candidate == MISSING_SCRIPT:
                continue  # present on the list, script never arrived
            identifier = "300080" if candidate == roll("3", 30) else candidate
            rows[batch_of_number(number)].append(
                (sheet(code, number), identifier, code, correct_of(code, number))
            )
    rows[0].append(("junk.tif", "100099", "1", 3))  # excluded below
    rows[1].append(("dupA.tif", roll("1", 13), "1", 40))  # same batch as s1_013
    rows[2].append(("dupB.tif", roll("2", 5), "2", 41))  # s2_005 is in batch 0
    rows[2].append(("blur.tif", "1?0?2?", "1", 5))  # identity never resolved

    exam.read_batch(rows[0])
    db = exam.database

    # Batch 0's operator decisions.
    for name in (sheet("1", 3), sheet("2", 4), sheet("3", 7)):
        scan_lifecycle.reject_scan(
            db, exam.ids[name], reviewer=OPERATOR, reason=RejectionReason.FOLDED
        )
    scan_lifecycle.exclude_scan(
        db, exam.ids["junk.tif"], reviewer=OPERATOR, reason=RejectionReason.FOLDED
    )

    rows[1].append(("R1_100003.tif", roll("1", 3), "1", 50))  # cross-batch rescan
    rows[1].append(("G1_200004.tif", roll("2", 4), "2", 51))  # generation 1
    exam.read_batch(rows[1])
    scan_lifecycle.confirm_replacement(
        db, exam.ids[sheet("1", 3)], exam.ids["R1_100003.tif"], reviewer=OPERATOR
    )
    scan_lifecycle.confirm_replacement(
        db, exam.ids[sheet("2", 4)], exam.ids["G1_200004.tif"], reviewer=OPERATOR
    )
    scan_lifecycle.reject_scan(
        db, exam.ids["G1_200004.tif"], reviewer=OPERATOR, reason=RejectionReason.FOLDED
    )
    scan_lifecycle.defer_scan(db, exam.ids[sheet("3", 20)], reviewer=OPERATOR)

    rows[2].append(("G2_200004.tif", roll("2", 4), "2", 52))  # generation 2
    exam.read_batch(rows[2])
    scan_lifecycle.confirm_replacement(
        db, exam.ids["G1_200004.tif"], exam.ids["G2_200004.tif"], reviewer=OPERATOR
    )
    exam.correct_roll(sheet("3", 30), roll("3", 30), "300080")


@pytest.fixture
def exam(workspace: Path, tmp_path: Path):
    session = create_project(workspace, "Session Acceptance")
    try:
        built = Exam(session, tmp_path)
        build(built)
        yield built
    finally:
        if not session.is_closed:
            session.close()


# ----------------------------------------------------------------------
# The expected population, by hand
# ----------------------------------------------------------------------
def expected_effective(exam: Exam) -> set[int]:
    names = {
        sheet(code, number)
        for code in SETS
        for number in range(1, CANDIDATES + 1)
        if roll(code, number) != MISSING_SCRIPT
    }
    names -= {sheet("1", 3), sheet("2", 4), sheet("3", 7), sheet("3", 20)}
    names |= {"dupA.tif", "dupB.tif", "blur.tif", "R1_100003.tif", "G2_200004.tif"}
    return {exam.ids[name] for name in names}


def expected_scripts(exam: Exam, code: str) -> dict[str, set[int]]:
    """``candidate -> scripts filed under them`` for one set's Attendance."""
    found: dict[str, set[int]] = {}
    for number in range(1, CANDIDATES + 1):
        candidate = roll(code, number)
        if candidate == MISSING_SCRIPT:
            found[candidate] = set()
            continue
        found[candidate] = {exam.ids[sheet(code, number)]}
    if code == "1":
        found[roll("1", 3)] = {exam.ids["R1_100003.tif"]}
        found[roll("1", 13)] |= {exam.ids["dupA.tif"]}
        # An unresolved identity is its own unmatched entry, under what was read.
        found["1?0?2?"] = {exam.ids["blur.tif"]}
    if code == "2":
        # The rejected original stays listed, never counted: G1 is history.
        found[roll("2", 4)] = {exam.ids["G2_200004.tif"]}
        found[roll("2", 5)] |= {exam.ids["dupB.tif"]}
    if code == "3":
        found[roll("3", 7)] = {exam.ids[sheet("3", 7)]}  # rejected, awaiting rescan
        found[roll("3", 20)] = {exam.ids[sheet("3", 20)]}  # deferred, listed
    return found


def expected_scored(exam: Exam, code: str) -> dict[str, int]:
    """``candidate -> the one scan scored for them`` (candidates with a mark)."""
    scripts = expected_scripts(exam, code)
    blocked = {roll("1", 13), roll("2", 5), roll("3", 7), roll("3", 20), ABSENT_WITH_SCRIPT}
    return {
        candidate: next(iter(scans))
        for candidate, scans in scripts.items()
        if len(scans) == 1 and candidate not in blocked and "?" not in candidate
    }


# ----------------------------------------------------------------------
# Reading every stage
# ----------------------------------------------------------------------
def attendance(database: ProjectDatabase, exam: Exam, code: str, batch: str) -> dict[str, set[int]]:
    roster = exam.rosters[code]
    reconciliation_store.reconcile_batch(database, roster, batch)
    return {
        entry.candidate_id: {view.script.scan_id for view in entry.scripts}
        for entry in reconciliation_store.list_entries(database, roster, batch)
        if entry.candidate_id
    }


def results(database: ProjectDatabase, exam: Exam, code: str, batch: str):
    roster = exam.rosters[code]
    reconciliation_store.reconcile_batch(database, roster, batch)
    scoring_store.score_batch(database, roster, batch, TEMPLATE, computed_by=OPERATOR)
    return {
        item.candidate_id: item
        for item in scoring_store.list_results(database, roster, batch, TEMPLATE)
    }


def scored(found) -> dict[str, int]:
    return {
        candidate: item.scan_id
        for candidate, item in found.items()
        if item.status is ResultStatus.SCORED and item.scan_id is not None
    }


def duplicate_scans(database: ProjectDatabase) -> set[int]:
    with database.session() as session:
        return {
            int(item)
            for item in session.scalars(
                select(ReviewConflict.scan_id)
                .where(ReviewConflict.conflict_type == ConflictType.IDENTIFIER_DUPLICATE.value)
                .where(ReviewConflict.state != ConflictState.WITHDRAWN.value)
            ).all()
        }


def snapshot(database: ProjectDatabase, exam: Exam) -> dict[str, object]:
    batch = exam.batches[0]
    population = session_population.population(database, batch)
    state: dict[str, object] = {
        "dispositions": dict(population.dispositions),
        "duplicates": duplicate_scans(database),
    }
    for code in SETS:
        state[f"attendance{code}"] = attendance(database, exam, code, batch)
        state[f"results{code}"] = {
            candidate: (item.status, item.scan_id, item.final_score)
            for candidate, item in results(database, exam, code, batch).items()
        }
    return state


# ----------------------------------------------------------------------
# The acceptance checks
# ----------------------------------------------------------------------
class TestAcceptance:
    def test_the_scenario_has_the_size_the_brief_asks_for(self, exam):
        with exam.database.session() as session:
            sheets = session.scalar(select(func.count()).select_from(BatchScan))
        assert len(exam.batches) == 3
        assert sheets >= 100
        assert len(expected_effective(exam)) >= 100

    def test_e1_one_canonical_population(self, exam):
        expected = expected_effective(exam)
        for batch in exam.batches:
            population = session_population.population(exam.database, batch)
            assert population.key_batch_id == exam.batches[0]
            assert population.effective == expected
        population = session_population.population(exam.database, exam.batches[1])
        assert population.dispositions[exam.ids["junk.tif"]] is D.EXCLUDED
        assert population.dispositions[exam.ids[sheet("3", 7)]] is D.REJECTED_PENDING_RESCAN
        assert population.dispositions[exam.ids[sheet("3", 20)]] is D.DEFERRED
        for name in (sheet("1", 3), sheet("2", 4), "G1_200004.tif"):
            assert population.dispositions[exam.ids[name]] is D.SUPERSEDED_BY_REPLACEMENT

    def test_e2_a_lineage_contributes_one_sheet(self, exam):
        population = session_population.population(exam.database, exam.batches[2])
        lineage = {
            exam.ids[name] for name in (sheet("2", 4), "G1_200004.tif", "G2_200004.tif")
        }
        assert lineage & population.effective == {exam.ids["G2_200004.tif"]}
        assert population.lineage_root[exam.ids["G2_200004.tif"]] == exam.ids[sheet("2", 4)]

    def test_e3_e4_duplicates_are_the_real_ones_only(self, exam):
        assert duplicate_scans(exam.database) == {
            exam.ids[sheet("1", 13)], exam.ids["dupA.tif"],  # within batch 1
            exam.ids[sheet("2", 5)], exam.ids["dupB.tif"],  # batches 0 and 2
        }

    def test_the_corrected_and_the_unresolved_identity(self, exam):
        population = session_population.population(exam.database, exam.batches[0])
        identifiers = session_population.effective_identifiers(exam.database, population)
        assert identifiers[exam.ids[sheet("3", 30)]].value == roll("3", 30)
        assert "?" in identifiers[exam.ids["blur.tif"]].value

    @pytest.mark.parametrize("code", list(SETS))
    def test_e6_attendance_holds_the_expected_scripts(self, exam, code):
        expected = expected_scripts(exam, code)
        for batch in exam.batches:
            assert attendance(exam.database, exam, code, batch) == expected

    @pytest.mark.parametrize("code", list(SETS))
    def test_e6_each_effective_script_is_scored_once(self, exam, code):
        found = results(exam.database, exam, code, exam.batches[-1])
        unresolved = {"1?0?2?"} if code == "1" else set()
        assert set(found) == {
            roll(code, number) for number in range(1, CANDIDATES + 1)
        } | unresolved
        assert all(found[item].status is ResultStatus.BLOCKED for item in unresolved)
        assert scored(found) == expected_scored(exam, code)
        marks = [item.scan_id for item in found.values() if item.scan_id is not None]
        assert len(marks) == len(set(marks)), "a script scored for two candidates"
        population = session_population.population(exam.database, exam.batches[-1])
        assert set(scored(found).values()) <= population.effective
        if code == "2":
            assert found[MISSING_SCRIPT].status is not ResultStatus.SCORED
            assert found[roll("2", 4)].scan_id == exam.ids["G2_200004.tif"]  # generation 2
        if code == "1":
            assert found[ABSENT_WITH_SCRIPT].status is not ResultStatus.SCORED
            assert found[roll("1", 3)].scan_id == exam.ids["R1_100003.tif"]  # the rescan

    def test_e6_results_and_reports_agree_with_attendance_and_scoring(self, exam, tmp_path):
        exports = tmp_path / "exports"
        totals = {}
        for code in SETS:
            found = results(exam.database, exam, code, exam.batches[1])
            inputs = report_store.gather_set_inputs(
                exam.database, exam.rosters[code], exam.batches[2], TEMPLATE, code
            )
            assert {
                entry.candidate_id: {view.script.scan_id for view in entry.scripts}
                for entry in inputs.entries
                if entry.candidate_id
            } == expected_scripts(exam, code)
            assert {
                candidate: (item.status, item.scan_id, item.final_score)
                for candidate, item in inputs.results_by_candidate.items()
            } == {
                candidate: (item.status, item.scan_id, item.final_score)
                for candidate, item in found.items()
            }
            overview = {
                item.set_code: item
                for item in report_store.set_overview(
                    exam.database, exam.rosters[code], exam.batches[0]
                )
            }
            totals[code] = overview[code]
            outcome = report_store.generate_for_set(
                exam.database, exam.set_ids[code], exam.batches[1], TEMPLATE,
                project_name="Session Acceptance", output_dir=exports,
                computed_by=OPERATOR, final=False,
            )
            assert outcome.output_path is not None, outcome.warnings
            workbook = openpyxl.load_workbook(outcome.output_path, read_only=True)
            try:
                rows = list(workbook.worksheets[0].iter_rows(values_only=True))
            finally:
                workbook.close()
            marked = {
                str(row[1])
                for row in rows[HEADER_ROW:]
                if row and row[1] is not None and isinstance(row[3], (int, float))
            }
            assert marked == set(expected_scored(exam, code)), code
        # Results by set, not by batch: each set's scored total, across batches.
        for code in SETS:
            assert totals[code].scored_count == len(expected_scored(exam, code))

    def test_e5_another_session_changes_nothing(self, exam):
        before = snapshot(exam.database, exam)
        other = scan_sessions.create_scan_session(exam.database, name="Supplementary")
        folder = exam.session.project.layout.scans_original_dir / "other"
        folder.mkdir()
        moment = datetime.now(UTC) + timedelta(hours=1)
        batch_id = batch_store.new_batch_id()
        with exam.database.session() as session:
            session.add(
                ScanBatch(
                    batch_id=batch_id, created_at=moment, updated_at=moment,
                    source_folder=str(folder), status="completed", total_scans=2,
                    scan_session_id=other.scan_session_id, sealed_at=moment,
                )
            )
            session.flush()
            for index, identifier in enumerate((roll("1", 1), roll("1", 1))):
                path = folder / f"o{index}.tif"
                path.write_bytes(f"other:{index}".encode())
                session.add(
                    BatchScan(
                        batch_id=batch_id, batch_index=index, source_path=str(path),
                        filename=path.name, status="completed", identifier_value=identifier,
                        set_code_value="1",
                        result_json=json.dumps(make_result(path, identifier, "1", 9).to_dict()),
                    )
                )
        review_store.sync_duplicate_identifiers(exam.database, batch_id)
        after = snapshot(exam.database, exam)
        foreign = session_population.population(exam.database, batch_id).effective
        # The other session's own pair is its duplicate, and only its.
        assert set(after["duplicates"]) - set(before["duplicates"]) == foreign
        after["duplicates"] = before["duplicates"]
        assert after == before

    def test_e7_e8_e9_history_kept_reopen_changes_nothing(self, exam):
        before = snapshot(exam.database, exam)
        with exam.database.session() as session:
            counts = {
                model.__tablename__: session.scalar(select(func.count()).select_from(model))
                for model in (BatchScan, ScanBatch, ScanSession, AuditEvent, ReviewConflict)
            }
        root = exam.session.root
        scans = exam.session.project.layout.scans_original_dir
        exam.session.close()
        # E9: the population is rebuilt from the database, not the images.
        for path in scans.rglob("*"):
            if path.is_file():
                path.unlink()
        for _ in range(2):
            with open_project(root) as reopened:
                with reopened.database.session() as session:
                    assert {
                        model.__tablename__: session.scalar(
                            select(func.count()).select_from(model)
                        )
                        for model in (BatchScan, ScanBatch, ScanSession, AuditEvent, ReviewConflict)
                    } == counts, "opening must not add sheets, batches, sessions or events"
                again = snapshot(reopened.database, exam)
                assert again == before

    def test_e8_running_the_passes_again_changes_nothing(self, exam):
        before = snapshot(exam.database, exam)
        with exam.database.session() as session:
            events = session.scalar(select(func.count()).select_from(AuditEvent))
        for batch in exam.batches:
            review_store.sync_duplicate_identifiers(exam.database, batch)
            review_store.sync_undefined_set_codes(exam.database, batch, session_wide=True)
        with exam.database.session() as session:
            assert session.scalar(select(func.count()).select_from(AuditEvent)) == events
        assert snapshot(exam.database, exam) == before


def test_a_session_with_one_batch_reads_as_before(workspace: Path, tmp_path: Path):
    """The single-batch session - every project before phase 4 - is unchanged."""
    session = create_project(workspace, "Single batch")
    try:
        exam = Exam(session, tmp_path)
        rows = [
            (sheet(code, number), roll(code, number), code, correct_of(code, number))
            for code in SETS
            for number in range(1, 4)
        ]
        batch = exam.read_batch(rows)
        population = session_population.population(exam.database, batch)
        assert population.key_batch_id == batch
        assert population.effective == set(exam.ids.values())
        assert attendance(exam.database, exam, "1", batch)[roll("1", 1)] == {
            exam.ids[sheet("1", 1)]
        }
    finally:
        session.close()

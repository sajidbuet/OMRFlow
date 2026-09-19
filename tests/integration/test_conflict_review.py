"""End-to-end conflict review: recognition through to provenance (Phase 6).

Scope:
    The real recognition engine, real rendered sheets, a real project database
    and the real review services. Nothing here is hand-built, because the claim
    Phase 6 makes - "every final value traces back to the machine or to a named
    human correction" - is a claim about what happens when those pieces are
    wired together.

Mapping to the Phase 6 scenarios:
    ========= ==============================================================
    Scenario  Covered by
    ========= ==============================================================
    A         :class:`TestScenarioAMultipleAnswer`
    B         :class:`TestScenarioBAcceptMachineValue`
    C         :class:`TestScenarioCReReviewKeepsHistory`
    D         :class:`TestScenarioDSurvivesRestart`
    E         :class:`TestScenarioEDuplicateIdentifier`
    ========= ==============================================================

Why the sheets are rendered rather than faked:
    Each scenario starts from marks on a page, so the conflicts under test are
    the ones the engine genuinely produces - not the ones a fixture author
    assumed it would.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import pytest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.database import open_project_database
from omr_scanner.domain.review import (
    ConflictState,
    ConflictType,
    ReasonCode,
    ReviewAction,
    ValueSource,
)
from omr_scanner.services import batch_store, review_store
from omr_scanner.services.batch_processor import BatchOptions, process_batch
from omr_scanner.services.scan_export import render_scan_results

if TYPE_CHECKING:
    from collections.abc import Iterator

    from omr_scanner.database import ProjectDatabase

REVIEWER = "Dr. Rahman"
SECOND_REVIEWER = "Dr. Haque"


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


def sheet_marks(roll: str = "120317", **overrides: object) -> dict:
    """Standard marks, with per-zone overrides."""
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
    def make(name: str, marks: dict | None = None) -> Path:
        path = scans_dir / name
        cv2.imwrite(str(path), render_marked_sheet(template, marks or sheet_marks()))
        return path

    return make


def run_and_detect(
    database,
    template,
    paths: list[Path],
    workers: int = 1,
    reports: list | None = None,
) -> str:
    """Process a batch the way the Scan page does, and detect its conflicts.

    Mirrors the real sequence exactly: results are recorded as they finish,
    then conflicts are detected in the coordinator from the finished results,
    then the batch-level duplicate pass runs. No worker process ever touches
    the database.
    """
    batch_id = batch_store.create_batch(
        database, paths, identity=batch_store.BatchIdentity.of(template)
    )
    recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)
    report = process_batch(
        paths,
        template,
        options=BatchOptions(),
        on_result=recorder.record,
        workers=workers,
    )
    recorder.flush()
    batch_store.finalise_batch(database, batch_id)
    if reports is not None:
        reports.append(report)

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


def only_conflict(database, batch_id, conflict_type: ConflictType):
    """The single conflict of ``conflict_type`` in the batch."""
    found = [
        item
        for item in review_store.list_conflicts(database, batch_id)
        if item.conflict_type is conflict_type
    ]
    assert len(found) == 1, f"expected exactly one {conflict_type.value}, got {len(found)}"
    return found[0]


# ----------------------------------------------------------------------
# Scenario A - a double mark, corrected
# ----------------------------------------------------------------------
class TestScenarioAMultipleAnswer:
    @pytest.fixture
    def prepared(self, database, template, make_scan):
        # Question 1 carries two marks. The engine reports "B-C"; a reviewer
        # decides the candidate meant "B".
        marks = sheet_marks()
        marks["questions_0"] = {**marks["questions_0"], 0: ["B", "C"]}
        path = make_scan("double.png", marks)
        batch_id = run_and_detect(database, template, [path])
        return batch_id, only_conflict(database, batch_id, ConflictType.ANSWER_MULTIPLE)

    def test_the_engine_produces_a_multiple_mark_conflict(self, prepared):
        _batch_id, conflict = prepared
        assert conflict.observation.value in ("B-C", "C-B")
        assert conflict.state is ConflictState.OPEN
        assert conflict.field.question_number == 1

    def test_correcting_it_leaves_the_machine_value_and_sets_the_effective_one(
        self, database, prepared
    ):
        _batch_id, conflict = prepared
        machine_value = conflict.observation.value

        found = review_store.correct_value(
            database,
            conflict.conflict_id,
            value="B",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )

        assert found.value == "B"
        assert found.source is ValueSource.HUMAN
        assert found.machine_value == machine_value
        assert found.reviewer == REVIEWER
        # And in storage, not just in the returned object.
        stored = review_store.get_conflict(database, conflict.conflict_id)
        assert stored.observation.value == machine_value
        assert stored.state is ConflictState.RESOLVED

    def test_an_audit_event_exists_for_the_correction(self, database, prepared):
        _batch_id, conflict = prepared
        review_store.correct_value(
            database,
            conflict.conflict_id,
            value="B",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )
        history = review_store.history_for(database, conflict.conflict_id)
        assert [item.action for item in history] == [
            ReviewAction.DETECTED,
            ReviewAction.CORRECTED,
        ]
        assert history[1].reviewer == REVIEWER
        assert history[1].reason_code == ReasonCode.DOMINANT_MARK.value

    def test_the_export_uses_the_corrected_value_and_says_so(
        self, database, template, prepared, make_scan
    ):
        batch_id, conflict = prepared
        review_store.correct_value(
            database,
            conflict.conflict_id,
            value="B",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )

        paths = batch_store.scan_paths(database, batch_id)
        report = process_batch(list(paths), template, workers=1)
        resolutions = review_store.sheet_resolutions(database, batch_id, template)
        csv_text = render_scan_results(
            report.processed, template, resolutions=resolutions
        )

        header, row = csv_text.splitlines()[0].split(","), csv_text.splitlines()[1].split(",")
        record = dict(zip(header, row, strict=True))
        assert record["Q1"] == "B"
        assert record["value_source"] == "human"
        assert record["unresolved_conflicts"] == "0"


# ----------------------------------------------------------------------
# Scenario B - an uncertain digit, machine value accepted
# ----------------------------------------------------------------------
class TestScenarioBAcceptMachineValue:
    @pytest.fixture
    def prepared(self, database, template, make_scan):
        # A deliberately faint mark in one roll-number column lands in the
        # ambiguous band, so the engine flags the digit without deciding it.
        marks = sheet_marks()
        marks["roll_number"] = {**marks["roll_number"], 2: ("0", 0.35)}
        path = make_scan("faint.png", marks)
        batch_id = run_and_detect(database, template, [path])
        found = [
            item
            for item in review_store.list_conflicts(database, batch_id)
            if item.conflict_type
            in (
                ConflictType.IDENTIFIER_UNCERTAIN,
                ConflictType.IDENTIFIER_LOW_CONFIDENCE,
                ConflictType.IDENTIFIER_INCOMPLETE,
            )
        ]
        assert found, "the faint digit should have produced an identifier conflict"
        return batch_id, found[0]

    def test_accepting_leaves_the_value_but_changes_its_source(self, database, prepared):
        _batch_id, conflict = prepared
        before = review_store.provenance_for(database, conflict.conflict_id)
        assert before.source is ValueSource.MACHINE

        after = review_store.accept_machine_value(
            database, conflict.conflict_id, reviewer=REVIEWER
        )
        assert after.value == before.value
        assert after.source is ValueSource.HUMAN
        assert after.reviewer == REVIEWER

    def test_the_audit_records_a_human_confirmation(self, database, prepared):
        _batch_id, conflict = prepared
        review_store.accept_machine_value(
            database, conflict.conflict_id, reviewer=REVIEWER
        )
        history = review_store.history_for(database, conflict.conflict_id)
        assert history[-1].action is ReviewAction.ACCEPTED
        assert history[-1].reviewer == REVIEWER
        # "A human checked this" is recorded distinctly from "nobody looked".
        assert history[-1].reason_code == ReasonCode.MACHINE_CONFIRMED.value


# ----------------------------------------------------------------------
# Scenario C - reopened and corrected again
# ----------------------------------------------------------------------
class TestScenarioCReReviewKeepsHistory:
    @pytest.fixture
    def prepared(self, database, template, make_scan):
        marks = sheet_marks()
        marks["questions_0"] = {**marks["questions_0"], 0: ["B", "D"]}
        path = make_scan("rereview.png", marks)
        batch_id = run_and_detect(database, template, [path])
        return batch_id, only_conflict(database, batch_id, ConflictType.ANSWER_MULTIPLE)

    def test_two_reviewers_two_corrections_one_machine_value(self, database, prepared):
        _batch_id, conflict = prepared
        machine_value = conflict.observation.value

        review_store.correct_value(
            database,
            conflict.conflict_id,
            value="C",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )
        review_store.reopen(
            database, conflict.conflict_id, reviewer=SECOND_REVIEWER,
            reason_text="Second opinion requested",
        )
        review_store.correct_value(
            database,
            conflict.conflict_id,
            value="D",
            reviewer=SECOND_REVIEWER,
            reason=ReasonCode.MISCLASSIFICATION,
        )

        found = review_store.provenance_for(database, conflict.conflict_id)
        assert found.value == "D"
        assert found.reviewer == SECOND_REVIEWER
        assert found.machine_value == machine_value

    def test_the_first_correction_is_not_rewritten(self, database, prepared):
        _batch_id, conflict = prepared
        review_store.correct_value(
            database,
            conflict.conflict_id,
            value="C",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )
        review_store.reopen(database, conflict.conflict_id, reviewer=SECOND_REVIEWER)
        review_store.correct_value(
            database,
            conflict.conflict_id,
            value="D",
            reviewer=SECOND_REVIEWER,
            reason=ReasonCode.MISCLASSIFICATION,
        )

        history = review_store.history_for(database, conflict.conflict_id)
        assert [item.action for item in history] == [
            ReviewAction.DETECTED,
            ReviewAction.CORRECTED,
            ReviewAction.REOPENED,
            ReviewAction.CORRECTED,
        ]
        # The history reads "machine read X, Dr. Rahman made it C, Dr. Haque
        # reopened it, Dr. Haque made it D" - never as though the first
        # reviewer had chosen D all along.
        assert history[1].reviewer == REVIEWER
        assert history[1].new_value == "C"
        assert history[3].reviewer == SECOND_REVIEWER
        assert history[3].new_value == "D"


# ----------------------------------------------------------------------
# Scenario D - closing and reopening the application
# ----------------------------------------------------------------------
class TestScenarioDSurvivesRestart:
    def test_states_history_and_effective_values_all_survive(
        self, tmp_path, template, scans_dir
    ):
        db_path = tmp_path / "database.sqlite"

        marks = sheet_marks()
        marks["questions_0"] = {**marks["questions_0"], 0: ["B", "D"], 1: ["A", "C"]}
        first_scan = scans_dir / "one.png"
        cv2.imwrite(str(first_scan), render_marked_sheet(template, marks))
        second_scan = scans_dir / "two.png"
        cv2.imwrite(str(second_scan), render_marked_sheet(template, sheet_marks("120318")))

        first = open_project_database(db_path, create=True)
        batch_id = run_and_detect(first, template, [first_scan, second_scan])
        conflicts = [
            item
            for item in review_store.list_conflicts(first, batch_id)
            if item.conflict_type is ConflictType.ANSWER_MULTIPLE
        ]
        assert len(conflicts) >= 2

        review_store.correct_value(
            first,
            conflicts[0].conflict_id,
            value="B",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
            reason_text="Bubble B visibly darker",
        )
        review_store.defer(first, conflicts[1].conflict_id, reviewer=REVIEWER)
        resolved_id = conflicts[0].conflict_id
        deferred_id = conflicts[1].conflict_id
        machine_value = conflicts[0].observation.value
        first.close()

        # Restart.
        second = open_project_database(db_path)
        try:
            resolved = review_store.get_conflict(second, resolved_id)
            deferred = review_store.get_conflict(second, deferred_id)
            assert resolved.state is ConflictState.RESOLVED
            assert deferred.state is ConflictState.DEFERRED

            found = review_store.provenance_for(second, resolved_id)
            assert found.value == "B"
            assert found.reviewer == REVIEWER
            assert found.reason_text == "Bubble B visibly darker"
            assert found.machine_value == machine_value

            history = review_store.history_for(second, resolved_id)
            assert [item.action for item in history] == [
                ReviewAction.DETECTED,
                ReviewAction.CORRECTED,
            ]

            counts = review_store.count_conflicts(second, batch_id)
            assert counts.resolved >= 1
            assert counts.deferred >= 1
        finally:
            second.close()

    def test_reopening_the_batch_does_not_duplicate_its_conflicts(
        self, database, template, make_scan
    ):
        # The Phase 5 interaction: resuming or re-running a batch must not fill
        # the queue with second copies of every conflict.
        marks = sheet_marks()
        marks["questions_0"] = {**marks["questions_0"], 0: ["B", "D"]}
        path = make_scan("resume.png", marks)
        batch_id = run_and_detect(database, template, [path])
        before = len(review_store.list_conflicts(database, batch_id))

        ids = batch_store.scan_ids_by_path(database, batch_id)
        report = process_batch([path], template, workers=1)
        for item in report.processed:
            review_store.sync_conflicts(
                database,
                batch_id=batch_id,
                scan_id=ids[item.source_path],
                result=item.result,
                template=template,
            )
        review_store.sync_duplicate_identifiers(database, batch_id)

        assert len(review_store.list_conflicts(database, batch_id)) == before


# ----------------------------------------------------------------------
# Scenario E - a duplicate identifier
# ----------------------------------------------------------------------
class TestScenarioEDuplicateIdentifier:
    @pytest.fixture
    def prepared(self, database, template, make_scan):
        first = make_scan("dup_one.png", sheet_marks("170501"))
        second = make_scan("dup_two.png", sheet_marks("170501"))
        third = make_scan("unique.png", sheet_marks("170502"))
        batch_id = run_and_detect(database, template, [first, second, third])
        return batch_id

    def test_a_batch_level_conflict_is_raised_for_both_sheets(self, database, prepared):
        found = [
            item
            for item in review_store.list_conflicts(database, prepared)
            if item.conflict_type is ConflictType.IDENTIFIER_DUPLICATE
        ]
        assert len(found) == 2
        assert {item.observation.value for item in found} == {"170501"}

    def test_each_sheet_points_at_the_other(self, database, prepared):
        found = sorted(
            (
                item
                for item in review_store.list_conflicts(database, prepared)
                if item.conflict_type is ConflictType.IDENTIFIER_DUPLICATE
            ),
            key=lambda item: item.scan_id,
        )
        assert found[0].related_scan_ids == (found[1].scan_id,)
        assert found[1].related_scan_ids == (found[0].scan_id,)

    def test_the_unique_sheet_has_no_duplicate_conflict(self, database, prepared):
        duplicates = {
            item.scan_id
            for item in review_store.list_conflicts(database, prepared)
            if item.conflict_type is ConflictType.IDENTIFIER_DUPLICATE
        }
        assert len(duplicates) == 2

    def test_a_resolution_is_recorded_against_one_of_them(self, database, prepared):
        found = next(
            item
            for item in review_store.list_conflicts(database, prepared)
            if item.conflict_type is ConflictType.IDENTIFIER_DUPLICATE
        )
        review_store.correct_value(
            database,
            found.conflict_id,
            value="170503",
            reviewer=REVIEWER,
            reason=ReasonCode.MISCLASSIFICATION,
            reason_text="Second sheet was miscoded by the candidate",
        )
        after = review_store.provenance_for(database, found.conflict_id)
        assert after.value == "170503"
        assert after.machine_value == "170501"
        assert after.reviewer == REVIEWER

    def test_re_detecting_duplicates_is_idempotent(self, database, prepared):
        before = len(review_store.list_conflicts(database, prepared))
        review_store.sync_duplicate_identifiers(database, prepared)
        review_store.sync_duplicate_identifiers(database, prepared)
        assert len(review_store.list_conflicts(database, prepared)) == before


# ----------------------------------------------------------------------
# Every conflict type can be generated from real recognition
# ----------------------------------------------------------------------
class TestConflictTypesFromRealSheets:
    def test_a_blank_identifier_produces_its_own_conflict(
        self, database, template, make_scan
    ):
        marks = sheet_marks()
        marks["roll_number"] = {}
        path = make_scan("blank_id.png", marks)
        batch_id = run_and_detect(database, template, [path])
        types = {item.conflict_type for item in review_store.list_conflicts(database, batch_id)}
        assert ConflictType.IDENTIFIER_BLANK in types

    def test_a_multiply_marked_identifier_column_produces_its_own_conflict(
        self, database, template, make_scan
    ):
        marks = sheet_marks()
        marks["roll_number"] = {**marks["roll_number"], 0: ["1", "7"]}
        path = make_scan("multi_id.png", marks)
        batch_id = run_and_detect(database, template, [path])
        types = {item.conflict_type for item in review_store.list_conflicts(database, batch_id)}
        assert ConflictType.IDENTIFIER_MULTIPLE in types

    def test_a_blank_set_code_produces_its_own_conflict(
        self, database, template, make_scan
    ):
        marks = sheet_marks()
        marks["set_code"] = {}
        path = make_scan("blank_set.png", marks)
        batch_id = run_and_detect(database, template, [path])
        types = {item.conflict_type for item in review_store.list_conflicts(database, batch_id)}
        assert ConflictType.SET_CODE_BLANK in types

    def test_a_multiply_marked_set_code_produces_its_own_conflict(
        self, database, template, make_scan
    ):
        marks = sheet_marks()
        marks["set_code"] = {0: ["A", "B"]}
        path = make_scan("multi_set.png", marks)
        batch_id = run_and_detect(database, template, [path])
        types = {item.conflict_type for item in review_store.list_conflicts(database, batch_id)}
        assert ConflictType.SET_CODE_MULTIPLE in types

    def test_an_undecodable_file_produces_a_sheet_level_conflict(
        self, database, template, scans_dir
    ):
        corrupt = scans_dir / "corrupt.png"
        corrupt.write_bytes(b"not an image at all")
        batch_id = run_and_detect(database, template, [corrupt])
        found = review_store.list_conflicts(database, batch_id)
        assert len(found) == 1
        assert found[0].conflict_type.is_processing_failure is True

    def test_a_clean_sheet_produces_no_conflicts(self, database, template, make_scan):
        # The control: if every sheet raised something, the queue would mean
        # nothing.
        path = make_scan("clean.png", sheet_marks())
        batch_id = run_and_detect(database, template, [path])
        assert review_store.list_conflicts(database, batch_id) == ()


class TestPhase5MultiprocessingIsIntact:
    """Detection must not have quietly cost the batch its worker pool.

    Phase 6's brief lists "multiprocessing is removed" as a failure condition
    outright. Detection runs in the coordinator *after* the batch precisely so
    that it cannot interfere - and the honest way to assert that is to run the
    same sheets on one worker and on several, and compare the conflicts rather
    than the timings.
    """

    def _conflict_signature(self, database, batch_id) -> list[tuple]:
        return sorted(
            (
                item.conflict_type.value,
                item.field.zone_id,
                item.field.group_key,
                item.observation.value,
                item.state.value,
            )
            for item in review_store.list_conflicts(database, batch_id)
        )

    def test_a_multi_worker_batch_produces_identical_conflicts(
        self, tmp_path, template, make_scan
    ):
        # Four sheets, each carrying a different problem, so the comparison is
        # over a real spread rather than four copies of one conflict.
        marks = sheet_marks()
        double = dict(marks)
        double["questions_0"] = {**marks["questions_0"], 0: ["B", "D"]}
        blank_id = {**marks, "roll_number": {}}
        double_set = {**marks, "set_code": {0: ["A", "B"]}}
        paths = [
            make_scan("mp_clean.png", marks),
            make_scan("mp_double.png", double),
            make_scan("mp_blank_id.png", blank_id),
            make_scan("mp_double_set.png", double_set),
        ]

        signatures = []
        for workers in (1, 4):
            db_path = tmp_path / f"workers_{workers}.sqlite"
            reports: list = []
            handle = open_project_database(db_path, create=True)
            try:
                batch_id = run_and_detect(
                    handle, template, paths, workers=workers, reports=reports
                )
                # Otherwise the comparison is between two sequential runs and
                # asserts nothing about multiprocessing at all.
                assert reports[0].worker_count == workers
                found = review_store.list_conflicts(handle, batch_id)
                assert found, "the four sheets should raise conflicts at any worker count"
                signatures.append(self._conflict_signature(handle, batch_id))
            finally:
                handle.close()

        assert signatures[0] == signatures[1]


class TestMigrationOntoAnExistingProject:
    """A project processed before Phase 6 must keep everything and gain the rest.

    The database is genuinely wound back to schema version 2 - the tables
    dropped, the ledger row deleted - rather than merely created fresh, because
    "a new database has the tables" says nothing about whether the *upgrade*
    path works on a project that already holds a batch.
    """

    def _wind_back_to_version_2(self, db_path: Path) -> None:
        import sqlite3

        connection = sqlite3.connect(db_path)
        try:
            connection.executescript(
                """
                DROP TRIGGER IF EXISTS audit_event_is_append_only_update;
                DROP TRIGGER IF EXISTS audit_event_is_append_only_delete;
                DROP TABLE IF EXISTS audit_event;
                DROP TABLE IF EXISTS review_conflict;
                DELETE FROM schema_migration WHERE version >= 3;
                """
            )
            connection.commit()
        finally:
            connection.close()

    def test_an_existing_batch_survives_the_upgrade_and_becomes_reviewable(
        self, tmp_path, template, make_scan
    ):
        from sqlalchemy import text

        from omr_scanner.database.migrations import SCHEMA_VERSION

        db_path = tmp_path / "database.sqlite"
        marks = sheet_marks(roll="551234")
        marks["questions_0"] = {**marks["questions_0"], 0: ["B", "D"]}
        path = make_scan("legacy.png", marks)

        # A project as Phase 5 would have left it: a processed batch, no review.
        handle = open_project_database(db_path, create=True)
        try:
            batch_id = run_and_detect(handle, template, [path])
            assert review_store.list_conflicts(handle, batch_id) != ()
        finally:
            handle.close()
        self._wind_back_to_version_2(db_path)

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
            assert {"review_conflict", "audit_event"} <= names

            # Phase 5's data is untouched by the upgrade.
            summary = batch_store.load_summary(reopened, batch_id)
            assert summary.processed == 1
            restored = batch_store.completed_results(reopened, batch_id)
            assert [item.identifier_value for item in restored] == ["551234"]

            # The batch carries no conflicts until it is looked at again...
            assert review_store.list_conflicts(reopened, batch_id) == ()

            # ...and detection on the stored results brings them back, with no
            # need to re-read a single image.
            ids = batch_store.scan_ids_by_path(reopened, batch_id)
            for item in restored:
                review_store.sync_conflicts(
                    reopened,
                    batch_id=batch_id,
                    scan_id=ids[item.source_path],
                    result=item,
                    template=template,
                )
            recovered = review_store.list_conflicts(reopened, batch_id)
            assert ConflictType.ANSWER_MULTIPLE in {
                item.conflict_type for item in recovered
            }

            # And the re-created ledger is append-only again, not merely present.
            review_store.correct_value(
                reopened,
                only_conflict(
                    reopened, batch_id, ConflictType.ANSWER_MULTIPLE
                ).conflict_id,
                value="B",
                reviewer=REVIEWER,
                reason=ReasonCode.DOMINANT_MARK,
            )
            with reopened.session() as session, pytest.raises(Exception, match="append-only"):
                session.execute(text("DELETE FROM audit_event"))
        finally:
            reopened.close()


class TestOriginalScansAreUntouched:
    def test_review_never_modifies_a_source_scan(
        self, database, template, make_scan
    ):
        # Phase 5's invariant, still in force. Review draws overlays; it never
        # writes an annotation back to the file.
        import hashlib

        marks = sheet_marks()
        marks["questions_0"] = {**marks["questions_0"], 0: ["B", "D"]}
        path = make_scan("untouched.png", marks)
        before = hashlib.sha256(path.read_bytes()).hexdigest()

        batch_id = run_and_detect(database, template, [path])
        conflict = only_conflict(database, batch_id, ConflictType.ANSWER_MULTIPLE)
        review_store.correct_value(
            database,
            conflict.conflict_id,
            value="B",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )

        assert hashlib.sha256(path.read_bytes()).hexdigest() == before

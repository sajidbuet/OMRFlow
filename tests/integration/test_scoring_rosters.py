"""Which candidate lists a batch is scored against.

A project that has defined its sets imports one attendance list per set, and
has no project-wide list at all. Scoring only the project-wide list - what the
Results stage once did - finds nothing to mark in such a project.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omr_scanner.domain.reconciliation import AttendanceState, CandidateRecord
from omr_scanner.services import project_sets, reconciliation_store, scoring_store
from omr_scanner.services.candidate_import import ColumnMapping, RosterValidation

if TYPE_CHECKING:
    from omr_scanner.services import ProjectSession


def roster(*candidate_ids: str) -> RosterValidation:
    records = tuple(
        CandidateRecord(
            candidate_id=candidate_id,
            display_name=f"Candidate {index}",
            source_row=index + 2,
            imported_attendance=AttendanceState.PRESENT,
            imported_value="---",
        )
        for index, candidate_id in enumerate(candidate_ids)
    )
    return RosterValidation(
        candidates=records,
        issues=(),
        rows_read=len(records),
        blank_ids=0,
        duplicate_ids=0,
        expected_present=len(records),
        expected_absent=0,
        attendance_unknown=0,
        mapping=ColumnMapping(candidate_id=0, name=1, attendance=2),
        source_name="roster.csv",
    )


def test_a_project_with_no_list_has_nothing_to_score(project_session: ProjectSession):
    assert scoring_store.scoring_rosters(project_session.database) == ()


def test_a_project_without_sets_scores_its_one_list(project_session: ProjectSession):
    database = project_session.database
    only = reconciliation_store.import_roster(database, roster("1001", "1002"))
    assert scoring_store.scoring_rosters(database) == (only,)


def test_every_defined_sets_list_is_scored_in_set_order(project_session: ProjectSession):
    database = project_session.database
    sets = [project_sets.add_set(database, code) for code in ("12", "10", "11")]
    ids = {
        exam_set.code: reconciliation_store.import_roster(
            database, roster(f"{exam_set.code}01"), set_id=exam_set.set_id
        )
        for exam_set in reversed(sets)
    }
    assert scoring_store.scoring_rosters(database) == (ids["12"], ids["10"], ids["11"])


def test_a_replaced_list_is_scored_instead_of_the_old_one(project_session: ProjectSession):
    database = project_session.database
    exam_set = project_sets.add_set(database, "A")
    reconciliation_store.import_roster(database, roster("1"), set_id=exam_set.set_id)
    newer = reconciliation_store.import_roster(database, roster("1", "2"), set_id=exam_set.set_id)
    assert scoring_store.scoring_rosters(database) == (newer,)


def test_a_set_without_attendance_is_skipped_and_a_legacy_list_kept(
    project_session: ProjectSession,
):
    database = project_session.database
    legacy = reconciliation_store.import_roster(database, roster("9001"))
    with_list = project_sets.add_set(database, "A")
    project_sets.add_set(database, "B")  # no attendance yet
    scoped = reconciliation_store.import_roster(database, roster("A1"), set_id=with_list.set_id)
    assert scoring_store.scoring_rosters(database) == (scoped, legacy)

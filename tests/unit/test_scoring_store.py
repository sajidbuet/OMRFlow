"""Answer-key revisions, scoring policy, results and recomputation (Phase 8).

Scope:
    :mod:`omr_scanner.services.scoring_store` against a real SQLite file: key
    revisions and verification, policy revisions, `CandidateResult` rows, the
    staleness rules, and the recomputation the whole phase turns on.

Why a real database rather than a mock:
    Every property worth asserting here belongs to the database - the unique
    constraint on ``(set_code, revision)``, the superseding of an earlier
    verified key, and the fact that a result still points at the revision that
    produced it after the key has moved on.

Privacy:
    Every identifier and name here is fictional.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from fractions import Fraction
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from tests.conftest import build_answer_sheet_template

from omr_scanner.database import open_project_database
from omr_scanner.database.models import BatchScan, CandidateResult, ScanBatch
from omr_scanner.domain.reconciliation import AttendanceState, CandidateRecord
from omr_scanner.domain.scoring import (
    AnswerKeyStatus,
    BlockReason,
    NegativeMarking,
    ResultStatus,
    ScoringPolicy,
    StaleReason,
)
from omr_scanner.services import batch_store, reconciliation_store, scoring_store
from omr_scanner.services.answer_key import plan_for, read_key
from omr_scanner.services.candidate_import import ColumnMapping, RosterValidation
from omr_scanner.services.recognition_models import (
    AnswerView,
    FieldView,
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
)
from omr_scanner.services.scoring_store import ScoringError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from omr_scanner.database import ProjectDatabase

OPERATOR = "Dr. X"


@pytest.fixture(scope="module")
def template():
    return build_answer_sheet_template()


@pytest.fixture(scope="module")
def plan(template):
    return plan_for(template)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[ProjectDatabase]:
    handle = open_project_database(tmp_path / "database.sqlite", create=True)
    try:
        yield handle
    finally:
        handle.close()


def validation_of(candidates: list[tuple[str, str, AttendanceState]]) -> RosterValidation:
    """A RosterValidation as the importer would have produced one."""
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
        issues=(),
        rows_read=len(records),
        blank_ids=0,
        duplicate_ids=0,
        expected_present=sum(
            1 for i in records if i.imported_attendance is AttendanceState.PRESENT
        ),
        expected_absent=sum(
            1 for i in records if i.imported_attendance is AttendanceState.ABSENT
        ),
        attendance_unknown=0,
        mapping=ColumnMapping(candidate_id=0, name=1, attendance=2),
        source_name="roster.csv",
    )


PRESENT = AttendanceState.PRESENT
ABSENT = AttendanceState.ABSENT

ROSTER = [
    ("10001", "CAND A", PRESENT),
    ("10002", "CAND B", PRESENT),
    ("10003", "CAND C", ABSENT),
    ("10004", "CAND D", PRESENT),
]


def make_result(path, roll: str, set_code: str, answers: dict[int, str], numbers):
    """A ScanResult carrying the given answers."""
    return ScanResult(
        source_path=path,
        outcome=RecognitionOutcome.COMPLETE,
        registration=RegistrationStatus.REGISTERED,
        fields=(
            FieldView(
                zone_id="roll_number", label="Roll", field_type="numeric",
                value=roll, status="complete", needs_review=False, characters=(),
            ),
            FieldView(
                zone_id="set_code", label="Set", field_type="set_code",
                value=set_code, status="complete", needs_review=False, characters=(),
            ),
        ),
        answers=tuple(
            AnswerView(
                number=number, zone_id="q", value=answers.get(number, "A"),
                status="resolved", needs_review=False,
                top_fill=0.9, margin=0.4, confidence=0.9,
            )
            for number in numbers
        ),
        identifier_zone_id="roll_number",
        set_code_zone_id="set_code",
    )


@pytest.fixture
def prepared(database, template, plan, tmp_path):
    """A reconciled batch, a verified Set A key, and the default policy."""
    roster_id = reconciliation_store.import_roster(
        database, validation_of(ROSTER), imported_by=OPERATOR
    )
    rows = [
        ("s1.png", "10001", "A", {}),
        ("s2.png", "10002", "A", dict.fromkeys(plan.numbers[:3], "B")),
        ("s4.png", "10004", "B", {}),
    ]
    now = datetime.now(UTC)
    batch_id = batch_store.new_batch_id()
    with database.session() as session:
        session.add(
            ScanBatch(
                batch_id=batch_id, created_at=now, updated_at=now,
                source_folder=str(tmp_path), status="completed", total_scans=len(rows),
            )
        )
        session.flush()
        for index, (name, roll, set_code, answers) in enumerate(rows):
            result = make_result(tmp_path / name, roll, set_code, answers, plan.numbers)
            session.add(
                BatchScan(
                    batch_id=batch_id, batch_index=index,
                    source_path=str(tmp_path / name), filename=name,
                    status="completed", identifier_value=roll,
                    set_code_value=set_code,
                    result_json=json.dumps(result.to_dict()),
                )
            )
    reconciliation_store.reconcile_batch(database, roster_id, batch_id)

    stored = scoring_store.save_key(
        database, read_key("A" * plan.question_count, plan, "A").to_key()
    )
    scoring_store.verify_key(database, stored.key_id, verified_by=OPERATOR)
    return roster_id, batch_id


def results_by_id(database, roster_id, batch_id, template):
    return {
        item.candidate_id: item
        for item in scoring_store.list_results(database, roster_id, batch_id, template)
    }


# ----------------------------------------------------------------------
class TestAnswerKeyRevisions:
    def test_a_key_is_stored_as_revision_one(self, database, plan):
        stored = scoring_store.save_key(
            database, read_key("A" * plan.question_count, plan, "A").to_key()
        )
        assert stored.revision == 1
        assert stored.key.status is AnswerKeyStatus.DRAFT

    def test_saving_again_creates_the_next_revision(self, database, plan):
        key = read_key("A" * plan.question_count, plan, "A").to_key()
        first = scoring_store.save_key(database, key)
        second = scoring_store.save_key(database, key)
        assert (first.revision, second.revision) == (1, 2)

    def test_the_store_owns_revision_numbering(self, database, plan):
        # A caller's revision number is ignored, so two operators cannot both
        # create "revision 2".
        import dataclasses

        key = read_key("A" * plan.question_count, plan, "A").to_key()
        forged = dataclasses.replace(key, revision=99)
        assert scoring_store.save_key(database, forged).revision == 1

    def test_different_sets_number_independently(self, database, plan):
        answers = "A" * plan.question_count
        a = scoring_store.save_key(database, read_key(answers, plan, "A").to_key())
        b = scoring_store.save_key(database, read_key(answers, plan, "B").to_key())
        assert a.revision == b.revision == 1

    def test_multi_character_set_codes_are_kept(self, database, plan):
        stored = scoring_store.save_key(
            database, read_key("A" * plan.question_count, plan, "10").to_key()
        )
        assert stored.set_code == "10"
        assert scoring_store.verified_key(database, "10") is None, "not yet verified"

    def test_wrong_questions_travel_with_the_revision(self, database, plan):
        key = read_key(
            "A" * plan.question_count, plan, "A", wrong_questions=[2, 5]
        ).to_key()
        stored = scoring_store.save_key(database, key)
        assert scoring_store.get_key(database, stored.key_id).key.wrong_questions == {2, 5}


class TestVerification:
    def test_verifying_records_who_checked_it(self, database, plan):
        stored = scoring_store.save_key(
            database, read_key("A" * plan.question_count, plan, "A").to_key()
        )
        verified = scoring_store.verify_key(
            database, stored.key_id, verified_by=OPERATOR
        )
        assert verified.key.status is AnswerKeyStatus.VERIFIED
        assert verified.verified_by == OPERATOR
        assert verified.verified_at is not None

    def test_verifying_without_a_name_is_refused(self, database, plan):
        stored = scoring_store.save_key(
            database, read_key("A" * plan.question_count, plan, "A").to_key()
        )
        with pytest.raises(ScoringError) as caught:
            scoring_store.verify_key(database, stored.key_id, verified_by="  ")
        assert "Settings" in caught.value.user_message
        assert scoring_store.verified_key(database, "A") is None

    def test_verifying_supersedes_the_previous_revision(self, database, plan):
        answers = "A" * plan.question_count
        first = scoring_store.save_key(database, read_key(answers, plan, "A").to_key())
        scoring_store.verify_key(database, first.key_id, verified_by=OPERATOR)
        second = scoring_store.save_key(
            database, read_key("B" * plan.question_count, plan, "A").to_key()
        )
        scoring_store.verify_key(database, second.key_id, verified_by=OPERATOR)

        assert scoring_store.get_key(database, first.key_id).key.status is (
            AnswerKeyStatus.SUPERSEDED
        )
        assert scoring_store.verified_key(database, "A").revision == 2

    def test_a_superseded_revision_is_kept_not_deleted(self, database, plan):
        # A result points at it, and must still be able to say what produced it.
        answers = "A" * plan.question_count
        first = scoring_store.save_key(database, read_key(answers, plan, "A").to_key())
        scoring_store.verify_key(database, first.key_id, verified_by=OPERATOR)
        second = scoring_store.save_key(database, read_key(answers, plan, "A").to_key())
        scoring_store.verify_key(database, second.key_id, verified_by=OPERATOR)
        kept = scoring_store.get_key(database, first.key_id)
        assert kept is not None
        assert kept.key.answers == answers

    def test_a_superseded_revision_cannot_be_reverified(self, database, plan):
        answers = "A" * plan.question_count
        first = scoring_store.save_key(database, read_key(answers, plan, "A").to_key())
        scoring_store.verify_key(database, first.key_id, verified_by=OPERATOR)
        second = scoring_store.save_key(database, read_key(answers, plan, "A").to_key())
        scoring_store.verify_key(database, second.key_id, verified_by=OPERATOR)
        with pytest.raises(ScoringError, match="superseded"):
            scoring_store.verify_key(database, first.key_id, verified_by=OPERATOR)

    def test_verifying_an_unknown_key_is_refused(self, database):
        with pytest.raises(ScoringError, match="not found"):
            scoring_store.verify_key(database, 999, verified_by=OPERATOR)


class TestScoringPolicyRevisions:
    def test_a_project_starts_with_a_conservative_default(self, database):
        stored = scoring_store.active_policy(database)
        assert stored.revision == 1
        assert stored.policy.mode is NegativeMarking.NONE
        assert stored.policy.correct_mark == 1

    def test_saving_an_unchanged_policy_creates_no_revision(self, database):
        first = scoring_store.active_policy(database)
        again = scoring_store.save_policy(database, first.policy)
        assert again.revision == first.revision
        assert len(scoring_store.list_policies(database)) == 1

    def test_changing_a_rule_creates_the_next_revision(self, database):
        scoring_store.active_policy(database)
        changed = scoring_store.save_policy(
            database,
            ScoringPolicy(
                correct_mark=Fraction(1),
                incorrect_penalty=Fraction(1, 4),
                mode=NegativeMarking.FIXED,
            ),
        )
        assert changed.revision == 2
        assert scoring_store.active_policy(database).revision == 2

    def test_an_earlier_revision_is_kept(self, database):
        first = scoring_store.active_policy(database)
        scoring_store.save_policy(
            database, ScoringPolicy(correct_mark=Fraction(2))
        )
        kept = scoring_store.get_policy(database, first.policy_id)
        assert kept is not None and kept.policy.correct_mark == 1

    @pytest.mark.parametrize(
        "policy",
        [
            ScoringPolicy(correct_mark=Fraction(2)),
            ScoringPolicy(blank_mark=Fraction(1, 4)),
            ScoringPolicy(incorrect_penalty=Fraction(1, 3), mode=NegativeMarking.FIXED),
            ScoringPolicy(mode=NegativeMarking.ONE_PER_THREE),
            ScoringPolicy(clamp_minimum=False),
            ScoringPolicy(minimum_score=Fraction(-5)),
            ScoringPolicy(multiple_penalty=Fraction(1, 2), mode=NegativeMarking.FIXED),
        ],
    )
    def test_every_score_affecting_field_creates_a_revision(self, database, policy):
        scoring_store.active_policy(database)
        assert scoring_store.save_policy(database, policy).revision == 2

    def test_exact_marks_survive_a_round_trip(self, database):
        stored = scoring_store.save_policy(
            database,
            ScoringPolicy(
                correct_mark=Fraction(1),
                incorrect_penalty=Fraction(1, 3),
                mode=NegativeMarking.FIXED,
            ),
        )
        read_back = scoring_store.get_policy(database, stored.policy_id)
        assert read_back.policy.incorrect_penalty == Fraction(1, 3)


class TestScoringABatch:
    def test_candidates_are_scored_against_their_own_set(
        self, database, prepared, template, plan
    ):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        found = results_by_id(database, roster_id, batch_id, template)
        assert found["10001"].final_score == plan.question_count
        assert found["10002"].final_score == plan.question_count - 3
        # 10004 sat Set B, which has no verified key.
        assert found["10004"].status is ResultStatus.BLOCKED

    def test_an_absent_candidate_has_no_mark(self, database, prepared, template):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        absent = results_by_id(database, roster_id, batch_id, template)["10003"]
        assert absent.status is ResultStatus.ABSENT
        assert absent.final_score is None
        assert absent.has_mark is False

    def test_a_missing_key_blocks_with_a_reason(self, database, prepared, template):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        blocked = results_by_id(database, roster_id, batch_id, template)["10004"]
        assert [item.reason for item in blocked.blocks] == [BlockReason.NO_VERIFIED_KEY]
        assert "Set B" in blocked.describe_blocks()

    def test_an_unverified_key_produces_no_marks(self, database, template, plan, tmp_path):
        roster_id = reconciliation_store.import_roster(
            database, validation_of([("10001", "CAND A", PRESENT)])
        )
        now = datetime.now(UTC)
        batch_id = batch_store.new_batch_id()
        with database.session() as session:
            session.add(
                ScanBatch(
                    batch_id=batch_id, created_at=now, updated_at=now,
                    source_folder=str(tmp_path), status="completed", total_scans=1,
                )
            )
            session.flush()
            result = make_result(tmp_path / "s.png", "10001", "A", {}, plan.numbers)
            session.add(
                BatchScan(
                    batch_id=batch_id, batch_index=0,
                    source_path=str(tmp_path / "s.png"), filename="s.png",
                    status="completed", identifier_value="10001", set_code_value="A",
                    result_json=json.dumps(result.to_dict()),
                )
            )
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        scoring_store.save_key(
            database, read_key("A" * plan.question_count, plan, "A").to_key()
        )
        counts = scoring_store.score_batch(database, roster_id, batch_id, template)
        assert counts.scored == 0

    def test_the_key_revision_used_is_recorded(self, database, prepared, template):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        scored = results_by_id(database, roster_id, batch_id, template)["10001"]
        assert scored.answer_key_revision == 1
        assert scored.policy_revision == 1
        assert "Set A / revision 1" in scored.describe_provenance()

    def test_the_answer_string_is_stored(self, database, prepared, template, plan):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        scored = results_by_id(database, roster_id, batch_id, template)["10001"]
        assert len(scored.answer_string) == plan.question_count
        assert scored.machine_answer_string == scored.answer_string

    def test_scoring_twice_produces_the_same_result(self, database, prepared, template):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        first = results_by_id(database, roster_id, batch_id, template)
        scoring_store.score_batch(database, roster_id, batch_id, template)
        second = results_by_id(database, roster_id, batch_id, template)
        assert {k: v.final_score for k, v in first.items()} == {
            k: v.final_score for k, v in second.items()
        }
        assert len(first) == len(second)

    def test_one_row_per_candidate_however_often_it_runs(
        self, database, prepared, template
    ):
        roster_id, batch_id = prepared
        for _ in range(3):
            scoring_store.score_batch(database, roster_id, batch_id, template)
        with database.session() as session:
            rows = session.scalars(select(CandidateResult)).all()
        assert len(rows) == len(ROSTER)

    def test_a_cancelled_run_writes_nothing(self, database, prepared, template):
        roster_id, batch_id = prepared
        counts = scoring_store.score_batch(
            database, roster_id, batch_id, template, should_cancel=lambda: True
        )
        assert counts.registered == 0
        with database.session() as session:
            assert session.scalars(select(CandidateResult)).all() == []

    def test_progress_is_reported(self, database, prepared, template):
        roster_id, batch_id = prepared
        seen: list[tuple[int, int]] = []
        scoring_store.score_batch(
            database, roster_id, batch_id, template, on_progress=lambda d, t: seen.append((d, t))
        )
        assert seen
        assert seen[-1][0] == seen[-1][1] == len(ROSTER)

    def test_one_candidate_can_be_scored_alone(self, database, prepared, template):
        roster_id, batch_id = prepared
        scoring_store.score_batch(
            database, roster_id, batch_id, template, candidates=("10001",)
        )
        found = results_by_id(database, roster_id, batch_id, template)
        assert set(found) == {"10001"}


class TestUnreadSetCodes:
    """A blank set code is not a set called "_"."""

    def test_a_blank_set_code_is_not_usable(self):
        from omr_scanner.services.scoring import usable_set_code

        # Recognition assembles a field with "_" for an unmarked position, so
        # an unread set code arrives as "_" rather than as an empty string.
        assert usable_set_code("_") == ""
        assert usable_set_code("?") == ""
        assert usable_set_code("1_") == ""
        assert usable_set_code("  ") == ""

    def test_a_real_set_code_survives(self):
        from omr_scanner.services.scoring import usable_set_code

        assert usable_set_code("a") == "A"
        assert usable_set_code(" 10 ") == "10"
        assert usable_set_code("X1") == "X1"

    def test_an_unread_set_blocks_with_the_right_reason(
        self, database, template, plan, tmp_path
    ):
        # Not "no verified answer key for Set _", which sends an operator
        # looking for a key rather than at the sheet.
        roster_id = reconciliation_store.import_roster(
            database, validation_of([("10001", "CAND A", PRESENT)])
        )
        now = datetime.now(UTC)
        batch_id = batch_store.new_batch_id()
        with database.session() as session:
            session.add(
                ScanBatch(
                    batch_id=batch_id, created_at=now, updated_at=now,
                    source_folder=str(tmp_path), status="completed", total_scans=1,
                )
            )
            session.flush()
            result = make_result(tmp_path / "s.png", "10001", "_", {}, plan.numbers)
            session.add(
                BatchScan(
                    batch_id=batch_id, batch_index=0,
                    source_path=str(tmp_path / "s.png"), filename="s.png",
                    status="completed", identifier_value="10001", set_code_value="_",
                    result_json=json.dumps(result.to_dict()),
                )
            )
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        stored = scoring_store.save_key(
            database, read_key("A" * plan.question_count, plan, "A").to_key()
        )
        scoring_store.verify_key(database, stored.key_id, verified_by=OPERATOR)
        scoring_store.score_batch(database, roster_id, batch_id, template)

        found = results_by_id(database, roster_id, batch_id, template)["10001"]
        reasons = {item.reason for item in found.blocks}
        assert BlockReason.SET_MISSING in reasons
        assert BlockReason.NO_VERIFIED_KEY not in reasons
        assert "Set _" not in found.describe_blocks()


class TestStaleness:
    def test_a_policy_change_makes_a_result_stale(self, database, prepared, template):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        scoring_store.save_policy(
            database,
            ScoringPolicy(
                correct_mark=Fraction(1),
                incorrect_penalty=Fraction(1, 4),
                mode=NegativeMarking.FIXED,
            ),
        )
        scored = results_by_id(database, roster_id, batch_id, template)["10001"]
        assert scored.is_stale
        assert StaleReason.SCORING_POLICY in scored.stale_reasons
        assert "scoring configuration has changed" in scored.describe_stale()

    def test_a_key_change_makes_a_result_stale(self, database, prepared, template, plan):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        revised = scoring_store.save_key(
            database, read_key("B" * plan.question_count, plan, "A").to_key()
        )
        scoring_store.verify_key(database, revised.key_id, verified_by=OPERATOR)
        scored = results_by_id(database, roster_id, batch_id, template)["10001"]
        assert StaleReason.ANSWER_KEY in scored.stale_reasons

    def test_a_stale_result_keeps_its_mark(self, database, prepared, template, plan):
        # It is still a true record of what the old inputs produced.
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        before = results_by_id(database, roster_id, batch_id, template)["10001"]
        revised = scoring_store.save_key(
            database, read_key("B" * plan.question_count, plan, "A").to_key()
        )
        scoring_store.verify_key(database, revised.key_id, verified_by=OPERATOR)
        after = results_by_id(database, roster_id, batch_id, template)["10001"]
        assert after.final_score == before.final_score
        assert after.answer_key_revision == 1

    def test_an_absent_result_is_not_stale_after_a_rule_change(
        self, database, prepared, template
    ):
        # A new key or penalty cannot change "did not sit the paper", so
        # counting them would fill the queue with work that changes nothing.
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        scoring_store.save_policy(database, ScoringPolicy(correct_mark=Fraction(2)))
        absent = results_by_id(database, roster_id, batch_id, template)["10003"]
        assert absent.status is ResultStatus.ABSENT
        assert absent.is_stale is False

    def test_a_blocked_result_is_never_stale(self, database, prepared, template):
        # It has no mark to be out of date, and flagging it would bury the
        # reason it was blocked under a second warning.
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        scoring_store.save_policy(database, ScoringPolicy(correct_mark=Fraction(2)))
        blocked = results_by_id(database, roster_id, batch_id, template)["10004"]
        assert blocked.status is ResultStatus.BLOCKED
        assert blocked.is_stale is False

    def test_rescoring_clears_staleness(self, database, prepared, template):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        scoring_store.save_policy(database, ScoringPolicy(correct_mark=Fraction(2)))
        scoring_store.score_batch(database, roster_id, batch_id, template)
        scored = results_by_id(database, roster_id, batch_id, template)["10001"]
        assert scored.is_stale is False
        assert scored.policy_revision == 2

    def test_the_summary_counts_stale_results(self, database, prepared, template):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        scoring_store.save_policy(database, ScoringPolicy(correct_mark=Fraction(2)))
        counts = scoring_store.count_results(database, roster_id, batch_id, template)
        assert counts.stale > 0
        assert counts.is_clear is False


class TestRecomputationNeverPatches:
    """The property the whole phase turns on."""

    def test_a_corrupted_stored_mark_is_ignored(self, database, prepared, template, plan):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)

        # Deliberately corrupt the calculated score, leaving every
        # authoritative input untouched.
        with database.session() as session:
            row = session.scalars(
                select(CandidateResult).where(CandidateResult.candidate_id == "10001")
            ).first()
            row.final_score = "999"
            row.raw_score = "999"
            row.correct_count = 0

        scoring_store.score_batch(database, roster_id, batch_id, template)
        scored = results_by_id(database, roster_id, batch_id, template)["10001"]
        assert scored.final_score == plan.question_count, (
            "recomputation must derive the mark from stored inputs, not adjust "
            "the stored number"
        )
        assert scored.correct_count == plan.question_count

    def test_a_policy_change_recomputes_rather_than_adjusting(
        self, database, prepared, template, plan
    ):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        before = results_by_id(database, roster_id, batch_id, template)["10002"]
        assert before.final_score == plan.question_count - 3

        scoring_store.save_policy(
            database,
            ScoringPolicy(
                correct_mark=Fraction(1),
                incorrect_penalty=Fraction(1, 4),
                mode=NegativeMarking.FIXED,
                clamp_minimum=False,
            ),
        )
        scoring_store.score_batch(database, roster_id, batch_id, template)
        after = results_by_id(database, roster_id, batch_id, template)["10002"]
        assert after.final_score == Fraction(plan.question_count - 3) - 3 * Fraction(1, 4)
        assert after.answer_string == before.answer_string, "the inputs did not change"
        assert after.answer_key_revision == before.answer_key_revision
        assert after.policy_revision == before.policy_revision + 1

    def test_the_full_key_then_policy_sequence(
        self, database, prepared, template, plan
    ):
        """Key v1 / policy v1 -> policy v2 -> key v2, as the brief specifies."""
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        r1 = results_by_id(database, roster_id, batch_id, template)["10001"]
        assert (r1.answer_key_revision, r1.policy_revision) == (1, 1)

        scoring_store.save_policy(database, ScoringPolicy(correct_mark=Fraction(2)))
        assert results_by_id(database, roster_id, batch_id, template)["10001"].is_stale
        scoring_store.score_batch(database, roster_id, batch_id, template)
        r2 = results_by_id(database, roster_id, batch_id, template)["10001"]
        assert r2.answer_string == r1.answer_string
        assert r2.set_code == r1.set_code
        assert r2.answer_key_revision == 1
        assert r2.policy_revision == 2
        assert r2.final_score == 2 * plan.question_count

        revised = scoring_store.save_key(
            database, read_key("B" * plan.question_count, plan, "A").to_key()
        )
        scoring_store.verify_key(database, revised.key_id, verified_by=OPERATOR)
        assert results_by_id(database, roster_id, batch_id, template)["10001"].is_stale
        scoring_store.score_batch(database, roster_id, batch_id, template)
        r3 = results_by_id(database, roster_id, batch_id, template)["10001"]
        assert r3.answer_key_revision == 2
        assert r3.final_score == 0, "every answer is now wrong"

    def test_repeated_recomputation_is_idempotent(self, database, prepared, template):
        roster_id, batch_id = prepared
        marks: list[object] = []
        for _ in range(4):
            scoring_store.score_batch(database, roster_id, batch_id, template)
            found = results_by_id(database, roster_id, batch_id, template)
            marks.append(
                {k: (v.final_score, v.correct_count, v.status) for k, v in found.items()}
            )
        assert all(item == marks[0] for item in marks)


class TestBreakdown:
    def test_the_breakdown_is_regenerated_and_agrees_with_the_mark(
        self, database, prepared, template, plan
    ):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        scored = results_by_id(database, roster_id, batch_id, template)["10002"]
        breakdown = scoring_store.breakdown_for(database, scored)
        assert breakdown is not None
        assert breakdown.final_score == scored.final_score
        assert breakdown.question_count == plan.question_count
        assert breakdown.correct_count == scored.correct_count

    def test_a_blocked_result_has_no_breakdown(self, database, prepared, template):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        blocked = results_by_id(database, roster_id, batch_id, template)["10004"]
        assert scoring_store.breakdown_for(database, blocked) is None

    def test_a_breakdown_uses_the_revision_the_result_names(
        self, database, prepared, template, plan
    ):
        # Not the current one: a stale result must explain the mark it has.
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        scored = results_by_id(database, roster_id, batch_id, template)["10001"]
        revised = scoring_store.save_key(
            database, read_key("B" * plan.question_count, plan, "A").to_key()
        )
        scoring_store.verify_key(database, revised.key_id, verified_by=OPERATOR)
        stale = results_by_id(database, roster_id, batch_id, template)["10001"]
        breakdown = scoring_store.breakdown_for(database, stale)
        assert breakdown.final_score == scored.final_score


class TestPersistence:
    def test_everything_survives_closing_and_reopening(
        self, tmp_path, template, plan
    ):
        path = tmp_path / "database.sqlite"
        handle = open_project_database(path, create=True)
        roster_id = reconciliation_store.import_roster(
            handle, validation_of([("10001", "CAND A", PRESENT)])
        )
        now = datetime.now(UTC)
        batch_id = batch_store.new_batch_id()
        with handle.session() as session:
            session.add(
                ScanBatch(
                    batch_id=batch_id, created_at=now, updated_at=now,
                    source_folder=str(tmp_path), status="completed", total_scans=1,
                )
            )
            session.flush()
            result = make_result(tmp_path / "s.png", "10001", "A", {}, plan.numbers)
            session.add(
                BatchScan(
                    batch_id=batch_id, batch_index=0,
                    source_path=str(tmp_path / "s.png"), filename="s.png",
                    status="completed", identifier_value="10001", set_code_value="A",
                    result_json=json.dumps(result.to_dict()),
                )
            )
        reconciliation_store.reconcile_batch(handle, roster_id, batch_id)
        stored = scoring_store.save_key(
            handle, read_key("A" * plan.question_count, plan, "A", wrong_questions=[2]).to_key()
        )
        scoring_store.verify_key(handle, stored.key_id, verified_by=OPERATOR)
        scoring_store.save_policy(
            handle,
            ScoringPolicy(
                correct_mark=Fraction(1),
                incorrect_penalty=Fraction(1, 3),
                mode=NegativeMarking.FIXED,
            ),
        )
        scoring_store.score_batch(handle, roster_id, batch_id, template, computed_by=OPERATOR)
        before = results_by_id(handle, roster_id, batch_id, template)["10001"]
        handle.close()

        reopened = open_project_database(path)
        try:
            after = results_by_id(reopened, roster_id, batch_id, template)["10001"]
            assert after.final_score == before.final_score
            assert after.answer_key_revision == before.answer_key_revision
            assert after.policy_revision == before.policy_revision
            assert after.answer_string == before.answer_string
            assert after.computed_by == OPERATOR
            assert after.is_stale is False

            key = scoring_store.verified_key(reopened, "A")
            assert key is not None and key.key.wrong_questions == {2}
            policy = scoring_store.active_policy(reopened)
            assert policy.policy.incorrect_penalty == Fraction(1, 3)

            # And the result can still explain itself.
            breakdown = scoring_store.breakdown_for(reopened, after)
            assert breakdown is not None
            assert breakdown.final_score == after.final_score
        finally:
            reopened.close()

    def test_results_can_be_cleared(self, database, prepared, template):
        roster_id, batch_id = prepared
        scoring_store.score_batch(database, roster_id, batch_id, template)
        removed = scoring_store.clear_results(database, roster_id, batch_id)
        assert removed == len(ROSTER)
        assert scoring_store.list_results(database, roster_id, batch_id) == ()


class TestMigrationOntoAnExistingPhase7Project:
    """A project reconciled before Phase 8 must keep everything and gain the rest."""

    def test_a_phase_7_project_upgrades_and_becomes_scorable(self, tmp_path, plan):
        import sqlite3

        from sqlalchemy import text

        from omr_scanner.database.migrations import SCHEMA_VERSION

        db_path = tmp_path / "database.sqlite"
        handle = open_project_database(db_path, create=True)
        roster_id = reconciliation_store.import_roster(
            handle, validation_of([("10001", "CAND A", PRESENT)])
        )
        handle.close()

        connection = sqlite3.connect(db_path)
        try:
            connection.executescript(
                """
                DROP TABLE IF EXISTS candidate_result;
                DROP TABLE IF EXISTS scoring_policy_revision;
                DROP TABLE IF EXISTS answer_key_revision;
                DELETE FROM schema_migration WHERE version >= 5;
                """
            )
            connection.commit()
        finally:
            connection.close()

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
                "answer_key_revision",
                "scoring_policy_revision",
                "candidate_result",
            } <= names
            # Phase 7's data is untouched.
            assert len(reconciliation_store.roster_candidates(reopened, roster_id)) == 1
            # ...and a key can now be written.
            stored = scoring_store.save_key(
                reopened, read_key("A" * plan.question_count, plan, "A").to_key()
            )
            assert stored.revision == 1
        finally:
            reopened.close()

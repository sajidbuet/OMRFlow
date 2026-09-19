"""From a scanned sheet to a defensible mark (Phase 8).

Scope:
    The real recognition engine, real rendered sheets, a real project database,
    and the real Phase 6, 7 and 8 services. This is where the claim Phase 8
    makes - "a mark is reproducible from stored inputs, and the key used for
    each candidate is recorded" - is tested against what the pieces actually do
    when wired together.

The scenario the phase brief requires (§55):

    ========== ======== ============================================
    Candidate  Set      Situation
    ========== ======== ============================================
    100001     A        every answer correct
    100002     A        several wrong
    100003     A        blanks and multiples
    100004     A        ABSENT
    100005     B        scored against Set B's different key
    100006     A        a wrong question, answered wrongly
    100007     A        an answer corrected on the Resolve stage
    100008     X        no verified key for that set
    ========== ======== ============================================

Why the sheets are rendered rather than faked:
    The answers being marked are the ones the engine genuinely read off a page.
    A hand-built ``ScanResult`` tests the arithmetic, which
    ``tests/unit/test_scoring.py`` already does far more thoroughly.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import pytest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.database import open_project_database
from omr_scanner.domain.review import ReasonCode
from omr_scanner.domain.scoring import (
    BLANK,
    MULTIPLE,
    BlockReason,
    NegativeMarking,
    QuestionOutcome,
    ResultStatus,
    ScoringPolicy,
    StaleReason,
    format_mark,
)
from omr_scanner.services import (
    batch_store,
    reconciliation_store,
    review_store,
    scoring_store,
)
from omr_scanner.services.answer_key import plan_for, read_key
from omr_scanner.services.batch_processor import BatchOptions, process_batch
from omr_scanner.services.candidate_import import read_roster

if TYPE_CHECKING:
    from collections.abc import Iterator

    from omr_scanner.database import ProjectDatabase

OPERATOR = "Dr. Rahman"


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


@pytest.fixture
def scans_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "scans"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def sheet_marks(roll: str, set_code: str, answers: dict[int, object]) -> dict:
    """Marks for a sheet: a roll number, a set code and per-question answers."""
    return {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: set_code},
        "questions_0": {index: answers.get(index + 1, "A") for index in range(10)},
        "questions_1": {index: answers.get(index + 11, "A") for index in range(10)},
    }


@pytest.fixture
def make_scan(scans_dir: Path, template):
    def make(name: str, marks: dict) -> Path:
        path = scans_dir / name
        cv2.imwrite(str(path), render_marked_sheet(template, marks))
        return path

    return make


@pytest.fixture
def roster_file(tmp_path: Path) -> Path:
    path = tmp_path / "candidates.csv"
    path.write_text(
        "Roll No.,Name,Total (90)\n"
        "100001,CAND ALL CORRECT,55\n"
        "100002,CAND SOME WRONG,55\n"
        "100003,CAND BLANKS,55\n"
        "100004,CAND ABSENT,ABSENT\n"
        "100005,CAND SET B,55\n"
        "100006,CAND WRONG Q,55\n"
        "100007,CAND CORRECTED,55\n"
        "100008,CAND UNKNOWN SET,55\n",
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
def prepared(database, template, plan, make_scan, roster_file):
    """The acceptance scenario, reconciled, keyed and scored once."""
    n = plan.question_count
    paths = [
        make_scan("c1.png", sheet_marks("100001", "A", {})),
        make_scan("c2.png", sheet_marks("100002", "A", {1: "B", 2: "B", 3: "B"})),
        make_scan("c3.png", sheet_marks("100003", "A", {1: [], 2: ["A", "C"]})),
        make_scan("c5.png", sheet_marks("100005", "B", {})),
        make_scan("c6.png", sheet_marks("100006", "A", {2: "D"})),
        make_scan("c7.png", sheet_marks("100007", "A", {5: ["A", "B"]})),
        make_scan("c8.png", sheet_marks("100008", "C", {})),
    ]
    batch_id = run_batch(database, template, paths)
    roster_id = reconciliation_store.import_roster(
        database, read_roster(roster_file), imported_by=OPERATOR
    )
    reconciliation_store.reconcile_batch(database, roster_id, batch_id)

    # Set A: all A. Set B: all D - deliberately different, so scoring a Set B
    # candidate against Set A's key would be obvious.
    for set_code, answers in (("A", "A" * n), ("B", "D" * n)):
        stored = scoring_store.save_key(
            database, read_key(answers, plan, set_code).to_key()
        )
        scoring_store.verify_key(database, stored.key_id, verified_by=OPERATOR)
    scoring_store.save_policy(
        database, ScoringPolicy(correct_mark=Fraction(1)), created_by=OPERATOR
    )
    scoring_store.score_batch(
        database, roster_id, batch_id, template, computed_by=OPERATOR
    )
    return roster_id, batch_id


def results_by_id(database, roster_id, batch_id, template):
    return {
        item.candidate_id: item
        for item in scoring_store.list_results(database, roster_id, batch_id, template)
    }


# ----------------------------------------------------------------------
class TestTheAcceptanceScenario:
    def test_a_perfect_paper_scores_full_marks(
        self, database, prepared, template, plan
    ):
        roster_id, batch_id = prepared
        found = results_by_id(database, roster_id, batch_id, template)["100001"]
        assert found.status is ResultStatus.SCORED
        assert found.final_score == plan.question_count
        assert found.correct_count == plan.question_count

    def test_wrong_answers_are_counted(self, database, prepared, template, plan):
        roster_id, batch_id = prepared
        found = results_by_id(database, roster_id, batch_id, template)["100002"]
        assert found.incorrect_count == 3
        assert found.final_score == plan.question_count - 3

    def test_blanks_and_multiples_are_distinguished_in_the_answer_string(
        self, database, prepared, template
    ):
        # A blank is not a conflict; a double mark is. So this candidate is
        # blocked pending review - but the string already tells the two apart,
        # which is exactly the distinction the phase brief insists on.
        roster_id, batch_id = prepared
        found = results_by_id(database, roster_id, batch_id, template)["100003"]
        assert found.answer_string[0] == BLANK
        assert found.answer_string[1] == MULTIPLE
        assert found.status is ResultStatus.BLOCKED

    def test_once_reviewed_blanks_and_multiples_are_counted_separately(
        self, database, prepared, template
    ):
        roster_id, batch_id = prepared
        scan_id = next(
            item.scripts[0].script.scan_id
            for item in reconciliation_store.list_entries(database, roster_id, batch_id)
            if item.candidate_id == "100003"
        )
        # Confirm the double mark as a multiple: the candidate really did mark
        # two bubbles, which is a fact to score rather than a doubt to hide.
        for conflict in review_store.list_conflicts(database, batch_id):
            if conflict.scan_id == scan_id and conflict.field.kind.value == "question":
                review_store.accept_machine_value(
                    database, conflict.conflict_id, reviewer=OPERATOR
                )
        scoring_store.score_batch(database, roster_id, batch_id, template)

        found = results_by_id(database, roster_id, batch_id, template)["100003"]
        assert found.status is ResultStatus.SCORED
        assert found.blank_count == 1
        assert found.multiple_count == 1
        assert found.answer_string[0] == BLANK
        assert found.answer_string[1] == MULTIPLE

    def test_an_absent_candidate_gets_no_mark(self, database, prepared, template):
        roster_id, batch_id = prepared
        found = results_by_id(database, roster_id, batch_id, template)["100004"]
        assert found.status is ResultStatus.ABSENT
        assert found.final_score is None

    def test_a_candidate_is_scored_against_their_own_set(
        self, database, prepared, template
    ):
        # Set B's key is all D and this candidate answered all A, so scoring
        # them against Set A's key would give full marks instead of nothing.
        roster_id, batch_id = prepared
        found = results_by_id(database, roster_id, batch_id, template)["100005"]
        assert found.set_code == "B"
        assert found.final_score == 0
        assert found.correct_count == 0

    def test_a_set_with_no_verified_key_is_blocked(self, database, prepared, template):
        roster_id, batch_id = prepared
        found = results_by_id(database, roster_id, batch_id, template)["100008"]
        assert found.status is ResultStatus.BLOCKED
        assert [item.reason for item in found.blocks] == [BlockReason.NO_VERIFIED_KEY]
        assert "Set C" in found.describe_blocks()

    def test_the_summary_accounts_for_everybody(self, database, prepared, template):
        roster_id, batch_id = prepared
        counts = scoring_store.count_results(database, roster_id, batch_id, template)
        assert counts.registered == 8
        assert counts.absent == 1
        assert counts.blocked >= 1
        assert counts.scored + counts.absent + counts.blocked == counts.registered


class TestWrongQuestions:
    def test_a_wrong_question_gives_full_credit_to_a_wrong_answer(
        self, database, prepared, template, plan
    ):
        roster_id, batch_id = prepared
        before = results_by_id(database, roster_id, batch_id, template)["100006"]
        assert before.final_score == plan.question_count - 1, "Q2 answered wrongly"

        revised = scoring_store.save_key(
            database,
            read_key("A" * plan.question_count, plan, "A", wrong_questions=[2]).to_key(),
        )
        scoring_store.verify_key(database, revised.key_id, verified_by=OPERATOR)
        scoring_store.score_batch(database, roster_id, batch_id, template)

        after = results_by_id(database, roster_id, batch_id, template)["100006"]
        assert after.final_score == plan.question_count
        assert after.wrong_question_count == 1
        breakdown = scoring_store.breakdown_for(database, after)
        second = next(item for item in breakdown.questions if item.number == 2)
        assert second.outcome is QuestionOutcome.WRONG_QUESTION
        assert second.mark == Fraction(1)

    def test_a_wrong_question_gives_full_credit_to_a_multiple(
        self, database, prepared, template, plan
    ):
        roster_id, batch_id = prepared
        revised = scoring_store.save_key(
            database,
            read_key("A" * plan.question_count, plan, "A", wrong_questions=[5]).to_key(),
        )
        scoring_store.verify_key(database, revised.key_id, verified_by=OPERATOR)
        scoring_store.score_batch(database, roster_id, batch_id, template)

        # 100007's Q5 is a double mark.
        found = results_by_id(database, roster_id, batch_id, template)["100007"]
        breakdown = scoring_store.breakdown_for(database, found)
        fifth = next(item for item in breakdown.questions if item.number == 5)
        assert fifth.candidate == MULTIPLE
        assert fifth.outcome is QuestionOutcome.WRONG_QUESTION
        assert fifth.mark == Fraction(1)

    def test_an_absent_candidate_gets_no_wrong_question_credit(
        self, database, prepared, template, plan
    ):
        roster_id, batch_id = prepared
        revised = scoring_store.save_key(
            database,
            read_key("A" * plan.question_count, plan, "A", wrong_questions=[1, 2]).to_key(),
        )
        scoring_store.verify_key(database, revised.key_id, verified_by=OPERATOR)
        scoring_store.score_batch(database, roster_id, batch_id, template)
        absent = results_by_id(database, roster_id, batch_id, template)["100004"]
        assert absent.status is ResultStatus.ABSENT
        assert absent.final_score is None


class TestPhase6Integration:
    def test_an_unresolved_answer_blocks_scoring(self, database, prepared, template):
        # 100007's Q5 is a double mark, which Phase 6 raises as a conflict.
        roster_id, batch_id = prepared
        found = results_by_id(database, roster_id, batch_id, template)["100007"]
        assert found.status is ResultStatus.BLOCKED
        assert BlockReason.UNRESOLVED_ANSWERS in {item.reason for item in found.blocks}
        assert "Question 5" in found.describe_blocks()

    def test_resolving_the_conflict_lets_the_candidate_be_scored(
        self, database, prepared, template, plan
    ):
        roster_id, batch_id = prepared
        scan_id = next(
            item.scripts[0].script.scan_id
            for item in reconciliation_store.list_entries(database, roster_id, batch_id)
            if item.candidate_id == "100007"
        )
        conflict = next(
            item
            for item in review_store.list_conflicts(database, batch_id)
            if item.scan_id == scan_id and item.field.kind.value == "question"
        )
        review_store.correct_value(
            database,
            conflict.conflict_id,
            value="A",
            reviewer=OPERATOR,
            reason=ReasonCode.DOMINANT_MARK,
        )
        scoring_store.score_batch(database, roster_id, batch_id, template)

        found = results_by_id(database, roster_id, batch_id, template)["100007"]
        assert found.status is ResultStatus.SCORED
        assert found.final_score == plan.question_count

    def test_a_correction_changes_the_effective_answer_not_the_machine_one(
        self, database, prepared, template
    ):
        roster_id, batch_id = prepared
        scan_id = next(
            item.scripts[0].script.scan_id
            for item in reconciliation_store.list_entries(database, roster_id, batch_id)
            if item.candidate_id == "100007"
        )
        conflict = next(
            item
            for item in review_store.list_conflicts(database, batch_id)
            if item.scan_id == scan_id and item.field.kind.value == "question"
        )
        review_store.correct_value(
            database,
            conflict.conflict_id,
            value="A",
            reviewer=OPERATOR,
            reason=ReasonCode.DOMINANT_MARK,
        )
        scoring_store.score_batch(database, roster_id, batch_id, template)

        found = results_by_id(database, roster_id, batch_id, template)["100007"]
        assert found.answer_string[4] == "A", "the effective answer"
        assert found.machine_answer_string[4] == MULTIPLE, "what the machine read"
        assert 5 in found.corrected_questions
        # And Phase 6's own record is intact.
        provenance = review_store.provenance_for(database, conflict.conflict_id)
        assert provenance.machine_value != "A"
        assert provenance.reviewer == OPERATOR

    def test_a_correction_makes_an_existing_result_stale(
        self, database, prepared, template
    ):
        roster_id, batch_id = prepared
        # 100003's Q2 is a double mark, so it is blocked; resolve it, score,
        # then correct it again and check the result goes stale.
        scan_id = next(
            item.scripts[0].script.scan_id
            for item in reconciliation_store.list_entries(database, roster_id, batch_id)
            if item.candidate_id == "100003"
        )
        conflicts = [
            item
            for item in review_store.list_conflicts(database, batch_id)
            if item.scan_id == scan_id and item.field.kind.value == "question"
        ]
        for conflict in conflicts:
            review_store.correct_value(
                database,
                conflict.conflict_id,
                value="A",
                reviewer=OPERATOR,
                reason=ReasonCode.DOMINANT_MARK,
            )
        scoring_store.score_batch(database, roster_id, batch_id, template)
        scored = results_by_id(database, roster_id, batch_id, template)["100003"]
        assert scored.status is ResultStatus.SCORED
        assert scored.is_stale is False

        target = conflicts[0]
        review_store.reopen(database, target.conflict_id, reviewer=OPERATOR)
        review_store.correct_value(
            database,
            target.conflict_id,
            value="B",
            reviewer=OPERATOR,
            reason=ReasonCode.MISCLASSIFICATION,
        )
        after = results_by_id(database, roster_id, batch_id, template)["100003"]
        assert after.is_stale
        assert StaleReason.ANSWERS in after.stale_reasons
        assert after.final_score == scored.final_score, "the mark is not patched"


class TestReproducibility:
    def test_a_mark_is_reproducible_after_reopening(
        self, tmp_path, database, prepared, template
    ):
        roster_id, batch_id = prepared
        before = results_by_id(database, roster_id, batch_id, template)
        database.close()

        reopened = open_project_database(tmp_path / "database.sqlite")
        try:
            after = results_by_id(reopened, roster_id, batch_id, template)
            assert {k: v.final_score for k, v in after.items()} == {
                k: v.final_score for k, v in before.items()
            }
            # ...and recomputing from the stored inputs gives the same answer.
            scoring_store.score_batch(reopened, roster_id, batch_id, template)
            again = results_by_id(reopened, roster_id, batch_id, template)
            assert {k: v.final_score for k, v in again.items()} == {
                k: v.final_score for k, v in before.items()
            }
        finally:
            reopened.close()

    def test_every_scored_candidate_names_its_key_revision(
        self, database, prepared, template
    ):
        roster_id, batch_id = prepared
        for item in scoring_store.list_results(database, roster_id, batch_id, template):
            if item.status is ResultStatus.SCORED:
                assert item.answer_key_revision >= 1
                assert item.policy_revision >= 1
                assert f"Set {item.set_code}" in item.describe_provenance()

    def test_changing_the_policy_recomputes_rather_than_patching(
        self, database, prepared, template, plan
    ):
        roster_id, batch_id = prepared
        before = results_by_id(database, roster_id, batch_id, template)["100002"]
        scoring_store.save_policy(
            database,
            ScoringPolicy(
                correct_mark=Fraction(1),
                mode=NegativeMarking.ONE_PER_THREE,
                clamp_minimum=False,
            ),
        )
        assert results_by_id(database, roster_id, batch_id, template)["100002"].is_stale
        scoring_store.score_batch(database, roster_id, batch_id, template)

        after = results_by_id(database, roster_id, batch_id, template)["100002"]
        assert after.final_score == Fraction(plan.question_count - 3) - 1
        assert after.answer_string == before.answer_string
        assert after.answer_key_revision == before.answer_key_revision
        assert after.policy_revision == before.policy_revision + 1

    def test_the_breakdown_explains_the_mark(self, database, prepared, template):
        roster_id, batch_id = prepared
        found = results_by_id(database, roster_id, batch_id, template)["100002"]
        breakdown = scoring_store.breakdown_for(database, found)
        assert breakdown is not None
        assert breakdown.final_score == found.final_score
        assert sum(item.mark for item in breakdown.questions) == breakdown.raw_score
        wrong = [item for item in breakdown.questions if item.outcome is QuestionOutcome.INCORRECT]
        assert [item.number for item in wrong] == [1, 2, 3]

    def test_a_displayed_mark_is_the_exact_one_formatted(
        self, database, prepared, template
    ):
        roster_id, batch_id = prepared
        scoring_store.save_policy(
            database,
            ScoringPolicy(
                correct_mark=Fraction(1),
                mode=NegativeMarking.ONE_PER_THREE,
                clamp_minimum=False,
            ),
        )
        scoring_store.score_batch(database, roster_id, batch_id, template)
        found = results_by_id(database, roster_id, batch_id, template)["100002"]
        assert format_mark(found.final_score) == format_mark(
            scoring_store.breakdown_for(database, found).final_score
        )


class TestSourceFilesAreUntouched:
    def test_scoring_never_modifies_a_scan(self, database, prepared, template, scans_dir):
        import hashlib

        roster_id, batch_id = prepared
        paths = sorted(scans_dir.glob("*.png"))
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        scoring_store.score_batch(database, roster_id, batch_id, template)
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        assert after == before

    def test_scoring_never_modifies_the_roster_file(
        self, database, prepared, template, roster_file
    ):
        import hashlib

        roster_id, batch_id = prepared
        before = hashlib.sha256(roster_file.read_bytes()).hexdigest()
        scoring_store.score_batch(database, roster_id, batch_id, template)
        assert hashlib.sha256(roster_file.read_bytes()).hexdigest() == before

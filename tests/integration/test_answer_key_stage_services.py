"""The services behind the Answer Key stage (Step 7).

Scope:
    Everything the stage relies on below the widgets: the project's template
    read from the project (not from the Scan stage), manual-key validation,
    template compatibility, per-set storage with provenance, verification, the
    per-set overview, solution-sheet reading and its set-code check, schema
    migration 12, and the equivalence of a typed and a scanned key when they
    agree.

Privacy:
    Every identifier here is fictional.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

import cv2
import pytest
from sqlalchemy import func, select, text
from tests.conftest import build_answer_sheet_template

from omr_scanner.database.models import AnswerKeyRevision, BatchScan, ScanBatch
from omr_scanner.domain.scoring import (
    BLANK,
    AnswerKeySource,
    AnswerKeyStatus,
    ScoringPolicy,
    score_answers,
)
from omr_scanner.evaluation.answer_keys import generate_answer_keys, solution_case
from omr_scanner.evaluation.synthetic_dataset import render_case
from omr_scanner.evaluation.test_cases import FieldLayout, SheetBuilder
from omr_scanner.services import (
    open_project,
    project_sets,
    save_template,
    scoring_store,
    set_active_template,
)
from omr_scanner.services.answer_key import (
    ReadingKind,
    SetCodeCheck,
    can_print_set_code,
    check_sheet_set,
    compatibility_issues,
    normalise_key_text,
    parse_wrong_questions,
    plan_for,
    read_key,
)
from omr_scanner.services.project_template import (
    TemplateAvailability,
    load_project_template,
)
from omr_scanner.services.scoring_store import ScoringError, SetKeyState
from omr_scanner.services.solution_sheet import read_solution_sheet

if TYPE_CHECKING:
    from pathlib import Path

    from omr_scanner.services import ProjectSession

OPERATOR = "Key Checker"


@pytest.fixture(scope="module")
def template():
    return build_answer_sheet_template()


@pytest.fixture(scope="module")
def plan(template):
    return plan_for(template)


def _activate(session: ProjectSession, template, name: str = "exam.omrt") -> Path:
    path = save_template(template, session.project.layout.templates_dir / name)
    set_active_template(session, path)
    return path


# ----------------------------------------------------------------------
# The template comes from the project
# ----------------------------------------------------------------------
class TestTheProjectTemplate:
    def test_a_project_without_one_says_so_and_names_the_template_stage(
        self, project_session
    ):
        found = load_project_template(project_session.project)
        assert found.availability is TemplateAvailability.NONE
        assert "does not yet have an active OMR template" in found.message
        assert "Template stage" in found.message
        assert "Scan" not in found.message

    def test_a_saved_template_gives_questions_and_labels(
        self, project_session, template, plan
    ):
        _activate(project_session, template)
        found = load_project_template(project_session.project)
        assert found.is_ready
        assert found.plan == plan
        assert found.plan.question_count == 20
        assert found.plan.labels == ("A", "B", "C", "D")

    def test_it_survives_closing_and_reopening(self, project_session, template):
        _activate(project_session, template)
        root = project_session.root
        project_session.close()
        with open_project(root) as reopened:
            assert load_project_template(reopened.project).plan == plan_for(template)

    def test_a_missing_file_is_not_called_no_template(self, project_session, template):
        path = _activate(project_session, template)
        path.unlink()
        found = load_project_template(project_session.project)
        assert found.availability is TemplateAvailability.MISSING
        assert path.name in found.message

    def test_a_corrupted_file_is_named_as_such(self, project_session, template):
        path = _activate(project_session, template)
        path.write_text("{ not json", encoding="utf-8")
        found = load_project_template(project_session.project)
        assert found.availability is TemplateAvailability.CORRUPTED
        assert "could not be read" in found.message

    def test_a_newer_format_is_unsupported_not_corrupted(self, project_session, template):
        path = _activate(project_session, template)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["format_version"] = 999
        path.write_text(json.dumps(payload), encoding="utf-8")
        found = load_project_template(project_session.project)
        assert found.availability is TemplateAvailability.UNSUPPORTED
        assert "newer version" in found.message

    def test_a_template_without_questions_is_distinguished(self, project_session):
        _activate(project_session, build_answer_sheet_template(question_blocks=0))
        found = load_project_template(project_session.project)
        assert found.availability is TemplateAvailability.NO_QUESTIONS
        assert found.message.startswith("The active template contains no question regions")
        assert found.template is not None and found.plan is None


# ----------------------------------------------------------------------
# Manual entry
# ----------------------------------------------------------------------
class TestManualEntry:
    @pytest.mark.parametrize(
        "typed",
        ["ABCD" * 5, "A B C D " * 5, ",".join("ABCD" * 5), "ABCD ABCD\nABCD\tABCD;ABCD"],
    )
    def test_harmless_separators_read_the_same(self, plan, typed):
        draft = read_key(typed, plan, "A")
        assert draft.is_valid
        assert draft.answers == "ABCD" * 5

    def test_an_invalid_symbol_is_named_with_its_question_and_the_choices(self, plan):
        typed = "A" * 16 + "E" + "A" * 3
        draft = read_key(typed, plan, "A")
        assert not draft.is_valid
        message = next(issue.message for issue in draft.issues if issue.question == 17)
        assert "Question 17" in message and "'E'" in message and "A/B/C/D" in message
        # Never normalised away: the answers still carry it.
        assert draft.answers[16] == "E"

    def test_too_few_names_the_missing_questions(self, plan):
        draft = read_key("A" * 18, plan, "A")
        assert "Questions 19-20" in draft.issues[0].message

    def test_too_many_is_refused_not_truncated(self, plan):
        draft = read_key("A" * 21, plan, "A")
        assert not draft.is_valid
        assert len(draft.answers) == 21
        assert "only 20 questions" in draft.issues[0].message

    def test_a_blank_is_allowed_only_on_a_full_credit_question(self, plan):
        typed = "A" * 4 + BLANK + "A" * 15
        assert not read_key(typed, plan, "A").is_valid
        withdrawn = read_key(typed, plan, "A", wrong_questions=[5])
        assert withdrawn.is_valid
        assert withdrawn.to_key().answers[4] == BLANK

    def test_full_credit_numbers_are_checked(self, plan):
        numbers, error = parse_wrong_questions("17 4", plan)
        assert numbers == {4, 17} and error == ""
        _, error = parse_wrong_questions("4, 21", plan)
        assert "21" in error and "Questions run 1-20" in error
        _, error = parse_wrong_questions("4, x", plan)
        assert "x" in error
        _, error = parse_wrong_questions("4, 4", plan)
        assert "more than once" in error and "4" in error

    def test_normalising_only_removes_separators(self):
        cleaned, ignored = normalise_key_text("a b*c")
        assert cleaned == "AB*C"
        assert ignored == " "


# ----------------------------------------------------------------------
# Per-set storage, provenance and verification
# ----------------------------------------------------------------------
def _save(database: Any, plan: Any, code: str, answers: str, **kwargs: Any) -> Any:
    return scoring_store.save_key(database, read_key(answers, plan, code).to_key(), **kwargs)


class TestPerSetKeys:
    def test_each_set_keeps_its_own_revisions(self, project_session, plan):
        database = project_session.database
        _save(database, plan, "A", "A" * 20)
        _save(database, plan, "B", "B" * 20)
        _save(database, plan, "A", "C" * 20)
        a = scoring_store.list_keys(database, set_code="A")
        b = scoring_store.list_keys(database, set_code="B")
        assert [item.revision for item in a] == [2, 1]
        assert [item.revision for item in b] == [1]
        assert b[0].key.answers == "B" * 20

    def test_provenance_is_stored_and_read_back(self, project_session, template, plan):
        database = project_session.database
        stored = scoring_store.save_key(
            database,
            read_key("A" * 20, plan, "10", source=AnswerKeySource.SCANNED).to_key(),
            source_scan="Set_10.png",
            created_by=OPERATOR,
            template=template,
            source_sha256="ab" * 32,
            source_metadata={"set_code_check": "match", "blank_at_read": [3]},
        )
        again = scoring_store.get_key(database, stored.key_id)
        assert again is not None
        assert again.created_by == OPERATOR
        assert again.template_id == template.template_id
        assert again.template_name == template.name
        assert again.template_fingerprint == template.geometry_fingerprint()
        assert again.source_sha256 == "ab" * 32
        assert again.source_metadata == {"set_code_check": "match", "blank_at_read": [3]}
        assert again.key.source is AnswerKeySource.SCANNED
        assert again.source_scan == "Set_10.png"

    def test_verification_is_recorded_and_supersedes_only_that_set(
        self, project_session, plan
    ):
        database = project_session.database
        a1 = _save(database, plan, "A", "A" * 20)
        b1 = _save(database, plan, "B", "B" * 20)
        scoring_store.verify_key(database, a1.key_id, verified_by=OPERATOR, plan=plan)
        scoring_store.verify_key(database, b1.key_id, verified_by=OPERATOR, plan=plan)
        a2 = _save(database, plan, "A", "D" * 20)
        scoring_store.verify_key(database, a2.key_id, verified_by=OPERATOR, plan=plan)
        assert scoring_store.get_key(database, a1.key_id).key.status is AnswerKeyStatus.SUPERSEDED
        assert scoring_store.get_key(database, b1.key_id).key.status is AnswerKeyStatus.VERIFIED
        verified = scoring_store.verified_key(database, "A")
        assert verified.key_id == a2.key_id
        assert verified.verified_by == OPERATOR and verified.verified_at is not None

    def test_an_incompatible_key_cannot_be_verified(self, project_session, plan):
        database = project_session.database
        short = scoring_store.save_key(
            database,
            read_key("A" * 10, plan_for(build_answer_sheet_template(question_blocks=1)), "A")
            .to_key(),
        )
        with pytest.raises(ScoringError) as caught:
            scoring_store.verify_key(database, short.key_id, verified_by=OPERATOR, plan=plan)
        assert "10 answer(s)" in caught.value.user_message
        assert "20 questions" in caught.value.user_message


class TestTheOverview:
    def test_every_state_is_distinguished(self, project_session, plan):
        database = project_session.database
        for code in ("A", "B", "C", "D"):
            project_sets.add_set(database, code)
        a = _save(database, plan, "A", "A" * 20)
        scoring_store.verify_key(database, a.key_id, verified_by=OPERATOR)
        _save(database, plan, "B", "B" * 20)
        old_plan = plan_for(build_answer_sheet_template(question_blocks=1))
        c = scoring_store.save_key(database, read_key("C" * 10, old_plan, "C").to_key())
        scoring_store.verify_key(database, c.key_id, verified_by=OPERATOR)

        overview = {
            item.set_code: item
            for item in scoring_store.key_overview(database, ("A", "B", "C", "D"), plan)
        }
        assert overview["A"].state is SetKeyState.VERIFIED and overview["A"].is_ready
        assert overview["B"].state is SetKeyState.DRAFT
        assert "not verified" in overview["B"].describe()
        assert overview["C"].state is SetKeyState.STALE
        assert "does not fit" in overview["C"].describe()
        assert overview["D"].state is SetKeyState.MISSING
        assert overview["D"].describe() == "Set D: no answer key."

    def test_a_template_change_marks_the_old_key_incompatible_and_keeps_it(
        self, project_session
    ):
        database = project_session.database
        twenty = build_answer_sheet_template()
        _activate(project_session, twenty)
        plan20 = load_project_template(project_session.project).plan
        stored = _save(database, plan20, "A", "B" * 20)
        scoring_store.verify_key(database, stored.key_id, verified_by=OPERATOR, plan=plan20)

        # The template is re-saved with thirty questions.
        _activate(project_session, build_answer_sheet_template(questions_per_block=15))
        plan30 = load_project_template(project_session.project).plan
        assert plan30.question_count == 30
        (item,) = scoring_store.key_overview(database, ("A",), plan30)
        assert item.state is SetKeyState.STALE
        assert compatibility_issues(item.verified.key, plan30)
        # Never truncated, extended or deleted.
        kept = scoring_store.get_key(database, stored.key_id)
        assert kept.key.answers == "B" * 20
        assert kept.key.status is AnswerKeyStatus.VERIFIED

    def test_a_newer_draft_beside_a_verified_key_is_reported(self, project_session, plan):
        database = project_session.database
        first = _save(database, plan, "A", "A" * 20)
        scoring_store.verify_key(database, first.key_id, verified_by=OPERATOR)
        _save(database, plan, "A", "B" * 20)
        (item,) = scoring_store.key_overview(database, ("A",), plan)
        assert item.state is SetKeyState.VERIFIED
        assert item.has_pending_draft
        assert "newer draft" in item.describe()


# ----------------------------------------------------------------------
# Solution sheets
# ----------------------------------------------------------------------
def _render(template, case, path: Path) -> Path:
    cv2.imwrite(str(path), render_case(template, case).image)
    return path


@pytest.fixture(scope="module")
def keyed(template):
    layout = FieldLayout.of(template)
    return layout, generate_answer_keys(template, layout, ("A", "B"), seed=11)


class TestSolutionSheets:
    def test_a_clean_sheet_reads_every_answer_and_matches_its_set(
        self, tmp_path, template, plan, keyed
    ):
        layout, keys = keyed
        path = _render(template, solution_case(keys["A"], layout, index=1), tmp_path / "a.png")
        reading = read_solution_sheet(
            path, template, plan, selected_set="A", defined_sets=("A", "B")
        )
        assert reading.registered
        assert reading.scanned.is_clean
        assert reading.scanned.answers == keys["A"].key_string
        assert reading.verdict.check is SetCodeCheck.MATCH
        assert len(reading.sha256) == 64

    def test_blank_and_multiple_marks_are_reported_not_guessed(
        self, tmp_path, template, plan, keyed
    ):
        layout, keys = keyed
        builder = SheetBuilder(layout, 2, __import__("random").Random(0))
        builder.set_code("A")
        for number, label in keys["A"].answers.items():
            if number == 4:
                continue  # left blank
            builder.answer(number, ("B", "C") if number == 7 else (label,))
        path = _render(template, builder.build(), tmp_path / "ambiguous.png")
        reading = read_solution_sheet(path, template, plan, selected_set="A")
        assert reading.scanned.blanks == (4,)
        assert reading.scanned.multiples == (7,)
        by_number = {item.number: item for item in reading.scanned.readings}
        assert by_number[4].kind is ReadingKind.BLANK
        assert by_number[7].kind is ReadingKind.MULTIPLE
        assert by_number[7].describe == "Multiple marks B + C"
        # The draft built from it is not a key until a person decides.
        assert not read_key(reading.scanned.answers, plan, "A").is_valid

    def test_a_sheet_for_another_set_needs_a_decision(self, tmp_path, template, plan, keyed):
        layout, keys = keyed
        path = _render(template, solution_case(keys["B"], layout, index=3), tmp_path / "b.png")
        reading = read_solution_sheet(
            path, template, plan, selected_set="A", defined_sets=("A", "B")
        )
        assert reading.verdict.check is SetCodeCheck.MISMATCH
        assert reading.verdict.check.blocks_without_decision
        assert "appears to be Set B, but you selected Set A" in reading.verdict.message

    def test_an_unregistrable_image_reads_nothing(self, tmp_path, template, plan):
        path = tmp_path / "blank.png"
        cv2.imwrite(str(path), 255 * __import__("numpy").ones((1400, 1000), dtype="uint8"))
        reading = read_solution_sheet(path, template, plan, selected_set="A")
        assert not reading.registered
        assert not reading.scanned.is_clean
        assert all(item.kind is ReadingKind.UNREADABLE for item in reading.scanned.readings)

    def test_reading_a_sheet_creates_no_candidate_scan(
        self, tmp_path, project_session, template, plan, keyed
    ):
        layout, keys = keyed
        path = _render(template, solution_case(keys["A"], layout, index=4), tmp_path / "c.png")
        read_solution_sheet(path, template, plan, selected_set="A")
        with project_session.database.session() as session:
            assert session.scalar(select(func.count()).select_from(BatchScan)) == 0
            assert session.scalar(select(func.count()).select_from(ScanBatch)) == 0

    def test_metadata_records_what_the_operator_changed(self, tmp_path, template, plan, keyed):
        layout, keys = keyed
        path = _render(template, solution_case(keys["A"], layout, index=5), tmp_path / "d.png")
        reading = read_solution_sheet(path, template, plan, selected_set="A")
        final = "D" + reading.scanned.answers[1:]
        meta = reading.metadata(target_set="A", final_answers=final)
        expected = [1] if reading.scanned.answers[0] != "D" else []
        assert meta["corrected_by_operator"] == expected
        assert meta["file_name"] == "d.png"
        assert meta["set_code_check"] == "match"


class TestTheSetCodeCheck:
    def test_blank_is_a_notice(self, template):
        verdict = check_sheet_set(template, selected="A", read="")
        assert verdict.check is SetCodeCheck.BLANK
        assert not verdict.check.blocks_without_decision

    def test_a_code_the_sheet_cannot_print_is_not_confirmable(self, template):
        # Set 10 on a sheet whose set field offers A-D only.
        assert not can_print_set_code(template, "10")
        verdict = check_sheet_set(template, selected="10", read="A", defined=("10", "11"))
        assert verdict.check is SetCodeCheck.UNREPRESENTABLE
        assert not verdict.check.blocks_without_decision
        assert "cannot print Set 10" in verdict.message

    def test_multi_digit_codes_print_on_a_positional_field(self):
        digits = build_answer_sheet_template(set_symbols=tuple("0123456789"), set_positions=2)
        assert can_print_set_code(digits, "10")
        assert not can_print_set_code(digits, "100")
        assert check_sheet_set(digits, selected="10", read="10").check is SetCodeCheck.MATCH
        assert check_sheet_set(digits, selected="10", read="11").check is SetCodeCheck.MISMATCH

    def test_multi_character_symbols_print_in_one_position(self):
        symbolic = build_answer_sheet_template(set_symbols=("10", "11", "12"))
        assert can_print_set_code(symbolic, "11")
        assert not can_print_set_code(symbolic, "13")


# ----------------------------------------------------------------------
# Scoring does not care where a key came from
# ----------------------------------------------------------------------
class TestManualAndScannedKeysScoreAlike:
    def test_identical_answers_score_identically(self, project_session, plan):
        database = project_session.database
        answers = "ABCD" * 5
        manual = scoring_store.save_key(database, read_key(answers, plan, "A").to_key())
        scanned = scoring_store.save_key(
            database,
            read_key(answers, plan, "B", source=AnswerKeySource.SCANNED).to_key(),
            source_scan="Set_B.png",
        )
        for stored in (manual, scanned):
            scoring_store.verify_key(database, stored.key_id, verified_by=OPERATOR, plan=plan)
        keys = scoring_store.verified_keys(database)
        candidate = "ABCA" * 5
        policy = ScoringPolicy()
        a = score_answers(candidate, keys["A"].key, policy)
        b = score_answers(candidate, keys["B"].key, policy)
        assert a.final_score == b.final_score
        assert [item.outcome for item in a.questions] == [item.outcome for item in b.questions]


# ----------------------------------------------------------------------
# Migration 12
# ----------------------------------------------------------------------
class TestMigrationTwelve:
    def test_an_older_database_gains_the_columns_and_keeps_its_keys(
        self, project_session, plan
    ):
        database = project_session.database
        stored = _save(database, plan, "A", "A" * 20)
        scoring_store.verify_key(database, stored.key_id, verified_by=OPERATOR)
        root = project_session.root
        db_path = database.path
        project_session.close()

        connection = sqlite3.connect(db_path)
        try:
            for column in (
                "created_by", "template_id", "template_name",
                "template_fingerprint", "source_sha256", "source_metadata_json",
            ):
                connection.execute(f"ALTER TABLE answer_key_revision DROP COLUMN {column}")
            connection.execute("DELETE FROM schema_migration WHERE version >= 12")
            connection.commit()
        finally:
            connection.close()

        with open_project(root) as reopened:
            with reopened.database.session() as session:
                columns = {
                    row[1]
                    for row in session.execute(
                        text("PRAGMA table_info(answer_key_revision)")
                    ).all()
                }
                assert {"created_by", "template_fingerprint", "source_metadata_json"} <= columns
                assert session.scalar(select(func.count()).select_from(AnswerKeyRevision)) == 1
            kept = scoring_store.verified_key(reopened.database, "A")
            assert kept is not None and kept.key.answers == "A" * 20
            # Nothing invented for a revision stored before provenance existed.
            assert kept.created_by == "" and kept.template_id == ""
            assert kept.source_metadata == {}
